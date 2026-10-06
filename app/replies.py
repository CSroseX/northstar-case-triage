"""Customer reply drafts.

Every reply has the same three-line shape, so a customer can read it at a glance and a
coordinator can scan a queue of them:

    We've received your request.
    Issue: <short description of the problem>
    Next step: <the line for this status>

The issue line comes from the model's short phrase for the problem, with the equipment
appended by us — the model is told not to repeat the equipment name, which is what made
the earlier drafts read clumsily. The next-step line is fixed per status and filled from
the records: the response window and work-order number for a dispatch, the actual question
when we are asking for something, the existing reference for a duplicate.

Rules taken from the policies:
- Safety escalation says to keep people away and that it is with the duty team. No
  diagnosis, no troubleshooting, no advice of any kind (POL-SAFETY-001).
- A safety clarification carries one plain question and nothing else.
- Other clarifications ask only for what changes the next action (OPS-INTAKE-003).
- Account review acknowledges the problem and says the account record is being checked,
  without promising attendance (POL-CONTRACT-002).
- Planned work names the service and says it is being scheduled under the contract, with
  no date (our D2: no scheduling lead-time data).
- A duplicate says it is linked to the existing request, with the reference.
- Dispatch states the response window, and only claims a technician once the work order
  actually exists (OPS-INTAKE-003: never imply a technician is confirmed before the
  work-order response has been reconciled).
- Resource escalation acknowledges without promising a time.
- Failure says a coordinator is reviewing it, with no raw error text.

Nothing here exposes internal notes, confidence scores, policy ids or system errors.
"""

from __future__ import annotations

import re
from typing import Any

from .decide import Decision
from .evidence import AssetEvidence
from .facts import RequestFacts

OPENING = "We've received your request."

# A reference a customer would recognise on their paperwork: WO-9290, REQ-8248.
# Internal identifiers such as database UUIDs are never shown to them.
_CUSTOMER_REFERENCE = re.compile(r"^(?:WO|REQ)-[A-Z0-9][A-Z0-9-]{0,19}$", re.IGNORECASE)


def _is_customer_reference(reference: str) -> bool:
    return bool(_CUSTOMER_REFERENCE.match(str(reference or "").strip()))


def _equipment_phrase(evidence: AssetEvidence) -> str:
    """How to name the equipment to the customer: nickname and id where we have them."""
    if not evidence.asset:
        return ""
    nickname = evidence.asset.get("nickname")
    asset_id = evidence.assetId
    if nickname and asset_id:
        return f"{nickname} ({asset_id})"
    return str(nickname or asset_id or "")


def _issue_line(facts: RequestFacts, evidence: AssetEvidence) -> str:
    """"Issue: <problem> - <equipment> - <site>", from whichever parts we have.

    A plain hyphen, not an em dash: the draft is read in terminals and pasted into mail
    clients whose encoding we do not control, and an em dash renders as mojibake there.
    """
    summary = (facts.symptomSummary or "").strip()
    # Defensive: the model is asked for a short phrase, but a stray sentence should not
    # produce a mangled line.
    summary = summary.split(";")[0].strip().rstrip(".").strip()
    if summary:
        first = summary.split()[0]
        if not (first.isupper() or any(ch.isdigit() for ch in first)):
            summary = summary[0].lower() + summary[1:]

    equipment = _equipment_phrase(evidence)
    site = (evidence.site or {}).get("name")
    place = " - ".join(part for part in [equipment, site] if part)

    if summary and place:
        return f"Issue: {summary} - {place}"
    if summary:
        return f"Issue: {summary}"
    if place:
        return f"Issue: reported on {place}"
    return "Issue: as described in your message"


def _response_window(evidence: AssetEvidence) -> str | None:
    hours = (evidence.agreement or {}).get("responseHours")
    return f"{hours} hours" if hours else None


