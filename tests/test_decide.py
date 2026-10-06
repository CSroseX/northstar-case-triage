"""Decision tests: hand-written facts + saved evidence, no model and no network.

Facts are written by hand here on purpose. They are what the model will later produce,
so writing them out keeps this layer's contract explicit and lets the decision be tested
without spending model budget.
"""

from __future__ import annotations

import pytest

from app.decide import _same_fault, decide, find_hazard_words, quote_is_in_text
from app.evidence import AssetEvidence, gather_asset_evidence
from app.facts import RequestFacts, parse_facts
from tests.case_fixtures import ALL_CASES, Case
from tests.saved_api import offline_client

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _evidence(asset_id: str | None, received_at: str) -> AssetEvidence:
    async with offline_client() as client:
        return await gather_asset_evidence(client, asset_id, received_at)


def _case(case_id: str) -> Case:
    """Look up a shared fixture so the tests and scripts/demo.py stay in step."""
    for case in ALL_CASES:
        if case.id == case_id:
            return case
    raise KeyError(case_id)


# --- every fixture case reaches its expected status ---------------------
# Facts and expected statuses live in tests/case_fixtures.py, shared with scripts/demo.py.


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
async def test_case_reaches_expected_status(case: Case) -> None:
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert result.status == case.expectedStatus, (
        f"{case.id}: expected {case.expectedStatus}, got {result.status} "
        f"({'; '.join(r.code for r in result.reasons)})"
    )


# --- what each outcome must carry, beyond the status --------------------


async def test_planned_work_is_never_dispatched_at_intake() -> None:
    case = _case("VIS-001")
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert result.recommendedTechnician is None
    assert any(r.code == "planned_not_dispatched" for r in result.reasons)


async def test_safety_escalation_preserves_the_customers_words() -> None:
    case = _case("VIS-003")
    # No asset resolved: escalation must not wait on identity.
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert "diesel smell" in result.safetyQuote
    assert any(r.policy == "POL-SAFETY-001" for r in result.reasons)


async def test_duplicate_links_the_earlier_request_and_open_job() -> None:
    case = _case("VIS-005")
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    # REQ-V001 is in the recent requests for this asset: email plus portal, one job.
    assert "REQ-V001" in result.linkedRequests
    assert result.linkedWorkOrders == ["WO-9294"]


async def test_dispatch_recommends_a_free_local_technician_provisionally() -> None:
    case = _case("VIS-006")
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert result.recommendedTechnician is not None
    assert result.recommendedTechnician.available is True
    assert result.recommendedTechnician.sameCityAsSite is True
    assert any("re-checked" in w for w in result.warnings)


async def test_resource_escalation_recommends_nobody() -> None:
    case = _case("VIS-007")
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert result.recommendedTechnician is None
    assert any(r.code == "no_available_qualified_technician" for r in result.reasons)


async def test_coverage_answer_cites_the_amendment() -> None:
    case = _case("VIS-008")
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert any("CON-012-A2" in r.detail for r in result.reasons)


async def test_remote_only_never_reaches_dispatch() -> None:
    case = _case("REQ-8271")
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert any(r.code == "remote_only_cannot_dispatch" for r in result.reasons)
    assert result.recommendedTechnician is None


async def test_water_near_ups_is_not_dispatched_despite_being_covered() -> None:
    case = _case("REQ-8279")
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert "water on the floor" in result.safetyQuote
    assert result.recommendedTechnician is None


async def test_hazard_word_with_absent_signal_is_held_for_a_question() -> None:
    """The model must not be the reason a hazard is missed (CLAUDE.md rule 2)."""
    case = _case("HAZARD-MISSED")
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert any(r.code == "safety_wording_unconfirmed" for r in result.reasons)
    assert result.safetyQuestion
    # Covered, with technicians available, and still held.
    assert result.recommendedTechnician is None


# --- supporting behaviour ----------------------------------------------


