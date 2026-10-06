"""The decision: request + facts + evidence -> status, reasons, recommendation.

No model calls and no I/O. Every rule traces to a policy id so a coordinator can see
which document drove the outcome.

Check order, first match wins:
  safety -> lookup failure -> identity -> duplicate -> coverage -> planned -> breakdown
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .evidence import AssetEvidence, TechnicianMatch
from .facts import RequestFacts

# Hazard vocabulary from POL-SAFETY-001, which lists exactly four triggers: a suspected
# fuel or gas leak, smoke, water near electrical equipment, and people feeling dizzy or
# unwell in an equipment room. This is a backstop over the model's own reading, never a
# replacement for it.
#
# A bare commodity noun is NOT a hazard: "diesel generator service", "the gas plant" and
# "water pump maintenance" are ordinary bookings. So fuel and water only fire in the
# hazardous *combination* the policy describes, within a short proximity window and in
# either order. Smoke/fire and a person unwell fire on their own, as the policy lists
# them unconditionally.
#
# This backstop only runs when the model reported safetySignal == "absent". A customer
# ruling a hazard out ("there is no smoke") comes back as "denied", which never reaches
# here — so there is deliberately no negation handling: it suppressed real hazards such
# as "the alarm is not working and there's smoke".

# Up to ~40 characters of filler between the two halves of a combination.
_GAP = r"[^.!?;]{0,40}?"

_FUEL = r"(?:diesel|fuel|gas|petrol|lpg)"
_LEAK = r"(?:leak(?:ing|age|s)?|spill(?:ed|age|ing)?|smell(?:s|ing)?|odou?r|fumes?)"

_WATER = r"(?:water|wet|flood(?:ed|ing)?|damp)"
_ELECTRICAL = r"(?:electrical|electrics|panel|ups|switchgear|wiring|wires?|socket|outlet|busbar|transformer|distribution board|db)"
_POOLING = r"(?:pool(?:s|ed|ing)?|collect(?:s|ed|ing)?|leak(?:s|ed|ing|age)?|drip(?:s|ped|ping)?|spill(?:ed|age|ing)?|on the floor|patch)"

HAZARD_PATTERNS: tuple[tuple[str, str], ...] = (
    # Smoke/fire: listed unconditionally by POL-SAFETY-001.
    (r"\b(?:smoke|smoking|burning|burnt|fire|sparks?)\b", "smoke or fire"),
    # Fuel or gas only when a leak/spill/smell/fumes word is nearby, either order.
    (
        rf"\b{_FUEL}\b{_GAP}\b{_LEAK}\b|\b{_LEAK}\b{_GAP}\b{_FUEL}\b",
        "fuel or gas leak",
    ),
    # Water only near electrical context, or described as pooling/leaking.
    (
        rf"\b{_WATER}\b{_GAP}\b{_ELECTRICAL}\b|\b{_ELECTRICAL}\b{_GAP}\b{_WATER}\b",
        "water near electrical equipment",
    ),
    (
        rf"\b{_WATER}\b{_GAP}\b{_POOLING}|\b{_POOLING}{_GAP}\b{_WATER}\b",
        "water pooling or leaking",
    ),
    # A person unwell: listed unconditionally by POL-SAFETY-001.
    (
        r"\b(?:dizzy|light[- ]?headed|lightheaded|faint(?:ing)?|nause\w*|unwell|collaps\w*)\b",
        "person unwell",
    ),
    # DELIBERATE EXTENSION BEYOND POL-SAFETY-001 — Chitransh's decision, not the policy's.
    # Electric shock, electrocution and exposed or live wiring are not among the policy's
    # four listed triggers, but they are immediate hazards to whoever is next to the
    # equipment and routing them to a human is the same cautious call the policy makes
    # everywhere else. Flag this in SOLUTION.md as an addition, and revisit with Meera.
    (
        r"\b(?:electrocut\w*"
        r"|electric(?:al)? shocks?"
        r"|got (?:an? )?shock"
        r"|exposed (?:wir\w*|cabl\w*|conductor\w*|terminal\w*)"
        r"|live (?:wir\w*|cabl\w*|conductor\w*|terminal\w*))\b",
        "electric shock risk",
    ),
)

ONSITE_REQUEST_PATTERN = re.compile(
    r"\b(?:on[- ]?site|onsite|someone (?:on site|out|to attend|to come)|send (?:someone|a )?"
    r"(?:technician|engineer)?|engineer|technician|visit|attend|dispatch|call[- ]?out)\b",
    re.IGNORECASE,
)


@dataclass
class Reason:
    """One rule that fired, and the document it comes from."""

    code: str
    detail: str
    policy: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "policy": self.policy}


@dataclass
class Decision:
    status: str
    reasons: list[Reason] = field(default_factory=list)
    recommendedTechnician: TechnicianMatch | None = None
    missingInformation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Carried through for the response layer: the exact words that drove an escalation,
    # and any earlier request/WO this one should be linked to.
    safetyQuote: str = ""
    linkedWorkOrders: list[str] = field(default_factory=list)
    linkedRequests: list[str] = field(default_factory=list)
    # One plain question, set only for a safety clarification.
    safetyQuestion: str = ""
    # Past jobs on this equipment, attached when the request asks about earlier work.
    relatedHistory: list[dict[str, Any]] = field(default_factory=list)
    # Qualified, available, same-city technicians other than the one recommended.
    alternativeTechnicians: list[TechnicianMatch] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": [r.as_dict() for r in self.reasons],
            "recommendedTechnician": (
                self.recommendedTechnician.as_dict() if self.recommendedTechnician else None
            ),
            "missingInformation": self.missingInformation,
            "warnings": self.warnings,
            "safetyQuote": self.safetyQuote,
            "linkedWorkOrders": self.linkedWorkOrders,
            "linkedRequests": self.linkedRequests,
            "safetyQuestion": self.safetyQuestion,
            "relatedHistory": self.relatedHistory,
            "alternativeTechnicians": [t.as_dict() for t in self.alternativeTechnicians],
        }


def find_hazard_words(text: str) -> list[str]:
    """Hazard wording present. The backstop over the model's reading.

    No negation handling: this only runs when the model said safetySignal == "absent",
    and a customer ruling a hazard out comes back as "denied" instead.
    """
    found: list[str] = []
    for pattern, label in HAZARD_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE) and label not in found:
            found.append(label)
    return found


def _normalise_quote(text: str) -> str:
    """Lowercase, collapse whitespace and drop punctuation, for quote comparison.

    Tolerates the small differences between what the model echoes back and the body
    text (curly quotes, a trailing full stop, line wrapping) without being so loose
    that unrelated words would match.
    """
    lowered = text.lower().replace("’", "'").replace("‘", "'")
    lowered = lowered.replace("“", '"').replace("”", '"')
    # Keep letters, digits and apostrophes; everything else becomes a separator.
    return " ".join(re.findall(r"[a-z0-9']+", lowered))


def quote_is_in_text(quote: str, text: str) -> bool:
    """Does the model's safety quote actually appear in the request text?

    The model must not be able to clear a hazard with words the customer never wrote
    (POL-SAFETY-001: preserve the customer's original wording).
    """
    normalised_quote = _normalise_quote(quote)
    if not normalised_quote:
        return False
    return normalised_quote in _normalise_quote(text)


def _wants_onsite(facts: RequestFacts, text: str) -> bool:
    """Did the customer ask for someone to attend? Explicit fact wins over the text."""
    if facts.requiresOnsite is not None:
        return facts.requiresOnsite
    if facts.intent == "breakdown":
        return True
    return bool(ONSITE_REQUEST_PATTERN.search(text))


# How close in time another request for the same asset has to be before a follow-up that
# names no reference is taken to be about it. Rohan's example was four minutes; a window of
# a few hours covers a shift without reaching back to unrelated work.
def _parse_timestamp(value: str) -> datetime | None:
    """ISO timestamp from a record, or None when it cannot be read."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class PriorRequest:
    """An earlier request for the same asset, and how long before this one it arrived."""

    requestId: str
    receivedAt: str
    gapHours: float


def _earlier_request_for_asset(evidence: AssetEvidence) -> PriorRequest | None:
    """The EARLIEST related request naming this asset, if there is one.

    The window is measured from the original report, not from the last chase-up. Taking
    the nearest earlier request would restart the clock on every follow-up, so a customer
    chasing every couple of hours would stay inside the window forever and never reach a
    human — the opposite of what the escalation is for.
    """
    if not evidence.assetId or not evidence.receivedAt:
        return None

    received = _parse_timestamp(evidence.receivedAt)
    if received is None:
        return None

    asset = evidence.assetId.upper()
    earliest: PriorRequest | None = None
    for prior in evidence.recentRequests:
        text = f"{prior.get('subject', '')} {prior.get('body', '')}".upper()
        if asset not in text:
            continue
        prior_time = _parse_timestamp(str(prior.get("receivedAt") or ""))
        if prior_time is None:
            continue
        gap_hours = (received - prior_time).total_seconds() / 3600
        if gap_hours <= 0:
            continue
        # Largest gap = earliest request = the start of this conversation.
        if earliest is None or gap_hours > earliest.gapHours:
            earliest = PriorRequest(
                requestId=str(prior.get("requestId") or prior.get("id") or ""),
                receivedAt=str(prior.get("receivedAt") or ""),
                gapHours=gap_hours,
            )
    return earliest


def _response_window_hours(evidence: AssetEvidence) -> float | None:
    """The contract's response commitment, or None when there is nothing to check against."""
    hours = (evidence.agreement or {}).get("responseHours")
    try:
        return float(hours) if hours is not None else None
    except (TypeError, ValueError):
        return None


def _same_fault(facts: RequestFacts, evidence: AssetEvidence) -> bool | None:
    """Same fault as an open job, a different one, or unclear?

    True  - the message points at an existing request/WO, or the model read it as the
            same fault.
    False - an open job exists but this is plainly a different fault.
    None  - cannot tell. A human decides (CLAUDE.md rule 7).

    Id-based evidence is checked first: a referenced request or work order id is a fact,
    not a judgement. Beyond that the model's own reading decides, because comparing two
    descriptions of a fault is a reading task, not something code should guess at.
    """
    if not evidence.openWorkOrders and not facts.referencedRequests:
        return False

    # A request that reports no fault cannot be a repeat of one. A coverage question or a
    # planned-service booking is a different kind of message from a breakdown report, and
    # an open job on the same asset does not make it a duplicate (CLAUDE.md rule 7:
    # duplicates turn on the fault). Without this, every administrative request about an
    # asset with an open job would be held for a duplicate check that cannot apply.
    if facts.intent in {"coverage_question", "planned_service"} and not facts.referencedRequests:
        return False

    referenced = {r.upper() for r in facts.referencedRequests}
    if referenced:
        known = {
            str(w.get("id", "")).upper() for w in evidence.openWorkOrders
        } | {str(w.get("requestId", "")).upper() for w in evidence.openWorkOrders}
        if referenced & known:
            return True

        # An earlier request for this same asset that produced no work order is still the
        # same incident: two channels reporting one job (OPS-INTAKE-003). This is the
        # email-plus-portal case, and it must not become a second work order.
        for prior in evidence.recentRequests:
            prior_id = str(prior.get("requestId") or prior.get("id") or "").upper()
            if prior_id and prior_id in referenced:
                prior_text = f"{prior.get('subject', '')} {prior.get('body', '')}"
                if evidence.assetId and evidence.assetId.upper() in prior_text.upper():
                    return True
                # Referenced by id, same asset context unconfirmed.
                return None

        # They referenced something we cannot see at all.
        if facts.intent == "follow_up":
            return None

    # The model's own reading of whether this repeats an open fault. Accessed
    # defensively so this works whether or not the field is present on the facts.
    assessment = str(getattr(facts, "sameFaultAsExisting", "") or "").strip().lower()
    if assessment == "same":
        return True
    if assessment == "different":
        return False
    if assessment == "unclear":
        # Left to the timing check at the call site: an earlier request for this asset
        # inside the contract's response window is one incident reported twice.
        return None

    # Not assessed, or an unrecognised value. Nothing open and nothing referenced is
    # plainly not a duplicate; anything else is a human's call, never a guess.
    if not evidence.openWorkOrders and not facts.referencedRequests:
        return False
    return None


def _coverage_problem(evidence: AssetEvidence, wants_onsite: bool) -> Reason | None:
    """The first coverage fault that requires account review, or None if covered."""
    agreement = evidence.agreement
    if agreement is None:
        return Reason(
            "agreement_missing",
            "No agreement record could be resolved for this asset",
            "POL-CONTRACT-002",
        )

    if agreement.get("customerMatchesAsset") is False:
        return Reason(
            "agreement_customer_mismatch",
            f"Agreement {agreement.get('contractRef')} belongs to "
            f"{agreement.get('customerId')}, not the asset's customer",
            "POL-CONTRACT-002",
        )

    status = str(agreement.get("status") or "").lower()
    if status in {"suspended", "expired", "cancelled", "terminated"}:
        return Reason(
            "agreement_not_active",
            f"Agreement {agreement.get('contractRef')} is {status}",
            "POL-CONTRACT-002",
        )

    if str(evidence.asset.get("coverage") or "").lower() == "suspended":
        return Reason(
            "asset_coverage_suspended",
            f"Asset {evidence.assetId} has suspended coverage",
            "POL-CONTRACT-002",
        )

    if agreement.get("receivedBeforeStart"):
        return Reason(
            "received_before_agreement_start",
            f"Request predates agreement {agreement.get('contractRef')} "
            f"(from {agreement.get('effectiveFrom')})",
            "POL-CONTRACT-002",
        )

    in_force = agreement.get("inForceAtReceivedAt")
    if in_force is False:
        return Reason(
            "agreement_not_in_force",
            f"Agreement {agreement.get('contractRef')} was not in force at the request time",
            "POL-CONTRACT-002",
        )
    if in_force is None:
        # Cannot establish is not the same as covered.
        return Reason(
            "agreement_window_unknown",
            f"Could not establish whether agreement {agreement.get('contractRef')} "
            "was in force at the request time",
            "POL-CONTRACT-002",
        )

    # Remote-only conflicts only when the request actually needs someone on site.
    mode = str(agreement.get("serviceMode") or "").lower()
    asset_coverage = str(evidence.asset.get("coverage") or "").lower()
    if wants_onsite and (mode == "remote_only" or asset_coverage == "remote_only"):
        return Reason(
            "remote_only_cannot_dispatch",
            f"Agreement {agreement.get('contractRef')} is remote-only and does not "
            "authorise an on-site visit",
            "POL-CONTRACT-002",
        )

    return None


def decide(
    facts: RequestFacts,
    evidence: AssetEvidence,
    request_text: str = "",
) -> Decision:
    """Pick the first action. First matching check wins."""

    # --- 1. safety ------------------------------------------------------
    # Runs before everything, including lookup failures: an escalation must never wait
    # on a record (POL-SAFETY-001).
    hazard_words = find_hazard_words(request_text)
    signal = facts.safetySignal

    if signal == "affirmed":
        return Decision(
            status="human_escalation_required",
            reasons=[
                Reason(
                    "safety_signal_affirmed",
                    "The report describes a possible immediate hazard",
                    "POL-SAFETY-001",
                )
            ],
            safetyQuote=facts.safetyQuote or request_text.strip()[:280],
            warnings=["Safety escalation: no troubleshooting advice and no automatic dispatch"],
        )

    # An answer to our own safety question that still leaves the hazard open stays with a
    # human — we never ask the same question twice (Meera: "not sure" never drifts back to
    # normal triage). A hazard word in the reply is a fresh credible signal and has already
    # been handled by the "affirmed" branch above.
    if facts.isSafetyAnswer and facts.safetyAnswerUncertain is not False:
        unresolved = (
            "The reply does not resolve whether there is a hazard"
            if facts.safetyAnswerUncertain
            else "The reply to the safety question could not be confirmed as resolving the hazard"
        )
        return Decision(
            status="human_escalation_required",
            reasons=[
                Reason(
                    "safety_answer_unresolved",
                    f"{unresolved}; the case stays with the duty owner rather than "
                    "returning to normal triage",
                    "POL-SAFETY-001",
                )
            ],
            safetyQuote=facts.safetyQuote or request_text.strip()[:280],
            warnings=[
                "Safety question answered without resolving the hazard; only the duty owner "
                "may release this case"
            ],
        )

    # The model cannot be the reason a hazard is missed (CLAUDE.md rule 2). Unsignalled
    # hazard wording, or an unusable model result, is treated as ambiguous — but an
    # explicit denial by the customer is theirs to make, not ours to override. The denial
    # has to be the customer's, though: it only stands when the quote is really in the
    # text, so the model cannot clear a hazard with words nobody wrote.
    escalate_as_ambiguous = signal == "ambiguous"
    ambiguity_reason: Reason | None = None

    if facts.modelUnavailable:
        # Checked first: an unusable model result is the more precise explanation, and
        # parse_facts() already forces the signal to "ambiguous" in that case.
        escalate_as_ambiguous = True
        ambiguity_reason = Reason(
            "safety_unverified",
            "The message could not be read reliably, so a hazard cannot be ruled out",
            "POL-SAFETY-001",
        )
    elif signal == "ambiguous":
        ambiguity_reason = Reason(
            "safety_signal_ambiguous",
            "The description may indicate a hazard and must be clarified before anything "
            "progresses",
            "POL-SAFETY-001",
        )
    elif signal == "denied" and not quote_is_in_text(facts.safetyQuote, request_text):
        # A denial with no verifiable wording behind it is not the customer's denial.
        escalate_as_ambiguous = True
        detail = (
            "The hazard was reported as ruled out, but the quoted wording "
            f'("{facts.safetyQuote}") does not appear in the request'
            if facts.safetyQuote
            else "The hazard was reported as ruled out, but no quoted wording was given"
        )
        ambiguity_reason = Reason(
            "safety_denial_unverified",
            f"{detail}; the denial cannot be confirmed and is treated as ambiguous",
            "POL-SAFETY-001",
        )
    elif signal == "absent" and hazard_words:
        escalate_as_ambiguous = True
        ambiguity_reason = Reason(
            "safety_wording_unconfirmed",
            f"Hazard wording present ({', '.join(hazard_words)}) but not reported by the "
            "extraction; treated as ambiguous",
            "POL-SAFETY-001",
        )

    if escalate_as_ambiguous and ambiguity_reason is not None:
        return Decision(
            status="clarification_required",
            reasons=[ambiguity_reason],
            safetyQuote=facts.safetyQuote,
            safetyQuestion=(
                "Before we go further: is there any smoke, burning smell, fuel or gas smell, "
                "or water near the equipment, and is anyone feeling unwell near it?"
            ),
            missingInformation=["Confirmation of whether there is an immediate hazard at the site"],
            warnings=["Nothing progresses until the safety question is answered"],
        )

    # --- 2. lookup failure ---------------------------------------------
    if evidence.outcome == "lookup_failed" or evidence.failedLookups:
        failed = ", ".join(evidence.failedLookups) or "northstar"
        return Decision(
            status="failed",
            reasons=[
                Reason(
                    "lookup_failed",
                    f"Could not read required records ({failed}) after bounded retries",
                    "SYS-CATALOG-001",
                )
            ],
            missingInformation=[f"Northstar {failed} lookup"],
            warnings=[
                f"Dependency lookup failed ({failed}); a coordinator must review this request"
            ],
        )

    # --- 3. identity ----------------------------------------------------
    if evidence.outcome == "no_asset_id":
        return Decision(
            status="clarification_required",
            reasons=[
                Reason(
                    "asset_not_identified",
                    "The request does not identify a specific asset",
                    "OPS-INTAKE-003",
                )
            ],
            missingInformation=[
                "Asset identifier, or the equipment name and location as labelled on site"
            ],
        )

    if evidence.outcome == "unknown_asset":
        return Decision(
            status="account_review_required",
            reasons=[
                Reason(
                    "asset_not_on_record",
                    f"Asset {evidence.assetId} does not appear in the customer records",
                    "POL-CONTRACT-002",
                )
            ],
            missingInformation=[
                f"Whether {evidence.assetId} is a covered asset on the account"
            ],
        )

    if len(facts.assetMentions) > 1:
        # Two candidates and no basis to choose: ask, don't pick (CLAUDE.md rule 6).
        return Decision(
            status="clarification_required",
            reasons=[
                Reason(
                    "ambiguous_asset_reference",
                    f"The request mentions more than one asset ({', '.join(facts.assetMentions)})",
                    "OPS-INTAKE-003",
                )
            ],
            missingInformation=["Which asset this request refers to"],
        )

    # --- 4. duplicate ---------------------------------------------------
    # A repeat is judged against the contract's own response window rather than an
    # invented interval: a follow-up that arrives while we are still inside the window we
    # promised is the same incident chased up, so it is linked. One that arrives after the
    # window has passed is a different conversation and belongs with a coordinator — we
    # record only that it came in late, never that a promise was missed, because the
    # records here cannot show whether anyone attended.
    same_fault = _same_fault(facts, evidence)
    linked_wos = [str(w.get("id")) for w in evidence.openWorkOrders if w.get("id")]
    prior = _earlier_request_for_asset(evidence)
    window_hours = _response_window_hours(evidence)

    if same_fault is not False:
        within_window = (
            prior is not None
            and window_hours is not None
            and prior.gapHours <= window_hours
        )

        if same_fault is True and within_window:
            linked_reqs = sorted(
                {
                    str(w.get("requestId"))
                    for w in evidence.openWorkOrders
                    if w.get("requestId")
                }
                | set(facts.referencedRequests)
                | ({prior.requestId} if prior and prior.requestId else set())
            )
            return Decision(
                status="duplicate_detected",
                reasons=[
                    Reason(
                        "same_fault_already_open",
                        "This reports a fault already raised for this equipment, within the "
                        f"{window_hours:g}-hour response window of the earlier request; it is "
                        "linked to that request rather than opening a second one",
                        "OPS-INTAKE-003",
                    )
                ],
                linkedWorkOrders=linked_wos,
                linkedRequests=linked_reqs,
                warnings=["Linked to the existing job; no second work order created"],
            )

        if same_fault is True and not within_window:
            # Same fault, but outside the window (or no window to check against).
            if prior is not None and window_hours is not None:
                detail = (
                    f"This repeats a request raised {prior.gapHours:.1f} hours ago, after the "
                    f"{window_hours:g}-hour response window for this agreement had passed; a "
                    "coordinator decides how to handle it"
                )
                code = "follow_up_after_response_window"
            else:
                detail = (
                    "This repeats an earlier request, but there is no response window on "
                    "the record to judge the timing against; a coordinator decides"
                )
                code = "follow_up_window_unknown"
            return Decision(
                status="human_escalation_required",
                reasons=[Reason(code, detail, "OPS-INTAKE-003")],
                linkedWorkOrders=linked_wos,
                linkedRequests=sorted(
                    set(facts.referencedRequests)
                    | ({prior.requestId} if prior and prior.requestId else set())
                ),
                missingInformation=["Coordinator decision on the repeated request"],
                warnings=[
                    "Follow-up received after the contract response window; not dispatched "
                    "automatically"
                ],
            )

        # "unclear": the records settle it when an earlier request for this equipment
        # arrived inside the window. Otherwise a human decides.
        if same_fault is None:
            if within_window and prior is not None:
                return Decision(
                    status="duplicate_detected",
                    reasons=[
                        Reason(
                            "same_fault_already_open",
                            f"An earlier request ({prior.requestId}) for this equipment "
                            f"arrived {prior.gapHours:.1f} hours ago, inside the "
                            f"{window_hours:g}-hour response window; this is treated as the "
                            "same incident rather than a second job",
                            "OPS-INTAKE-003",
                        )
                    ],
                    linkedWorkOrders=linked_wos,
                    linkedRequests=sorted(
                        set(facts.referencedRequests) | {prior.requestId}
                        if prior.requestId
                        else set(facts.referencedRequests)
                    ),
                    warnings=["Linked to the existing job; no second work order created"],
                )

            if prior is not None and window_hours is not None:
                reason = Reason(
                    "follow_up_after_response_window",
                    f"This may repeat a request raised {prior.gapHours:.1f} hours ago, after "
                    f"the {window_hours:g}-hour response window had passed; a coordinator "
                    "decides how to handle it",
                    "OPS-INTAKE-003",
                )
                return Decision(
                    status="human_escalation_required",
                    reasons=[reason],
                    linkedWorkOrders=linked_wos,
                    linkedRequests=sorted(
                        set(facts.referencedRequests)
                        | ({prior.requestId} if prior.requestId else set())
                    ),
                    missingInformation=["Coordinator decision on the repeated request"],
                    warnings=[
                        "Follow-up received after the contract response window; not "
                        "dispatched automatically"
                    ],
                )

            return Decision(
                status="clarification_required",
                reasons=[
                    Reason(
                        "possible_duplicate_unclear",
                        "This may be the same fault as an open job on this equipment; a "
                        "coordinator must confirm before a second work order is considered",
                        "OPS-INTAKE-003",
                    )
                ],
                linkedWorkOrders=linked_wos,
                linkedRequests=sorted(set(facts.referencedRequests)),
                missingInformation=["Whether this is the same fault as the job already open"],
                warnings=["Possible duplicate held for operator review"],
            )
    # same_fault is False: a different fault, so it continues through the normal checks.

    # --- 5. coverage ----------------------------------------------------
    wants_onsite = _wants_onsite(facts, request_text)
    problem = _coverage_problem(evidence, wants_onsite)
    if problem is not None:
        return Decision(
            status="account_review_required",
            reasons=[problem],
            missingInformation=["Confirmation of coverage from the account record"],
            warnings=["Coverage could not be confirmed from the agreement record"],
        )

    agreement = evidence.agreement
    contract_ref = agreement.get("contractRef")
    response_hours = agreement.get("responseHours")
    covered = Reason(
        "coverage_confirmed",
        f"Agreement {contract_ref} is active and in force at the request time",
        "POL-CONTRACT-002",
    )

    # --- 6. planned work and coverage questions -------------------------
    if facts.intent in {"planned_service", "coverage_question"}:
        label = (
            "Planned service is scheduled, not dispatched at intake"
            if facts.intent == "planned_service"
            else "Coverage confirmed from the agreement record"
        )
        return Decision(
            status="covered_action",
            reasons=[
                covered,
                Reason("planned_not_dispatched", label, "OPS-DISPATCH-004"),
            ],
            warnings=["No work order at intake; the visit is scheduled first"],
        )

    # --- 6b. earlier work on this equipment -----------------------------
    # A message that points back to a previous visit or job is asking someone to look at
    # what was already done. Dispatching over the top of that is how a job gets created
    # twice (OPS-INTAKE-003: check for the same incident first). The matching history goes
    # to a coordinator with the case.
    # A reply to our own safety question is never a request to review past work, whatever
    # the extraction says: the model sets this flag liberally on short replies.
    if facts.refersToPreviousWork and not facts.isSafetyAnswer:
        history = [
            {
                "id": h.get("id"),
                "requestId": h.get("requestId"),
                "summary": h.get("summary"),
                "status": h.get("status"),
                "openedAt": h.get("openedAt"),
                "closedAt": h.get("closedAt"),
            }
            for h in evidence.assetHistory
        ]
        return Decision(
            status="human_escalation_required",
            reasons=[
                covered,
                Reason(
                    "refers_to_previous_work",
                    "The request asks about earlier work on this equipment; a coordinator "
                    "reviews the previous jobs before anything new is raised",
                    "OPS-INTAKE-003",
                ),
            ],
            relatedHistory=history,
            linkedWorkOrders=[str(h["id"]) for h in history if h.get("id")],
            missingInformation=["Coordinator review of the previous work on this equipment"],
            warnings=[
                "Not dispatched: the request refers to earlier work that must be reviewed first"
            ],
        )

    # --- 7. breakdown ---------------------------------------------------
    if facts.intent == "breakdown":
        # D1: for a breakdown, only qualified, available, same-city technicians count.
        candidates = [
            t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite
        ]
        if candidates:
            chosen = candidates[0]
            warnings = [
                "Technician is a recommendation; availability must be re-checked "
                "immediately before the work order is created",
                f"Response commitment: {response_hours} hours" if response_hours else "",
            ]
            # A technician already on the way to this equipment is worth a coordinator's
            # eye even when this is a genuinely different fault: they may be able to take
            # both while on site, rather than two people attending the same equipment.
            en_route = [
                w
                for w in evidence.openWorkOrders
                if str(w.get("status") or "").lower()
                in {"technician_en_route", "assigned", "awaiting_site_access"}
                and w.get("technicianId")
            ]
            for job in en_route:
                warnings.append(
                    f"{job['technicianId']} is already assigned to {job['id']} on this "
                    f"equipment ({job.get('status')}): \"{job.get('summary')}\". A "
                    "coordinator may prefer to combine the visits."
                )
            return Decision(
                status="dispatch_ready",
                reasons=[
                    covered,
                    Reason(
                        "technician_matched",
                        f"{chosen.technicianId} holds the required skill and certifications "
                        f"and is available in {chosen.city}",
                        "OPS-DISPATCH-004",
                    ),
                ],
                recommendedTechnician=chosen,
                # The others who could take it, so a coordinator can swap without re-deriving.
                alternativeTechnicians=candidates[1:],
                warnings=warnings,
            )

        qualified = len(evidence.qualifiedTechnicians)
        return Decision(
            status="resource_escalation_required",
            reasons=[
                covered,
                Reason(
                    "no_available_qualified_technician",
                    f"{qualified} technician(s) hold the required certifications but none are "
                    "available in the site's city",
                    "OPS-DISPATCH-004",
                ),
            ],
            missingInformation=["An available qualified technician for this asset"],
            warnings=[
                "No placeholder work order created; the dispatch lead decides how to resource this"
            ],
        )

    # --- fallback -------------------------------------------------------
    # follow_up / other on a covered asset with nothing open to link to.
    return Decision(
        status="clarification_required",
        reasons=[
            covered,
            Reason(
                "intent_unclear",
                "The request does not state what action is needed",
                "OPS-INTAKE-003",
            ),
        ],
        missingInformation=["What the customer would like Northstar to do"],
    )
