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


async def test_a_non_request_reply_omits_the_issue_line() -> None:
    """Nothing was described, so there is no issue to state."""
    case = Case(
        id="NOT-A-REQUEST",
        assetId=None,
        receivedAt="2026-09-20T09:00:00Z",
        expectedStatus="clarification_required",
        subject="Quick question",
        body="Hi, can I get your number",
        facts=RequestFacts(
            assetMentions=[], safetySignal="absent", intent="other", symptomSummary=""
        ),
    )
    result = await _run(case)
    draft = result.customerResponseDraft

    assert result.status == "clarification_required"
    assert "Issue:" not in draft
    assert "which equipment or service you need help with" in draft
    # The three-line shape still holds for everything that does describe an issue.
    assert draft.startswith("We've received your request.")


# --- identifier spellings (X-14) ----------------------------------------
# A customer copies an id off a sticker, out of a portal, or from memory. Requiring the
# canonical spelling meant asking someone with a failed unit to name equipment they had
# already identified twice. SYS-CATALOG-001, CLAUDE.md rule 6.


def test_asset_ids_are_matched_whatever_the_spelling() -> None:
    for text in ("AST-1202", "ast-1202", "AST 1202", "AST1202", "ast 1202", "AST_1202"):
        assert find_equipment_ids(text) == ["AST-1202"], text


def test_mixed_spellings_of_one_id_are_one_asset() -> None:
    """X-14's actual message named it two ways; that is one asset, not two."""
    found = find_equipment_ids(
        "The label reads AST 1202 (the sticker has a space in it) and our portal shows "
        "it as ast-1202."
    )
    assert found == ["AST-1202"]


def test_prose_is_not_mistaken_for_an_asset_id() -> None:
    """The looser matching must not turn ordinary words into equipment.

    "Unit 4" and "AHU 2" read like ids but are prose. Without this guard the first bogus
    id would drive the whole lookup, because asset_id is taken from the first match.
    """
    for text in ("Unit 4 stopped", "Line 3 is down", "Bay 2 chiller", "Room 5", "AHU 2 stopped"):
        assert find_equipment_ids(text) == [], text
    # A real id alongside prose still resolves, and only the real one.
    assert find_equipment_ids("Please check Unit 12 and AST-1001") == ["AST-1001"]


def test_an_explicit_hyphen_is_enough_for_an_unknown_prefix() -> None:
    """cmp-77 is clearly an identifier; "unit 77" is not."""
    assert find_equipment_ids("cmp-77 is faulty") == ["CMP-77"]
    assert find_equipment_ids("unit 77 is faulty") == []


def test_the_model_asset_id_counts_only_if_it_is_in_the_message() -> None:
    """The fallback must not let the model name an asset from the records it was shown."""
    from app.identifiers import id_appears_in_text

    message = "The ink store AC label reads AST 1202 and the portal shows ast-1202."
    assert id_appears_in_text("AST-1202", message)
    # An id the model could only have taken from the records it was shown.
    assert not id_appears_in_text("AST-1001", message)
    assert not id_appears_in_text("", message)


# --- attachment summaries reach the model and the backstop (S-11) -------


def test_attachment_summaries_are_in_the_model_prompt() -> None:
    """A hazard described only in a photo summary must be visible to the model."""
    from app.model_client import build_user_prompt

    prompt = build_user_prompt(
        subject="Routine photo upload for packing hall AC",
        body="Uploading this week's inspection photo. Nothing urgent.",
        attachments=[
            {
                "id": "ATT-S11",
                "name": "inspection-photo.jpg",
                "type": "image",
                "summary": (
                    "A pool of water has spread under the AC unit and reaches the base "
                    "of the open electrical panel beside it."
                ),
            }
        ],
    )

    assert "open electrical panel" in prompt
    assert "ATTACHMENTS ON THIS REQUEST" in prompt
    # And it is framed as the customer's report, not as metadata.
    assert "part of the customer's report" in prompt


def test_an_attachment_without_a_summary_adds_nothing() -> None:
    from app.model_client import build_user_prompt

    prompt = build_user_prompt(
        subject="S",
        body="B",
        attachments=[{"id": "ATT-1", "name": "photo.jpg", "type": "image"}],
    )
    assert "ATTACHMENTS ON THIS REQUEST" not in prompt


