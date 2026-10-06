"""Shared case fixtures: the hand-written facts and their expected statuses.

These are what the model will later produce from each message. They live here, rather than
inline in the tests, so that tests/test_decide.py and scripts/demo.py read from one
definition and cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.facts import RequestFacts


@dataclass(frozen=True)
class Case:
    """One request, the facts extracted from it, and the status we expect."""

    id: str
    assetId: str | None
    receivedAt: str
    expectedStatus: str
    subject: str
    body: str
    facts: RequestFacts
    note: str = ""


# --- the eight runner cases --------------------------------------------

RUNNER_CASES: tuple[Case, ...] = (
    Case(
        id="VIS-001",
        assetId="AST-101",
        receivedAt="2026-09-20T08:42:00Z",
        expectedStatus="covered_action",
        subject="Main DG scheduled service",
        body=(
            "Please arrange the covered quarterly service for Main DG at our Whitefield "
            "warehouse. The asset label reads AST-101."
        ),
        facts=RequestFacts(
            assetMentions=["AST-101"],
            safetySignal="absent",
            intent="planned_service",
            symptomSummary="quarterly service for Main DG",
            requiresOnsite=False,
            sameFaultAsExisting="different",
        ),
        note="Covered planned work: scheduled, not dispatched at intake.",
    ),
    Case(
        id="VIS-002",
        assetId=None,
        receivedAt="2026-09-20T08:57:00Z",
        expectedStatus="clarification_required",
        subject="Hoskote freezer temperature rising",
        body=(
            "The freezer plant at our Hoskote cold store is not pulling down below -12C. "
            "I do not have the asset number."
        ),
        facts=RequestFacts(
            assetMentions=[],
            safetySignal="absent",
            intent="breakdown",
            symptomSummary="freezer plant not pulling below -12C",
        ),
        note="No asset identifier, so nothing can be looked up.",
    ),
    Case(
        id="VIS-003",
        assetId=None,
        receivedAt="2026-09-20T09:04:00Z",
        expectedStatus="human_escalation_required",
        subject="Fuel smell near backup generator",
        body=(
            "Security reports a strong diesel smell and unusual vibration near the backup "
            "generator at our Indiranagar centre."
        ),
        facts=RequestFacts(
            assetMentions=[],
            safetySignal="affirmed",
            safetyQuote="a strong diesel smell and unusual vibration near the backup generator",
            intent="breakdown",
            symptomSummary="diesel smell and vibration near backup generator",
        ),
        note="Escalates with no asset resolved: safety never waits on a lookup.",
    ),
    Case(
        id="VIS-004",
        assetId="CMP-77",
        receivedAt="2026-09-20T09:18:00Z",
        expectedStatus="account_review_required",
        subject="Compressor not listed in portal",
        body=(
            "Please dispatch someone for compressor CMP-77 on line 2. I cannot find it in "
            "our covered asset list."
        ),
        facts=RequestFacts(
            assetMentions=["CMP-77"],
            safetySignal="absent",
            intent="breakdown",
            symptomSummary="compressor not in covered asset list",
        ),
        note="Asset is not on the account; a premium plan does not cover a new asset.",
    ),
    Case(
        id="VIS-005",
        assetId="AST-101",
        receivedAt="2026-09-20T09:24:00Z",
        expectedStatus="duplicate_detected",
        subject="Following up: Main DG service",
        body=(
            "Kavya raised the Main DG service request from the portal a few minutes ago. "
            "This email is for the same AST-101 visit."
        ),
        facts=RequestFacts(
            assetMentions=["AST-101"],
            safetySignal="absent",
            intent="follow_up",
            symptomSummary="Main DG service already raised from the portal",
            referencedRequests=["REQ-V001"],
            sameFaultAsExisting="same",
            sameFaultReference="REQ-V001",
        ),
        note="Email plus portal reporting one job. The earlier request made no work order.",
    ),
    Case(
        id="VIS-006",
        assetId="AST-302",
        receivedAt="2026-09-20T09:31:00Z",
        expectedStatus="dispatch_ready",
        subject="Freezer plant 2 stopped",
        body=(
            "AST-302 at SITE-021 has stopped cooling. There is no smoke, water or unusual "
            "smell."
        ),
        facts=RequestFacts(
            assetMentions=["AST-302"],
            safetySignal="denied",
            safetyQuote="There is no smoke, water or unusual smell.",
            intent="breakdown",
            symptomSummary="stopped cooling, room at -9C and rising",
            requiresOnsite=True,
            sameFaultAsExisting="different",
        ),
        note="Customer rules the hazard out themselves, so normal triage continues.",
    ),
    Case(
        id="VIS-007",
        assetId="AST-205",
        receivedAt="2026-09-20T09:38:00Z",
        expectedStatus="resource_escalation_required",
        subject="Electrical-certified technician needed",
        body=(
            "AST-205 at the Whitefield clinic needs an electrical-certified generator "
            "technician today. No one is in danger and the generator remains offline."
        ),
        facts=RequestFacts(
            assetMentions=["AST-205"],
            safetySignal="denied",
            safetyQuote="No one is in danger",
            intent="breakdown",
            symptomSummary="generator offline, needs electrical-certified technician",
            requiresOnsite=True,
        ),
        note="Covered, but the only qualified technician is already assigned.",
    ),
    Case(
        id="VIS-008",
        assetId="AST-101",
        receivedAt="2026-09-20T09:46:00Z",
        expectedStatus="covered_action",
        subject="Coverage confirmation for AST-101",
        body=(
            "Please confirm coverage for AST-101 using the amendment signed in August, "
            "not the older base contract."
        ),
        facts=RequestFacts(
            assetMentions=["AST-101"],
            safetySignal="absent",
            intent="coverage_question",
            symptomSummary="confirm coverage using the August amendment",
            requiresOnsite=False,
            sameFaultAsExisting="different",
        ),
        note="Answered from the amendment CON-012-A2, not a base contract.",
    ),
)


# --- the four extra cases ----------------------------------------------

EXTRA_CASES: tuple[Case, ...] = (
    Case(
        id="REQ-8268",
        assetId="AST-801",
        receivedAt="2026-09-20T10:51:00Z",
        expectedStatus="account_review_required",
        subject="Banquet chiller inspection",
        body=(
            "Please schedule an inspection for the banquet chiller AST-801 before this "
            "weekend's event. The account portal says service is on hold, but our finance "
            "team says the renewal payment was made yesterday."
        ),
        facts=RequestFacts(
            assetMentions=["AST-801"],
            safetySignal="absent",
            intent="planned_service",
            symptomSummary="banquet chiller inspection before the weekend event",
            requiresOnsite=True,
        ),
        note="A payment receipt is context, not evidence, until Commercial Ops allocates it.",
    ),
    Case(
        id="REQ-8271",
        assetId="AST-901",
        receivedAt="2026-09-20T11:06:00Z",
        expectedStatus="account_review_required",
        subject="Backup DG controller fault",
        body=(
            "Dialysis backup DG AST-901 shows controller fault 118. There is no smoke or "
            "smell. Please arrange someone on site before the evening shift."
        ),
        facts=RequestFacts(
            assetMentions=["AST-901"],
            safetySignal="denied",
            safetyQuote="There is no smoke or smell.",
            intent="breakdown",
            symptomSummary="controller fault 118 on backup DG",
            requiresOnsite=True,
        ),
        note="Remote-only agreement does not authorise the on-site visit they asked for.",
    ),
    Case(
        id="REQ-8279",
        assetId="AST-1102",
        receivedAt="2026-09-20T11:37:00Z",
        expectedStatus="human_escalation_required",
        subject="Water beside instrument UPS",
        body=(
            "There is water on the floor beside instrument UPS AST-1102. The area is being "
            "kept clear and nobody has touched the equipment."
        ),
        facts=RequestFacts(
            assetMentions=["AST-1102"],
            safetySignal="affirmed",
            safetyQuote="There is water on the floor beside instrument UPS AST-1102.",
            intent="breakdown",
            symptomSummary="water on the floor beside the UPS",
        ),
        note="Water near electrical equipment. Covered and staffed, and still not dispatched.",
    ),
    Case(
        id="HAZARD-MISSED",
        assetId="AST-302",
        receivedAt="2026-09-20T09:31:00Z",
        expectedStatus="clarification_required",
        subject="Freezer stopped",
        body="The freezer has stopped and there is a burning smell coming from the panel.",
        facts=RequestFacts(
            assetMentions=["AST-302"],
            safetySignal="absent",  # the model missed it
            intent="breakdown",
            symptomSummary="freezer stopped",
            requiresOnsite=True,
            sameFaultAsExisting="different",
        ),
        note="Constructed case: the model reports no hazard but the text says otherwise.",
    ),
)

ALL_CASES: tuple[Case, ...] = RUNNER_CASES + EXTRA_CASES

# Assets walked through in the lookup section of the demo.
LOOKUP_ASSETS: tuple[tuple[str, str], ...] = (
    ("AST-101", "2026-09-20T08:42:00Z"),
    ("AST-205", "2026-09-20T09:38:00Z"),
    ("AST-302", "2026-09-20T09:31:00Z"),
    ("AST-801", "2026-09-20T10:51:00Z"),
    ("AST-901", "2026-09-20T11:06:00Z"),
    ("CMP-77", "2026-09-20T09:18:00Z"),
)