def _next_step(
    facts: RequestFacts,
    evidence: AssetEvidence,
    decision: Decision,
    work_order: dict[str, Any] | None,
) -> str:
    """The one line that says what happens now, per status."""
    status = decision.status
    equipment = _equipment_phrase(evidence) or "the equipment"

    # --- safety: no advice, no diagnosis, no troubleshooting ------------
    if status == "human_escalation_required":
        if any(r.code == "refers_to_previous_work" for r in decision.reasons):
            return (
                "A coordinator is reviewing the previous visits to this equipment before we "
                "arrange anything further, so we build on that work rather than repeating it. "
                "We will come back to you shortly."
            )
        if any(
            r.code in {"follow_up_after_response_window", "follow_up_window_unknown"}
            for r in decision.reasons
        ):
            references = [r for r in decision.linkedRequests if _is_customer_reference(r)]
            reference_text = f" ({references[0]})" if references else ""
            return (
                f"A coordinator is reviewing this against your earlier request{reference_text} "
                "and will come back to you shortly."
            )
        # A hazard, or an unresolved answer to our safety question.
        return (
            "Please keep people away from the equipment and do not attempt to operate or "
            "investigate it. This has been escalated to our duty team, who will contact you "
            "directly."
        )

    # --- clarification: the one safety question, or only what we need ---
    if status == "clarification_required":
        if decision.safetyQuestion:
            # The question carries its own lead-in; adding another repeats it.
            return (
                f"{decision.safetyQuestion} Nothing will progress until we hear back from you."
            )
        if any(r.code == "not_a_service_request" for r in decision.reasons):
            # Nothing to triage yet: ask what they need, not for an equipment id.
            return (
                "Please tell us which equipment or service you need help with and a "
                "coordinator will get back to you."
            )
        needed = decision.missingInformation
        ask = needed[0] if needed else "a little more detail about the equipment involved"
        return (
            f"Could you confirm {ask.rstrip('.').lower()}? As soon as we have that we will "
            "take this forward."
        )

    # --- account review: acknowledge, no promise of attendance ----------
    if status == "account_review_required":
        return (
            "We are checking the service agreement record for this equipment before we "
            "confirm what happens next. We will come back to you as soon as that is complete."
        )

    # --- duplicate: linked to the existing request ----------------------
    if status == "duplicate_detected":
        # Only what this repeat actually matched. Other open jobs on the same equipment
        # are a different fault, and citing one would tell the customer we have linked
        # them to the wrong work. The request reference is preferred: it is the one the
        # customer raised and recognises, and created work orders carry only a UUID.
        references = [
            ref
            for ref in decision.linkedRequests + decision.linkedWorkOrders
            if _is_customer_reference(ref)
        ]
        reference_text = f" (reference {references[0]})" if references else ""
        return (
            f"This matches a request we already have open for this equipment{reference_text}, "
            "so we have linked your message to it rather than raising a second job. The team "
            "handling it will keep you updated."
        )

    # --- dispatch: only claim a technician if the job really exists -----
    if status == "dispatch_ready":
        window = _response_window(evidence)
        if work_order and work_order.get("id"):
            # Work orders created through this API come back with a UUID id and no WO-
            # reference, which means nothing to a customer. Their own request id is the
            # reference they already hold, so quote that instead; if a WO- id ever does
            # come back, prefer it.
            candidates = [work_order.get("id"), work_order.get("requestId")]
            reference = next(
                (str(c) for c in candidates if c and _is_customer_reference(str(c))), ""
            )
            job_text = f", and your reference for this visit is {reference}" if reference else ""
            within = f" We will be with you within {window}." if window else ""
            return (
                f"An engineer has been assigned to attend{job_text}.{within} "
                "We will confirm the visit details shortly."
            )
        within = f" Your agreement provides a response within {window}." if window else ""
        return (
            f"We are arranging an engineer to attend.{within} We will confirm the details "
            "as soon as the visit is booked."
        )

    # --- resource escalation: acknowledge, no time promise --------------
    if status == "resource_escalation_required":
        return (
            "This needs an engineer with specific qualifications for your equipment, and our "
            "dispatch team is arranging that now. We will contact you with the arrangements "
            "once they are confirmed."
        )

    # --- covered planned work: named, scheduled, no date ----------------
    if status == "covered_action":
        contract = (evidence.agreement or {}).get("contractRef")
        contract_text = f" under agreement {contract}" if contract else " under your agreement"
        # Remote-only support is covered, but no visit is being arranged — promising one
        # would contradict the agreement and, in the case this came from, the customer's
        # own request not to send anyone.
        if any(r.code == "remote_support_covered" for r in decision.reasons):
            return (
                f"This is covered{contract_text}, which provides remote support for this "
                "equipment. One of our engineers will contact you to work through it with "
                "you over the phone."
            )
        if facts.intent == "coverage_question":
            window = _response_window(evidence)
            carries = f" It carries a response commitment of {window}." if window else ""
            return (
                f"We can confirm {equipment} is covered{contract_text}.{carries} Please let "
                "us know if you would like us to arrange a visit."
            )
        return (
            f"This is covered{contract_text} and we are scheduling the visit now. We will "
            "come back to you shortly to agree a date and time."
        )

    # --- failure: a coordinator has it, no raw errors -------------------
    if status == "failed":
        return (
            "A coordinator is reviewing this personally and will come back to you shortly."
        )

    return "A coordinator is reviewing your request and will come back to you shortly."


def build_customer_reply(
    facts: RequestFacts,
    evidence: AssetEvidence,
    decision: Decision,
    work_order: dict[str, Any] | None = None,
) -> str:
    """One reply, in the customer's terms, for the status we reached."""
    lines = [OPENING]

    # A message that described no issue gets no Issue line: inventing one ("as described
    # in your message") says nothing and reads oddly when nothing was described.
    if not any(r.code == "not_a_service_request" for r in decision.reasons):
        lines.append(_issue_line(facts, evidence))

    lines.append(f"Next step: {_next_step(facts, evidence, decision, work_order)}")

    # POL-SAFETY-001: a safety escalation keeps the customer's exact words on the record.
    if decision.status == "human_escalation_required" and decision.safetyQuote:
        quote = decision.safetyQuote.strip().rstrip(".")
        if not any(r.code == "refers_to_previous_work" for r in decision.reasons):
            lines.insert(2, f'You told us: "{quote}."')

    return "\n".join(lines)