def test_customer_ruling_out_hazards_is_cleared_by_the_denial_not_the_keywords() -> None:
    """A customer ruling a hazard out is cleared by the model's 'denied' signal.

    The keyword backstop does NOT try to read negation: "there is no smoke" still
    contains the word "smoke", and the backstop is deliberately blunt. What keeps
    these requests in normal triage is that the model reports safetySignal="denied"
    with a quote we can verify against the body, so find_hazard_words is never
    consulted for them.
    """
    assert find_hazard_words("There is no smoke, water or unusual smell.")
    assert find_hazard_words("There is no smoke or smell.")


def test_negation_no_longer_suppresses_a_real_hazard() -> None:
    """The negation window used to hide the hazard in this sentence. It must not."""
    assert find_hazard_words("the alarm is not working and there's smoke")


def test_real_hazard_wording_is_detected() -> None:
    """Every phrasing POL-SAFETY-001 would escalate, taken from the real dataset."""
    assert find_hazard_words(
        "a strong diesel smell and unusual vibration near the backup generator"
    )
    assert find_hazard_words("Security can see smoke from the generator enclosure")
    assert find_hazard_words("There is water on the floor beside instrument UPS AST-1102")
    assert find_hazard_words("staff feel light-headed in the plant room")
    assert find_hazard_words(
        "The freezer has stopped and there is a burning smell coming from the panel."
    )
    assert find_hazard_words("the alarm is not working and there's smoke")


def test_normal_service_wording_is_not_a_hazard() -> None:
    """A bare commodity noun is an ordinary booking, not one of the four triggers."""
    assert find_hazard_words(
        "Please arrange the covered quarterly service for Main DG at our Whitefield "
        "warehouse. The asset label reads AST-101."
    ) == []
    assert find_hazard_words("Fulfilment DG B AST-1002 failed a remote start test.") == []
    assert find_hazard_words(
        "Please plan the quarterly service for auditorium chiller AST-1302 next week "
        "under amendment CON-129-A1."
    ) == []
    assert find_hazard_words("Dialysis backup DG AST-901 shows controller fault 118.") == []
    assert find_hazard_words(
        "The packing hall AC has developed a rattling noise. Cooling is normal."
    ) == []
    assert find_hazard_words("Please book the annual diesel generator service.") == []
    assert find_hazard_words("Quarterly service for the gas plant, please.") == []
    assert find_hazard_words("Water pump maintenance is due this month.") == []


def test_electric_shock_risk_is_detected_as_a_deliberate_extension() -> None:
    """Not one of POL-SAFETY-001's four triggers; added by our own decision."""
    assert find_hazard_words("an operator got a shock from the panel")
    assert find_hazard_words("there are exposed wires near the unit")
    assert find_hazard_words("live cables are hanging near the walkway")
    assert find_hazard_words("risk of electrocution at the distribution board")


def test_shock_in_a_mechanical_sense_is_not_a_hazard() -> None:
    """'shock absorber' and 'shocked the system' are ordinary equipment language."""
    assert find_hazard_words("Shock absorber replacement on the mounting.") == []
    assert find_hazard_words("The compressor shocked the system with a pressure spike.") == []


# --- a denial has to be the customer's own words ------------------------


async def test_denied_signal_with_an_unverifiable_quote_is_held() -> None:
    """The model cannot clear a hazard with words the customer never wrote."""
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="denied",
        safetyQuote="The customer confirms there is no hazard of any kind.",
        intent="breakdown",
        symptomSummary="stopped cooling",
        requiresOnsite=True,
        sameFaultAsExisting="different",
    )
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(facts, ev, "Freezer plant 2 stopped. AST-302 at SITE-021 has stopped cooling.")

    assert result.status == "clarification_required"
    assert any(r.code == "safety_denial_unverified" for r in result.reasons)
    assert any(r.policy == "POL-SAFETY-001" for r in result.reasons)
    assert result.safetyQuestion


