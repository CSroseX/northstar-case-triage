"""Creating the work order — the only place this service writes to Northstar.

Runs only for `dispatch_ready`. Three things make this safe:

1. **Availability is re-checked immediately before the write** (OPS-DISPATCH-004).
   A technician can accept another emergency between triage and the write, so the
   recommendation is re-verified against a live read. If they have gone, the next
   qualified same-city technician takes it; if nobody is left, the case becomes a
   resource escalation instead of a work order against someone unavailable.

2. **The X-Event-ID is the externalEventId**, so a redelivered event cannot create a
   second job. `duplicate: true` in the response is a success, not an error.

3. **A timeout leaves the outcome uncertain**, so the work orders are read back by
   request and event reference before any retry, and the retry reuses the same id
   (SYS-CATALOG-001). If the outcome still cannot be established, the case fails
   visibly rather than risking a second job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .decide import Decision
from .evidence import AssetEvidence, TechnicianMatch, _normalise_work_order
from .northstar_client import LookupFailure, NorthstarClient, WorkOrderRejected

logger = logging.getLogger("northstar.booking")


@dataclass
class BookingOutcome:
    """What happened when we tried to book. `status` overrides the decision when set."""

    workOrder: dict[str, Any] | None = None
    duplicate: bool = False
    status: str | None = None
    technician: TechnicianMatch | None = None
    warnings: list[str] = field(default_factory=list)
    reasons: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def created(self) -> bool:
        return self.workOrder is not None


async def _available_now(
    client: NorthstarClient, evidence: AssetEvidence
) -> list[TechnicianMatch]:
    """Re-read technicians and return the qualified, available, same-city ones.

    Qualification comes from the evidence we already gathered; only availability is
    re-read, because that is the field that moves during a shift.
    """
    live = await client.technicians()
    availability = {t.get("id"): bool(t.get("available")) for t in live}
    assignments = {t.get("id"): t.get("currentAssignment") for t in live}

    refreshed: list[TechnicianMatch] = []
    for tech in evidence.qualifiedTechnicians:
        if not tech.sameCityAsSite:
            continue
        if not availability.get(tech.technicianId, False):
            continue
        # Carry the live assignment through so the record reflects what we checked.
        refreshed.append(
            TechnicianMatch(
                technicianId=tech.technicianId,
                name=tech.name,
                city=tech.city,
                skills=tech.skills,
                certifications=tech.certifications,
                available=True,
                sameCityAsSite=True,
                currentAssignment=assignments.get(tech.technicianId),
            )
        )
    return refreshed


async def _reconcile(
    client: NorthstarClient, request_id: str, event_id: str | None
) -> dict[str, Any] | None:
    """Did our write land? Look the asset's work orders up by request/event reference.

    Returns the work order if one already exists for this request or event, else None.
    """
    orders = [_normalise_work_order(w) for w in await client.work_orders()]
    for order in orders:
        if order.get("requestId") == request_id:
            return order
        if event_id and str(order.get("externalEventId") or "") == event_id:
            return order
    return None


async def book_work_order(
    client: NorthstarClient,
    decision: Decision,
    evidence: AssetEvidence,
    request_id: str,
    event_id: str | None,
    summary: str = "",
) -> BookingOutcome:
    """Create the work order for a dispatch-ready case. Never called for anything else."""
    outcome = BookingOutcome(technician=decision.recommendedTechnician)

    if not event_id:
        # Without the idempotency key a retry could create a second job, so we do not write.
        return BookingOutcome(
            status="resource_escalation_required",
            warnings=[
                "No X-Event-ID supplied, so no work order was created: the idempotency key "
                "is required before a dispatch can be written"
            ],
            reasons=[
                (
                    "missing_event_id",
                    "A work order cannot be created without the delivery event id",
                    "OPS-INTAKE-003",
                )
            ],
        )

    # 1. Re-check availability immediately before the write.
    try:
        available = await _available_now(client, evidence)
    except LookupFailure as exc:
        return BookingOutcome(
            status="failed",
            warnings=[
                f"Could not re-check technician availability ({exc.detail}); no work order "
                "was created"
            ],
            reasons=[
                (
                    "availability_recheck_failed",
                    f"Technician availability could not be confirmed before the write: {exc.detail}",
                    "OPS-DISPATCH-004",
                )
            ],
        )

    if not available:
        # Everyone qualified has gone since triage. Never write against someone unavailable.
        return BookingOutcome(
            status="resource_escalation_required",
            warnings=[
                "No qualified technician remained available at the final check; the dispatch "
                "lead decides how to resource this"
            ],
            reasons=[
                (
                    "no_available_qualified_technician",
                    "The recommended technician was no longer available and no qualified "
                    "replacement was free in the site's city",
                    "OPS-DISPATCH-004",
                )
            ],
        )

    chosen = decision.recommendedTechnician
    still_free = chosen is not None and any(
        t.technicianId == chosen.technicianId for t in available
    )
    if not still_free:
        # Automatically take the next qualified same-city technician.
        replacement = available[0]
        outcome.warnings.append(
            f"{chosen.technicianId if chosen else 'The recommended technician'} was no longer "
            f"available at the final check; {replacement.technicianId} was assigned instead"
        )
        chosen = replacement
    outcome.technician = chosen

    payload = {
        "externalEventId": event_id,
        "requestId": request_id,
        "assetId": evidence.assetId,
        "technicianId": chosen.technicianId,
        "safetyRisk": False,  # dispatch_ready is never reached with a safety signal
    }
    # The fault this job is for, in the reporter's terms. Northstar's own work orders
    # carry one ("Freezer plant 2 compressor cycling") and ours did not, so a later
    # report of the same fault had a blank line to compare itself against and looked
    # like a new problem (OPS-INTAKE-003: compare the symptoms).
    if summary:
        payload["summary"] = summary

    # 2. Write, keyed on the event id.
    try:
        response = await client.create_work_order(payload)
    except WorkOrderRejected as exc:
        # A 4xx is our payload's fault and will not improve on retry.
        return BookingOutcome(
            status="failed",
            warnings=[
                "Northstar rejected the work order; a coordinator must raise it manually"
            ],
            reasons=[
                (
                    "work_order_rejected",
                    f"The work-order service rejected the request (HTTP {exc.status_code})",
                    "SYS-CATALOG-001",
                )
            ],
        )
    except LookupFailure as exc:
        # 3. Uncertain outcome. Reconcile before retrying, and retry with the SAME id.
        logger.warning("work-order write uncertain (%s); reconciling", exc.detail)
        try:
            existing = await _reconcile(client, request_id, event_id)
        except LookupFailure as reconcile_exc:
            return BookingOutcome(
                status="failed",
                warnings=[
                    "The work-order service did not respond and the outcome could not be "
                    "confirmed; a coordinator must check before anything is raised"
                ],
                reasons=[
                    (
                        "work_order_outcome_unknown",
                        f"Write timed out and reconciliation also failed ({reconcile_exc.detail})",
                        "SYS-CATALOG-001",
                    )
                ],
            )

        if existing is not None:
            outcome.workOrder = existing
            outcome.duplicate = True
            outcome.warnings.append(
                "The first write timed out but the work order was found on reconciliation; "
                "no second job was created"
            )
            return outcome

        # Nothing landed. One retry, same event id, so a duplicate is impossible.
        try:
            response = await client.create_work_order(payload)
        except (LookupFailure, WorkOrderRejected) as retry_exc:
            return BookingOutcome(
                status="failed",
                warnings=[
                    "The work order could not be confirmed as created; a coordinator must "
                    "check before raising it manually"
                ],
                reasons=[
                    (
                        "work_order_outcome_unknown",
                        f"Write failed and the retry did not confirm an outcome ({retry_exc})",
                        "SYS-CATALOG-001",
                    )
                ],
            )

    work_order = response.get("workOrder") if isinstance(response, dict) else None
    if not work_order:
        return BookingOutcome(
            status="failed",
            warnings=[
                "The work-order service returned no work order; a coordinator must check "
                "before raising it manually"
            ],
            reasons=[
                (
                    "work_order_outcome_unknown",
                    "The create call succeeded but returned no work order record",
                    "SYS-CATALOG-001",
                )
            ],
        )

    outcome.workOrder = _normalise_work_order(work_order)
    # "duplicate: true" means our event id was already accepted. That is the idempotency
    # guarantee working, not a failure.
    outcome.duplicate = bool(response.get("duplicate"))
    if outcome.duplicate:
        outcome.warnings.append(
            f"This delivery was already processed; the original work order "
            f"{outcome.workOrder.get('id')} was returned rather than a second one"
        )
    return outcome
