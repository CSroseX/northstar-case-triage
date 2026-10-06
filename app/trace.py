"""The decision trace: a readable record of what the service did, step by step.

This module only *observes*. It reads what the evidence, model, decision and booking
layers already produced and writes it down in order — it never influences an outcome,
and nothing in `decide()` depends on it.

What is recorded:
  lookups   - what each Northstar read found, or that it failed
  model     - the facts extracted from the message, and the gateway trace id
  checks    - each rule that ran, in order, and what it concluded
  action    - what was done at the end

What is never recorded: credentials of any kind, and the customer's message body. The
request id and the safety quote are enough to find the original, and the quote is kept
because POL-SAFETY-001 requires the reporter's own words on the record.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .booking import BookingOutcome
from .decide import Decision
from .evidence import AssetEvidence
from .facts import RequestFacts
from .models import CaseInput

logger = logging.getLogger("northstar.trace")

# The check each reason code belongs to, so the trace can say which rule concluded what.
# Derived from the codes decide() already emits; adding a code here changes nothing about
# the decision, only how it is labelled in the trace.
CHECK_BY_REASON: dict[str, str] = {
    "safety_signal_affirmed": "safety",
    "safety_answer_unresolved": "safety",
    "safety_signal_ambiguous": "safety",
    "safety_wording_unconfirmed": "safety",
    "safety_denial_unverified": "safety",
    "safety_unverified": "safety",
    "lookup_failed": "lookups",
    "asset_not_identified": "identity",
    "asset_not_on_record": "identity",
    "ambiguous_asset_reference": "identity",
    "same_fault_already_open": "duplicate",
    "possible_duplicate_unclear": "duplicate",
    "follow_up_after_response_window": "duplicate",
    "follow_up_window_unknown": "duplicate",
    "agreement_missing": "coverage",
    "agreement_customer_mismatch": "coverage",
    "agreement_not_active": "coverage",
    "asset_coverage_suspended": "coverage",
    "received_before_agreement_start": "coverage",
    "agreement_not_in_force": "coverage",
    "agreement_window_unknown": "coverage",
    "remote_only_cannot_dispatch": "coverage",
    "coverage_confirmed": "coverage",
    "planned_not_dispatched": "planned_vs_breakdown",
    "refers_to_previous_work": "previous_work",
    "technician_matched": "technician",
    "no_available_qualified_technician": "technician",
    "availability_recheck_failed": "technician",
    "missing_event_id": "action",
    "work_order_rejected": "action",
    "work_order_outcome_unknown": "action",
    "intent_unclear": "intent",
}

# The order checks run in, so the trace reads the way the decision was made.
CHECK_ORDER = [
    "safety",
    "lookups",
    "identity",
    "duplicate",
    "coverage",
    "previous_work",
    "planned_vs_breakdown",
    "technician",
    "intent",
    "action",
]


def _lookup_step(evidence: AssetEvidence) -> dict[str, Any]:
    """What the Northstar reads found, or that they failed."""
    step: dict[str, Any] = {"outcome": evidence.outcome, "assetId": evidence.assetId}

    if evidence.outcome == "resolved":
        agreement = evidence.agreement or {}
        step["found"] = {
            "customer": (evidence.customer or {}).get("id"),
            "site": (evidence.site or {}).get("id"),
            "agreement": agreement.get("contractRef"),
            "agreementStatus": agreement.get("status"),
            "serviceMode": agreement.get("serviceMode"),
            "responseHours": agreement.get("responseHours"),
            "inForceAtReceivedAt": agreement.get("inForceAtReceivedAt"),
            "qualifiedTechnicians": len(evidence.qualifiedTechnicians),
            "availableInSiteCity": len(
                [t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite]
            ),
            "openWorkOrders": [w.get("id") for w in evidence.openWorkOrders],
            "closedJobsOnAsset": len(evidence.assetHistory),
        }
    if evidence.failedLookups:
        step["failed"] = list(evidence.failedLookups)
    if evidence.gaps:
        step["gaps"] = list(evidence.gaps)
    return step


def _model_step(facts: RequestFacts, trace_ids: list[str]) -> dict[str, Any]:
    """The facts the model pulled out of the message, and its trace id."""
    return {
        "traceIds": list(trace_ids),
        "unavailable": facts.modelUnavailable,
        "facts": {
            "assetMentions": list(facts.assetMentions),
            "safetySignal": facts.safetySignal,
            # Kept deliberately: POL-SAFETY-001 wants the reporter's own words.
            "safetyQuote": facts.safetyQuote,
            "intent": facts.intent,
            "symptomSummary": facts.symptomSummary,
            "referencedRequests": list(facts.referencedRequests),
            "sameFaultAsExisting": facts.sameFaultAsExisting,
            "sameFaultReference": facts.sameFaultReference,
            "requiresOnsite": facts.requiresOnsite,
            "refersToPreviousWork": facts.refersToPreviousWork,
            "isSafetyAnswer": facts.isSafetyAnswer,
            "safetyAnswerUncertain": facts.safetyAnswerUncertain,
        },
    }


def _check_steps(decision: Decision) -> list[dict[str, Any]]:
    """Each rule that fired, grouped by the check it belongs to, in decision order."""
    by_check: dict[str, list[dict[str, str]]] = {}
    for reason in decision.reasons:
        check = CHECK_BY_REASON.get(reason.code, "other")
        by_check.setdefault(check, []).append(
            {"code": reason.code, "concluded": reason.detail, "policy": reason.policy}
        )

    ordered = [c for c in CHECK_ORDER if c in by_check]
    ordered += [c for c in by_check if c not in CHECK_ORDER]
    return [{"check": check, "results": by_check[check]} for check in ordered]


def _action_step(
    decision: Decision, booking: BookingOutcome | None, work_order: dict[str, Any] | None
) -> dict[str, Any]:
    """What was actually done at the end."""
    if work_order and work_order.get("id"):
        return {
            "taken": "work_order_created",
            "workOrderId": work_order.get("id"),
            "technicianId": work_order.get("technicianId"),
            "wasAlreadyCreated": bool(booking and booking.duplicate),
        }
    if decision.status == "duplicate_detected":
        return {
            "taken": "linked_to_existing",
            "linkedRequests": list(decision.linkedRequests),
            "linkedWorkOrders": list(decision.linkedWorkOrders),
            "notLinked": list(decision.otherOpenWorkOrders),
        }
    if decision.recommendedTechnician:
        return {
            "taken": "no_write",
            "recommendedTechnician": decision.recommendedTechnician.technicianId,
            "note": "Recommendation only; no work order created",
        }
    return {"taken": "no_write"}


def build_trace(
    case: CaseInput,
    evidence: AssetEvidence,
    facts: RequestFacts,
    decision: Decision,
    trace_ids: list[str],
    booking: BookingOutcome | None = None,
    work_order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ordered record of how this request reached its status."""
    return {
        "requestId": case.requestId,
        "receivedAt": case.receivedAt,
        "channel": case.channel,
        "status": decision.status,
        "steps": {
            "lookups": _lookup_step(evidence),
            "model": _model_step(facts, trace_ids),
            "checks": _check_steps(decision),
            "action": _action_step(decision, booking, work_order),
        },
    }


