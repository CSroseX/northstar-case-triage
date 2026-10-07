"""The decision trace, the log file and /cases/recent.

The trace is a record, not a decision: these tests check it reports what happened and
that it can never leak a credential, carry the customer's message body, or break a
response when logging fails.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.trace import TraceLog, build_trace
from tests.case_fixtures import ALL_CASES, Case
from tests.saved_api import reset_created_work_orders

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- shape --------------------------------------------------------------


async def test_every_case_carries_a_trace_with_the_four_steps() -> None:
    from tests.test_pipeline import _run

    reset_created_work_orders()
    for case in ALL_CASES:
        result = await _run(case)
        trace = result.decisionTrace
        assert trace is not None, case.id
        assert trace["requestId"] == case.id
        assert trace["status"] == result.status
        assert set(trace["steps"]) == {"lookups", "model", "checks", "action"}


async def test_the_trace_records_what_the_lookups_found() -> None:
    from tests.test_pipeline import _run

    case = next(c for c in ALL_CASES if c.id == "VIS-006")
    trace = (await _run(case)).decisionTrace

    lookups = trace["steps"]["lookups"]
    assert lookups["outcome"] == "resolved"
    assert lookups["assetId"] == "AST-302"
    assert lookups["found"]["agreement"] == "CON-027-A1"
    assert lookups["found"]["responseHours"] == 4
    assert lookups["found"]["availableInSiteCity"] >= 1


async def test_the_trace_records_a_failed_lookup_rather_than_inventing_one() -> None:
    from app.decide import decide
    from app.evidence import AssetEvidence
    from app.facts import RequestFacts
    from app.models import CaseInput

    evidence = AssetEvidence(
        outcome="lookup_failed", assetId="AST-101", failedLookups=["customers"]
    )
    facts = RequestFacts(intent="breakdown", safetySignal="denied", safetyQuote="no smoke")
    decision = decide(facts, evidence, "AST-101 is down. There is no smoke.")
    trace = build_trace(
        CaseInput(
            requestId="REQ-FAIL",
            receivedAt="2026-09-20T09:00:00Z",
            channel="email",
            sender={"name": "T", "email": "t@example.com"},
            subject="S",
            body="B",
            attachments=[],
        ),
        evidence,
        facts,
        decision,
        [],
    )

    assert trace["steps"]["lookups"]["outcome"] == "lookup_failed"
    assert trace["steps"]["lookups"]["failed"] == ["customers"]
    assert "found" not in trace["steps"]["lookups"]


async def test_the_trace_names_each_check_and_what_it_concluded() -> None:
    from tests.test_pipeline import _run

    case = next(c for c in ALL_CASES if c.id == "VIS-007")
    trace = (await _run(case)).decisionTrace

    checks = {c["check"]: c["results"] for c in trace["steps"]["checks"]}
    assert "coverage" in checks
    assert "technician" in checks
    codes = [r["code"] for results in checks.values() for r in results]
    assert "no_available_qualified_technician" in codes
    # Every conclusion cites the document behind it.
    for results in checks.values():
        for entry in results:
            assert entry["policy"]
            assert entry["concluded"]


async def test_checks_appear_in_the_order_they_ran() -> None:
    from tests.test_pipeline import _run

    case = next(c for c in ALL_CASES if c.id == "VIS-006")
    trace = (await _run(case)).decisionTrace

    order = [c["check"] for c in trace["steps"]["checks"]]
    assert order.index("coverage") < order.index("technician")


async def test_the_trace_records_the_model_facts_and_trace_id() -> None:
    from tests.test_pipeline import _run

    case = next(c for c in ALL_CASES if c.id == "VIS-003")
    trace = (await _run(case)).decisionTrace

    model = trace["steps"]["model"]
    assert model["traceIds"] == ["trace-pipeline"]
    assert model["facts"]["safetySignal"] == "affirmed"
    assert model["facts"]["intent"]


async def test_the_action_step_reports_a_created_work_order() -> None:
    from tests.test_pipeline import _run

    reset_created_work_orders()
    case = next(c for c in ALL_CASES if c.id == "VIS-006")
    trace = (await _run(case)).decisionTrace

    action = trace["steps"]["action"]
    assert action["taken"] == "work_order_created"
    assert action["workOrderId"]
    assert action["technicianId"]


async def test_the_action_step_reports_a_link_and_what_was_not_linked() -> None:
    from tests.test_pipeline import _run

    case = next(c for c in ALL_CASES if c.id == "VIS-005")
    trace = (await _run(case)).decisionTrace

    action = trace["steps"]["action"]
    assert action["taken"] == "linked_to_existing"
    assert "REQ-V001" in action["linkedRequests"]
    # The unrelated open job is recorded as deliberately not linked.
    assert "WO-9294" in action["notLinked"]


async def test_no_write_is_recorded_when_nothing_was_created() -> None:
    from tests.test_pipeline import _run

    case = next(c for c in ALL_CASES if c.id == "VIS-001")
    trace = (await _run(case)).decisionTrace
    assert trace["steps"]["action"]["taken"] == "no_write"


# --- what must never appear --------------------------------------------


async def test_the_trace_never_carries_the_customer_message_body() -> None:
    from tests.test_pipeline import _run

    reset_created_work_orders()
    for case in ALL_CASES:
        trace = (await _run(case)).decisionTrace
        blob = json.dumps(trace)
        # The body is not recorded; the request id and safety quote locate the original.
        assert case.body not in blob, case.id
        assert trace["requestId"]


async def test_the_safety_quote_is_kept() -> None:
    """POL-SAFETY-001 wants the reporter's own words on the record."""
    from tests.test_pipeline import _run

    case = next(c for c in ALL_CASES if c.id == "VIS-003")
    trace = (await _run(case)).decisionTrace
    assert "diesel smell" in trace["steps"]["model"]["facts"]["safetyQuote"]


