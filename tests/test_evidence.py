"""Evidence tests, served from exploration/raw/ with HTTP mocked.

No test here touches the live API: a transport stub answers from the saved responses.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest import mock
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Secret, Settings
from app.evidence import gather_asset_evidence
from app.northstar_client import NorthstarClient

RAW = Path(__file__).parent.parent / "exploration" / "raw"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _saved(route: str) -> dict[str, Any]:
    return json.loads((RAW / f"{route}.json").read_text(encoding="utf-8"))


def _settings() -> Settings:
    return Settings(
        northstar_api_base="https://example.invalid/api/assessment",
        northstar_token=Secret("test-token"),
        model_endpoint="https://example.invalid/api/assessment?route=model",
        model_key=Secret("test-model-key"),
        model_alias="celeco-assessment-chat-v1",
        port=8080,
        request_timeout_seconds=5.0,
    )


class Recorder:
    """Serves saved responses and records calls. Optionally fails a route."""

    def __init__(self, fail_routes: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_routes = fail_routes or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        route = request.url.params.get("route", "")
        self.calls.append(route)
        if route in self.fail_routes:
            return httpx.Response(self.fail_routes[route], json={"error": "dependency_timeout"})

        payload = _saved(route)
        q = request.url.params.get("q")
        # Mirror the real API: q= filters only on these routes (see FINDINGS.md).
        if q and route in {"customers", "agreements", "work-order-history"}:
            needle = q.lower()
            payload = {
                "results": [
                    r for r in payload["results"] if needle in json.dumps(r).lower()
                ]
            }
        return httpx.Response(200, json=payload)


def _client(recorder: Recorder, attempts: int = 3) -> NorthstarClient:
    transport = httpx.MockTransport(recorder.handler)
    http = httpx.AsyncClient(transport=transport)
    return NorthstarClient(_settings(), client=http, attempts=attempts, backoff_seconds=0.0)


# --- the cases asked for ------------------------------------------------


async def test_ast_101_resolves_to_con_012_a2_with_4h_response() -> None:
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, "AST-101", "2026-09-20T08:42:00Z")

    assert ev.outcome == "resolved"
    assert ev.customer["id"] == "CUS-012"
    assert ev.customer["aliases"] == ["Aster Logistics"]
    assert ev.site["id"] == "SITE-008"
    assert ev.agreement["contractRef"] == "CON-012-A2"
    assert ev.agreement["responseHours"] == 4
    assert ev.agreement["serviceMode"] == "onsite"
    assert ev.agreement["status"] == "active"
    assert ev.agreement["inForceAtReceivedAt"] is True
    assert ev.agreement["customerMatchesAsset"] is True
    assert ev.agreement["supersedes"] == "CON-012"


async def test_ast_205_has_one_qualified_technician_and_none_available() -> None:
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, "AST-205", "2026-09-20T09:38:00Z")

    assert ev.outcome == "resolved"
    assert len(ev.qualifiedTechnicians) == 1
    only = ev.qualifiedTechnicians[0]
    assert only.technicianId == "TECH-04"
    assert only.available is False
    assert only.currentAssignment == "WO-9285"
    # Reported as a fact; the escalation decision is not made here.
    assert not [t for t in ev.qualifiedTechnicians if t.available]


async def test_ast_801_shows_the_suspended_agreement() -> None:
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, "AST-801", "2026-09-20T10:51:00Z")

    assert ev.agreement["contractRef"] == "CON-074"
    assert ev.agreement["status"] == "suspended"
    assert ev.agreement["assetCoverage"] == "suspended"
    assert ev.agreement["note"] == "Renewal payment awaiting allocation."
    # Expired 2026-08-31, before the request date.
    assert ev.agreement["receivedAfterEnd"] is True
    assert ev.agreement["inForceAtReceivedAt"] is False
    assert any("after agreement" in gap for gap in ev.gaps)
    # Still only facts: no status was assigned.
    assert ev.outcome == "resolved"


async def test_ast_901_shows_remote_only() -> None:
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, "AST-901", "2026-09-20T11:06:00Z")

    assert ev.agreement["contractRef"] == "CON-083-A1"
    assert ev.agreement["serviceMode"] == "remote_only"
    assert ev.agreement["assetCoverage"] == "remote_only"
    assert ev.agreement["status"] == "active"
    assert ev.agreement["inForceAtReceivedAt"] is True


async def test_received_before_agreement_start_is_flagged() -> None:
    # CON-012-A2 starts 2026-08-12; this request predates it.
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, "AST-101", "2026-07-01T09:00:00Z")

    assert ev.agreement["receivedBeforeStart"] is True
    assert ev.agreement["inForceAtReceivedAt"] is False
    assert any("before agreement" in gap for gap in ev.gaps)
    # Flagged, not decided.
    assert ev.outcome == "resolved"


async def test_cmp_77_is_an_unknown_asset() -> None:
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, "CMP-77", "2026-09-20T09:18:00Z")

    assert ev.outcome == "unknown_asset"
    assert ev.asset is None
    assert ev.customer is None
    assert ev.agreement is None
    assert any("not present in the customer records" in gap for gap in ev.gaps)


async def test_persistent_timeout_is_lookup_failed() -> None:
    recorder = Recorder(fail_routes={"customers": 504})
    async with _client(recorder, attempts=3) as client:
        ev = await gather_asset_evidence(client, "AST-101", "2026-09-20T08:42:00Z")

    assert ev.outcome == "lookup_failed"
    assert ev.failedLookups == ["customers"]
    assert ev.agreement is None
    # Retried, bounded, then gave up. No guess substituted.
    assert recorder.calls.count("customers") == 3


# --- supporting behaviour ----------------------------------------------


async def test_no_asset_id_means_nothing_to_look_up() -> None:
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, None, "2026-09-20T08:57:00Z")

    assert ev.outcome == "no_asset_id"
    assert ev.asset is None
    assert "nothing to look up" in " ".join(ev.gaps)


async def test_work_orders_are_normalised_and_filtered_to_the_asset() -> None:
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, "AST-101", "2026-09-20T08:42:00Z")

    assert len(ev.openWorkOrders) == 1
    wo = ev.openWorkOrders[0]
    # snake_case from the API becomes camelCase here.
    assert wo["id"] == "WO-9294"
    assert wo["assetId"] == "AST-101"
    assert wo["requestId"] == "REQ-8254"
    assert wo["technicianId"] == "TECH-01"
    assert wo["createdAt"] == "2026-09-20T09:20:00.000Z"
    assert "asset_id" not in wo


async def test_same_city_is_reported_per_technician() -> None:
    """AST-1301 needs generator+electrical; TECH-07 qualifies but sits in Mysuru."""
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, "AST-1301", "2026-09-20T12:08:00Z")

    by_id = {t.technicianId: t for t in ev.qualifiedTechnicians}
    assert by_id["TECH-07"].city == "Mysuru"
    assert by_id["TECH-07"].sameCityAsSite is False
    assert by_id["TECH-13"].sameCityAsSite is True
    # Reported, not filtered: D1 is applied by the decision layer.
    assert len(ev.qualifiedTechnicians) == 4


async def test_partial_failure_still_returns_the_rest() -> None:
    """A technicians outage must not discard the identity and agreement we did resolve."""
    recorder = Recorder(fail_routes={"technicians": 504})
    async with _client(recorder, attempts=2) as client:
        ev = await gather_asset_evidence(client, "AST-101", "2026-09-20T08:42:00Z")

    assert ev.outcome == "resolved"
    assert ev.agreement["contractRef"] == "CON-012-A2"
    assert ev.qualifiedTechnicians == []
    assert ev.failedLookups == ["technicians"]
    assert any("Technician lookup failed" in gap for gap in ev.gaps)


async def test_recent_requests_are_carried_for_duplicate_work() -> None:
    async with _client(Recorder()) as client:
        ev = await gather_asset_evidence(client, "AST-101", "2026-09-20T08:42:00Z")

    assert len(ev.recentRequests) == 18
    assert any(r.get("requestId") == "REQ-V001" for r in ev.recentRequests)


async def test_token_is_sent_as_a_bearer_header_not_a_query_param() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_saved("technicians"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with NorthstarClient(_settings(), client=http) as client:
        await client.technicians()

    assert seen["auth"] == "Bearer test-token"
    assert "test-token" not in seen["url"]


def test_a_summary_posted_under_payload_is_still_read() -> None:
    """Work orders we create keep what we posted under `payload`.

    Northstar promotes `summary` to the top level only for its own seeded rows. Ours
    stay nested, so without reading `payload` the summary we send is invisible to the
    duplicate check — which is the only reason for sending it (D-03/D-04).
    """
    from app.evidence import _normalise_work_order

    created = {
        "id": "bbafc236-98f9-4ada-95cd-1494354cd93e",
        "request_id": "REQ-SUMCHK2",
        "asset_id": "AST-401",
        "technician_id": "TECH-02",
        "status": "created",
        "payload": {
            "assetId": "AST-401",
            "summary": "Lab UPS battery fault, offline",
            "requestId": "REQ-SUMCHK2",
            "technicianId": "TECH-02",
        },
        "source": "project",
    }
    normalised = _normalise_work_order(created)
    assert normalised["summary"] == "Lab UPS battery fault, offline"
    assert normalised["assetId"] == "AST-401"
    assert normalised["requestId"] == "REQ-SUMCHK2"


def test_a_top_level_summary_still_wins() -> None:
    """Northstar's own rows are unaffected by the payload fallback."""
    from app.evidence import _normalise_work_order

    seeded = {
        "id": "WO-9290",
        "request_id": "REQ-8248",
        "asset_id": "AST-302",
        "summary": "Freezer plant 2 compressor cycling",
        "status": "assigned",
        "source": "operations",
    }
    assert _normalise_work_order(seeded)["summary"] == "Freezer plant 2 compressor cycling"


