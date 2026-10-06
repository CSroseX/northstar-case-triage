"""Identifier extraction and the wired /cases/process endpoint, with the model mocked."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.identifiers import find_equipment_ids, find_request_references
from app.model_client import ModelClient
from app.models import CaseInput
from app.northstar_client import NorthstarClient
from app.pipeline import process_case
from tests.case_fixtures import ALL_CASES, Case
from tests.saved_api import offline_settings, saved_handler

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- identifier extraction (code, not the model) ------------------------


def test_asset_ids_are_found_in_message_text() -> None:
    assert find_equipment_ids("The asset label reads AST-101.") == ["AST-101"]
    assert find_equipment_ids("AST-302 at SITE-021 has stopped cooling.") == ["AST-302"]
    assert find_equipment_ids("Fulfilment DG B AST-1002 failed a remote start") == ["AST-1002"]


def test_customer_invented_ids_are_found_too() -> None:
    """CMP-77 is not on record, but it must reach the lookup to be reported unknown."""
    assert find_equipment_ids("Please dispatch someone for compressor CMP-77 on line 2.") == [
        "CMP-77"
    ]
    assert find_equipment_ids("Please dispatch someone for laminator LM-4.") == ["LM-4"]


def test_non_equipment_identifiers_are_excluded() -> None:
    found = find_equipment_ids(
        "Following up on REQ-V001 under CON-012-A2 for CUS-012 at SITE-008 with TECH-01"
    )
    assert found == []


def test_several_assets_are_returned_in_order() -> None:
    assert find_equipment_ids("Either AST-101 or AST-102 is down") == ["AST-101", "AST-102"]


def test_no_identifier_returns_empty() -> None:
    assert find_equipment_ids("The freezer plant is not pulling down below -12C.") == []


def test_request_references_are_found() -> None:
    assert find_request_references("This is the same as REQ-V001 and WO-9294") == [
        "REQ-V001",
        "WO-9294",
    ]


# --- the wired pipeline, model mocked -----------------------------------


def _model_answer(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "trace-pipeline",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}],
            "celeco": {"traceId": "trace-pipeline"},
        },
    )


def _facts_payload(case: Case) -> dict[str, Any]:
    """The answer the model would give for a fixture case."""
    facts = case.facts
    return {
        "assetMentions": facts.assetMentions,
        "safetySignal": facts.safetySignal,
        "safetyQuote": facts.safetyQuote,
        "intent": facts.intent,
        "symptomSummary": facts.symptomSummary,
        "referencedRequests": facts.referencedRequests,
        "requiresOnsite": facts.requiresOnsite,
        "sameFaultAsExisting": facts.sameFaultAsExisting,
        "sameFaultReference": facts.sameFaultReference,
    }


def _routed_handler(model_payload: dict[str, Any]):
    """Serves saved Northstar records, and a fixed model answer for gateway posts."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" or request.url.params.get("route") == "model":
            return _model_answer(model_payload)
        return saved_handler(request)

    return handler


async def _run(case: Case, model_payload: dict[str, Any] | None = None):
    payload = model_payload if model_payload is not None else _facts_payload(case)
    http = httpx.AsyncClient(transport=httpx.MockTransport(_routed_handler(payload)))
    settings = offline_settings()
    async with http:
        northstar = NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0)
        model = ModelClient(settings, client=http, cache_dir="")
        return await process_case(
            CaseInput(
                requestId=case.id,
                receivedAt=case.receivedAt,
                channel="email",
                sender={"name": "Test", "email": "test@example.com"},
                subject=case.subject,
                body=case.body,
                attachments=[],
            ),
            northstar,
            model,
            event_id=f"evt-{case.id}",
        )


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
async def test_pipeline_reaches_expected_status(case: Case) -> None:
    """End to end with the model answering exactly what the fixtures say it would."""
    result = await _run(case)
    assert result.status == case.expectedStatus


