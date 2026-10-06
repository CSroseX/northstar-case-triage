"""Booking: the re-check, the idempotency key, and the uncertain-outcome path.

Every Northstar call here is mocked. No live writes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.booking import book_work_order
from app.decide import Decision, Reason
from app.evidence import gather_asset_evidence
from app.northstar_client import NorthstarClient
from tests.saved_api import offline_settings, saved

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _dispatch_decision(technician) -> Decision:
    return Decision(
        status="dispatch_ready",
        reasons=[Reason("coverage_confirmed", "covered", "POL-CONTRACT-002")],
        recommendedTechnician=technician,
    )


class Gateway:
    """Saved reads, with configurable technician availability and write behaviour."""

    def __init__(
        self,
        unavailable: set[str] | None = None,
        write_responses: list[Any] | None = None,
    ) -> None:
        self.unavailable = unavailable or set()
        self.write_responses = write_responses or []
        self.writes: list[dict[str, Any]] = []
        self.created: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        route = request.url.params.get("route", "")

        if request.method == "POST" and route == "work-orders":
            body = json.loads(request.content)
            self.writes.append(body)
            if self.write_responses:
                behaviour = self.write_responses.pop(0)
                if behaviour == "timeout":
                    raise httpx.ReadTimeout("timed out", request=request)
                if behaviour == "504":
                    return httpx.Response(504, json={"error": "dependency_timeout"})
                if behaviour == "422":
                    return httpx.Response(422, json={"error": "invalid", "reasons": ["bad"]})
            event_id = body["externalEventId"]
            if event_id in self.created:
                return httpx.Response(
                    200, json={"workOrder": self.created[event_id], "duplicate": True}
                )
            work_order = {
                "id": f"WO-NEW-{len(self.created) + 1}",
                "request_id": body["requestId"],
                "asset_id": body["assetId"],
                "technician_id": body["technicianId"],
                "status": "assigned",
                "externalEventId": event_id,
            }
            self.created[event_id] = work_order
            return httpx.Response(200, json={"workOrder": work_order, "duplicate": False})

        payload = saved(route)
        if route == "technicians" and self.unavailable:
            payload = {
                "results": [
                    {**t, "available": False} if t["id"] in self.unavailable else t
                    for t in payload["results"]
                ]
            }
        if route == "work-orders":
            # Reconciliation reads see whatever this gateway has created.
            payload = {"results": payload["results"] + list(self.created.values())}
        q = request.url.params.get("q")
        if q and route in {"customers", "agreements", "work-order-history"}:
            payload = {
                "results": [r for r in payload["results"] if q.lower() in json.dumps(r).lower()]
            }
        return httpx.Response(200, json=payload)


async def _setup(gateway: Gateway):
    http = httpx.AsyncClient(transport=httpx.MockTransport(gateway.handler))
    client = NorthstarClient(offline_settings(), client=http, attempts=1, backoff_seconds=0.0)
    await client.__aenter__()
    evidence = await gather_asset_evidence(client, "AST-302", "2026-09-20T09:31:00Z")
    return client, evidence


async def test_work_order_is_created_with_the_event_id_as_external_reference() -> None:
    gateway = Gateway()
    client, evidence = await _setup(gateway)
    chosen = next(t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite)

    outcome = await book_work_order(
        client, _dispatch_decision(chosen), evidence, "REQ-X", "evt-123"
    )

    assert outcome.created
    assert outcome.workOrder["id"] == "WO-NEW-1"
    assert gateway.writes[0]["externalEventId"] == "evt-123"
    assert gateway.writes[0]["safetyRisk"] is False


async def test_a_repeated_event_id_returns_the_original_and_is_not_an_error() -> None:
    gateway = Gateway()
    client, evidence = await _setup(gateway)
    chosen = next(t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite)

    first = await book_work_order(
        client, _dispatch_decision(chosen), evidence, "REQ-X", "evt-same"
    )
    second = await book_work_order(
        client, _dispatch_decision(chosen), evidence, "REQ-X", "evt-same"
    )

    assert first.workOrder["id"] == second.workOrder["id"]
    assert second.duplicate is True
    # duplicate: true is a success, so the job is still reported as booked.
    assert second.created
    assert second.status is None
    assert len(gateway.created) == 1


async def test_an_unavailable_recommendation_is_replaced_automatically() -> None:
    """TECH-02 has gone since triage; the next qualified local technician takes it."""
    gateway = Gateway(unavailable={"TECH-02"})
    client, evidence = await _setup(gateway)
    gone = next(t for t in evidence.qualifiedTechnicians if t.technicianId == "TECH-02")

    outcome = await book_work_order(
        client, _dispatch_decision(gone), evidence, "REQ-X", "evt-swap"
    )

    assert outcome.created
    assert outcome.technician.technicianId != "TECH-02"
    assert gateway.writes[0]["technicianId"] == outcome.technician.technicianId
    assert any("no longer available" in w for w in outcome.warnings)


async def test_nobody_left_becomes_resource_escalation_not_a_work_order() -> None:
    gateway = Gateway(unavailable={"TECH-02", "TECH-05", "TECH-10", "TECH-06", "TECH-12"})
    client, evidence = await _setup(gateway)
    chosen = next(t for t in evidence.qualifiedTechnicians if t.technicianId == "TECH-02")

    outcome = await book_work_order(
        client, _dispatch_decision(chosen), evidence, "REQ-X", "evt-none"
    )

    assert outcome.status == "resource_escalation_required"
    assert not outcome.created
    assert gateway.writes == []  # never write against an unavailable technician


async def test_a_timeout_reconciles_and_does_not_create_a_second_job() -> None:
    """The write lands but the response is lost; reconciliation finds it."""

    class LosesTheResponse(Gateway):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.params.get("route") == "work-orders":
                body = json.loads(request.content)
                self.writes.append(body)
                # Record it as created, then fail to answer.
                self.created[body["externalEventId"]] = {
                    "id": "WO-LANDED",
                    "request_id": body["requestId"],
                    "asset_id": body["assetId"],
                    "technician_id": body["technicianId"],
                    "status": "assigned",
                    "externalEventId": body["externalEventId"],
                }
                raise httpx.ReadTimeout("timed out", request=request)
            return super().handler(request)

    gateway = LosesTheResponse()
    client, evidence = await _setup(gateway)
    chosen = next(t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite)

    outcome = await book_work_order(
        client, _dispatch_decision(chosen), evidence, "REQ-FRESH", "evt-lost"
    )

    assert outcome.created
    assert outcome.workOrder["id"] == "WO-LANDED"
    # One write attempt only: reconciliation found it, so no retry was needed.
    assert len(gateway.writes) == 1
    assert any("reconciliation" in w for w in outcome.warnings)


async def test_a_timeout_that_did_not_land_retries_with_the_same_event_id() -> None:
    gateway = Gateway(write_responses=["timeout"])
    client, evidence = await _setup(gateway)
    chosen = next(t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite)

    outcome = await book_work_order(
        client, _dispatch_decision(chosen), evidence, "REQ-X", "evt-retry"
    )

    assert outcome.created
    assert len(gateway.writes) == 2
    # The retry must reuse the id, or it could create a second job.
    assert gateway.writes[0]["externalEventId"] == gateway.writes[1]["externalEventId"]


async def test_an_unconfirmable_outcome_is_failed() -> None:
    gateway = Gateway(write_responses=["timeout", "timeout"])
    client, evidence = await _setup(gateway)
    chosen = next(t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite)

    outcome = await book_work_order(
        client, _dispatch_decision(chosen), evidence, "REQ-X", "evt-unknown"
    )

    assert outcome.status == "failed"
    assert not outcome.created
    assert any("could not be confirmed" in w for w in outcome.warnings)


async def test_a_rejected_write_is_not_retried() -> None:
    gateway = Gateway(write_responses=["422"])
    client, evidence = await _setup(gateway)
    chosen = next(t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite)

    outcome = await book_work_order(
        client, _dispatch_decision(chosen), evidence, "REQ-X", "evt-bad"
    )

    assert outcome.status == "failed"
    assert len(gateway.writes) == 1  # a 4xx will not improve on retry


async def test_no_event_id_means_no_write() -> None:
    """Without the idempotency key a retry could duplicate the job, so we do not write."""
    gateway = Gateway()
    client, evidence = await _setup(gateway)
    chosen = next(t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite)

    outcome = await book_work_order(client, _dispatch_decision(chosen), evidence, "REQ-X", None)

    assert outcome.status == "resource_escalation_required"
    assert gateway.writes == []
