"""Triage decision.

Walking skeleton: every case takes the cautious path and goes to a human, with a warning
saying triage is not implemented. The real pipeline (safety -> identity -> duplicate ->
coverage -> technician) lands here later.

Defaulting to human_escalation_required is deliberate, not a placeholder convenience: an
unimplemented triage cannot clear a safety signal, so the safe failure mode is a human.
"""

from __future__ import annotations

from .models import (
    Audit,
    CaseInput,
    CaseResult,
    Classification,
    Entitlement,
    Entities,
    NextAction,
)

NOT_IMPLEMENTED_WARNING = (
    "Triage not implemented: this is a walking skeleton. Every case is routed to a human "
    "duty owner without automated safety, entitlement or dispatch assessment."
)

HOLDING_DRAFT = (
    "Thank you for contacting Northstar Field Services. We have received your request and "
    "a coordinator is reviewing it now. We will confirm the next step shortly."
)


def triage(case: CaseInput, event_id: str | None) -> CaseResult:
    """Return the first action for one request.

    `event_id` is the X-Event-ID header: the idempotency key for any later write.
    """
    return CaseResult(
        caseId=case.requestId,
        status="human_escalation_required",
        # Nothing is resolved yet, and identity is never guessed.
        entities=Entities(customerId=None, siteId=None, assetId=None),
        classification=Classification(
            category="unclassified",
            urgency="unknown",
            # No automated assessment has run, so we cannot assert this is safe.
            safetyRisk=False,
            confidence=0.0,
        ),
        entitlement=Entitlement(
            status="not_assessed",
            sla="unknown",
            evidence=[],
        ),
        nextActions=[
            NextAction(
                type="route_to_duty_owner",
                reason=(
                    "Automated triage is not yet implemented; a coordinator must assess "
                    "safety, entitlement and dispatch for this request."
                ),
            )
        ],
        customerResponseDraft=HOLDING_DRAFT,
        missingInformation=[
            "Automated safety assessment",
            "Customer, site and asset resolution",
            "Entitlement decision from the agreement record",
        ],
        workOrder=None,
        audit=Audit(
            sourceReferences=[
                {"type": "request", "id": case.requestId, "channel": case.channel},
                {"type": "policy", "id": "POL-SAFETY-001"},
            ],
            modelTraceIds=[],
            warnings=[
                NOT_IMPLEMENTED_WARNING,
                # Recorded so a coordinator can tie the case to the delivery event.
                f"externalEventId={event_id}" if event_id else "No X-Event-ID header supplied",
            ],
        ),
    )