async def test_a_hazard_only_in_an_attachment_is_not_treated_as_routine() -> None:
    """S-11 end to end: the keyword backstop sees the attachment even if the model does not.

    The model payload here deliberately reports no hazard — this is the case where the
    extraction misses it, and the backstop is the only thing standing between a hazard
    and a routine acknowledgement (POL-SAFETY-001).

    The outcome is the safety question, not an escalation: hazard wording with no signal
    from the sender is CLAUDE.md rule 2's ambiguous case, so we ask and progress nothing.
    What must never happen is the original S-11 result — covered_action, safetyRisk
    false, the water and the open panel never mentioned again.
    """
    payload = {
        "assetMentions": ["AST-501"],
        "safetySignal": "absent",
        "safetyQuote": "",
        "intent": "planned_service",
        "symptomSummary": "routine inspection photo upload",
        "referencedRequests": [],
        "requiresOnsite": False,
        "sameFaultAsExisting": "different",
        "sameFaultReference": "",
        "refersToPreviousWork": False,
        "isSafetyAnswer": False,
        "safetyAnswerUncertain": False,
    }
    http = httpx.AsyncClient(transport=httpx.MockTransport(_routed_handler(payload)))
    settings = offline_settings()
    async with http:
        northstar = NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0)
        model = ModelClient(settings, client=http, cache_dir="")
        result = await process_case(
            CaseInput(
                requestId="STRESS-S11",
                receivedAt="2026-09-21T09:10:00Z",
                channel="portal",
                sender={"name": "R Patil", "email": "storeops@example.in"},
                subject="Routine photo upload for packing hall AC",
                body="Uploading this week's inspection photo for AST-501. Nothing urgent.",
                attachments=[
                    {
                        "id": "ATT-S11",
                        "name": "inspection-photo.jpg",
                        "type": "image",
                        "summary": (
                            "A pool of water has spread under the AC unit and reaches "
                            "the base of the open electrical panel beside it. Two staff "
                            "are standing in the water."
                        ),
                    }
                ],
            ),
            northstar,
            model,
            event_id="evt-s11",
        )

    assert result.status == "clarification_required"
    assert result.status != "covered_action"
    assert result.workOrder is None
    # The safety question is what is being asked, and nothing has progressed.
    assert "smoke" in result.customerResponseDraft
    assert any("safety" in m.lower() or "hazard" in m.lower() for m in result.missingInformation)


async def test_an_attachment_cannot_redirect_the_case_to_another_asset() -> None:
    """Summaries feed the hazard check, not identity: they are written by whoever
    processed the file and may name other equipment in passing."""
    payload = dict(_facts_payload(_case_by_id("VIS-001")))
    http = httpx.AsyncClient(transport=httpx.MockTransport(_routed_handler(payload)))
    settings = offline_settings()
    async with http:
        northstar = NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0)
        model = ModelClient(settings, client=http, cache_dir="")
        result = await process_case(
            CaseInput(
                requestId="ATT-REDIRECT",
                receivedAt="2026-09-20T08:42:00Z",
                channel="email",
                sender={"name": "T", "email": "t@example.com"},
                subject="Main DG scheduled service",
                body="Please arrange the quarterly service. The label reads AST-101.",
                attachments=[
                    {
                        "id": "ATT-X",
                        "name": "sheet.pdf",
                        "type": "document",
                        "summary": "Service sheet also listing AST-302 and AST-402.",
                    }
                ],
            ),
            northstar,
            model,
            event_id="evt-att-redirect",
        )

    assert result.entities.assetId == "AST-101"


def _case_by_id(case_id: str) -> Case:
    for case in ALL_CASES:
        if case.id == case_id:
            return case
    raise KeyError(case_id)


async def test_an_attachment_hazard_the_model_reports_escalates() -> None:
    """With the summary in the prompt the model can report it, and then it escalates.

    The quote comes from the attachment, so this also proves the verbatim check accepts
    a quote taken from a summary — `request_text` includes them (POL-SAFETY-001).
    """
    quote = (
        "A pool of water has spread under the AC unit and reaches the base of the open "
        "electrical panel beside it."
    )
    payload = {
        "assetMentions": ["AST-501"],
        "safetySignal": "affirmed",
        "safetyQuote": quote,
        "intent": "planned_service",
        "symptomSummary": "water reaching an open electrical panel",
        "referencedRequests": [],
        "requiresOnsite": True,
        "sameFaultAsExisting": "",
        "sameFaultReference": "",
        "refersToPreviousWork": False,
        "isSafetyAnswer": False,
        "safetyAnswerUncertain": False,
    }
    http = httpx.AsyncClient(transport=httpx.MockTransport(_routed_handler(payload)))
    settings = offline_settings()
    async with http:
        northstar = NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0)
        model = ModelClient(settings, client=http, cache_dir="")
        result = await process_case(
            CaseInput(
                requestId="STRESS-S11B",
                receivedAt="2026-09-21T09:10:00Z",
                channel="portal",
                sender={"name": "R Patil", "email": "storeops@example.in"},
                subject="Routine photo upload for packing hall AC",
                body="Uploading this week's inspection photo for AST-501. Nothing urgent.",
                attachments=[
                    {"id": "ATT-S11", "name": "photo.jpg", "type": "image", "summary": quote}
                ],
            ),
            northstar,
            model,
            event_id="evt-s11b",
        )

    assert result.status == "human_escalation_required"
    assert result.classification.safetyRisk is True
    assert result.workOrder is None
    # The sender's own words are kept, even though they came from the attachment.
    assert "open electrical panel" in result.customerResponseDraft