async def test_the_trace_never_carries_a_credential() -> None:
    from tests.test_pipeline import _run

    reset_created_work_orders()
    for case in ALL_CASES:
        blob = json.dumps((await _run(case)).decisionTrace)
        assert "cel_northstar_" not in blob
        assert "cel_model_" not in blob
        assert "Bearer" not in blob
        assert "authorization" not in blob.lower()


# --- the log ------------------------------------------------------------


def test_the_log_writes_one_line_per_request(tmp_path) -> None:
    log = TraceLog(str(tmp_path / "traces.jsonl"))
    log.record({"requestId": "REQ-1", "status": "covered_action"})
    log.record({"requestId": "REQ-2", "status": "dispatch_ready"})

    lines = (tmp_path / "traces.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["requestId"] == "REQ-1"
    assert json.loads(lines[1])["loggedAt"]


def test_recent_returns_newest_first(tmp_path) -> None:
    log = TraceLog(str(tmp_path / "traces.jsonl"))
    for i in range(5):
        log.record({"requestId": f"REQ-{i}", "status": "covered_action"})

    recent = log.recent(3)
    assert [t["requestId"] for t in recent] == ["REQ-4", "REQ-3", "REQ-2"]


def test_a_failing_log_does_not_raise(tmp_path) -> None:
    """A logging failure must never break the response."""
    log = TraceLog(str(tmp_path / "traces.jsonl"))
    # A value json cannot serialise, and a path that has become a directory.
    log.record({"requestId": "REQ-X", "bad": object()})
    log._path.unlink(missing_ok=True)
    log._path.mkdir()
    log.record({"requestId": "REQ-Y", "status": "ok"})
    # Both survived, and the in-memory tail still has them.
    assert [t["requestId"] for t in log.recent()] == ["REQ-Y", "REQ-X"]


def test_an_unwritable_directory_is_tolerated() -> None:
    log = TraceLog("/nonexistent\x00path/traces.jsonl")
    log.record({"requestId": "REQ-Z", "status": "ok"})
    assert log.recent()[0]["requestId"] == "REQ-Z"


# --- the endpoint -------------------------------------------------------


def test_recent_endpoint_returns_traces_for_any_caller(monkeypatch, tmp_path) -> None:
    import app.main as main
    from tests.test_contract import _stub_handler

    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_stub_handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", patched)
    monkeypatch.setattr(main, "trace_log", TraceLog(str(tmp_path / "t.jsonl")))
    main._RESULTS_BY_EVENT_ID.clear()

    body = {
        "requestId": "REQ-RECENT",
        "receivedAt": "2026-09-20T08:42:00Z",
        "channel": "portal",
        "sender": {"name": "T", "email": "t@example.com"},
        "subject": "Main DG service",
        "body": "Please service AST-101.",
        "attachments": [],
    }
    with TestClient(main.app) as client:
        client.post("/cases/process", json=body, headers={"X-Event-ID": "evt-recent"})
        response = client.get("/cases/recent")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["traces"][0]["requestId"] == "REQ-RECENT"
    # No credentials, whoever is calling.
    assert "cel_" not in response.text


def test_recent_endpoint_is_empty_before_any_request(monkeypatch, tmp_path) -> None:
    import app.main as main

    monkeypatch.setattr(main, "trace_log", TraceLog(str(tmp_path / "empty.jsonl")))
    with TestClient(main.app) as client:
        payload = client.get("/cases/recent").json()
    assert payload == {"count": 0, "traces": []}


# --- restart recovery through the endpoint ------------------------------


def test_the_endpoint_recovers_a_redelivered_event_after_a_restart(monkeypatch, tmp_path) -> None:
    """Simulates the restart: the in-memory store is cleared, the records are not.

    This is the wiring test — recover_dispatched_case has to run before process_case,
    and only for an event id the store does not recognise.
    """
    import app.main as main
    from app.pipeline import handled_requests
    from tests.saved_api import created_work_orders, reset_created_work_orders, saved_handler

    model_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("route") == "model":
            model_calls.append("call")
            return httpx.Response(
                200,
                json={
                    "id": "cmpl-restart",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "assetMentions": ["AST-302"],
                                        "safetySignal": "denied",
                                        "safetyQuote": (
                                            "There is no smoke, water or unusual smell."
                                        ),
                                        "intent": "breakdown",
                                        "symptomSummary": "stopped cooling",
                                        "referencedRequests": [],
                                        "requiresOnsite": True,
                                        "sameFaultAsExisting": "different",
                                        "sameFaultReference": "",
                                        "refersToPreviousWork": False,
                                        "isSafetyAnswer": False,
                                        "safetyAnswerUncertain": False,
                                    }
                                )
                            }
                        }
                    ],
                },
            )
        return saved_handler(request)

    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", patched)
    monkeypatch.setattr(main, "trace_log", TraceLog(str(tmp_path / "t.jsonl")))
    main._RESULTS_BY_EVENT_ID.clear()
    handled_requests._by_asset.clear()
    reset_created_work_orders()

    body = {
        "requestId": "REQ-RESTART",
        "receivedAt": "2026-09-20T09:31:00Z",
        "channel": "portal",
        "sender": {"name": "Deepa", "email": "ops@example.in"},
        "subject": "Freezer plant 2 stopped",
        "body": "AST-302 at SITE-021 has stopped cooling. There is no smoke, water or unusual smell.",
        "attachments": [],
    }
    headers = {"X-Event-ID": "evt-restart-endpoint"}

    with TestClient(main.app) as client:
        first = client.post("/cases/process", json=body, headers=headers).json()
        assert first["status"] == "dispatch_ready"
        assert len(model_calls) == 1
        created_id = first["workOrder"]["id"]

        # The restart: the result store is lost. The work order is not.
        main._RESULTS_BY_EVENT_ID.clear()
        handled_requests._by_asset.clear()
        model_calls.clear()

        second = client.post("/cases/process", json=body, headers=headers).json()

    assert second["status"] == "dispatch_ready"
    assert second["workOrder"]["id"] == created_id
    assert model_calls == [], "a recovered event must not spend a model call"
    assert len(created_work_orders()) == 1, "no second work order"
    assert any("recovered" in w.lower() for w in second["audit"]["warnings"])
