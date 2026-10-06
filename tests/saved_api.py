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
    route = request.url.params.get("route", "")
    payload = saved(route)
    q = request.url.params.get("q")
    if q and route in FILTERING_ROUTES:
        needle = q.lower()
        payload = {"results": [r for r in payload["results"] if needle in json.dumps(r).lower()]}
    return httpx.Response(200, json=payload)


def offline_client(attempts: int = 3) -> NorthstarClient:
    """A NorthstarClient that answers from exploration/raw/ and makes no network calls."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(saved_handler))
    return NorthstarClient(offline_settings(), client=http, attempts=attempts, backoff_seconds=0.0)
