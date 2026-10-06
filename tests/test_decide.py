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
    assert result.linkedRequests == ["REQ-V001"]
    # WO-9294 was raised from REQ-8254 ("Main DG failed remote start"), a different
    # incident, so it is context rather than something this request is linked to.
    assert result.linkedWorkOrders == []
    assert result.otherOpenWorkOrders == ["WO-9294"]


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


async def test_an_unclear_repeat_inside_the_response_window_is_a_duplicate() -> None:
    """A follow-up while we are still inside the promised window is the same incident."""
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="absent",
        intent="follow_up",
        symptomSummary="checking on the job raised earlier",
        referencedRequests=[],
        sameFaultAsExisting="unclear",
    )
    # REQ-V001 opened this conversation at 08:42, 64 minutes before, inside CON-012-A2's 4h.
    ev = await _evidence("AST-101", "2026-09-20T09:46:00Z")
    result = decide(facts, ev, "Any update on the job we raised earlier?")

    assert result.status == "duplicate_detected"
    # Linked to the original report, not the most recent chase.
    assert "REQ-V001" in result.linkedRequests


async def test_a_repeat_after_the_response_window_goes_to_a_human() -> None:
    """Outside the window it is a coordinator's call, and we never claim a missed promise."""
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="absent",
        intent="follow_up",
        symptomSummary="still waiting on the Main DG job",
        referencedRequests=[],
        sameFaultAsExisting="same",
    )
    # 09:24 + 4h window; this arrives the next morning, long after it closed.
    ev = await _evidence("AST-101", "2026-09-21T09:00:00Z")
    result = decide(facts, ev, "Still waiting on the Main DG job from yesterday.")

    assert result.status == "human_escalation_required"
    assert any(r.code == "follow_up_after_response_window" for r in result.reasons)
    # The wording must not assert that anyone missed anything.
    joined = " ".join(r.detail for r in result.reasons).lower()
    assert "missed" not in joined and "breach" not in joined


async def test_a_repeat_with_no_response_window_goes_to_a_human() -> None:
    """Nothing to judge the timing against, so a human decides."""
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="absent",
        intent="follow_up",
        symptomSummary="chasing the earlier request",
        referencedRequests=[],
        sameFaultAsExisting="same",
    )
    ev = await _evidence("AST-101", "2026-09-20T09:46:00Z")
    # Strip the response commitment: the agreement no longer states one.
    ev.agreement = {**(ev.agreement or {}), "responseHours": None}
    result = decide(facts, ev, "Chasing the request we raised earlier.")

    assert result.status == "human_escalation_required"
    assert any(r.code == "follow_up_window_unknown" for r in result.reasons)


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


async def test_a_different_fault_continues_through_the_normal_checks() -> None:
    """When the model says it is a different fault, nothing about the repeat applies."""
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="denied",
        safetyQuote="no smoke or smell",
        intent="breakdown",
        symptomSummary="generator will not start",
        requiresOnsite=True,
        sameFaultAsExisting="different",
    )
    ev = await _evidence("AST-101", "2026-09-20T09:46:00Z")
    result = decide(facts, ev, "The generator will not start. There is no smoke or smell.")

    # Straight through to dispatch: a new problem is treated like any other request.
    assert result.status == "dispatch_ready"



# --- earlier work on the equipment (item 3) -----------------------------


async def test_a_request_about_previous_work_is_not_dispatched() -> None:
    """OPS-INTAKE-003: check for the same incident before raising anything new."""
    facts = RequestFacts(
        assetMentions=["AST-1001"],
        safetySignal="denied",
        safetyQuote="Cooling remains within range",
        intent="breakdown",
        symptomSummary="weak airflow again",
        requiresOnsite=True,
        sameFaultAsExisting="different",
        refersToPreviousWork=True,
    )
    ev = await _evidence("AST-1001", "2026-09-20T11:19:00Z")
    result = decide(
        facts,
        ev,
        "AHU 2 airflow weak. Sortation hall AHU 2, AST-1001, has weak airflow again. "
        "Cooling remains within range. Please review the June visit before assigning this.",
    )

    assert result.status == "human_escalation_required"
    assert any(r.code == "refers_to_previous_work" for r in result.reasons)
    assert result.recommendedTechnician is None
    # The matching history is attached for the coordinator.
    assert result.relatedHistory
    assert all(h["id"] for h in result.relatedHistory)


