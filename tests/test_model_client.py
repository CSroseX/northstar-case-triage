"""Model client tests: every failure mode must reach the cautious path, not raise.

All model answers here are mocked. No test spends budget.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.model_client import MAX_OUTPUT_TOKENS, ModelClient, build_user_prompt
from tests.saved_api import offline_settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _gateway_response(payload: Any, trace_id: str = "trace-123") -> httpx.Response:
    """A well-formed gateway envelope wrapping whatever the model 'said'."""
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(
        200,
        json={
            "id": trace_id,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "celeco": {"traceId": trace_id, "allowance": {"requests": 1}, "warning": None},
        },
    )


def _client(handler) -> ModelClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # cache_dir="" keeps the dev cache off regardless of the environment.
    return ModelClient(offline_settings(), client=http, cache_dir="")


GOOD_PAYLOAD = {
    "assetMentions": ["AST-302"],
    "safetySignal": "denied",
    "safetyQuote": "There is no smoke, water or unusual smell.",
    "intent": "breakdown",
    "symptomSummary": "stopped cooling",
    "referencedRequests": [],
    "requiresOnsite": True,
    "sameFaultAsExisting": "different",
    "sameFaultReference": "",
}


async def test_well_formed_answer_is_parsed() -> None:
    async with _client(lambda r: _gateway_response(GOOD_PAYLOAD)) as model:
        result = await model.extract_facts("Freezer stopped", "AST-302 has stopped cooling.")

    assert result.facts.modelUnavailable is False
    assert result.facts.safetySignal == "denied"
    assert result.facts.intent == "breakdown"
    assert result.facts.sameFaultAsExisting == "different"
    assert result.traceIds == ["trace-123"]


async def test_malformed_json_is_model_unavailable() -> None:
    async with _client(lambda r: _gateway_response("{not valid json at all")) as model:
        result = await model.extract_facts("Subject", "Body")

    assert result.facts.modelUnavailable is True
    # Trace id is still captured for the audit record even though the answer was unusable.
    assert result.traceIds == ["trace-123"]
    assert any("malformed" in w.lower() for w in result.warnings or [])


async def test_missing_fields_degrade_safely() -> None:
    """An answer with no safety signal must not be read as 'no hazard'."""
    async with _client(lambda r: _gateway_response({"intent": "breakdown"})) as model:
        result = await model.extract_facts("Subject", "Body")

    assert result.facts.safetySignal == "ambiguous"
    assert result.facts.assetMentions == []


async def test_invalid_safety_value_becomes_ambiguous() -> None:
    payload = dict(GOOD_PAYLOAD, safetySignal="probably fine")
    async with _client(lambda r: _gateway_response(payload)) as model:
        result = await model.extract_facts("Subject", "Body")

    assert result.facts.safetySignal == "ambiguous"
    assert any("unrecognised safety signal" in w for w in result.warnings or [])


async def test_timeout_is_model_unavailable_and_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async with _client(handler) as model:
        result = await model.extract_facts("Subject", "Body")

    assert result.facts.modelUnavailable is True
    # One call only: a retry costs budget and the cautious path is already correct.
    assert calls["n"] == 1


async def test_http_error_is_model_unavailable() -> None:
    async with _client(lambda r: httpx.Response(503, json={"error": "unavailable"})) as model:
        result = await model.extract_facts("Subject", "Body")

    assert result.facts.modelUnavailable is True
    assert any("503" in w for w in result.warnings or [])


async def test_envelope_without_choices_is_model_unavailable() -> None:
    async with _client(lambda r: httpx.Response(200, json={"id": "t-1"})) as model:
        result = await model.extract_facts("Subject", "Body")

    assert result.facts.modelUnavailable is True


async def test_request_uses_the_documented_gateway_contract() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return _gateway_response(GOOD_PAYLOAD)

    async with _client(handler) as model:
        await model.extract_facts("Subject", "Body")

    body = seen["body"]
    assert body["model"] == "celeco-assessment-chat-v1"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == MAX_OUTPUT_TOKENS <= 2048
    assert len(body["messages"]) == 2
    # The key travels in the header, never the URL.
    assert seen["auth"].startswith("Bearer ")
    assert "test-model-key" not in seen["url"]


async def test_prompt_carries_the_message_and_records_but_no_credentials() -> None:
    prompt = build_user_prompt(
        subject="Freezer plant 2 stopped",
        body="AST-302 at SITE-021 has stopped cooling.",
        open_work_orders=[
            {
                "id": "WO-9290",
                "summary": "Freezer plant 2 compressor cycling",
                "status": "assigned",
                "requestId": "REQ-8248",
            }
        ],
        recent_requests=[
            {
                "requestId": "REQ-8011",
                "receivedAt": "2026-09-01T10:00:00Z",
                "subject": "Freezer tripping",
                "body": "Freezer plant tripping under load.",
            }
        ],
    )

    assert "AST-302 at SITE-021 has stopped cooling." in prompt
    assert "WO-9290" in prompt
    assert "REQ-8011" in prompt
    # Nothing secret, and no coverage terms: the model does not decide entitlement.
    assert "cel_" not in prompt
    assert "Bearer" not in prompt
    assert "responseHours" not in prompt


async def test_no_records_is_stated_explicitly() -> None:
    prompt = build_user_prompt("S", "B", open_work_orders=[], recent_requests=[])
    assert "No open jobs or earlier requests" in prompt


async def test_dev_cache_is_off_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MODEL_CACHE_DIR", raising=False)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _gateway_response(GOOD_PAYLOAD)))
    model = ModelClient(offline_settings(), client=http)
    assert model._cache.enabled is False


async def test_dev_cache_serves_a_repeat_without_calling_the_gateway(tmp_path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _gateway_response(GOOD_PAYLOAD)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with ModelClient(offline_settings(), client=http, cache_dir=str(tmp_path)) as model:
        first = await model.extract_facts("Freezer stopped", "AST-302 has stopped cooling.")
        second = await model.extract_facts("Freezer stopped", "AST-302 has stopped cooling.")

    assert calls["n"] == 1  # the second answer came from the cache
    assert first.facts.safetySignal == second.facts.safetySignal
    assert any("cache" in w.lower() for w in second.warnings or [])


async def test_dev_cache_misses_when_the_message_changes(tmp_path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _gateway_response(GOOD_PAYLOAD)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with ModelClient(offline_settings(), client=http, cache_dir=str(tmp_path)) as model:
        await model.extract_facts("Subject", "First body")
        await model.extract_facts("Subject", "A different body")

    assert calls["n"] == 2