async def test_denied_signal_with_an_empty_quote_is_held() -> None:
    """A denial with nothing quoted behind it is not the customer's denial."""
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="denied",
        safetyQuote="",
        intent="breakdown",
        symptomSummary="stopped cooling",
        requiresOnsite=True,
        sameFaultAsExisting="different",
    )
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(facts, ev, "AST-302 at SITE-021 has stopped cooling.")

    assert result.status == "clarification_required"
    assert any(r.code == "safety_denial_unverified" for r in result.reasons)


async def test_denied_signal_with_a_verified_quote_proceeds() -> None:
    """The quote is in the body, so the customer's denial stands and triage continues."""
    case = _case("VIS-006")
    ev = await _evidence(case.assetId, case.receivedAt)
    result = decide(case.facts, ev, f"{case.subject}. {case.body}")

    assert result.status == "dispatch_ready"
    assert not any(r.code == "safety_denial_unverified" for r in result.reasons)


def test_quote_verification_tolerates_punctuation_and_spacing() -> None:
    """Minor punctuation and whitespace differences must not fail a real quote."""
    body = "AST-302 at SITE-021 has stopped cooling. There is no smoke, water or unusual smell."

    assert quote_is_in_text("There is no smoke, water or unusual smell.", body)
    # Same words, different punctuation, casing and spacing.
    assert quote_is_in_text("there is no  smoke water or unusual smell", body)
    # The quote may sit anywhere in the "subject. body" string the caller builds.
    assert quote_is_in_text("No one is in danger", "Technician needed. No one is in danger today.")
    # Not so loose that unrelated wording passes.
    assert not quote_is_in_text("There is no hazard at all", body)
    assert not quote_is_in_text("", body)


# --- the duplicate judgement comes from the model -----------------------


async def test_same_fault_honours_the_models_assessment() -> None:
    """same / different / unclear each map straight through; no word counting."""
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    assert ev.openWorkOrders  # there is an open job to compare against

    def _facts(assessment: str) -> RequestFacts:
        return RequestFacts(
            assetMentions=["AST-302"],
            safetySignal="absent",
            intent="breakdown",
            symptomSummary="stopped cooling",
            sameFaultAsExisting=assessment,
        )

    assert _same_fault(_facts("same"), ev) is True
    assert _same_fault(_facts("different"), ev) is False
    assert _same_fault(_facts("unclear"), ev) is None


async def test_same_fault_without_an_assessment_does_not_guess() -> None:
    """Unassessed with something open is a human's call, not a guess either way."""
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="absent",
        intent="breakdown",
        symptomSummary="stopped cooling",
    )

    assert _same_fault(facts, ev) is None


async def test_same_fault_is_false_with_nothing_open_and_nothing_referenced() -> None:
    ev = await _evidence("AST-205", "2026-09-20T09:38:00Z")
    assert not ev.openWorkOrders
    facts = RequestFacts(
        assetMentions=["AST-205"],
        safetySignal="absent",
        intent="breakdown",
        symptomSummary="generator offline",
    )

    assert _same_fault(facts, ev) is False


async def test_failed_lookup_cannot_block_a_safety_escalation() -> None:
    """Safety is checked before lookups (POL-SAFETY-001)."""
    facts = RequestFacts(
        safetySignal="affirmed",
        safetyQuote="smoke from the enclosure",
        intent="breakdown",
    )
    broken = AssetEvidence(
        outcome="lookup_failed",
        assetId="AST-101",
        failedLookups=["customers"],
    )
    result = decide(facts, broken, "Security can see smoke from the generator enclosure.")

    assert result.status == "human_escalation_required"


async def test_lookup_failure_without_safety_is_failed() -> None:
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="denied",
        # Verifiable against the body below, so the denial stands and the lookup
        # failure is the reportable problem.
        safetyQuote="No smoke or smell.",
        intent="breakdown",
    )
    broken = AssetEvidence(
        outcome="lookup_failed",
        assetId="AST-101",
        failedLookups=["customers"],
    )
    result = decide(facts, broken, "AST-101 has stopped. No smoke or smell.")

    assert result.status == "failed"
    assert any(r.code == "lookup_failed" for r in result.reasons)


