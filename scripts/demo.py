#!/usr/bin/env python
"""Walk the evidence lookup and the decision over real cases, offline.

Reads the saved responses in exploration/raw/ through a mock transport: no live Northstar
calls and no model calls. The facts and expected statuses come from tests/case_fixtures.py,
the same definitions the tests use, so this demo and the test suite cannot disagree.

    python scripts/demo.py            # writes exploration/demo-output.md
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.decide import Decision, decide  # noqa: E402
from app.evidence import AssetEvidence, gather_asset_evidence  # noqa: E402
from tests.case_fixtures import ALL_CASES, EXTRA_CASES, LOOKUP_ASSETS, Case  # noqa: E402
from tests.saved_api import offline_client  # noqa: E402

OUTPUT_PATH = PROJECT_ROOT / "exploration" / "demo-output.md"


def _fmt(value: object, dash: str = "—") -> str:
    """Render a value for a table cell."""
    if value is None or value == "":
        return dash
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


# --- lookup section -----------------------------------------------------


def render_lookup(evidence: AssetEvidence, asset_id: str, received_at: str) -> list[str]:
    out: list[str] = [f"### {asset_id}", ""]
    out.append(f"Request time: `{received_at}` · Outcome: **{evidence.outcome}**")
    out.append("")

    if not evidence.resolved:
        for gap in evidence.gaps:
            out.append(f"- {gap}")
        out.append("")
        return out

    customer = evidence.customer or {}
    site = evidence.site or {}
    asset = evidence.asset or {}
    aliases = ", ".join(customer.get("aliases") or []) or "—"

    out.append(
        f"**{customer.get('name')}** ({customer.get('id')}, {customer.get('status')}) · "
        f"also known as {aliases}"
    )
    out.append(
        f"{site.get('name')} ({site.get('id')}, {site.get('city')}) · "
        f"{asset.get('nickname') or 'no nickname'} — {asset.get('type')}, "
        f"coverage {asset.get('coverage')}"
    )
    out.append("")

    agreement = evidence.agreement
    if agreement is None:
        out.append("_No agreement resolved._")
        out.append("")
    else:
        in_force = agreement.get("inForceAtReceivedAt")
        in_force_text = {True: "in force", False: "NOT in force", None: "cannot tell"}[in_force]
        out.append(
            f"**Agreement {agreement.get('contractRef')}** — {agreement.get('status')}, "
            f"{agreement.get('serviceMode')}, {agreement.get('responseHours')}h response · "
            f"{agreement.get('effectiveFrom')} to {agreement.get('effectiveTo')} · "
            f"**{in_force_text}** at the request time"
        )
        if agreement.get("supersedes"):
            out.append(f"Supersedes `{agreement['supersedes']}` (not retrievable as a record).")
        if agreement.get("note"):
            out.append(f"Note on the record: _{agreement['note']}_")
        out.append("")

    if evidence.qualifiedTechnicians:
        out.append("| Technician | City | Free | Same city | On |")
        out.append("|---|---|---|---|---|")
        for tech in evidence.qualifiedTechnicians:
            out.append(
                f"| {tech.technicianId} {tech.name} | {tech.city} | {_fmt(tech.available)} | "
                f"{_fmt(tech.sameCityAsSite)} | {_fmt(tech.currentAssignment)} |"
            )
        required = ", ".join(asset.get("requiredCertifications") or []) or "none"
        dispatchable = [t for t in evidence.qualifiedTechnicians if t.available and t.sameCityAsSite]
        out.append("")
        out.append(
            f"Needs skill `{asset.get('type')}` + certifications `{required}`. "
            f"{len(evidence.qualifiedTechnicians)} qualify, "
            f"**{len(dispatchable)} free in {site.get('city')}**."
        )
    else:
        out.append("_No technician holds the required skill and certifications._")
    out.append("")

    if evidence.openWorkOrders:
        out.append("Open jobs on this asset:")
        for wo in evidence.openWorkOrders:
            out.append(
                f"- `{wo['id']}` ({wo['status']}) — {wo['summary']} · "
                f"from {wo['requestId']}, technician {_fmt(wo['technicianId'])}"
            )
    else:
        out.append("No open jobs on this asset.")
    out.append("")

    if evidence.gaps:
        out.append("Flags:")
        for gap in evidence.gaps:
            out.append(f"- {gap}")
        out.append("")

    return out


# --- decision section ---------------------------------------------------


def render_decision(case: Case, decision: Decision) -> list[str]:
    matched = decision.status == case.expectedStatus
    mark = "✅" if matched else "❌"
    out: list[str] = [f"### {mark} {case.id} — {case.subject}", ""]
    out.append(f"> {case.body}")
    out.append("")

    facts = case.facts
    quote = f' · quote: "{facts.safetyQuote}"' if facts.safetyQuote else ""
    mentions = ", ".join(facts.assetMentions) or "none"
    refs = ", ".join(facts.referencedRequests) or "none"
    out.append(
        f"**Facts given:** intent `{facts.intent}` · safety `{facts.safetySignal}`{quote}"
    )
    out.append(
        f"assets mentioned: {mentions} · refers back to: {refs} · "
        f"wants someone on site: {_fmt(facts.requiresOnsite, 'not stated')}"
    )
    out.append(f"symptoms: _{facts.symptomSummary or '—'}_")
    out.append("")

    if matched:
        out.append(f"**Status: `{decision.status}`** (expected `{case.expectedStatus}`)")
    else:
        out.append(
            f"**Status: `{decision.status}`** — MISMATCH, expected `{case.expectedStatus}`"
        )
    out.append("")

    out.append("Why:")
    for reason in decision.reasons:
        out.append(f"- {reason.detail} _({reason.policy})_")

    if decision.recommendedTechnician:
        tech = decision.recommendedTechnician
        out.append(
            f"- Recommended technician: **{tech.technicianId} {tech.name}** "
            f"({tech.city}) — provisional until re-checked at creation time"
        )
    if decision.safetyQuestion:
        out.append(f"- Question to ask: _{decision.safetyQuestion}_")
    if decision.linkedWorkOrders or decision.linkedRequests:
        links = ", ".join(decision.linkedWorkOrders + decision.linkedRequests)
        out.append(f"- Linked to: {links}")
    if decision.missingInformation:
        out.append(f"- Still needed: {'; '.join(decision.missingInformation)}")
    for warning in decision.warnings:
        if warning:
            out.append(f"- ⚠️ {warning}")

    if case.note:
        out.append("")
        out.append(f"_{case.note}_")
    out.append("")
    return out


# --- main ---------------------------------------------------------------


async def main() -> int:
    lines: list[str] = [
        "# Demo: evidence lookup and decision on real cases",
        "",
        "Generated by `scripts/demo.py`. Every record comes from the saved responses in",
        "`exploration/raw/` through a mock transport — no live Northstar calls, no model",
        "calls. Facts and expected statuses come from `tests/case_fixtures.py`, shared with",
        "the test suite.",
        "",
        "---",
        "",
        "## Part 1 — What the lookup finds",
        "",
    ]

    async with offline_client() as client:
        for asset_id, received_at in LOOKUP_ASSETS:
            evidence = await gather_asset_evidence(client, asset_id, received_at)
            lines.extend(render_lookup(evidence, asset_id, received_at))

        lines.extend(["---", "", "## Part 2 — What the decision picks", ""])

        results: list[tuple[Case, Decision]] = []
        for case in ALL_CASES:
            evidence = await gather_asset_evidence(client, case.assetId, case.receivedAt)
            decision = decide(case.facts, evidence, f"{case.subject}. {case.body}")
            results.append((case, decision))

    matched = sum(1 for case, decision in results if decision.status == case.expectedStatus)
    lines.append(
        f"**{matched} of {len(results)} cases reached the expected status.**"
    )
    lines.append("")

    lines.append("| Case | Expected | Got | |")
    lines.append("|---|---|---|---|")
    for case, decision in results:
        ok = decision.status == case.expectedStatus
        lines.append(
            f"| {case.id} | `{case.expectedStatus}` | `{decision.status}` | "
            f"{'✅' if ok else '❌'} |"
        )
    lines.append("")

    lines.extend(["### The eight runner cases", ""])
    for case, decision in results:
        if case in EXTRA_CASES:
            continue
        lines.extend(render_decision(case, decision))

    lines.extend(["---", "", "### The four extra cases", ""])
    for case, decision in results:
        if case not in EXTRA_CASES:
            continue
        lines.extend(render_decision(case, decision))

    OUTPUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{matched}/{len(results)} cases reached the expected status.")
    print(f"Written to {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0 if matched == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