async def test_previous_work_does_not_override_a_coverage_problem() -> None:
    """Entitlement is still checked first: a suspended account is account review."""
    facts = RequestFacts(
        assetMentions=["AST-801"],
        safetySignal="absent",
        intent="breakdown",
        symptomSummary="chiller needs looking at again",
        requiresOnsite=True,
        sameFaultAsExisting="different",
        refersToPreviousWork=True,
    )
    ev = await _evidence("AST-801", "2026-09-20T10:51:00Z")
    result = decide(facts, ev, "Please check what was done last time on AST-801.")

    assert result.status == "account_review_required"


# --- answers to our safety question (item 4) ----------------------------


async def test_an_uncertain_safety_answer_escalates_rather_than_asking_again() -> None:
    """Meera: 'not sure' stays with a human, it never drifts back to normal triage."""
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="ambiguous",
        safetyQuote="not sure",
        intent="breakdown",
        symptomSummary="cannot confirm whether there is a smell",
        isSafetyAnswer=True,
        safetyAnswerUncertain=True,
    )
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(facts, ev, "Not sure, nobody has been down there to check.")

    assert result.status == "human_escalation_required"
    assert any(r.code == "safety_answer_unresolved" for r in result.reasons)
    # Never a second question.
    assert result.safetyQuestion == ""


async def test_a_safety_answer_with_an_unreported_certainty_still_escalates() -> None:
    """A missing safetyAnswerUncertain is treated cautiously, not as 'resolved'."""
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="ambiguous",
        intent="breakdown",
        symptomSummary="reply to the safety question",
        isSafetyAnswer=True,
        safetyAnswerUncertain=None,
    )
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(facts, ev, "Replying about the freezer.")

    assert result.status == "human_escalation_required"


async def test_a_clear_safety_answer_returns_to_normal_triage() -> None:
    """'No smoke or smell, just the alarm' resolves it and triage continues."""
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="denied",
        safetyQuote="No smoke or smell, just the alarm.",
        intent="breakdown",
        symptomSummary="alarm only, no hazard",
        requiresOnsite=True,
        isSafetyAnswer=True,
        safetyAnswerUncertain=False,
        sameFaultAsExisting="different",
    )
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(facts, ev, "No smoke or smell, just the alarm.")

    assert result.status == "dispatch_ready"


async def test_a_hazard_word_in_the_answer_escalates_as_a_fresh_signal() -> None:
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="affirmed",
        safetyQuote="there is smoke coming from the panel",
        intent="breakdown",
        symptomSummary="smoke from the panel",
        isSafetyAnswer=True,
        safetyAnswerUncertain=False,
    )
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(facts, ev, "Checked now - there is smoke coming from the panel.")

    assert result.status == "human_escalation_required"
    assert any(r.code == "safety_signal_affirmed" for r in result.reasons)


# --- alternatives and the en-route warning (item 5) ---------------------


async def test_other_available_technicians_are_listed() -> None:
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="denied",
        safetyQuote="There is no smoke, water or unusual smell.",
        intent="breakdown",
        symptomSummary="stopped cooling",
        requiresOnsite=True,
        sameFaultAsExisting="different",
    )
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(
        facts, ev, "AST-302 has stopped cooling. There is no smoke, water or unusual smell."
    )

    assert result.status == "dispatch_ready"
    assert result.alternativeTechnicians
    chosen = result.recommendedTechnician.technicianId
    assert chosen not in [t.technicianId for t in result.alternativeTechnicians]
    assert all(t.available and t.sameCityAsSite for t in result.alternativeTechnicians)


async def test_a_technician_already_on_the_way_is_flagged() -> None:
    """AST-302 has WO-9290 assigned to TECH-05."""
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="denied",
        safetyQuote="There is no smoke, water or unusual smell.",
        intent="breakdown",
        symptomSummary="door seal damaged",
        requiresOnsite=True,
        sameFaultAsExisting="different",
    )
    ev = await _evidence("AST-302", "2026-09-20T09:31:00Z")
    result = decide(
        facts, ev, "The door seal is damaged. There is no smoke, water or unusual smell."
    )

    assert result.status == "dispatch_ready"
    assert any("already assigned to WO-9290" in w for w in result.warnings)