# --- work orders we create carry a summary (D-03/D-04) ------------------


async def test_a_created_work_order_carries_a_summary() -> None:
    """Northstar's own jobs carry one; ours were null, so a later report of the same
    fault had a blank line to compare itself against."""
    reset_created_work_orders()
    case = _case_by_id("VIS-006")
    result = await _run(case)

    assert result.status == "dispatch_ready"
    created = list(created_work_orders().values())
    assert len(created) == 1
    summary = created[0]["summary"]
    assert summary, "a work order with no summary is invisible to the duplicate check"
    # The equipment and the fault, like "Freezer plant 2 compressor cycling".
    assert "stopped cooling" in summary.lower()


async def test_the_work_order_summary_survives_a_missing_nickname() -> None:
    from app.evidence import AssetEvidence
    from app.facts import RequestFacts
    from app.pipeline import _work_order_summary

    facts = RequestFacts(symptomSummary="stopped cooling")
    assert _work_order_summary(facts, AssetEvidence(outcome="no_asset_id")) == "Stopped cooling"
    # And nothing to say is an empty summary, not a stray capital.
    assert _work_order_summary(RequestFacts(), AssetEvidence(outcome="no_asset_id")) == ""


# --- our own handled requests are shown to the model (D-03/D-04) --------


async def test_requests_we_handled_are_offered_to_the_next_one() -> None:
    """D-01 was sent to us, so it is not in Northstar's queue. Without this the second
    report of the same fault sees "no earlier requests" and becomes a second job."""
    from app.model_client import build_user_prompt
    from app.pipeline import HandledRequests

    store = HandledRequests()
    store.record(
        "AST-1001",
        CaseInput(
            requestId="STRESS-D01",
            receivedAt="2026-09-20T17:10:00Z",
            channel="portal",
            sender={"name": "Amit", "email": "a@example.com"},
            subject="Sortation hall AHU 2 stopped",
            body="AST-1001 has stopped cooling at the Nelamangala centre.",
            attachments=[],
        ),
    )

    ours = store.for_asset("AST-1001", exclude_request_id="STRESS-D03")
    assert [r["requestId"] for r in ours] == ["STRESS-D01"]
    # Case-insensitive, and the current request never compares against itself.
    assert store.for_asset("ast-1001", exclude_request_id="STRESS-D01") == []

    # And it reaches the model in the form it reads earlier requests in.
    prompt = build_user_prompt("S", "B", recent_requests=ours)
    assert "EARLIER REQUESTS FOR THIS EQUIPMENT" in prompt
    assert "STRESS-D01" in prompt
    assert "stopped cooling" in prompt


async def test_the_handled_store_ignores_cases_with_no_asset() -> None:
    from app.pipeline import HandledRequests

    store = HandledRequests()
    case = CaseInput(
        requestId="NO-ASSET",
        receivedAt="2026-09-20T17:10:00Z",
        channel="email",
        sender={"name": "T", "email": "t@example.com"},
        subject="S",
        body="B",
        attachments=[],
    )
    store.record(None, case)
    assert store.for_asset(None) == []
    # Recording the same request twice keeps one entry.
    store.record("AST-1", case)
    store.record("AST-1", case)
    assert len(store.for_asset("AST-1")) == 1


# --- a reference only counts if the customer wrote it (C-01/C-04) -------


def test_uncited_references_are_dropped() -> None:
    """The model sees the records we show it; ids recalled from those are not citations."""
    from app.facts import RequestFacts
    from app.pipeline import keep_cited_references

    facts = RequestFacts(referencedRequests=["REQ-8268"])
    message = "Please schedule an inspection for the banquet chiller AST-801."
    assert keep_cited_references(facts, message).referencedRequests == []


