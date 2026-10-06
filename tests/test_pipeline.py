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
from app.facts import RequestFacts
from app.models import CaseInput
from app.northstar_client import NorthstarClient
from app.pipeline import process_case
from tests.case_fixtures import ALL_CASES, Case
from tests.saved_api import (
    created_work_orders,
    offline_settings,
    reset_created_work_orders,
    saved_handler,
)

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
        "refersToPreviousWork": facts.refersToPreviousWork,
        "isSafetyAnswer": facts.isSafetyAnswer,
        "safetyAnswerUncertain": facts.safetyAnswerUncertain,
    }


def _routed_handler(model_payload: dict[str, Any]):
    """Serves saved Northstar records, and a fixed model answer for gateway posts."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Route on the route parameter, not the verb: the work-order write is also a POST.
        if request.url.params.get("route") == "model":
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


async def test_dispatch_creates_the_work_order() -> None:
    reset_created_work_orders()
    case = next(c for c in ALL_CASES if c.id == "VIS-006")
    result = await _run(case)

    assert result.status == "dispatch_ready"
    assert result.workOrder is not None
    # A real work order, not a recommendation stub.
    assert result.workOrder["id"].startswith("WO-TEST-")
    assert result.workOrder["assetId"] == "AST-302"
    assert result.workOrder["technicianId"]
    assert any(a.type == "work_order_created" for a in result.nextActions)


async def test_the_same_event_id_creates_only_one_work_order() -> None:
    """A redelivered event must return the original job, never raise a second."""
    reset_created_work_orders()
    case = next(c for c in ALL_CASES if c.id == "VIS-006")

    first = await _run(case)
    second = await _run(case)  # same event id, built from case.id

    assert first.workOrder["id"] == second.workOrder["id"]
    assert len(created_work_orders()) == 1


async def test_only_dispatch_ready_writes_a_work_order() -> None:
    """Nothing else in the flow may create a job."""
    reset_created_work_orders()
    for case in ALL_CASES:
        if case.expectedStatus == "dispatch_ready":
            continue
        await _run(case)
    assert created_work_orders() == {}


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

    async def fake_process(case, northstar, model, event_id=None, trace_log=None):
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


async def test_a_non_hazard_escalation_is_not_labelled_a_safety_risk() -> None:
    """A request to review earlier work goes to a human, but is not a hazard."""
    case = Case(
        id="PREV-WORK",
        assetId="AST-1001",
        receivedAt="2026-09-20T11:19:00Z",
        expectedStatus="human_escalation_required",
        subject="AHU 2 airflow weak",
        body=(
            "Sortation hall AHU 2, AST-1001, has weak airflow again. Cooling remains "
            "within range. Please review the June visit before assigning this."
        ),
        facts=RequestFacts(
            assetMentions=["AST-1001"],
            safetySignal="absent",
            intent="breakdown",
            symptomSummary="weak airflow again",
            requiresOnsite=True,
            sameFaultAsExisting="different",
            refersToPreviousWork=True,
        ),
    )
    result = await _run(case)

    assert result.status == "human_escalation_required"
    # The escalation is procedural, not a hazard.
    assert result.classification.safetyRisk is False
    assert result.classification.category != "safety"
    assert result.classification.urgency != "immediate"


async def test_a_real_hazard_is_still_labelled_a_safety_risk() -> None:
    case = next(c for c in ALL_CASES if c.id == "VIS-003")
    result = await _run(case)

    assert result.classification.safetyRisk is True
    assert result.classification.category == "safety"
    assert result.classification.urgency == "immediate"


async def test_an_unanswered_safety_question_is_still_a_safety_risk() -> None:
    """clarification_required for safety reasons must not read as hazard-free."""
    case = next(c for c in ALL_CASES if c.id == "HAZARD-MISSED")
    result = await _run(case)

    assert result.status == "clarification_required"
    assert result.classification.safetyRisk is True


async def test_a_customer_reply_never_quotes_an_internal_identifier() -> None:
    """Only references the customer would recognise (WO-…, REQ-…) may appear."""
    import re as _re

    reset_created_work_orders()
    for case in ALL_CASES:
        result = await _run(case)
        draft = result.customerResponseDraft
        assert not _re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", draft), draft
        for banned in ("POL-", "OPS-", "SYS-", "confidence", "traceId", "HTTP "):
            assert banned not in draft, f"{banned} leaked into: {draft}"


async def test_every_reply_uses_the_three_line_format() -> None:
    reset_created_work_orders()
    for case in ALL_CASES:
        draft = (await _run(case)).customerResponseDraft
        lines = draft.split("\n")
        assert lines[0] == "We've received your request."
        assert any(line.startswith("Issue: ") for line in lines), draft
        assert any(line.startswith("Next step: ") for line in lines), draft


async def test_the_issue_line_does_not_repeat_the_equipment_name() -> None:
    """The model supplies a short phrase; we append the equipment exactly once."""
    case = next(c for c in ALL_CASES if c.id == "VIS-006")
    draft = (await _run(case)).customerResponseDraft
    issue = next(l for l in draft.split("\n") if l.startswith("Issue: "))
    assert issue.count("AST-302") == 1
    assert issue.count("Freezer plant 2") == 1


async def test_a_safety_answer_is_never_treated_as_a_previous_work_request() -> None:
    """The model sets refersToPreviousWork liberally on short replies; code overrides it."""
    case = Case(
        id="SAFETY-REPLY",
        assetId="AST-302",
        receivedAt="2026-09-20T13:00:00Z",
        expectedStatus="human_escalation_required",
        subject="Re: checking before we continue",
        body="Not sure, nobody has been down to the plant room to look yet.",
        facts=RequestFacts(
            assetMentions=["AST-302"],
            safetySignal="ambiguous",
            safetyQuote="Not sure, nobody has been down to the plant room to look yet",
            intent="breakdown",
            symptomSummary="cannot confirm whether there is a hazard",
            isSafetyAnswer=True,
            safetyAnswerUncertain=True,
            refersToPreviousWork=True,  # the model over-sets this
        ),
    )
    result = await _run(case)

    assert result.status == "human_escalation_required"
    assert any(a.type == "safety_answer_unresolved" for a in result.nextActions)
    assert not any(a.type == "refers_to_previous_work" for a in result.nextActions)
    # The safety wording must be present, not a "reviewing previous visits" line.
    assert "keep people away" in result.customerResponseDraft


async def test_a_dispatch_reply_quotes_a_readable_reference_never_a_uuid() -> None:
    """Created work orders come back with a UUID id, so the request id is quoted instead."""
    reset_created_work_orders()
    case = next(c for c in ALL_CASES if c.id == "VIS-006")
    result = await _run(case)

    draft = result.customerResponseDraft
    assert result.status == "dispatch_ready"
    assert "An engineer has been assigned" in draft
    # The mock returns a WO- style id, which is preferred when present.
    assert "WO-TEST-001" in draft


async def test_a_uuid_work_order_id_falls_back_to_the_request_id() -> None:
    """What the real API actually returns: a UUID and no WO- reference."""
    from app.decide import Decision, Reason
    from app.evidence import AssetEvidence
    from app.replies import build_customer_reply

    decision = Decision(
        status="dispatch_ready",
        reasons=[Reason("coverage_confirmed", "covered", "POL-CONTRACT-002")],
    )
    evidence = AssetEvidence(
        outcome="resolved",
        assetId="AST-302",
        asset={"nickname": "Freezer plant 2", "coverage": "premium"},
        site={"name": "Hoskote cold store"},
        agreement={"responseHours": 4, "contractRef": "CON-027-A1"},
    )
    facts = RequestFacts(intent="breakdown", symptomSummary="stopped cooling")
    draft = build_customer_reply(
        facts,
        evidence,
        decision,
        work_order={"id": "8180a1c5-a030-4ddc-8d7a-30190d5e464a", "requestId": "REQ-V006"},
    )

    assert "8180a1c5" not in draft
    assert "REQ-V006" in draft
    assert "within 4 hours" in draft