async def test_an_unclear_follow_up_is_resolved_by_a_recent_request_for_the_asset() -> None:
    """Email plus portal, minutes apart: the records settle what the wording cannot.

    The second message says "the same AST-101 visit" but quotes no reference, so the
    model cannot extract one and may answer "unclear". An earlier request for the same
    asset shortly before is enough to treat it as one incident (OPS-INTAKE-003).
    """
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="absent",
        intent="follow_up",
        symptomSummary="follow-up on the Main DG service raised via the portal",
        referencedRequests=[],
        sameFaultAsExisting="unclear",
    )
    ev = await _evidence("AST-101", "2026-09-20T09:24:00Z")
    result = decide(
        facts,
        ev,
        "Following up: Main DG service. Kavya raised the Main DG service request from the "
        "portal a few minutes ago. This email is for the same AST-101 visit.",
    )

    assert result.status == "duplicate_detected"


async def test_an_unclear_follow_up_with_no_recent_request_still_asks_a_human() -> None:
    """Without a nearby request for the asset, "unclear" stays a human's call."""
    facts = RequestFacts(
        assetMentions=["AST-1201"],
        safetySignal="absent",
        intent="follow_up",
        symptomSummary="checking on something raised before",
        referencedRequests=[],
        sameFaultAsExisting="unclear",
        refersToPreviousWork=False,
    )
    # AST-1201 has no open job and no same-day request in the records.
    ev = await _evidence("AST-1201", "2026-09-20T11:52:00Z")
    result = decide(facts, ev, "Any update on AST-1201?")

    assert result.status != "duplicate_detected"


async def test_repeated_chasing_eventually_reaches_a_human() -> None:
    """The window runs from the ORIGINAL report, not from the last chase-up.

    A customer who chases every couple of hours must not stay inside the window forever.
    Measured from the nearest earlier request each chase would reset the clock; measured
    from the earliest, the conversation crosses the window and reaches a coordinator.
    """
    ev = await _evidence("AST-101", "2026-09-20T08:42:00Z")
    # One original report plus chases at +2h and +4h, all naming the same equipment.
    ev.recentRequests = [
        {
            "requestId": "REQ-ORIG",
            "receivedAt": "2026-09-20T08:00:00Z",
            "subject": "Main DG stopped",
            "body": "AST-101 has stopped.",
        },
        {
            "requestId": "REQ-CHASE-1",
            "receivedAt": "2026-09-20T10:00:00Z",
            "subject": "Any update?",
            "body": "Still waiting on AST-101.",
        },
        {
            "requestId": "REQ-CHASE-2",
            "receivedAt": "2026-09-20T12:00:00Z",
            "subject": "Chasing again",
            "body": "Nobody has been out to AST-101 yet.",
        },
    ]
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="absent",
        intent="follow_up",
        symptomSummary="still waiting",
        sameFaultAsExisting="same",
    )

    # Third chase at 13:00. The last chase was an hour ago — inside the 4h window — but
    # the original report was five hours ago, which is outside it.
    ev.receivedAt = "2026-09-20T13:00:00Z"
    result = decide(facts, ev, "Still nobody out to AST-101.")

    assert result.status == "human_escalation_required"
    assert any(r.code == "follow_up_after_response_window" for r in result.reasons)
    # Measured from the original, not the most recent chase.
    assert "REQ-ORIG" in result.linkedRequests


async def test_a_first_chase_inside_the_window_is_still_a_duplicate() -> None:
    """The change must not break the ordinary case: one chase, soon after, is linked."""
    ev = await _evidence("AST-101", "2026-09-20T09:24:00Z")
    ev.recentRequests = [
        {
            "requestId": "REQ-ORIG",
            "receivedAt": "2026-09-20T08:42:00Z",
            "subject": "Main DG scheduled service",
            "body": "Please arrange the quarterly service for AST-101.",
        }
    ]
    facts = RequestFacts(
        assetMentions=["AST-101"],
        safetySignal="absent",
        intent="follow_up",
        symptomSummary="same visit",
        sameFaultAsExisting="same",
    )
    result = decide(facts, ev, "This email is for the same AST-101 visit.")

    assert result.status == "duplicate_detected"
    assert "REQ-ORIG" in result.linkedRequests


