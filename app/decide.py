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

# Hazard vocabulary from POL-SAFETY-001: fuel/gas leak, smoke, water near electrical
# equipment, people dizzy or unwell in an equipment room. This is a backstop over the
# model's own reading, never a replacement for it.
HAZARD_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bsmoke\b|\bsmoking\b|\bburning\b|\bburnt\b|\bfire\b|\bsparks?\b", "smoke or fire"),
    (r"\bdiesel\b|\bfuel\b|\bgas\b|\bpetrol\b|\blpg\b", "fuel or gas"),
    (r"\bleak(?:ing|age|s)?\b|\bspill(?:ed|age|ing)?\b", "leak or spill"),
    (r"\bsmell(?:s|ing)?\b|\bodour\b|\bodor\b|\bfumes?\b", "smell or fumes"),
    (r"\bwater\b|\bwet\b|\bflood(?:ed|ing)?\b|\bdamp\b", "water"),
    (r"\bdizzy\b|\blight[- ]?headed\b|\bfaint(?:ing)?\b|\bnause\w*\b|\bunwell\b|\bcollaps\w*\b",
     "person unwell"),
    (r"\bshock(?:ed|s)?\b|\belectrocut\w*\b|\bexposed wir\w*\b|\blive wir\w*\b", "electrical risk"),
)

# "no smoke, water or unusual smell" — the customer ruling a hazard out. A negated hazard
# word must not trip the backstop.
NEGATION_PATTERN = re.compile(
    r"\b(?:no|not|without|never|nothing|none|neither|nor|n't)\b", re.IGNORECASE
)

# How far before a hazard word a negation still applies. Covers "there is no smoke, water
# or unusual smell" without reaching across a sentence boundary.
NEGATION_WINDOW_CHARS = 60

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


def _is_negated(text: str, start: int) -> bool:
    """True when a negation sits shortly before the match, within the same clause."""
    window_start = max(0, start - NEGATION_WINDOW_CHARS)
    window = text[window_start:start]
    # Don't let a negation reach across a sentence boundary.
    for boundary in (".", "!", "?", ";"):
        if boundary in window:
            window = window.rsplit(boundary, 1)[1]
    return bool(NEGATION_PATTERN.search(window))


def find_hazard_words(text: str) -> list[str]:
    """Hazard vocabulary present and not negated. The backstop over the model's reading."""
    found: list[str] = []
    for pattern, label in HAZARD_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if not _is_negated(text, match.start()):
                if label not in found:
                    found.append(label)
                break
    return found


def _wants_onsite(facts: RequestFacts, text: str) -> bool:
    """Did the customer ask for someone to attend? Explicit fact wins over the text."""
    if facts.requiresOnsite is not None:
        return facts.requiresOnsite
    if facts.intent == "breakdown":
        return True
    return bool(ONSITE_REQUEST_PATTERN.search(text))


def _same_fault(facts: RequestFacts, evidence: AssetEvidence) -> bool | None:
    """Same fault as an open job, a different one, or unclear?

    True  - the message points at an existing request/WO, or repeats its symptoms.
    False - an open job exists but this is plainly a different fault.
    None  - cannot tell. A human decides (CLAUDE.md rule 7).
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

    if not evidence.openWorkOrders:
        return False

    # Compare the reported symptoms with the open job's summary.
    summary_words = {
        w
        for wo in evidence.openWorkOrders
        for w in re.findall(r"[a-z]{4,}", str(wo.get("summary", "")).lower())
    }
    symptom_words = set(re.findall(r"[a-z]{4,}", facts.symptomSummary.lower()))
    if not summary_words or not symptom_words:
        # An open job exists but we have nothing to compare. Don't guess either way.
        return None

    overlap = summary_words & symptom_words
    if len(overlap) >= 2:
        return True
    if facts.intent == "follow_up":
        # A follow-up against an open job, with no symptom match either way.
        return None
    return False


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
    # explicit denial by the customer is theirs to make, not ours to override.
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
