"""Pydantic models mirroring tests/case-input.schema.json and case-result.schema.json.

Both schemas set additionalProperties: true, so both sides allow extra fields:
inputs may carry more than the schema requires (the seeded request store, for example,
also has `priority` and attachment summaries), and the result may grow fields the
evaluator ignores.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class _Open(BaseModel):
    """Base for every model: additionalProperties: true in both schemas."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --- input -------------------------------------------------------------


class Sender(_Open):
    name: str
    email: EmailStr


class CaseInput(_Open):
    """A Northstar service request (case-input.schema.json)."""

    requestId: str = Field(min_length=1)
    receivedAt: str
    channel: Literal["email", "portal"]
    sender: Sender
    subject: str
    body: str
    attachments: list[dict[str, Any]]


# --- result ------------------------------------------------------------

CaseStatus = Literal[
    "covered_action",
    "clarification_required",
    "human_escalation_required",
    "account_review_required",
    "duplicate_detected",
    "dispatch_ready",
    "resource_escalation_required",
    "processing",
    "failed",
]


class Entities(_Open):
    """Null until resolved from records — never guessed."""

    customerId: str | None = None
    siteId: str | None = None
    assetId: str | None = None


class Classification(_Open):
    category: str
    urgency: str
    safetyRisk: bool
    confidence: float = Field(ge=0.0, le=1.0)


class Entitlement(_Open):
    """`evidence` carries the records the coverage decision rests on."""

    status: str
    sla: str | None = None
    evidence: list[Any] = Field(default_factory=list)


class NextAction(_Open):
    type: str
    reason: str


class Audit(_Open):
    sourceReferences: list[Any] = Field(default_factory=list)
    modelTraceIds: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CaseResult(_Open):
    """The structured first action (case-result.schema.json)."""

    caseId: str = Field(min_length=1)
    status: CaseStatus
    entities: Entities
    classification: Classification
    entitlement: Entitlement
    nextActions: list[NextAction] = Field(default_factory=list)
    customerResponseDraft: str
    missingInformation: list[str] = Field(default_factory=list)
    workOrder: dict[str, Any] | None = None
    audit: Audit
