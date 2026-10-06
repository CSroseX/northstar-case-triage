"""Evidence gathering: everything we know about one asset, as facts.

This module reports and never decides. It computes no status, applies no policy and
chooses no technician. Where a fact cannot be established it says so explicitly
(`unknown_asset`, `no_asset_id`, `lookup_failed`) rather than filling in a default,
so the decision layer can tell "false" apart from "we don't know".

Flags like `receivedAt` falling outside the agreement window, or an agreement whose
customer differs from the asset's, are recorded as observations. What they *mean*
(account review, dispatch, escalation) is the decision layer's call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from .northstar_client import LookupFailure, NorthstarClient

logger = logging.getLogger("northstar.evidence")

EvidenceOutcome = Literal["resolved", "unknown_asset", "no_asset_id", "lookup_failed"]

# Work orders considered still open. From the observed data; anything unrecognised is
# treated as open so an unknown status never hides a live job.
CLOSED_WORK_ORDER_STATUSES = frozenset(
    {"completed", "closed", "closed_after_review", "cancelled", "remote_resolution"}
)


@dataclass(frozen=True)
class TechnicianMatch:
    """A technician qualified for the asset. Qualification is factual; selection is not."""

    technicianId: str
    name: str
    city: str
    skills: list[str]
    certifications: list[str]
    available: bool
    sameCityAsSite: bool
    currentAssignment: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "technicianId": self.technicianId,
            "name": self.name,
            "city": self.city,
            "skills": self.skills,
            "certifications": self.certifications,
            "available": self.available,
            "sameCityAsSite": self.sameCityAsSite,
            "currentAssignment": self.currentAssignment,
        }


@dataclass
class AssetEvidence:
    """Facts about one asset at one point in time. No judgement attached."""

    outcome: EvidenceOutcome
    assetId: str | None = None
    receivedAt: str | None = None

    asset: dict[str, Any] | None = None
    site: dict[str, Any] | None = None
    customer: dict[str, Any] | None = None

    agreement: dict[str, Any] | None = None
    qualifiedTechnicians: list[TechnicianMatch] = field(default_factory=list)
    openWorkOrders: list[dict[str, Any]] = field(default_factory=list)
    recentRequests: list[dict[str, Any]] = field(default_factory=list)

    # Things we noticed but did not interpret.
    gaps: list[str] = field(default_factory=list)
    sourceReferences: list[dict[str, Any]] = field(default_factory=list)
    failedLookups: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.outcome == "resolved"

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "assetId": self.assetId,
            "receivedAt": self.receivedAt,
            "asset": self.asset,
            "site": self.site,
            "customer": self.customer,
            "agreement": self.agreement,
            "qualifiedTechnicians": [t.as_dict() for t in self.qualifiedTechnicians],
            "openWorkOrders": self.openWorkOrders,
            "recentRequests": self.recentRequests,
            "gaps": self.gaps,
            "sourceReferences": self.sourceReferences,
            "failedLookups": self.failedLookups,
        }


def _parse_date(value: str | None) -> date | None:
    """Accept both a date (`2026-08-12`) and an ISO timestamp (`2026-09-20T08:42:00Z`)."""
    if not value:
        return None
    text = value.strip()
    try:
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text)
    except ValueError:
        return None


def _index_assets(customers: list[dict[str, Any]]) -> dict[str, tuple[dict, dict, dict]]:
    """asset id -> (asset, site, customer). Asset IDs are unique across the dataset."""
    index: dict[str, tuple[dict, dict, dict]] = {}
    for customer in customers:
        for site in customer.get("sites") or []:
            for asset in site.get("assets") or []:
                asset_id = asset.get("id")
                if asset_id:
                    index[asset_id] = (asset, site, customer)
    return index


def _normalise_work_order(raw: dict[str, Any]) -> dict[str, Any]:
    """The work-orders route uses snake_case; every other route is camelCase."""
    return {
        "id": raw.get("id"),
        "requestId": raw.get("request_id", raw.get("requestId")),
        "customerId": raw.get("customer_id", raw.get("customerId")),
        "siteId": raw.get("site_id", raw.get("siteId")),
        "assetId": raw.get("asset_id", raw.get("assetId")),
        "technicianId": raw.get("technician_id", raw.get("technicianId")),
        "status": raw.get("status"),
        "summary": raw.get("summary"),
        "createdAt": raw.get("created_at", raw.get("createdAt")),
        "source": raw.get("source"),
    }


def _agreement_facts(
    agreement: dict[str, Any],
    asset: dict[str, Any],
    customer: dict[str, Any],
    received: date | None,
    gaps: list[str],
) -> dict[str, Any]:
    """Flatten the agreement into the fields the decision layer needs, plus observations."""
    effective_from = _parse_date(agreement.get("effectiveFrom"))
    effective_to = _parse_date(agreement.get("effectiveTo"))

    before_start = bool(received and effective_from and received < effective_from)
    after_end = bool(received and effective_to and received > effective_to)

    in_force: bool | None
    if received is None or effective_from is None or effective_to is None:
        in_force = None  # Cannot be established, which is not the same as False.
        gaps.append("Agreement effective window or receivedAt could not be parsed")
    else:
        in_force = not before_start and not after_end

    agreement_customer = agreement.get("customerId")
    customer_matches = agreement_customer == customer.get("id")
    if not customer_matches:
        gaps.append(
            f"Agreement {agreement.get('id')} belongs to {agreement_customer}, "
            f"but asset {asset.get('id')} belongs to {customer.get('id')}"
        )
    if before_start:
        gaps.append(
            f"Request received {received} is before agreement {agreement.get('id')} "
            f"starts {effective_from}"
        )
    if after_end:
        gaps.append(
            f"Request received {received} is after agreement {agreement.get('id')} "
            f"ended {effective_to}"
        )

    return {
        "contractRef": agreement.get("id"),
        "customerId": agreement_customer,
        "status": agreement.get("status"),
        "serviceMode": agreement.get("serviceMode"),
        "responseHours": agreement.get("responseHours"),
        "effectiveFrom": agreement.get("effectiveFrom"),
        "effectiveTo": agreement.get("effectiveTo"),
        "supersedes": agreement.get("supersedes"),
        "note": agreement.get("note"),
        # Asset-level coverage can disagree with the agreement; both are reported as-is.
        "assetCoverage": asset.get("coverage"),
        "inForceAtReceivedAt": in_force,
        "receivedBeforeStart": before_start,
        "receivedAfterEnd": after_end,
        "customerMatchesAsset": customer_matches,
    }


def _match_technicians(
    technicians: list[dict[str, Any]],
    asset: dict[str, Any],
    site: dict[str, Any],
) -> list[TechnicianMatch]:
    """Technicians with the asset's skill AND every required certification.

    Availability and city are reported per technician, not filtered on: whether a
    distant or busy technician counts is the decision layer's rule, not a fact.
    """
    asset_type = asset.get("type")
    required = set(asset.get("requiredCertifications") or [])
    site_city = site.get("city")

    matches: list[TechnicianMatch] = []
    for tech in technicians:
        skills = list(tech.get("skills") or [])
        certifications = list(tech.get("certifications") or [])
        if asset_type not in skills:
            continue
        if not required.issubset(set(certifications)):
            continue
        matches.append(
            TechnicianMatch(
                technicianId=tech.get("id", ""),
                name=tech.get("name", ""),
                city=tech.get("city", ""),
                skills=skills,
                certifications=certifications,
                available=bool(tech.get("available")),
                sameCityAsSite=bool(site_city) and tech.get("city") == site_city,
                currentAssignment=tech.get("currentAssignment"),
            )
        )
    return matches


async def gather_asset_evidence(
    client: NorthstarClient,
    asset_id: str | None,
    received_at: str | None,
) -> AssetEvidence:
    """Collect everything known about one asset. Reports facts and gaps; decides nothing.

    Outcomes:
      no_asset_id   - nothing to look up
      unknown_asset - the id is not in the records
      lookup_failed - a dependency did not answer after bounded retries
      resolved      - facts gathered (which may still contain gaps)
    """
    if not asset_id:
        return AssetEvidence(
            outcome="no_asset_id",
            receivedAt=received_at,
            gaps=["No asset identifier supplied; nothing to look up"],
        )

    evidence = AssetEvidence(outcome="resolved", assetId=asset_id, receivedAt=received_at)
    received = _parse_date(received_at)

    # Identity first: without the asset, the remaining lookups have no subject.
    try:
        customers = await client.customers()
    except LookupFailure as exc:
        logger.warning("evidence: customers lookup failed for %s (%s)", asset_id, exc.detail)
        return AssetEvidence(
            outcome="lookup_failed",
            assetId=asset_id,
            receivedAt=received_at,
            failedLookups=["customers"],
            gaps=[f"Customer lookup failed: {exc.detail}"],
        )

    located = _index_assets(customers).get(asset_id)
    if located is None:
        return AssetEvidence(
            outcome="unknown_asset",
            assetId=asset_id,
            receivedAt=received_at,
            gaps=[f"Asset {asset_id} is not present in the customer records"],
            sourceReferences=[{"type": "route", "id": "customers"}],
        )

    asset, site, customer = located
    evidence.asset = asset
    evidence.site = {k: v for k, v in site.items() if k != "assets"}
    evidence.customer = {
        "id": customer.get("id"),
        "name": customer.get("name"),
        # Aliases matter: customers appear under legal and trading names.
        "aliases": list(customer.get("aliases") or []),
        "status": customer.get("status"),
    }
    evidence.sourceReferences.extend(
        [
            {"type": "customer", "id": customer.get("id")},
            {"type": "site", "id": site.get("id")},
            {"type": "asset", "id": asset.get("id")},
        ]
    )

    if not asset.get("nickname"):
        evidence.gaps.append(f"Asset {asset_id} has no nickname recorded")

    # Agreement, taken from the asset's own contractRef.
    contract_ref = asset.get("contractRef")
    if not contract_ref:
        evidence.gaps.append(f"Asset {asset_id} has no contractRef")
    else:
        try:
            agreements = await client.agreements(q=contract_ref)
        except LookupFailure as exc:
            logger.warning("evidence: agreements lookup failed for %s (%s)", contract_ref, exc.detail)
            evidence.failedLookups.append("agreements")
            evidence.gaps.append(f"Agreement lookup failed: {exc.detail}")
        else:
            match = next((a for a in agreements if a.get("id") == contract_ref), None)
            if match is None:
                evidence.gaps.append(
                    f"Agreement {contract_ref} referenced by asset {asset_id} was not found"
                )
            else:
                evidence.agreement = _agreement_facts(
                    match, asset, customer, received, evidence.gaps
                )
                evidence.sourceReferences.append({"type": "agreement", "id": contract_ref})

    # Technicians. Read live every time: availability changes during a shift.
    try:
        technicians = await client.technicians()
    except LookupFailure as exc:
        logger.warning("evidence: technicians lookup failed (%s)", exc.detail)
        evidence.failedLookups.append("technicians")
        evidence.gaps.append(f"Technician lookup failed: {exc.detail}")
    else:
        evidence.qualifiedTechnicians = _match_technicians(technicians, asset, site)
        evidence.sourceReferences.append({"type": "route", "id": "technicians"})
        if not evidence.qualifiedTechnicians:
            evidence.gaps.append(
                f"No technician holds skill '{asset.get('type')}' with certifications "
                f"{sorted(asset.get('requiredCertifications') or [])}"
            )

    # Open work orders on this asset. q= is ignored here, so filter client-side.
    try:
        work_orders = await client.work_orders()
    except LookupFailure as exc:
        logger.warning("evidence: work-orders lookup failed (%s)", exc.detail)
        evidence.failedLookups.append("work-orders")
        evidence.gaps.append(f"Work-order lookup failed: {exc.detail}")
    else:
        normalised = [_normalise_work_order(w) for w in work_orders]
        evidence.openWorkOrders = [
            w
            for w in normalised
            if w["assetId"] == asset_id
            and (w["status"] or "").lower() not in CLOSED_WORK_ORDER_STATUSES
        ]
        evidence.sourceReferences.append({"type": "route", "id": "work-orders"})

    # Recent requests, carried for later duplicate work. q= is ignored here too.
    try:
        evidence.recentRequests = await client.requests()
        evidence.sourceReferences.append({"type": "route", "id": "requests"})
    except LookupFailure as exc:
        logger.warning("evidence: requests lookup failed (%s)", exc.detail)
        evidence.failedLookups.append("requests")
        evidence.gaps.append(f"Request lookup failed: {exc.detail}")

    return evidence