async def test_a_duplicate_links_only_the_job_it_matched() -> None:
    """The bug: a repeat of REQ-V006 was linked to WO-9290, a different fault.

    AST-302 has WO-9290 open for "compressor cycling". A fresh "stopped cooling" report
    matches the REQ-V006 incident, not that job. Linking every open work order on the
    equipment tells the customer we have attached them to the wrong work.
    """
    ev = await _evidence("AST-302", "2026-09-20T09:40:00Z")
    ev.recentRequests = [
        {
            "requestId": "REQ-V006",
            "receivedAt": "2026-09-20T09:31:00Z",
            "subject": "Freezer plant 2 stopped",
            "body": "AST-302 at SITE-021 has stopped cooling.",
        }
    ]
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="denied",
        safetyQuote="no smoke, water or unusual smell",
        intent="breakdown",
        symptomSummary="stopped cooling",
        requiresOnsite=True,
        sameFaultAsExisting="same",
        sameFaultReference="REQ-V006",
    )
    result = decide(
        facts, ev, "AST-302 has stopped cooling. There is no smoke, water or unusual smell."
    )

    assert result.status == "duplicate_detected"
    # Linked to what it matched...
    assert result.linkedRequests == ["REQ-V006"]
    # ...and NOT to the unrelated compressor job.
    assert "WO-9290" not in result.linkedWorkOrders
    # which is still available as context for a coordinator.
    assert "WO-9290" in result.otherOpenWorkOrders


async def test_a_duplicate_reply_cites_the_matched_reference() -> None:
    """The reply must quote the matched request, not the first readable WO- it finds."""
    from app.replies import build_customer_reply

    ev = await _evidence("AST-302", "2026-09-20T09:40:00Z")
    ev.recentRequests = [
        {
            "requestId": "REQ-V006",
            "receivedAt": "2026-09-20T09:31:00Z",
            "subject": "Freezer plant 2 stopped",
            "body": "AST-302 at SITE-021 has stopped cooling.",
        }
    ]
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="denied",
        safetyQuote="no smoke, water or unusual smell",
        intent="breakdown",
        symptomSummary="stopped cooling",
        requiresOnsite=True,
        sameFaultAsExisting="same",
        sameFaultReference="REQ-V006",
    )
    decision = decide(
        facts, ev, "AST-302 has stopped cooling. There is no smoke, water or unusual smell."
    )
    draft = build_customer_reply(facts, ev, decision)

    assert "REQ-V006" in draft
    assert "WO-9290" not in draft


async def test_a_duplicate_that_references_an_open_job_links_that_job() -> None:
    """When the customer does point at an open job, that is the one we link."""
    ev = await _evidence("AST-302", "2026-09-20T09:40:00Z")
    facts = RequestFacts(
        assetMentions=["AST-302"],
        safetySignal="absent",
        intent="follow_up",
        symptomSummary="chasing the compressor job",
        referencedRequests=["WO-9290"],
        sameFaultAsExisting="same",
    )
    result = decide(facts, ev, "Any update on WO-9290?")

    assert result.linkedWorkOrders == ["WO-9290"]
    assert "WO-9290" not in result.otherOpenWorkOrders


# --- messages that are not service requests -----------------------------


async def test_a_message_that_is_not_a_service_request_asks_what_they_need() -> None:
    """"Hi, can I get your number" must not be asked for an equipment identifier."""
    facts = RequestFacts(
        assetMentions=[],
        safetySignal="absent",
        intent="other",
        # The live model writes a summary even for a non-request, so the rule must not
        # depend on it being blank.
        symptomSummary="request for contact number",
    )
    ev = AssetEvidence(outcome="no_asset_id")
    result = decide(facts, ev, "Hi, can I get your number")

    assert result.status == "clarification_required"
    assert any(r.code == "not_a_service_request" for r in result.reasons)
    assert result.missingInformation == [
        "Which equipment or service the customer needs help with"
    ]
    # Not the equipment-identifier ask.
    assert not any(r.code == "asset_not_identified" for r in result.reasons)


async def test_a_fault_without_equipment_still_asks_for_the_equipment() -> None:
    """The narrow case must not swallow a real report that omits the asset."""
    facts = RequestFacts(
        assetMentions=[],
        safetySignal="absent",
        intent="breakdown",
        symptomSummary="freezer not holding temperature",
    )
    ev = AssetEvidence(outcome="no_asset_id")
    result = decide(facts, ev, "The freezer at our cold store is not holding temperature.")

    assert any(r.code == "asset_not_identified" for r in result.reasons)
    assert not any(r.code == "not_a_service_request" for r in result.reasons)