class TraceLog:
    """Append-only JSONL log plus an in-memory tail for /cases/recent.

    Writing is best effort: a log failure must never change or break a response, so
    every write is guarded. The tail lives in memory and is lost on restart, which is
    acceptable for a coordinator looking at the last few requests.
    """

    def __init__(self, path: str | None = None, keep: int = 50) -> None:
        self._path = Path(path) if path else None
        self._recent: deque[dict[str, Any]] = deque(maxlen=keep)
        self._lock = threading.Lock()

        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError) as exc:
                # An unusable path disables file logging; the in-memory tail still works.
                logger.warning("trace log directory unavailable: %s", exc)
                self._path = None

    def record(self, trace: dict[str, Any]) -> None:
        """Keep the trace in memory and append one line to the log file."""
        entry = {"loggedAt": datetime.now(timezone.utc).isoformat(), **trace}
        with self._lock:
            self._recent.append(entry)

        if self._path is None:
            return
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except (OSError, TypeError, ValueError, RecursionError) as exc:
            # Logging is never allowed to break the actual response.
            logger.warning("could not write trace for %s: %s", trace.get("requestId"), exc)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            entries = list(self._recent)
        return list(reversed(entries))[:limit]


def build_trace_log() -> TraceLog:
    """The process-wide log. TRACE_LOG_PATH overrides the default location."""
    return TraceLog(os.getenv("TRACE_LOG_PATH", "/tmp/northstar-traces.jsonl"))
