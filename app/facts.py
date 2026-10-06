"""The small set of facts the model extracts from a customer's message.

The model READS; this structure is all it is trusted to produce. Code decides from these
facts plus the looked-up evidence, so the facts stay deliberately small, literal and
checkable — no status, no recommendation, no judgement.

Everything here is about what the message SAYS. Whether an asset exists, whether the
agreement covers it, or who should attend is evidence, not fact extraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Did the reporter affirm a hazard, rule one out, leave it unclear, or not mention it?
SafetySignal = Literal["affirmed", "denied", "ambiguous", "absent"]

# What the customer is asking for.
Intent = Literal["planned_service", "breakdown", "coverage_question", "follow_up", "other"]

VALID_SAFETY_SIGNALS = frozenset({"affirmed", "denied", "ambiguous", "absent"})
VALID_INTENTS = frozenset(
    {"planned_service", "breakdown", "coverage_question", "follow_up", "other"}
)
VALID_SAME_FAULT = frozenset({"same", "different", "unclear"})


@dataclass(frozen=True)
class RequestFacts:
    """What the message says. Produced by the model, validated before use."""

    # Asset identifiers the message mentions. May be empty; may be more than one.
    assetMentions: list[str] = field(default_factory=list)

    safetySignal: SafetySignal = "absent"
    # The customer's exact words that drove the signal. Never a paraphrase: POL-SAFETY-001
    # requires the original wording be preserved in the record.
    safetyQuote: str = ""

    intent: Intent = "other"
    # One short line in the customer's terms, used to compare faults for duplicates.
    symptomSummary: str = ""

    # Earlier requests or work orders the message points back to (REQ-…, WO-…).
    referencedRequests: list[str] = field(default_factory=list)

    # Judged against the open jobs and earlier requests supplied in the prompt:
    # "same", "different", "unclear", or "" when nothing was there to compare.
    sameFaultAsExisting: str = ""
    # Which job or request it matches (WO-… / REQ-…), when the answer is "same".
    sameFaultReference: str = ""

    # Does the message ask for someone to attend in person? Decides whether a remote_only
    # agreement actually conflicts with what was asked for.
    requiresOnsite: bool | None = None

    # Does the message point back to earlier work on this equipment — a previous visit, an
    # old work order, "check what was done last time"? A coordinator reviews those rather
    # than the automation dispatching over the top of them (OPS-INTAKE-003).
    refersToPreviousWork: bool | None = None

    # Is this the customer answering our safety question? And if so, did the answer leave
    # the hazard unresolved ("not sure", "can't tell")? Meera: "not sure" stays with a human.
    isSafetyAnswer: bool | None = None
    safetyAnswerUncertain: bool | None = None

    # Set when the model failed, timed out or returned something unusable. Forces the
    # cautious path: a missing model result can never clear a safety signal.
    modelUnavailable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "assetMentions": list(self.assetMentions),
            "safetySignal": self.safetySignal,
            "safetyQuote": self.safetyQuote,
            "intent": self.intent,
            "symptomSummary": self.symptomSummary,
            "referencedRequests": list(self.referencedRequests),
            "sameFaultAsExisting": self.sameFaultAsExisting,
            "sameFaultReference": self.sameFaultReference,
            "requiresOnsite": self.requiresOnsite,
            "refersToPreviousWork": self.refersToPreviousWork,
            "isSafetyAnswer": self.isSafetyAnswer,
            "safetyAnswerUncertain": self.safetyAnswerUncertain,
            "modelUnavailable": self.modelUnavailable,
        }


def parse_facts(payload: dict[str, Any] | None) -> RequestFacts:
    """Build facts from a model payload, degrading safely rather than trusting it.

    Anything missing or malformed becomes the cautious value: an unreadable payload is
    treated as `modelUnavailable`, and an unrecognised safety signal becomes "ambiguous"
    rather than "absent", so a broken model cannot quietly clear a hazard.
    """
    if not isinstance(payload, dict):
        return RequestFacts(modelUnavailable=True, safetySignal="ambiguous")

    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()]

    raw_signal = payload.get("safetySignal")
    signal: SafetySignal
    if raw_signal in VALID_SAFETY_SIGNALS:
        signal = raw_signal  # type: ignore[assignment]
    else:
        # Unknown or missing is not the same as absent.
        signal = "ambiguous"

    raw_intent = payload.get("intent")
    intent: Intent = raw_intent if raw_intent in VALID_INTENTS else "other"  # type: ignore[assignment]

    def _tristate(key: str) -> bool | None:
        """A real boolean, or None when the model did not say.

        None means "not reported", which the decision treats cautiously. It is never
        collapsed to False: a missing answer is not the same as a negative one.
        """
        value = payload.get(key)
        return value if isinstance(value, bool) else None

    requires_onsite = _tristate("requiresOnsite")
    refers_to_previous_work = _tristate("refersToPreviousWork")
    is_safety_answer = _tristate("isSafetyAnswer")
    safety_answer_uncertain = _tristate("safetyAnswerUncertain")

    raw_same_fault = payload.get("sameFaultAsExisting")
    same_fault = (
        raw_same_fault if raw_same_fault in VALID_SAME_FAULT else ""
    )
    same_fault_ref = payload.get("sameFaultReference")

    quote = payload.get("safetyQuote")
    return RequestFacts(
        assetMentions=_string_list(payload.get("assetMentions")),
        safetySignal=signal,
        safetyQuote=str(quote).strip() if isinstance(quote, str) else "",
        intent=intent,
        symptomSummary=str(payload.get("symptomSummary") or "").strip(),
        referencedRequests=_string_list(payload.get("referencedRequests")),
        sameFaultAsExisting=same_fault,
        sameFaultReference=str(same_fault_ref).strip() if isinstance(same_fault_ref, str) else "",
        requiresOnsite=requires_onsite,
        refersToPreviousWork=refers_to_previous_work,
        isSafetyAnswer=is_safety_answer,
        safetyAnswerUncertain=safety_answer_uncertain,
        modelUnavailable=False,
    )
