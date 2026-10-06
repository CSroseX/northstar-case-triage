"""The response must validate against the provided case-result.schema.json.

Northstar reads and the model call are stubbed for the whole module: these tests check
the contract, and must never touch the live API or spend model budget.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

import app.main as main
from app.main import app
from tests.saved_api import saved_handler

SCHEMA_PATH = Path(__file__).parent / "case-result.schema.json"
CASES_PATH = Path(__file__).parent / "visible-cases.json"

client = TestClient(app)

# A model answer that asserts nothing about safety, so the cautious path applies and the
# contract is exercised without any real call.
_STUB_MODEL_ANSWER = {
    "assetMentions": [],
    "safetySignal": "absent",
    "safetyQuote": "",
    "intent": "other",
    "symptomSummary": "stubbed",
    "referencedRequests": [],
    "requiresOnsite": False,
    "sameFaultAsExisting": "",
    "sameFaultReference": "",
}


def _stub_handler(request: httpx.Request) -> httpx.Response:
    # Route on the route parameter, not the verb: the work-order write is also a POST.
    if request.url.params.get("route") == "model":
        return httpx.Response(
            200,
            json={
                "id": "trace-contract",
                "choices": [
                    {"message": {"role": "assistant", "content": json.dumps(_STUB_MODEL_ANSWER)}}
                ],
                "celeco": {"traceId": "trace-contract"},
            },
        )
    return saved_handler(request)


@pytest.fixture(autouse=True)
def _no_live_calls(monkeypatch):
    """Route every outbound call in this module to the saved records and a stub model."""
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_stub_handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", patched)
    main._RESULTS_BY_EVENT_ID.clear()


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _visible_requests() -> list[tuple[str, dict]]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return [(case["id"], case["request"]) for case in cases]


def test_health_returns_200() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_never_exposes_secrets() -> None:
    body = client.get("/health").text
    assert "cel_northstar_" not in body
    assert "cel_model_" not in body


@pytest.mark.parametrize("case_id,request_body", _visible_requests())
def test_result_matches_schema(case_id: str, request_body: dict, validator) -> None:
    response = client.post(
        "/cases/process",
        json=request_body,
        headers={"X-Event-ID": f"test-{case_id.lower()}"},
    )
    assert response.status_code == 200, response.text

    result = response.json()
    errors = sorted(validator.iter_errors(result), key=lambda e: e.path)
    assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors)


def test_every_result_reports_a_known_status(validator) -> None:
    """Whatever the triage concludes, it must be one of the schema's statuses."""
    allowed = set(
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["properties"]["status"]["enum"]
    )
    for case_id, request_body in _visible_requests():
        result = client.post(
            "/cases/process", json=request_body, headers={"X-Event-ID": f"test-{case_id}"}
        ).json()
        assert result["status"] in allowed
        # Nothing is ever created by a read-only triage pass.
        assert result["workOrder"] is None or result["workOrder"].get("created") is False


def test_extra_input_fields_are_accepted(validator) -> None:
    """The input schema sets additionalProperties: true; the seeded store sends more."""
    _, request_body = _visible_requests()[0]
    enriched = {
        **request_body,
        "priority": "high",
        "workflow": None,
        "attachments": [{"id": "ATT-001", "type": "image", "summary": "Label photo."}],
    }
    response = client.post(
        "/cases/process", json=enriched, headers={"X-Event-ID": "test-extra"}
    )
    assert response.status_code == 200
    assert not list(validator.iter_errors(response.json()))


def test_missing_required_field_is_rejected() -> None:
    response = client.post("/cases/process", json={"requestId": "REQ-1"})
    assert response.status_code == 422


def test_missing_event_id_still_returns_a_valid_result(validator) -> None:
    """X-Event-ID is required for writes, but its absence must not crash triage."""
    _, request_body = _visible_requests()[0]
    response = client.post("/cases/process", json=request_body)
    assert response.status_code == 200
    assert not list(validator.iter_errors(response.json()))


def test_responses_declare_utf8() -> None:
    """Without an explicit charset, terminals and mail clients garble the drafts."""
    health = client.get("/health")
    assert health.headers["content-type"] == "application/json; charset=utf-8"

    _, request_body = _visible_requests()[0]
    response = client.post(
        "/cases/process", json=request_body, headers={"X-Event-ID": "test-charset"}
    )
    assert response.headers["content-type"] == "application/json; charset=utf-8"


def test_drafts_use_a_plain_hyphen_separator() -> None:
    """An em dash renders as mojibake where we do not control the encoding."""
    for case_id, request_body in _visible_requests():
        draft = client.post(
            "/cases/process", json=request_body, headers={"X-Event-ID": f"dash-{case_id}"}
        ).json()["customerResponseDraft"]
        assert "\u2014" not in draft and "\u2013" not in draft, draft
