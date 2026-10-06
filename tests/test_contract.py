"""The response must validate against the provided case-result.schema.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.main import app

SCHEMA_PATH = Path(__file__).parent / "case-result.schema.json"
CASES_PATH = Path(__file__).parent / "visible-cases.json"

client = TestClient(app)


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


def test_skeleton_routes_everything_to_a_human(validator) -> None:
    _, request_body = _visible_requests()[0]
    result = client.post(
        "/cases/process", json=request_body, headers={"X-Event-ID": "test-skeleton"}
    ).json()

    assert result["status"] == "human_escalation_required"
    assert result["workOrder"] is None
    assert any("not implemented" in w.lower() for w in result["audit"]["warnings"])


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