async def test_a_safety_signal_is_never_treated_as_a_non_request() -> None:
    """Safety outranks everything; a hazard with no equipment still escalates."""
    facts = RequestFacts(
        assetMentions=[],
        safetySignal="affirmed",
        safetyQuote="there is smoke in the plant room",
        intent="other",
        symptomSummary="",
    )
    ev = AssetEvidence(outcome="no_asset_id")
    result = decide(facts, ev, "There is smoke in the plant room.")

    assert result.status == "human_escalation_required"


async def test_an_unreadable_message_is_not_treated_as_a_non_request() -> None:
    """A failed model read takes the cautious path, not the 'nothing to triage' path."""
    facts = parse_facts(None)
    ev = AssetEvidence(outcome="no_asset_id")
    result = decide(facts, ev, "Something is wrong with the unit.")

    assert not any(r.code == "not_a_service_request" for r in result.reasons)


# --- the safety question wording ----------------------------------------


def test_the_safety_question_is_one_plain_sentence() -> None:
    """Meera: answerable by whoever is next to the equipment, not a form."""
    from app.decide import SAFETY_QUESTION

    assert SAFETY_QUESTION.count("?") == 1
    assert len(SAFETY_QUESTION) < 140
    # Still covers POL-SAFETY-001's four triggers.
    lowered = SAFETY_QUESTION.lower()
    for trigger in ("smoke", "burning", "fuel", "gas", "water", "unwell"):
        assert trigger in lowered


async def test_the_safety_reply_states_the_lead_in_once() -> None:
    from app.replies import build_customer_reply

    facts = RequestFacts(
        assetMentions=[], safetySignal="ambiguous", intent="breakdown", symptomSummary="odd noise"
    )
    ev = AssetEvidence(outcome="no_asset_id")
    decision = decide(facts, ev, "There is an odd noise from the unit.")
    draft = build_customer_reply(facts, ev, decision)

    assert "Before we arrange anything" not in draft
    assert "Before we go further" not in draft
    # The question appears exactly once.
    assert draft.count("Is there any smoke") == 1


# --- remote-only agreements (C-08) --------------------------------------
# A remote-only agreement must never produce a work order, whatever the customer asks
# for. CLAUDE.md rule 5. Previously this was gated on the model's `requiresOnsite`, so a
# customer saying "do not send anyone" cleared the gate and a technician was booked.


def _remote_only_evidence(service_mode: str = "remote_only", coverage: str = "standard") -> AssetEvidence:
    from app.evidence import TechnicianMatch

    return AssetEvidence(
        outcome="resolved",
        assetId="AST-901",
        asset={"id": "AST-901", "nickname": "Dialysis backup DG", "coverage": coverage},
        customer={"id": "CUS-083", "name": "Lotus Kidney Care"},
        site={"id": "SITE-071", "name": "Jayanagar dialysis centre", "city": "Bengaluru"},
        agreement={
            "contractRef": "CON-083-A1",
            "status": "active",
            "serviceMode": service_mode,
            "responseHours": 2,
            "inForceAtReceivedAt": True,
            "customerMatchesAsset": True,
        },
        qualifiedTechnicians=[
            TechnicianMatch(
                technicianId="TECH-01",
                name="A Tech",
                city="Bengaluru",
                skills=["generator"],
                certifications=["hv"],
                available=True,
                sameCityAsSite=True,
            )
        ],
    )


async def test_remote_only_with_a_remote_request_is_covered_not_account_review() -> None:
    """Nothing is wrong with the cover, so there is nothing for an account reviewer."""
    facts = RequestFacts(
        assetMentions=["AST-901"],
        safetySignal="denied",
        safetyQuote="There is no smoke, no smell and nobody is at risk.",
        intent="breakdown",
        symptomSummary="controller fault 118 again",
        requiresOnsite=False,
    )
    result = decide(
        facts,
        _remote_only_evidence(),
        "Dialysis backup DG AST-901 controller fault 118. Please do not send anyone to "
        "site - our engineer just needs talking through the reset over the phone. There "
        "is no smoke, no smell and nobody is at risk.",
    )

    assert result.status == "covered_action"
    assert any(r.code == "remote_support_covered" for r in result.reasons)
    # The thing that actually went wrong: a technician was booked.
    assert result.recommendedTechnician is None


