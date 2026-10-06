"""Evidence tests, served from exploration/raw/ with HTTP mocked.

No test here touches the live API: a transport stub answers from the saved responses.
"""

from __future__ import annotations

import json
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