async def test_result_carries_the_model_trace_id() -> None:
    case = next(c for c in ALL_CASES if c.id == "VIS-006")
    result = await _run(case)
    assert result.audit.modelTraceIds == ["trace-pipeline"]


async def test_dispatch_result_records_a_recommendation_not_a_booking() -> None:
    case = next(c for c in ALL_CASES if c.id == "VIS-006")
    result = await _run(case)

    assert result.status == "dispatch_ready"
    assert result.workOrder is not None
    assert result.workOrder["created"] is False
    assert result.workOrder["recommendedTechnicianId"]


async def test_classification_is_filled_from_the_facts() -> None:
    safety = next(c for c in ALL_CASES if c.id == "VIS-003")
    result = await _run(safety)
    assert result.classification.safetyRisk is True
    assert result.classification.category == "safety"
    assert result.classification.urgency == "immediate"

    planned = next(c for c in ALL_CASES if c.id == "VIS-001")
    result = await _run(planned)
    assert result.classification.safetyRisk is False
    assert result.classification.category == "planned_service"
    assert result.classification.urgency == "routine"
    assert 0.0 <= result.classification.confidence <= 1.0


async def test_entitlement_cites_the_agreement_evidence() -> None:
    case = next(c for c in ALL_CASES if c.id == "VIS-008")
    result = await _run(case)

    assert result.entitlement.status == "covered"
    refs = json.dumps(result.entitlement.evidence)
    assert "CON-012-A2" in refs


async def test_malformed_model_answer_takes_the_cautious_path() -> None:
    case = next(c for c in ALL_CASES if c.id == "VIS-006")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(
                    200,
                    json={
                        "id": "t",
                        "choices": [{"message": {"content": "not json"}}],
                        "celeco": {"traceId": "t"},
                    },
                )
                if r.method == "POST"
                else saved_handler(r)
            )
        )
    )
    settings = offline_settings()
    async with http:
        result = await process_case(
            CaseInput(
                requestId=case.id,
                receivedAt=case.receivedAt,
                channel="portal",
                sender={"name": "T", "email": "t@example.com"},
                subject=case.subject,
                body=case.body,
                attachments=[],
            ),
            NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0),
            ModelClient(settings, client=http, cache_dir=""),
        )

    # A covered breakdown with technicians free, held because the message could not be read.
    assert result.status == "clarification_required"
    assert result.workOrder is None


# --- the HTTP endpoint --------------------------------------------------


def test_health_is_200() -> None:
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_transport_repeat_returns_the_stored_result(monkeypatch) -> None:
    """The same X-Event-ID delivered twice must not be decided twice."""
    import app.main as main

    calls = {"n": 0}

    async def fake_process(case, northstar, model, event_id=None):
        calls["n"] += 1
        from app.models import Audit, Classification, Entitlement, Entities, CaseResult

        return CaseResult(
            caseId=case.requestId,
            status="covered_action",
            entities=Entities(),
            classification=Classification(
                category="x", urgency="normal", safetyRisk=False, confidence=0.9
            ),
            entitlement=Entitlement(status="covered", sla="4h", evidence=[]),
            nextActions=[],
            customerResponseDraft="ok",
            missingInformation=[],
            workOrder=None,
            audit=Audit(sourceReferences=[], modelTraceIds=[], warnings=[]),
        )

    monkeypatch.setattr(main, "process_case", fake_process)
    main._RESULTS_BY_EVENT_ID.clear()

    body = {
        "requestId": "REQ-REPEAT",
        "receivedAt": "2026-09-20T08:42:00Z",
        "channel": "portal",
        "sender": {"name": "T", "email": "t@example.com"},
        "subject": "S",
        "body": "B",
        "attachments": [],
    }
    with TestClient(main.app) as client:
        first = client.post("/cases/process", json=body, headers={"X-Event-ID": "evt-dup"})
        second = client.post("/cases/process", json=body, headers={"X-Event-ID": "evt-dup"})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert calls["n"] == 1  # decided once, replayed on the repeat