# --- a hanging Northstar must not hold a hazard -------------------------
# POL-SAFETY-001: an escalation must not wait on a record. These reads used to run one
# after another with a 20s timeout and 3 attempts each, so a timing-out Northstar could
# hold a hazard for minutes — past the runner's own 120s allowance for the whole case.


def _hanging_transport(hang_routes: set[str], delay: float = 60.0) -> httpx.MockTransport:
    """A transport where the named routes never answer within the test's lifetime."""

    async def handler(request: httpx.Request) -> httpx.Response:
        route = request.url.params.get("route", "")
        if route in hang_routes:
            await asyncio.sleep(delay)
        payload = _saved(route)
        q = request.url.params.get("q")
        if q and route in {"customers", "agreements", "work-order-history"}:
            needle = q.lower()
            payload = {"results": [r for r in payload["results"] if needle in json.dumps(r).lower()]}
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def test_evidence_gathering_gives_up_within_its_budget() -> None:
    """Everything still hanging when the budget expires is reported as a failed lookup."""
    import app.evidence as evidence_module

    budget = 1.0
    transport = _hanging_transport(
        {"technicians", "work-orders", "requests", "work-order-history", "agreements"}
    )
    http = httpx.AsyncClient(transport=transport)
    started = time.perf_counter()
    async with http:
        client = NorthstarClient(_settings(), client=http, attempts=1, backoff_seconds=0.0)
        with mock.patch.object(evidence_module, "EVIDENCE_BUDGET_SECONDS", budget):
            ev = await gather_asset_evidence(client, "AST-101", "2026-09-20T08:42:00Z")
    elapsed = time.perf_counter() - started

    # It returned, promptly, with the identity it did resolve.
    assert elapsed < budget + 2, f"took {elapsed:.1f}s against a {budget}s budget"
    assert ev.outcome == "resolved"
    assert ev.assetId == "AST-101"
    assert ev.customer["id"] == "CUS-012"

    # Everything that hung is a reported failure, not a silent default.
    assert set(ev.failedLookups) == {
        "agreements",
        "technicians",
        "work-orders",
        "requests",
        "work-order-history",
    }
    assert ev.agreement is None
    assert ev.qualifiedTechnicians == []
    assert any("evidence budget" in gap for gap in ev.gaps)