async def test_remote_only_needing_onsite_goes_to_account_review() -> None:
    """Attending needs cover this agreement does not grant: an account decision."""
    facts = RequestFacts(
        assetMentions=["AST-901"],
        safetySignal="denied",
        safetyQuote="no smoke",
        intent="breakdown",
        symptomSummary="unit completely dead",
        requiresOnsite=True,
    )
    result = decide(facts, _remote_only_evidence(), "AST-901 is dead, send an engineer. no smoke")

    assert result.status == "account_review_required"
    assert any(r.code == "remote_only_cannot_dispatch" for r in result.reasons)
    assert result.recommendedTechnician is None


async def test_remote_only_never_dispatches_however_the_request_is_worded() -> None:
    """The contract limit does not depend on the customer's phrasing.

    This is the regression that matters: whatever combination of wording and extracted
    `requiresOnsite` arrives, a remote-only agreement cannot reach `dispatch_ready`.
    """
    bodies = [
        ("Please do not send anyone, phone help only. no smoke", False),
        ("Send an engineer today please. no smoke", True),
        ("AST-901 has stopped working. no smoke", None),
        ("Need someone on site urgently, production is down. no smoke", True),
    ]
    for body, requires_onsite in bodies:
        facts = RequestFacts(
            assetMentions=["AST-901"],
            safetySignal="denied",
            safetyQuote="no smoke",
            intent="breakdown",
            symptomSummary="stopped working",
            requiresOnsite=requires_onsite,
        )
        result = decide(facts, _remote_only_evidence(), body)
        assert result.status != "dispatch_ready", body
        assert result.recommendedTechnician is None, body


async def test_asset_level_remote_only_also_blocks_dispatch() -> None:
    """The stricter of agreement serviceMode and asset coverage controls (rule 5)."""
    facts = RequestFacts(
        assetMentions=["AST-901"],
        safetySignal="denied",
        safetyQuote="no smoke",
        intent="breakdown",
        symptomSummary="stopped working",
        requiresOnsite=True,
    )
    evidence = _remote_only_evidence(service_mode="onsite", coverage="remote_only")
    result = decide(facts, evidence, "AST-901 stopped, send someone. no smoke")

    assert result.status == "account_review_required"
    assert any(r.code == "remote_only_cannot_dispatch" for r in result.reasons)


async def test_the_remote_only_reply_promises_no_visit() -> None:
    from app.replies import build_customer_reply

    facts = RequestFacts(
        assetMentions=["AST-901"],
        safetySignal="denied",
        safetyQuote="no smoke",
        intent="breakdown",
        symptomSummary="controller fault 118",
        requiresOnsite=False,
    )
    evidence = _remote_only_evidence()
    decision = decide(facts, evidence, "AST-901 fault, phone help only please. no smoke")
    draft = build_customer_reply(facts, evidence, decision)

    assert "remote support" in draft
    # The old covered_action wording promised a visit the customer had declined.
    assert "scheduling the visit" not in draft
    assert "engineer has been assigned" not in draft


# --- check order: coverage and previous work before duplicate -----------
# C-01, C-04 and D-06. A duplicate link is housekeeping; an account question and a
# "is this the old fault back?" question both outrank it.


def _suspended_evidence() -> AssetEvidence:
    """C-01: cover lapsed, and an open request for the same equipment."""
    return AssetEvidence(
        outcome="resolved",
        assetId="AST-801",
        asset={"id": "AST-801", "nickname": "Banquet chiller", "coverage": "suspended"},
        customer={"id": "CUS-074", "name": "Riverbend Hotels"},
        site={"id": "SITE-063", "name": "MG Road hotel", "city": "Bengaluru"},
        agreement={
            "contractRef": "CON-074",
            "status": "suspended",
            "serviceMode": "onsite",
            "responseHours": 8,
            "inForceAtReceivedAt": False,
            "customerMatchesAsset": True,
        },
        receivedAt="2026-09-21T10:00:00Z",
        recentRequests=[
            {
                "requestId": "REQ-8268",
                "receivedAt": "2026-09-20T10:51:00Z",
                "subject": "Banquet chiller inspection",
                "body": "Please schedule an inspection for the banquet chiller AST-801.",
            }
        ],
    )


