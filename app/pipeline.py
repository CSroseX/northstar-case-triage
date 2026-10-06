"""The per-request pipeline: intake -> read -> evidence -> decide -> respond.

Order matters and is deliberate:

1. Code pulls exact equipment ids out of the text. A regex is cheaper and more reliable
   than a model call for an exact pattern.
2. Evidence is gathered for that asset, so the open jobs and earlier requests exist
   before the model is asked to judge whether this is a repeat fault.
3. ONE model call reads the message against those records.
4. `decide()` applies the policy. Model reads, code decides.
5. The case result is assembled.

No work order is created here: that is the act step, and it is not wired yet.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from .booking import BookingOutcome, book_work_order
from .decide import Decision, Reason, decide
from .evidence import AssetEvidence, gather_asset_evidence
from .facts import RequestFacts
from .identifiers import find_equipment_ids, find_request_references, id_appears_in_text
from .replies import build_customer_reply
from .models import (
    Audit,
    CaseInput,
    CaseResult,
    Classification,
    Entitlement,
    Entities,
    NextAction,
)
from .model_client import ModelClient
from .northstar_client import NorthstarClient
from .trace import TraceLog, build_trace

logger = logging.getLogger("northstar.pipeline")


class HandledRequests:
    """The requests this service has already answered, by asset.

    Northstar's queue holds requests raised through its own channels, so a request
    delivered to *us* is invisible to the next one: two reports of the same fault minutes
    apart each saw "no earlier requests" and each became a work order.

    Showing the model what we already handled for the same equipment gives it something
    to compare against, and the existing response-window rule does the rest. Nothing here
    decides anything — it only supplies evidence.

    Known limit: in-process, so it is lost when the container restarts. A submitted
    runtime would persist this alongside the event-id store.
    """

    def __init__(self, keep_per_asset: int = 5) -> None:
        self._by_asset: dict[str, list[dict[str, Any]]] = {}
        self._keep = keep_per_asset

    def record(self, asset_id: str | None, case: CaseInput) -> None:
        if not asset_id:
            return
        entries = self._by_asset.setdefault(asset_id.upper(), [])
        if any(e.get("requestId") == case.requestId for e in entries):
            return
        entries.append(
            {
                "requestId": case.requestId,
                "receivedAt": case.receivedAt,
                "subject": case.subject,
                "body": case.body,
                "channel": case.channel,
            }
        )
        del entries[: -self._keep]

    def for_asset(self, asset_id: str | None, exclude_request_id: str = "") -> list[dict[str, Any]]:
        if not asset_id:
            return []
        return [
            e
            for e in self._by_asset.get(asset_id.upper(), [])
            if e.get("requestId") != exclude_request_id
        ]


# Process-wide, like the event-id store in main.py.
handled_requests = HandledRequests()

# Status -> the holding line sent to the customer. Proper replies come later; these are
# neutral placeholders that promise nothing and expose no internal detail
# (OPS-INTAKE-003: never imply a technician is confirmed).
PLACEHOLDER_DRAFTS: dict[str, str] = {
    "human_escalation_required": (
        "Thank you for contacting Northstar Field Services. We have passed your report to "
        "our duty owner for immediate attention and will be in touch shortly."
    ),
    "clarification_required": (
        "Thank you for contacting Northstar Field Services. We need a little more "
        "information before we can take the next step, and have set out what we need."
    ),
    "account_review_required": (
        "Thank you for contacting Northstar Field Services. We are checking the account "
        "record for this equipment and will come back to you once that is confirmed."
    ),
    "duplicate_detected": (
        "Thank you for contacting Northstar Field Services. This relates to a job we "
        "already have open, so we have added your message to it rather than raising a "
        "second one."
    ),
    "dispatch_ready": (
        "Thank you for contacting Northstar Field Services. We are arranging an "
        "attendance for this fault and will confirm the details shortly."
    ),
    "resource_escalation_required": (
        "Thank you for contacting Northstar Field Services. We are arranging a suitably "
        "qualified engineer for this fault and will confirm the details shortly."
    ),
    "covered_action": (
        "Thank you for contacting Northstar Field Services. We have the details of your "
        "request and are scheduling this against your service agreement."
    ),
    "failed": (
        "Thank you for contacting Northstar Field Services. A coordinator is reviewing "
        "your request and will come back to you shortly."
    ),
}

DEFAULT_DRAFT = (
    "Thank you for contacting Northstar Field Services. A coordinator is reviewing your "
    "request and will come back to you shortly."
)

# Status -> the action a coordinator should take, stated first in nextActions.
PRIMARY_ACTION: dict[str, tuple[str, str]] = {
    "human_escalation_required": (
        "escalate_to_duty_owner",
        "Route to the duty owner for immediate human review; no dispatch and no advice",
    ),
    "clarification_required": (
        "request_information",
        "Ask the customer for the information needed to decide the next step",
    ),
    "account_review_required": (
        "route_to_account_review",
        "Hold for account review; coverage is not confirmed from the agreement record",
    ),
    "duplicate_detected": (
        "link_to_existing_job",
        "Link this request to the existing job; do not raise a second work order",
    ),
    "dispatch_ready": (
        "dispatch_technician",
        "Attend the fault under the agreement's response commitment",
    ),
    "resource_escalation_required": (
        "escalate_to_dispatch_lead",
        "Resource decision for the dispatch lead; no placeholder work order",
    ),
    "covered_action": (
        "schedule_visit",
        "Schedule the covered visit; the work order is raised when it is booked",
    ),
    "failed": (
        "route_to_coordinator",
        "A required record could not be read; a coordinator must review this request",
    ),
}

# Intent -> the category reported on the case result.
CATEGORY_BY_INTENT: dict[str, str] = {
    "planned_service": "planned_service",
    "breakdown": "breakdown",
    "coverage_question": "coverage_enquiry",
    "follow_up": "follow_up",
    "other": "general_enquiry",
}


# Reasons that mean this escalation really is about a hazard. Not every human escalation
# is one: a request to review earlier work also goes to a human, and labelling that an
# immediate safety risk would put false alarms into anything keyed on safetyRisk.
SAFETY_REASON_CODES = frozenset(
    {
        "safety_signal_affirmed",
        "safety_answer_unresolved",
        "safety_signal_ambiguous",
        "safety_wording_unconfirmed",
        "safety_denial_unverified",
        "safety_unverified",
    }
)


def _classification(facts: RequestFacts, decision: Decision) -> Classification:
    """Derive the classification from the facts and the decision."""
    safety_risk = any(r.code in SAFETY_REASON_CODES for r in decision.reasons)

    if safety_risk and decision.status == "human_escalation_required":
        urgency = "immediate"
    elif decision.status == "human_escalation_required":
        urgency = "high"
    elif decision.status == "resource_escalation_required":
        urgency = "high"
    elif facts.intent == "breakdown":
        urgency = "high"
    elif facts.intent == "planned_service":
        urgency = "routine"
    else:
        urgency = "normal"

    # Confidence is about how firmly the facts supported the decision, not about how
    # likely it is to be right. Anything resting on an unread or unclear message is low.
    if facts.modelUnavailable:
        confidence = 0.2
    elif facts.safetySignal == "ambiguous":
        confidence = 0.4
    elif decision.status == "clarification_required":
        confidence = 0.5
    elif facts.intent == "other":
        confidence = 0.5
    else:
        confidence = 0.9

    category = CATEGORY_BY_INTENT.get(facts.intent, "general_enquiry")
    if safety_risk:
        category = "safety"

    return Classification(
        category=category,
        urgency=urgency,
        safetyRisk=safety_risk,
        confidence=confidence,
    )


def _entitlement(evidence: AssetEvidence, decision: Decision) -> Entitlement:
    """Report the coverage position and the records it rests on."""
    agreement = evidence.agreement
    evidence_refs: list[Any] = []

    if agreement:
        # A response commitment from an agreement that was not in force is not a
        # commitment. Reporting the number anyway invites a coordinator to quote a window
        # we never owed, so it is withheld rather than shown with a caveat.
        in_force = agreement.get("inForceAtReceivedAt")
        entry = {
            "type": "agreement",
            "id": agreement.get("contractRef"),
            "status": agreement.get("status"),
            "serviceMode": agreement.get("serviceMode"),
            "responseHours": agreement.get("responseHours") if in_force else None,
            "effectiveFrom": agreement.get("effectiveFrom"),
            "effectiveTo": agreement.get("effectiveTo"),
            "inForceAtReceivedAt": in_force,
        }
        if not in_force:
            entry["responseHoursWithheld"] = "Agreement was not in force at the request time"
        evidence_refs.append(entry)
        if agreement.get("supersedes"):
            evidence_refs.append(
                {"type": "supersedes", "id": agreement["supersedes"], "retrievable": False}
            )
    if evidence.asset:
        evidence_refs.append(
            {
                "type": "asset_coverage",
                "id": evidence.assetId,
                "coverage": evidence.asset.get("coverage"),
            }
        )

    if decision.status in {"covered_action", "dispatch_ready", "resource_escalation_required"}:
        status = "covered"
    elif decision.status == "account_review_required":
        status = "review_required"
    elif agreement is None:
        status = "not_established"
    else:
        status = "not_assessed"

    hours = agreement.get("responseHours") if agreement else None
    # Only state a response window where coverage is actually confirmed.
    sla = f"{hours}h response" if (hours and status == "covered") else "not_applicable"

    return Entitlement(status=status, sla=sla, evidence=evidence_refs)


def build_case_result(
    case: CaseInput,
    facts: RequestFacts,
    evidence: AssetEvidence,
    decision: Decision,
    trace_ids: list[str],
    extra_warnings: list[str] | None = None,
    booked_work_order: dict[str, Any] | None = None,
) -> CaseResult:
    """Assemble the structured first action."""
    # Lead with the action this status actually calls for, then the reasons behind it,
    # so a coordinator reads what to do before why.
    next_actions: list[NextAction] = []
    primary = PRIMARY_ACTION.get(decision.status)
    if decision.safetyQuestion:
        next_actions.append(
            NextAction(type="ask_safety_question", reason=decision.safetyQuestion)
        )
    elif primary:
        next_actions.append(NextAction(type=primary[0], reason=primary[1]))

    if booked_work_order and booked_work_order.get("id"):
        next_actions.append(
            NextAction(
                type="work_order_created",
                reason=f"Work order {booked_work_order['id']} was created for this request",
            )
        )

    next_actions.extend(
        NextAction(type=reason.code, reason=reason.detail) for reason in decision.reasons
    )
    if not next_actions:
        next_actions = [
            NextAction(type="route_to_coordinator", reason="No automated action was determined")
        ]

    source_references: list[Any] = [
        {"type": "request", "id": case.requestId, "channel": case.channel}
    ]
    source_references.extend(evidence.sourceReferences)
    source_references.extend(
        {"type": "policy", "id": reason.policy} for reason in decision.reasons
    )
    if decision.linkedWorkOrders:
        source_references.extend(
            {"type": "work_order", "id": wo} for wo in decision.linkedWorkOrders
        )
    if decision.linkedRequests:
        source_references.extend(
            {"type": "linked_request", "id": req} for req in decision.linkedRequests
        )
    if decision.otherOpenWorkOrders:
        # Context for a coordinator: other jobs open on the same equipment, which this
        # request was deliberately NOT linked to.
        source_references.extend(
            {"type": "other_open_work_order", "id": wo, "linked": False}
            for wo in decision.otherOpenWorkOrders
        )

    warnings = [w for w in (decision.warnings + (extra_warnings or [])) if w]
    if decision.safetyQuote:
        # POL-SAFETY-001: keep the customer's exact words, not a generic label.
        warnings.append(f'Reported wording: "{decision.safetyQuote}"')

    work_order = booked_work_order
    if work_order is None and decision.recommendedTechnician:
        # A recommendation, not a booking. Nothing has been created.
        work_order = {
            "created": False,
            "recommendedTechnicianId": decision.recommendedTechnician.technicianId,
            "assetId": evidence.assetId,
            "note": "Recommendation only; availability is re-checked before creation",
        }

    return CaseResult(
        caseId=case.requestId,
        status=decision.status,
        entities=Entities(
            customerId=(evidence.customer or {}).get("id"),
            siteId=(evidence.site or {}).get("id"),
            assetId=evidence.assetId if evidence.resolved else None,
        ),
        classification=_classification(facts, decision),
        entitlement=_entitlement(evidence, decision),
        nextActions=next_actions,
        customerResponseDraft=build_customer_reply(facts, evidence, decision, booked_work_order),
        missingInformation=decision.missingInformation,
        workOrder=work_order,
        # Extra, schema-permitted context for a coordinator (additionalProperties: true).
        alternativeTechnicians=[t.as_dict() for t in decision.alternativeTechnicians],
        relatedHistory=decision.relatedHistory,
        audit=Audit(
            sourceReferences=source_references,
            modelTraceIds=trace_ids,
            warnings=warnings,
        ),
    )


async def process_case(
    case: CaseInput,
    northstar: NorthstarClient,
    model: ModelClient,
    event_id: str | None = None,
    trace_log: TraceLog | None = None,
) -> CaseResult:
    """Run one request through the pipeline."""
    # Attachment summaries are part of the report, not metadata. They feed the same text
    # the keyword backstop scans and the safety quote is verified against, so a hazard
    # described only in a photo summary is caught even when the model misses it, and a
    # quote lifted from a summary still verifies (POL-SAFETY-001).
    attachment_text = " ".join(
        summary
        for a in case.attachments
        if isinstance(a, dict) and (summary := str(a.get("summary") or "").strip())
    )
    message_text = f"{case.subject}. {case.body}"
    text = f"{message_text} {attachment_text}".strip() if attachment_text else message_text

    # 1. Exact identifiers, found in code. Deliberately from the customer's own words
    # only: an attachment summary is written by whoever processed the file and may name
    # other equipment in passing, which must not redirect the whole case.
    equipment_ids = find_equipment_ids(message_text)
    asset_id = equipment_ids[0] if equipment_ids else None
    logger.info("request=%s equipment ids=%s", case.requestId, equipment_ids or "none")

    # 2. Evidence, so the model can be shown this asset's open jobs and earlier requests.
    evidence = await gather_asset_evidence(northstar, asset_id, case.receivedAt)

    related_requests = [
        r
        for r in evidence.recentRequests
        if asset_id
        and asset_id.upper() in f"{r.get('subject', '')} {r.get('body', '')}".upper()
        and str(r.get("requestId") or "") != case.requestId
    ]

    # Requests we handled ourselves for this equipment are not in Northstar's queue, so
    # without them a second report of the same fault looks like the first one.
    ours = handled_requests.for_asset(asset_id, case.requestId)
    seen = {str(r.get("requestId") or "") for r in related_requests}
    unseen = [r for r in ours if str(r.get("requestId") or "") not in seen]
    related_requests += unseen

    # The same requests go into the evidence, not just the prompt: the response-window
    # rule reads `recentRequests` to find the original report, and showing the model a
    # prior the timing check cannot see produced "same fault, but no window to judge it
    # against" — an escalation where the contract plainly had a window.
    if unseen:
        evidence = replace(
            evidence, recentRequests=list(evidence.recentRequests) + unseen
        )

    # 3. One model call.
    result = await model.extract_facts(
        subject=case.subject,
        body=case.body,
        open_work_orders=evidence.openWorkOrders,
        recent_requests=related_requests,
        attachments=case.attachments,
    )
    facts = result.facts

    # The text's own identifiers are authoritative over the model's reading of them.
    if equipment_ids:
        facts = replace_asset_mentions(facts, equipment_ids)
    elif facts.assetMentions:
        # The regex found nothing. The model may still have read an id the matcher does
        # not recognise — but only one that is genuinely in the message counts, so it
        # cannot invent an asset or pull one out of the records it was shown.
        cited = [m for m in facts.assetMentions if id_appears_in_text(m, message_text)]
        if cited:
            # The lookup above ran with no asset, so it has to be redone for the
            # recovered id — otherwise the case carries an asset with no customer,
            # site or agreement behind it.
            logger.info("recovered equipment id %s from the model; re-reading", cited)
            asset_id = cited[0]
            evidence = await gather_asset_evidence(northstar, asset_id, case.receivedAt)
            facts = replace_asset_mentions(facts, cited)
        else:
            logger.info("ignored uncited model equipment ids %s", facts.assetMentions)
            facts = replace_asset_mentions(facts, [])

    # A reference only counts if the customer actually wrote it. The model is shown this
    # asset's open jobs and earlier requests, and it was returning those ids as things
    # "the message points to" when the message pointed at nothing — which then read as
    # the customer citing a prior request. Same principle as verifying a safety quote
    # against the text: the model reports, the text decides.
    facts = keep_cited_references(facts, message_text)

    # 4. Decide.
    decision = decide(facts, evidence, text)
    logger.info("request=%s -> status=%s", case.requestId, decision.status)

    # 5. Act. The only write this service makes, and only for a dispatch-ready case.
    booked: dict[str, Any] | None = None
    booking_outcome: BookingOutcome | None = None
    extra_warnings = list(result.warnings or [])

    if decision.status == "dispatch_ready":
        outcome = booking_outcome = await book_work_order(
            northstar,
            decision,
            evidence,
            case.requestId,
            event_id,
            summary=_work_order_summary(facts, evidence),
        )
        extra_warnings.extend(outcome.warnings)

        if outcome.status:
            # The booking could not go ahead: the outcome's status replaces the decision's.
            logger.info(
                "request=%s dispatch not completed -> %s", case.requestId, outcome.status
            )
            decision = replace(
                decision,
                status=outcome.status,
                reasons=decision.reasons
                + [Reason(code, detail, policy) for code, detail, policy in outcome.reasons],
                recommendedTechnician=(
                    outcome.technician if outcome.status != "failed" else None
                ),
            )
        else:
            booked = outcome.workOrder
            if outcome.technician:
                decision = replace(decision, recommendedTechnician=outcome.technician)
            logger.info(
                "request=%s work order=%s duplicate=%s",
                case.requestId,
                (booked or {}).get("id"),
                outcome.duplicate,
            )

    # 6. Respond. The trace only records what the steps above already decided.
    case_result = build_case_result(
        case, facts, evidence, decision, result.traceIds, extra_warnings, booked
    )

    trace = build_trace(
        case, evidence, facts, decision, result.traceIds, booking_outcome, booked
    )
    case_result.decisionTrace = trace
    if trace_log is not None:
        # Best effort: a logging failure must not change the response.
        trace_log.record(trace)

    # Remember this request so the next one for the same equipment can be compared
    # against it. Recorded whatever the outcome: a repeat of a request we held for
    # clarification is still a repeat.
    handled_requests.record(asset_id, case)

    return case_result


def keep_cited_references(facts: RequestFacts, message_text: str) -> RequestFacts:
    """Drop referenced ids the customer never wrote.

    `referencedRequests` is evidence that the sender pointed at an earlier request, and
    `_same_fault` treats it as a fact rather than a judgement. That only holds if the id
    is really in their message: the model also sees the records we show it, and ids
    recalled from those are not citations.

    Matching is tolerant of spacing and case ("req 8268", "wo-9294"), because the test is
    whether the customer referred to it, not how neatly they typed it.
    """
    if not facts.referencedRequests:
        return facts

    cited = {r.upper().replace(" ", "").replace("_", "-") for r in find_request_references(message_text)}
    normalised_text = re.sub(r"[\s_]+", "-", message_text.upper())

    kept = [
        r
        for r in facts.referencedRequests
        if r.upper() in cited or r.upper().replace(" ", "") in normalised_text
    ]
    dropped = [r for r in facts.referencedRequests if r not in kept]
    if dropped:
        logger.info("dropped uncited references %s", dropped)
    return replace(facts, referencedRequests=kept)


def _work_order_summary(facts: RequestFacts, evidence: AssetEvidence) -> str:
    """One line naming the fault, matching the style of Northstar's own work orders.

    Theirs read "Freezer plant 2 compressor cycling" — the equipment's nickname and the
    symptom. Built from the same short phrase the customer reply uses, so the job and the
    acknowledgement describe the same thing.
    """
    symptom = (facts.symptomSummary or "").strip().rstrip(".")
    nickname = str((evidence.asset or {}).get("nickname") or "").strip()
    parts = [p for p in (nickname, symptom) if p]
    if not parts:
        return ""
    summary = " ".join(parts)
    return summary[0].upper() + summary[1:]


def replace_asset_mentions(facts: RequestFacts, equipment_ids: list[str]) -> RequestFacts:
    """Trust the regex over the model for exact identifiers."""
    return replace(facts, assetMentions=equipment_ids)
