"""Serves the saved exploration responses over a mock transport.

Used by the tests and by scripts/demo.py so neither touches the live API. The handler
reproduces the real API's behaviour, including that `q=` filters only on customers,
agreements and work-order-history (see exploration/FINDINGS.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from app.config import Secret, Settings
from app.northstar_client import NorthstarClient

RAW = Path(__file__).parent.parent / "exploration" / "raw"

# Routes where the API honours q=. Elsewhere it is ignored and the full list comes back.
FILTERING_ROUTES = frozenset({"customers", "agreements", "work-order-history"})


def saved(route: str) -> dict[str, Any]:
    return json.loads((RAW / f"{route}.json").read_text(encoding="utf-8"))


def offline_settings() -> Settings:
    """Settings pointing at an unroutable host, with placeholder credentials."""
    return Settings(
        northstar_api_base="https://example.invalid/api/assessment",
        northstar_token=Secret("test-token"),
        model_endpoint="https://example.invalid/api/assessment?route=model",
        model_key=Secret("test-model-key"),
        model_alias="celeco-assessment-chat-v1",
        port=8080,
        request_timeout_seconds=5.0,
    )


def saved_handler(request: httpx.Request) -> httpx.Response:
    """Reads come from the saved files; a work-order POST is simulated idempotently."""
    route = request.url.params.get("route", "")

    if request.method == "POST" and route == "work-orders":
        return _created_work_order(request)

    payload = saved(route)

    # A work order created during the run is visible to later reads, as it would be
    # against the real API. Without this a second request for the same equipment sees no
    # open job and the duplicate check has nothing to work with.
    if route == "work-orders" and _CREATED:
        payload = {"results": list(payload["results"]) + list(_CREATED.values())}

    q = request.url.params.get("q")
    if q and route in FILTERING_ROUTES:
        needle = q.lower()
        payload = {"results": [r for r in payload["results"] if needle in json.dumps(r).lower()]}
    return httpx.Response(200, json=payload)


# Work orders "created" during a test run, keyed by externalEventId, so a repeated event
# returns the original with duplicate: true — the real API's documented behaviour.
_CREATED: dict[str, dict] = {}


def reset_created_work_orders() -> None:
    _CREATED.clear()


def created_work_orders() -> dict[str, dict]:
    return dict(_CREATED)


def _created_work_order(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    event_id = str(body.get("externalEventId") or "")

    if event_id in _CREATED:
        # Repeating an accepted externalEventId returns the original work order.
        return httpx.Response(200, json={"workOrder": _CREATED[event_id], "duplicate": True})

    work_order = {
        "id": f"WO-TEST-{len(_CREATED) + 1:03d}",
        "request_id": body.get("requestId"),
        "asset_id": body.get("assetId"),
        "technician_id": body.get("technicianId"),
        "status": "assigned",
        # Echo what was sent, like the real API: a hardcoded value here would hide
        # whether the service supplies a summary at all.
        "summary": body.get("summary"),
        "created_at": "2026-09-20T10:00:00.000Z",
        "source": "triage",
        "externalEventId": event_id,
    }
    _CREATED[event_id] = work_order
    return httpx.Response(200, json={"workOrder": work_order, "duplicate": False})


def offline_client(attempts: int = 3) -> NorthstarClient:
    """A NorthstarClient that answers from exploration/raw/ and makes no network calls."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(saved_handler))
    return NorthstarClient(offline_settings(), client=http, attempts=attempts, backoff_seconds=0.0)
