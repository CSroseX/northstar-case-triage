"""The decision: request + facts + evidence -> status, reasons, recommendation.

No model calls and no I/O. Every rule traces to a policy id so a coordinator can see
which document drove the outcome.

Check order, first match wins:
  safety -> lookup failure -> identity -> duplicate -> coverage -> planned -> breakdown
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    same_fault = _same_fault(facts, evidence)
    if same_fault is True:
        linked_wos = [str(w.get("id")) for w in evidence.openWorkOrders if w.get("id")]
        linked_reqs = sorted(
            {str(w.get("requestId")) for w in evidence.openWorkOrders if w.get("requestId")}
            | set(facts.referencedRequests)
        )
        return Decision(
            status="duplicate_detected",
            reasons=[
                Reason(
                    "same_fault_already_open",
                    "This reports a fault already open on this asset; it is linked to the "
                    "existing work order rather than opening a second one",
                    "OPS-INTAKE-003",
                )
            ],
            linkedWorkOrders=linked_wos,
            linkedRequests=linked_reqs,
            warnings=["Linked to the existing job; no second work order created"],
        )

    if same_fault is None:
        return Decision(
            status="clarification_required",
            reasons=[
                Reason(
                    "possible_duplicate_unclear",
                    "This may be the same fault as an open job on this asset; a coordinator "
                    "must confirm before a second work order is considered",
                    "OPS-INTAKE-003",
                )
            ],
            linkedWorkOrders=[str(w.get("id")) for w in evidence.openWorkOrders if w.get("id")],
            linkedRequests=sorted(set(facts.referencedRequests)),
            missingInformation=["Whether this is the same fault as the job already open"],
            warnings=["Possible duplicate held for operator review"],
        )

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

    # --- 7. breakdown ---------------------------------------------------
    if facts.intent == "breakdown":
        # D1: for a breakdown, only qualified, available, same-city technicians count.
        candidates = [
            t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite
        ]
        if candidates:
            chosen = candidates[0]
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
                warnings=[
                    "Technician is a recommendation; availability must be re-checked "
                    "immediately before the work order is created",
                    f"Response commitment: {response_hours} hours" if response_hours else "",
                ],
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