async def test_unreadable_model_output_takes_the_cautious_path() -> None:
    facts = parse_facts(None)
    assert facts.modelUnavailable is True
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(facts, ev, "AST-302 has stopped cooling.")

    assert result.status == "clarification_required"
    assert any(r.code == "safety_unverified" for r in result.reasons)


def test_malformed_safety_signal_becomes_ambiguous_not_absent() -> None:
    facts = parse_facts({"safetySignal": "probably fine", "intent": "breakdown"})
    assert facts.safetySignal == "ambiguous"


async def test_two_assets_mentioned_is_clarification() -> None:
    """Two candidates, no basis to choose: ask, don't pick."""
    facts = RequestFacts(
        assetMentions=["AST-101", "AST-102"],
        safetySignal="absent",
        intent="breakdown",
        symptomSummary="one of the units at the warehouse is down",
    )
    ev = await _evidence("AST-101", "2026-09-20T08:42:00Z")
    result = decide(facts, ev, "One of our warehouse units is down.")

    assert result.status == "clarification_required"
    assert any(r.code == "ambiguous_asset_reference" for r in result.reasons)


async def test_unclear_duplicate_goes_to_a_human() -> None:
    """A follow-up against an open job we cannot match either way is a human's call."""
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="absent",
        intent="follow_up",
        symptomSummary="checking on the job raised earlier",
        referencedRequests=["REQ-9999"],  # not in the records
    )
    ev = await _evidence("AST-101", "2026-09-20T09:24:00Z")
    result = decide(facts, ev, "Any update on the job we raised earlier?")

    assert result.status == "clarification_required"
    assert any(r.code == "possible_duplicate_unclear" for r in result.reasons)
    assert result.linkedWorkOrders == ["WO-9294"]


async def test_different_fault_on_same_asset_is_not_a_duplicate() -> None:
    """An open job on AST-302 (compressor cycling) must not block a different fault."""
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="denied",
        safetyQuote="No smoke, water or smell.",
        intent="breakdown",
        symptomSummary="door seal damaged and ice building on the frame",
        requiresOnsite=True,
        # The model read this as a different fault from the open compressor job.
        sameFaultAsExisting="different",
    )
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(facts, ev, "The door seal on AST-302 is damaged. No smoke, water or smell.")

    assert result.status == "dispatch_ready"
    assert result.recommendedTechnician is not None


async def test_coverage_question_is_not_held_by_an_open_job() -> None:
    """A request reporting no fault cannot duplicate one, whatever is open on the asset."""
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="absent",
        intent="coverage_question",
        symptomSummary="asking which contract version applies",
        requiresOnsite=False,
        sameFaultAsExisting="",  # the model has no fault to compare
    )
    ev = await _evidence("AST-101", "2026-09-20T09:46:00Z")
    assert ev.openWorkOrders  # WO-9294 is open on this asset
    result = decide(facts, ev, "Please confirm coverage for AST-101 using the August amendment.")

    assert result.status == "covered_action"


async def test_planned_service_is_not_held_by_an_open_job() -> None:
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="absent",
        intent="planned_service",
        symptomSummary="quarterly service booking",
        requiresOnsite=False,
        sameFaultAsExisting="",
    )
    ev = await _evidence("AST-101", "2026-09-20T08:42:00Z")
    result = decide(facts, ev, "Please arrange the covered quarterly service for Main DG.")

    assert result.status == "covered_action"


async def test_a_breakdown_with_an_open_job_and_no_assessment_still_asks_a_human() -> None:
    """The fallback stays cautious where a fault IS reported."""
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="denied",
        safetyQuote="no smoke or smell",
        intent="breakdown",
        symptomSummary="generator will not start",
        requiresOnsite=True,
        sameFaultAsExisting="",
    )
    ev = await _evidence("AST-101", "2026-09-20T09:46:00Z")
    result = decide(facts, ev, "The generator will not start. There is no smoke or smell.")

    assert result.status == "clarification_required"
    assert any(r.code == "possible_duplicate_unclear" for r in result.reasons)