def test_cited_references_are_kept_in_any_spelling() -> None:
    from app.facts import RequestFacts
    from app.pipeline import keep_cited_references

    facts = RequestFacts(referencedRequests=["REQ-8268"])
    for message in ("Following up on REQ-8268.", "about req 8268 again", "see req-8268"):
        assert keep_cited_references(facts, message).referencedRequests == ["REQ-8268"], message


def test_only_the_uncited_references_are_dropped() -> None:
    from app.facts import RequestFacts
    from app.pipeline import keep_cited_references

    facts = RequestFacts(referencedRequests=["REQ-8268", "WO-9294"])
    kept = keep_cited_references(facts, "This follows WO-9294.").referencedRequests
    assert kept == ["WO-9294"]


# --- response hours are withheld when cover was not in force (C-03) -----


async def test_response_hours_are_withheld_when_the_agreement_was_not_in_force() -> None:
    """A commitment from an agreement that had not started is not a commitment."""
    payload = {
        "assetMentions": ["AST-102"],
        "safetySignal": "denied",
        "safetyQuote": "No smoke, water or unusual smell, and nobody is at risk.",
        "intent": "breakdown",
        "symptomSummary": "not holding temperature",
        "referencedRequests": [],
        "requiresOnsite": True,
        "sameFaultAsExisting": "",
        "sameFaultReference": "",
        "refersToPreviousWork": False,
        "isSafetyAnswer": False,
        "safetyAnswerUncertain": False,
    }
    http = httpx.AsyncClient(transport=httpx.MockTransport(_routed_handler(payload)))
    settings = offline_settings()
    async with http:
        northstar = NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0)
        model = ModelClient(settings, client=http, cache_dir="")
        result = await process_case(
            CaseInput(
                requestId="STRESS-C03",
                # Before CON-012-A2 starts on 2026-08-12.
                receivedAt="2026-08-01T09:00:00Z",
                channel="portal",
                sender={"name": "T", "email": "t@example.com"},
                subject="Cold room not holding temperature",
                body=(
                    "Cold room unit AST-102 at our Whitefield warehouse has stopped "
                    "holding temperature. No smoke, water or unusual smell, and nobody "
                    "is at risk."
                ),
                attachments=[],
            ),
            northstar,
            model,
            event_id="evt-c03",
        )

    assert result.status == "account_review_required"
    agreement_evidence = [
        e for e in result.entitlement.evidence if isinstance(e, dict) and e.get("type") == "agreement"
    ]
    assert agreement_evidence, "the agreement should still be cited as evidence"
    entry = agreement_evidence[0]
    assert entry["inForceAtReceivedAt"] is False
    assert entry["responseHours"] is None
    assert entry["responseHoursWithheld"]
    assert result.entitlement.sla == "not_applicable"


async def test_a_repeat_of_a_request_we_handled_is_linked_not_escalated() -> None:
    """The window rule must see the same priors the model does.

    AST-102 deliberately: no request in Northstar's queue names it, so the earlier report
    can only come from our own store. Feeding the model a prior that `recentRequests`
    does not carry produced "same fault, but no response window to judge the timing
    against" — an escalation on a contract that plainly has a 4-hour window.
    """
    from app.pipeline import handled_requests

    def _payload(same_fault: str, reference: str = "") -> dict[str, Any]:
        return {
            "assetMentions": ["AST-102"],
            "safetySignal": "denied",
            "safetyQuote": "There is no smoke, water or unusual smell.",
            "intent": "breakdown",
            "symptomSummary": "stopped cooling",
            "referencedRequests": [],
            "requiresOnsite": True,
            "sameFaultAsExisting": same_fault,
            "sameFaultReference": reference,
            "refersToPreviousWork": False,
            "isSafetyAnswer": False,
            "safetyAnswerUncertain": False,
        }

    body = (
        "Cold room unit AST-102 at our Whitefield warehouse has stopped holding "
        "temperature. There is no smoke, water or unusual smell."
    )

    def _case(request_id: str, received_at: str) -> CaseInput:
        return CaseInput(
            requestId=request_id,
            receivedAt=received_at,
            channel="portal",
            sender={"name": "D K", "email": "ops@example.in"},
            subject="Cold room not holding temperature",
            body=body,
            attachments=[],
        )

    reset_created_work_orders()
    handled_requests._by_asset.clear()
    settings = offline_settings()

    # Nothing to compare against on the first report; the repeat matches it.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_routed_handler(_payload("different")))
    ) as http:
        first = await process_case(
            _case("REQ-FIRST", "2026-09-20T09:31:00Z"),
            NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0),
            ModelClient(settings, client=http, cache_dir=""),
            event_id="evt-first",
        )
    # 40 minutes later, inside AST-102's 4-hour response window.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_routed_handler(_payload("same", "REQ-FIRST")))
    ) as http:
        second = await process_case(
            _case("REQ-SECOND", "2026-09-20T10:11:00Z"),
            NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0),
            ModelClient(settings, client=http, cache_dir=""),
            event_id="evt-second",
        )

    assert first.status == "dispatch_ready"
    assert second.status == "duplicate_detected", second.status
    # Linked to our own earlier request, and no second job written.
    assert second.workOrder is None
    assert len(created_work_orders()) == 1
    assert "REQ-FIRST" in json.dumps(second.decisionTrace)