async def test_the_reads_run_together_not_one_after_another() -> None:
    """Five reads that each take a beat must cost one beat, not five.

    This is the property that keeps a slow Northstar inside the runner's limit; run
    sequentially the same delays would take five times as long.
    """
    delay = 0.3
    transport = _hanging_transport(
        {"agreements", "technicians", "work-orders", "requests", "work-order-history"},
        delay=delay,
    )
    http = httpx.AsyncClient(transport=transport)
    started = time.perf_counter()
    async with http:
        client = NorthstarClient(_settings(), client=http, attempts=1, backoff_seconds=0.0)
        ev = await gather_asset_evidence(client, "AST-101", "2026-09-20T08:42:00Z")
    elapsed = time.perf_counter() - started

    assert ev.outcome == "resolved"
    assert ev.failedLookups == [], "nothing should have failed at this delay"
    # Sequential would be ~5x delay. Allow generous headroom for a slow machine.
    assert elapsed < delay * 3, f"{elapsed:.2f}s suggests the reads ran sequentially"


async def test_a_hazard_escalates_even_when_northstar_hangs() -> None:
    """The case that matters: every read hangs, and the hazard still escalates fast."""
    import app.evidence as evidence_module
    from app.decide import decide
    from app.facts import RequestFacts

    budget = 1.0
    # Every route hangs, including the identity read.
    transport = _hanging_transport(
        {"customers", "agreements", "technicians", "work-orders", "requests", "work-order-history"}
    )
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="affirmed",
        safetyQuote="there is smoke coming from the generator",
        intent="breakdown",
        symptomSummary="smoke from the generator",
    )
    body = "AST-101 - there is smoke coming from the generator. We have moved everyone back."

    http = httpx.AsyncClient(transport=transport)
    started = time.perf_counter()
    async with http:
        client = NorthstarClient(_settings(), client=http, attempts=1, backoff_seconds=0.0)
        with mock.patch.object(evidence_module, "EVIDENCE_BUDGET_SECONDS", budget):
            ev = await gather_asset_evidence(client, "AST-101", "2026-09-20T08:42:00Z")
        decision = decide(facts, ev, body)
    elapsed = time.perf_counter() - started

    assert elapsed < budget + 2, f"a hazard waited {elapsed:.1f}s on the records"
    # The lookups failed, and the hazard still wins: safety is checked before the
    # lookup-failure branch, so no record outage can bury it.
    assert ev.outcome == "lookup_failed"
    assert decision.status == "human_escalation_required"
    assert any(r.code == "safety_signal_affirmed" for r in decision.reasons)
    assert decision.safetyQuote == "there is smoke coming from the generator"