async def test_lapsed_cover_is_account_review_not_a_duplicate_link() -> None:
    """C-01: linking it would imply cover we cannot evidence (POL-CONTRACT-002)."""
    facts = RequestFacts(
        assetMentions=["AST-801"],
        safetySignal="denied",
        safetyQuote="No smoke, water or smell.",
        intent="planned_service",
        symptomSummary="inspection requested before weekend event",
        # The model read it as a repeat; coverage still comes first.
        sameFaultAsExisting="same",
        sameFaultReference="REQ-8268",
    )
    result = decide(
        facts,
        _suspended_evidence(),
        "Please schedule an inspection for the banquet chiller AST-801 before this "
        "weekend's event. Our finance team paid the renewal yesterday. No smoke, water "
        "or smell.",
    )

    assert result.status == "account_review_required"
    assert any(r.code in {"agreement_not_active", "asset_coverage_suspended"} for r in result.reasons)
    # Not linked: the customer is not told we have attached this to an existing job.
    assert result.linkedRequests == []


async def test_a_customer_who_cannot_tell_gets_a_human_not_a_duplicate_link() -> None:
    """D-06: "I am not sure whether this is the same fault" is rule 7's unclear case."""
    evidence = AssetEvidence(
        outcome="resolved",
        assetId="AST-1201",
        asset={"id": "AST-1201", "nickname": "Press line compressor", "coverage": "standard"},
        customer={"id": "CUS-117"},
        site={"id": "SITE-105", "name": "Rajajinagar packaging plant", "city": "Bengaluru"},
        agreement={
            "contractRef": "CON-117",
            "status": "active",
            "serviceMode": "onsite",
            "responseHours": 12,
            "inForceAtReceivedAt": True,
            "customerMatchesAsset": True,
        },
        openWorkOrders=[{"id": "WO-8876", "requestId": "REQ-8283", "summary": "pressure oscillating"}],
        assetHistory=[{"id": "WO-8876", "requestId": "REQ-8283", "summary": "pressure oscillating"}],
    )
    facts = RequestFacts(
        assetMentions=["AST-1201"],
        safetySignal="denied",
        safetyQuote="No smoke, water or smell.",
        intent="breakdown",
        symptomSummary="pressure oscillating during long runs",
        sameFaultAsExisting="same",
        sameFaultReference="REQ-8283",
        refersToPreviousWork=True,
    )
    result = decide(
        facts,
        evidence,
        "AST-1201 pressure is oscillating again during long runs. Please compare with "
        "the July work order - I am not sure whether this is the same fault coming back "
        "or something new. No smoke, water or smell.",
    )

    assert result.status == "human_escalation_required"
    assert any(r.code == "refers_to_previous_work" for r in result.reasons)


async def test_a_pending_service_request_is_context_not_a_duplicate() -> None:
    """C-04: covered_action, with the earlier request visible to whoever schedules it."""
    evidence = AssetEvidence(
        outcome="resolved",
        assetId="AST-1302",
        asset={"id": "AST-1302", "nickname": "Auditorium chiller", "coverage": "premium"},
        customer={"id": "CUS-129"},
        site={"id": "SITE-116", "name": "Koramangala campus", "city": "Bengaluru"},
        agreement={
            "contractRef": "CON-129-A1",
            "status": "active",
            "serviceMode": "onsite",
            "responseHours": 6,
            "inForceAtReceivedAt": True,
            "customerMatchesAsset": True,
        },
        receivedAt="2026-09-20T18:45:00Z",
        recentRequests=[
            {
                "requestId": "REQ-8287",
                "receivedAt": "2026-09-20T12:08:00Z",
                "subject": "Auditorium chiller service",
                "body": "Please plan the quarterly service for auditorium chiller AST-1302.",
            }
        ],
    )
    facts = RequestFacts(
        assetMentions=["AST-1302"],
        safetySignal="absent",
        intent="planned_service",
        symptomSummary="quarterly service due",
    )
    result = decide(
        facts,
        evidence,
        "Please plan the quarterly service for auditorium chiller AST-1302 next week "
        "under amendment CON-129-A1. Nothing is wrong with it - this is the scheduled visit.",
    )

    assert result.status == "covered_action"
    # The pending request is surfaced as context so the same visit is not booked twice.
    assert any(h.get("requestId") == "REQ-8287" for h in result.relatedHistory)
    assert any("REQ-8287" in w for w in result.warnings)
    # Context, not a link.
    assert result.linkedRequests == []