# --- recovery after a restart -------------------------------------------
# The event-id store is in-process, so a restart loses it. A redelivered event that
# already created a work order must report that job, not be triaged again: the write is
# idempotent, but a second decision can reach a different status than the one the
# customer was already given (CLAUDE.md rule 11).


async def test_a_redelivered_event_is_recovered_without_a_model_call() -> None:
    from app.pipeline import handled_requests, recover_dispatched_case

    reset_created_work_orders()
    handled_requests._by_asset.clear()
    case = _case_by_id("VIS-006")
    settings = offline_settings()
    model_calls: list[str] = []

    def counting_handler(payload: dict[str, Any]):
        inner = _routed_handler(payload)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("route") == "model":
                model_calls.append("call")
            return inner(request)

        return handler

    request = CaseInput(
        requestId=case.id,
        receivedAt=case.receivedAt,
        channel="portal",
        sender={"name": "D K", "email": "ops@example.in"},
        subject=case.subject,
        body=case.body,
        attachments=[],
    )

    # First delivery: triaged normally and a work order is written.
    transport = httpx.MockTransport(counting_handler(_facts_payload(case)))
    async with httpx.AsyncClient(transport=transport) as http:
        first = await process_case(
            request,
            NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0),
            ModelClient(settings, client=http, cache_dir=""),
            event_id="evt-restart",
        )
    assert first.status == "dispatch_ready"
    assert len(model_calls) == 1
    created_id = first.workOrder["id"]

    # The process restarts: the in-memory store is gone, the records are not.
    model_calls.clear()
    async with httpx.AsyncClient(transport=transport) as http:
        recovered = await recover_dispatched_case(
            request,
            NorthstarClient(settings, client=http, attempts=1, backoff_seconds=0.0),
            "evt-restart",
        )

    assert recovered is not None
    assert recovered.status == "dispatch_ready"
    assert recovered.workOrder["id"] == created_id
    # The two things that matter: no model call, and no second job.
    assert model_calls == []
    assert len(created_work_orders()) == 1
    assert recovered.audit.modelTraceIds == []
    assert any("recovered" in w.lower() for w in recovered.audit.warnings)


async def test_recovery_returns_none_for_an_event_with_no_work_order() -> None:
    """An event we have never seen must be triaged normally, not short-circuited."""
    from app.pipeline import recover_dispatched_case

    reset_created_work_orders()
    case = _case_by_id("VIS-006")
    settings = offline_settings()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_routed_handler(_facts_payload(case)))
    ) as http:
        recovered = await recover_dispatched_case(
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
            "evt-never-seen",
        )

    assert recovered is None


async def test_recovery_returns_none_when_the_lookup_fails() -> None:
    """Recovery is an optimisation, never a gate: a failed read falls through."""
    from app.pipeline import recover_dispatched_case

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    settings = offline_settings()
    case = _case_by_id("VIS-006")
    async with httpx.AsyncClient(transport=httpx.MockTransport(failing)) as http:
        recovered = await recover_dispatched_case(
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
            "evt-anything",
        )

    assert recovered is None


def test_the_normaliser_reads_the_event_id_in_either_spelling() -> None:
    """Reconciling by event reference depended on a field the normaliser never produced."""
    from app.evidence import _normalise_work_order

    top_level = {"id": "WO-1", "external_event_id": "evt-a"}
    nested = {"id": "WO-2", "payload": {"externalEventId": "evt-b"}}
    assert _normalise_work_order(top_level)["externalEventId"] == "evt-a"
    assert _normalise_work_order(nested)["externalEventId"] == "evt-b"
    assert _normalise_work_order({"id": "WO-3"})["externalEventId"] is None
