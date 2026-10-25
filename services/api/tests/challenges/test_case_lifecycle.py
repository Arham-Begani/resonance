"""Case state machine, available actions, ownership, close ordering, and outcomes.

Uses the analytic TEST DOUBLE simulator (fast); physics/identifiability use real ngspice in
test_case_identifiability_ngspice.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest
from challenges_testkit import SESSION_A, SESSION_B, AnalyticTestSimulator, Rig, seed_for

from resonance.api.errors import ApiException
from resonance.challenges import service as service_module
from resonance.contracts.common import ErrorCode
from resonance.contracts.investigation import (
    CaseExperimentRequest,
    CaseStatus,
    DiagnosisSubmission,
    EvidenceCitation,
    InstrumentKind,
    PublicCase,
    RepairProposalInput,
)
from resonance.store.session_state import load_session_state

S = SESSION_A
_counter = iter(range(10_000))


def req(instrument: str, probe: str | None = None, key: str | None = None) -> CaseExperimentRequest:
    return CaseExperimentRequest.model_validate(
        {
            "instrument": instrument,
            "probe": probe,
            "client_request_id": key or f"key-{next(_counter):06d}",
        }
    )


def abstain(*candidates: str) -> DiagnosisSubmission:
    return DiagnosisSubmission.model_validate(
        {
            "conclusion": "insufficient_evidence",
            "candidate_hypothesis_ids": list(candidates),
            "explanation": "Two explanations still fit.",
        }
    )


def fault_dx(hypothesis: str, obs_id: str, quantity: str = "tone_gain_db") -> DiagnosisSubmission:
    return DiagnosisSubmission(
        conclusion="fault",
        hypothesis_id=hypothesis,
        citations=[EvidenceCitation(observation_id=obs_id, quantity=quantity)],
        explanation="The cited measurement fits this explanation.",
    )


def no_fault_dx(obs_id: str) -> DiagnosisSubmission:
    return DiagnosisSubmission(
        conclusion="no_fault",
        citations=[EvidenceCitation(observation_id=obs_id, quantity="tone_gain_db")],
        explanation="The symptom matches the intended design.",
    )


async def error_of(awaitable: Awaitable[Any]) -> ApiException:
    with pytest.raises(ApiException) as info:
        await awaitable
    return info.value


async def new_case(rig: Rig, fault: str = "r1_increased", session: str = S) -> PublicCase:
    return await rig.service.harness_create_case(session, seed_for(fault))  # type: ignore[arg-type]


async def test_new_case_is_ready_with_uncounted_symptom(rig: Rig) -> None:
    case = await new_case(rig)
    assert case.status == CaseStatus.READY
    assert case.available_actions == ["choose_mode", "submit_diagnosis", "close"]
    assert case.budget.model_dump() == {
        "total": 3,
        "used": 0,
        "remaining": 3,
        "infrastructure_retries_per_experiment": 1,
        "infrastructure_failures": 0,
    }
    (symptom,) = case.observations
    assert (symptom.kind, symptom.sequence, symptom.counted, symptom.requested_by) == (
        "symptom",
        0,
        False,
        "system",
    )
    assert symptom.probe == "output_voltage" and symptom.replay is False
    assert symptom.transient is not None and symptom.transient.audio is not None
    assert [c.role for c in symptom.transient.audio.clips] == ["expected", "observed"]
    assert symptom.settings.transient is not None and symptom.settings.sweep is None
    assert case.reveal is None
    assert case.evidence.distinguishing_instruments is None
    rev = case.intended_revision
    assert rev.frozen and rev.origin == "case_intended"
    assert rig.store.get("revision", rev.id, S, type(rev)) == rev
    assert load_session_state(rig.store, S).active_case_id == case.id


async def test_only_one_open_case_per_session(rig: Rig) -> None:
    first = await new_case(rig)
    err = await error_of(rig.service.create_case(S))
    assert err.error.code == ErrorCode.CONFLICT
    other = await new_case(rig, session=SESSION_B)  # other sessions are independent
    assert other.id != first.id
    await rig.service.close_case(S, first.id)
    second = await rig.service.create_case(S)
    assert second.id != first.id and second.status == CaseStatus.READY


async def test_guide_flow_transitions_and_actions(rig: Rig) -> None:
    case = await new_case(rig)
    case = await rig.service.set_mode(S, case.id, "guide")
    assert case.status == CaseStatus.OBSERVING
    assert case.available_actions == [
        "choose_mode",
        "run_experiment",
        "request_hint",
        "submit_diagnosis",
        "close",
    ]
    case = await rig.service.set_mode(S, case.id, "agent")  # allowed while no run is active
    assert "start_agent" in case.available_actions and "request_hint" not in (
        case.available_actions
    )
    case = await rig.service.set_mode(S, case.id, "guide")
    resp = await rig.service.run_experiment(
        S, case.id, req("output_voltage_sweep"), requested_by="user"
    )
    assert resp.observation.counted and resp.case.budget.used == 1
    case = await rig.service.submit_diagnosis(
        S, case.id, abstain("r1_increased", "c1_increased"), submitted_by="user"
    )
    assert case.status == CaseStatus.UNRESOLVED
    assert case.diagnosis is not None
    assert case.diagnosis.evidence_check.status == "consistent_abstention"
    assert case.available_actions == ["propose_repair", "close"]
    assert case.evidence.distinguishing_instruments is not None  # published after diagnosis
    case = await rig.service.propose_repair(
        S, case.id, RepairProposalInput(component_id="C1"), proposed_by="user"
    )
    assert case.available_actions == ["propose_repair", "verify_repair", "close"]
    case = await rig.service.propose_repair(  # replaceable until verification
        S,
        case.id,
        RepairProposalInput(component_id="R1", rationale="R1 drifted"),
        proposed_by="user",
    )
    assert case.repair_proposal is not None and case.repair_proposal.component_id == "R1"
    nominal_r1 = next(c for c in case.intended_revision.components if c.id == "R1").value_si
    assert case.repair_proposal.value_si == nominal_r1
    case = await rig.service.verify_repair(S, case.id)
    assert case.status == CaseStatus.REPAIR_VERIFICATION
    assert case.available_actions == ["close"]
    assert case.repair is not None and case.repair.functional_recovery is True
    assert case.repair.audio is not None
    assert [c.role for c in case.repair.audio.clips] == ["expected", "repaired"]
    assert case.reveal is None
    closed = await rig.service.close_case(S, case.id)
    assert closed.status == CaseStatus.CLOSED and closed.available_actions == []
    assert closed.reveal is not None
    outcome = closed.reveal.outcome
    assert outcome.terminal_status == "closed"
    assert outcome.exact_identification == "abstained"
    assert outcome.true_cause_among_candidates is True
    assert outcome.functional_recovery == "restored"
    used = (outcome.experiments_used, outcome.experiments_by_user, outcome.experiments_by_agent)
    assert used == (1, 1, 0)
    again = await rig.service.close_case(S, case.id)  # idempotent
    assert again.reveal == closed.reveal


async def test_diagnosis_allowed_from_ready(rig: Rig) -> None:
    case = await new_case(rig, "no_fault")
    case = await rig.service.submit_diagnosis(
        S, case.id, no_fault_dx(case.observations[0].id), submitted_by="user"
    )
    assert case.status == CaseStatus.DIAGNOSIS_SUBMITTED
    assert case.diagnosis is not None and case.diagnosis.evidence_check.status == "supported"
    # propose_repair stays available after a no_fault diagnosis (public-only actions, V-06)
    assert case.available_actions == ["propose_repair", "close"]


async def test_agent_ownership_blocks_user_mutations(rig: Rig) -> None:
    case = await new_case(rig)
    await rig.service.set_mode(S, case.id, "agent")
    rig.service.claim_for_agent(S, case.id, "run_owner123")
    case = rig.service.get_case(S, case.id)
    assert case.active_run_id == "run_owner123"
    assert case.available_actions == ["cancel_agent", "close"]
    for awaitable in (
        rig.service.run_experiment(S, case.id, req("output_voltage_sweep"), requested_by="user"),
        rig.service.set_mode(S, case.id, "guide"),
        rig.service.submit_diagnosis(
            S, case.id, abstain("r1_increased", "c1_increased"), submitted_by="user"
        ),
    ):
        assert (await error_of(awaitable)).error.code == ErrorCode.RUN_IN_PROGRESS
    with pytest.raises(ApiException) as other_run:
        rig.service.claim_for_agent(S, case.id, "run_other4567")
    assert other_run.value.error.code == ErrorCode.RUN_IN_PROGRESS
    wrong = await error_of(
        rig.service.run_experiment(
            S,
            case.id,
            req("output_voltage_sweep"),
            requested_by="agent",
            agent_run_id="run_other4567",
        )
    )
    assert wrong.error.code == ErrorCode.RUN_IN_PROGRESS
    resp = await rig.service.run_experiment(
        S, case.id, req("source_current_sweep"), requested_by="agent", agent_run_id="run_owner123"
    )
    assert resp.observation.requested_by == "agent"
    assert resp.observation.agent_run_id == "run_owner123"
    case = await rig.service.submit_diagnosis(
        S,
        case.id,
        fault_dx("r1_increased", resp.observation.id, "source_current_ma"),
        submitted_by="agent",
        agent_run_id="run_owner123",
    )
    case = await rig.service.propose_repair(
        S, case.id, RepairProposalInput(component_id="R1"), proposed_by="agent"
    )
    assert case.repair_proposal is not None and case.repair_proposal.proposed_by == "agent"
    assert (await error_of(rig.service.verify_repair(S, case.id))).error.code == (
        ErrorCode.RUN_IN_PROGRESS
    )
    rig.service.release_agent(S, case.id, "run_owner123")
    case = await rig.service.verify_repair(S, case.id)
    closed = await rig.service.close_case(S, case.id)
    assert closed.reveal is not None
    assert closed.reveal.outcome.experiments_by_agent == 1
    assert closed.reveal.outcome.exact_identification == "correct"


async def test_illegal_transitions_conflict(rig: Rig) -> None:
    case = await new_case(rig)
    sid, cid = S, case.id
    svc = rig.service
    assert (
        await error_of(
            svc.run_experiment(sid, cid, req("output_voltage_sweep"), requested_by="user")
        )
    ).error.code == (ErrorCode.CONFLICT)  # experiments need observing
    assert (
        await error_of(
            svc.propose_repair(sid, cid, RepairProposalInput(component_id="R1"), proposed_by="user")
        )
    ).error.code == (ErrorCode.CONFLICT)  # repair before diagnosis
    with pytest.raises(ApiException) as no_mode:
        svc.claim_for_agent(sid, cid, "run_nomode1234")
    assert no_mode.value.error.code == ErrorCode.CONFLICT
    await svc.set_mode(sid, cid, "guide")
    with pytest.raises(ApiException) as wrong_kind:
        svc.claim_for_agent(sid, cid, "run_kind12345", kind="investigate")
    assert wrong_kind.value.error.code == ErrorCode.CONFLICT
    await svc.submit_diagnosis(
        sid, cid, abstain("r1_increased", "c1_increased"), submitted_by="user"
    )
    for awaitable in (
        svc.submit_diagnosis(
            sid, cid, abstain("r1_increased", "c1_increased"), submitted_by="user"
        ),
        svc.run_experiment(sid, cid, req("output_voltage_sweep"), requested_by="user"),
        svc.set_mode(sid, cid, "agent"),
        svc.verify_repair(sid, cid),  # no proposal yet
        svc.record_hint(sid, cid),
    ):
        assert (await error_of(awaitable)).error.code == ErrorCode.CONFLICT
    await svc.propose_repair(sid, cid, RepairProposalInput(component_id="R1"), proposed_by="user")
    await svc.verify_repair(sid, cid)
    for awaitable in (
        svc.verify_repair(sid, cid),  # once
        svc.propose_repair(sid, cid, RepairProposalInput(component_id="C1"), proposed_by="user"),
    ):
        assert (await error_of(awaitable)).error.code == ErrorCode.CONFLICT


async def test_closed_case_refuses_everything(rig: Rig) -> None:
    case = await new_case(rig)
    await rig.service.set_mode(S, case.id, "guide")
    closed = await rig.service.close_case(S, case.id)
    assert closed.reveal is not None
    assert rig.service.is_closing_or_closed(S, case.id)
    svc, cid = rig.service, case.id
    for awaitable in (
        svc.run_experiment(S, cid, req("output_voltage_sweep"), requested_by="user"),
        svc.submit_diagnosis(S, cid, abstain("r1_increased", "c1_increased"), submitted_by="user"),
        svc.propose_repair(S, cid, RepairProposalInput(component_id="R1"), proposed_by="user"),
        svc.verify_repair(S, cid),
        svc.set_mode(S, cid, "agent"),
        svc.record_hint(S, cid),
    ):
        assert (await error_of(awaitable)).error.code == ErrorCode.CONFLICT
    with pytest.raises(ApiException) as claim:
        svc.claim_for_agent(S, cid, "run_closed1234")
    assert claim.value.error.code == ErrorCode.CONFLICT
    assert all(not o.replay for o in svc.get_case(S, cid).observations)


async def test_hints_are_guide_only_and_counted(rig: Rig) -> None:
    case = await new_case(rig)
    await rig.service.set_mode(S, case.id, "agent")
    assert (await error_of(rig.service.record_hint(S, case.id))).error.code == ErrorCode.CONFLICT
    await rig.service.set_mode(S, case.id, "guide")
    rig.service.claim_for_agent(S, case.id, "run_hint12345")  # mode guide → hint run
    case = await rig.service.record_hint(S, case.id)
    assert case.hints_used == 1
    rig.service.release_agent(S, case.id, "run_hint12345")
    closed = await rig.service.close_case(S, case.id)
    assert closed.reveal is not None and closed.reveal.outcome.hints_used == 1


async def test_citation_validation_never_consumes_the_diagnosis(rig: Rig) -> None:
    case = await new_case(rig)
    other = await new_case(rig, "c1_increased", session=SESSION_B)
    await rig.service.set_mode(S, case.id, "guide")
    resp = await rig.service.run_experiment(
        S, case.id, req("output_voltage_sweep"), requested_by="user"
    )
    sweep_id = resp.observation.id
    symptom_id = case.observations[0].id

    def dx(obs_id: str, quantity: str, freq: float | None = None) -> DiagnosisSubmission:
        return DiagnosisSubmission(
            conclusion="fault",
            hypothesis_id="r1_increased",
            citations=[
                EvidenceCitation(observation_id=obs_id, quantity=quantity, frequency_hz=freq)
            ],
            explanation="x",
        )

    bad = [
        (dx("obs_doesnotexist1", "output_gain_db"), "citations.0.observation_id"),
        (dx(other.observations[0].id, "tone_gain_db"), "citations.0.observation_id"),
        (dx(sweep_id, "tone_gain_db"), "citations.0.quantity"),
        (dx(symptom_id, "source_current_ma"), "citations.0.quantity"),
        (dx(sweep_id, "output_gain_db", 1234.0), "citations.0.frequency_hz"),
    ]
    messages = set()
    for submission, field in bad:
        err = await error_of(
            rig.service.submit_diagnosis(S, case.id, submission, submitted_by="user")
        )
        assert err.error.code == ErrorCode.VALIDATION_ERROR and err.error.field == field
        if field.endswith("observation_id"):
            messages.add(err.error.message)
    assert len(messages) == 1  # unknown and other-case IDs are indistinguishable
    grid_point = resp.observation.sweep.frequency_hz[20]  # type: ignore[union-attr]
    ok = await rig.service.submit_diagnosis(
        S, case.id, dx(sweep_id, "output_gain_db", grid_point * 1.0005), submitted_by="user"
    )
    assert ok.status == CaseStatus.DIAGNOSIS_SUBMITTED


async def test_close_stops_agent_before_reveal(rig: Rig) -> None:
    case = await new_case(rig)
    await rig.service.set_mode(S, case.id, "agent")
    rig.service.claim_for_agent(S, case.id, "run_closeme123")
    seen: dict[str, Any] = {}

    async def canceller(run_id: str) -> None:
        seen["run_id"] = run_id
        seen["closing"] = rig.service.is_closing_or_closed(S, case.id)
        snapshot = rig.service.get_case(S, case.id)
        seen["reveal_during_cancel"] = snapshot.reveal
        seen["status_during_cancel"] = snapshot.status
        seen["mutation"] = (
            await error_of(
                rig.service.run_experiment(
                    S,
                    case.id,
                    req("output_voltage_sweep"),
                    requested_by="agent",
                    agent_run_id=run_id,
                )
            )
        ).error.code

    rig.service.register_run_canceller(canceller)
    closed = await rig.service.close_case(S, case.id)
    assert seen["run_id"] == "run_closeme123"
    assert seen["closing"] is True
    assert seen["reveal_during_cancel"] is None
    assert seen["status_during_cancel"] == CaseStatus.OBSERVING
    assert seen["mutation"] == ErrorCode.CONFLICT
    assert closed.reveal is not None and closed.active_run_id is None


async def test_close_cancels_in_flight_experiment(rig: Rig, double: AnalyticTestSimulator) -> None:
    case = await new_case(rig)
    await rig.service.set_mode(S, case.id, "guide")
    double.delay_s = 5.0
    task = asyncio.create_task(
        rig.service.run_experiment(S, case.id, req("output_voltage_sweep"), requested_by="user")
    )
    await asyncio.sleep(0.05)
    in_flight = rig.service.get_case(S, case.id)
    assert in_flight.budget.remaining == 2  # reserved while running
    closed = await asyncio.wait_for(rig.service.close_case(S, case.id), timeout=3.0)
    err = await error_of(task)
    assert err.error.code == ErrorCode.CANCELLED
    assert closed.reveal is not None
    assert closed.budget.used == 0 and closed.reveal.outcome.experiments_used == 0
    assert [o.status for o in closed.observations] == ["succeeded", "cancelled"]
    assert closed.observations[1].counted is False


async def test_close_with_unresponsive_canceller_is_bounded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "CASE_CLOSE_AGENT_WAIT_S", 0.2)
    case = await new_case(rig)
    await rig.service.set_mode(S, case.id, "agent")
    rig.service.claim_for_agent(S, case.id, "run_stuck12345")

    async def never_returns(run_id: str) -> None:
        await asyncio.sleep(3600)

    rig.service.register_run_canceller(never_returns)
    closed = await asyncio.wait_for(rig.service.close_case(S, case.id), timeout=2.0)
    assert closed.status == CaseStatus.CLOSED and closed.reveal is not None


async def test_no_fault_outcome_is_not_needed_even_after_repair(rig: Rig) -> None:
    case = await new_case(rig, "no_fault")
    await rig.service.set_mode(S, case.id, "guide")
    case = await rig.service.submit_diagnosis(
        S, case.id, no_fault_dx(case.observations[0].id), submitted_by="user"
    )
    await rig.service.propose_repair(
        S, case.id, RepairProposalInput(component_id="C1"), proposed_by="user"
    )
    case = await rig.service.verify_repair(S, case.id)
    assert case.repair is not None and case.repair.functional_recovery is True
    closed = await rig.service.close_case(S, case.id)
    assert closed.reveal is not None
    assert closed.reveal.fault.hypothesis_id == "no_fault"
    assert closed.reveal.outcome.functional_recovery == "not_needed"
    assert closed.reveal.outcome.exact_identification == "correct"
    assert closed.reveal.outcome.unsupported_certainty is False


async def test_experiment_kinds_have_declared_settings_and_shapes(rig: Rig) -> None:
    case = await new_case(rig)
    await rig.service.set_mode(S, case.id, "guide")
    sweep = (
        await rig.service.run_experiment(
            S, case.id, req("source_current_sweep"), requested_by="user"
        )
    ).observation
    assert sweep.settings.sweep is not None and sweep.settings.sweep.points == 31
    assert sweep.sweep is not None and sweep.sweep.magnitude_unit == "mA"
    assert len(sweep.sweep.frequency_hz) == 31
    cur = (
        await rig.service.run_experiment(
            S, case.id, req("transient_capture", "source_current"), requested_by="user"
        )
    ).observation
    assert cur.probe == "source_current" and cur.transient is not None
    assert cur.transient.audio is None  # current is never played as audio
    assert cur.transient.waveform.unit == "mA"
    assert all(t.amplitude_unit == "mA" and t.gain_db is None for t in cur.transient.tones)
    assert len(cur.transient.waveform.observed) <= 1024
    assert InstrumentKind.TRANSIENT_CAPTURE.value == cur.kind


async def test_available_actions_for_the_owning_run(rig: Rig) -> None:
    """B-04: during its own run the agent is told what IT may do (not the user's view)."""
    svc = rig.service
    case = await new_case(rig)
    await svc.set_mode(S, case.id, "agent")
    run = "run_actions1234"
    assert svc.available_actions_for_run(S, case.id, run) == []  # not the owner yet
    svc.claim_for_agent(S, case.id, run)
    assert svc.get_case(S, case.id).available_actions == ["cancel_agent", "close"]  # user view
    assert svc.available_actions_for_run(S, case.id, run) == ["run_experiment", "submit_diagnosis"]
    assert svc.available_actions_for_run(S, case.id, "run_someone_else") == []
    for i in range(3):
        await svc.run_experiment(
            S,
            case.id,
            req("output_voltage_sweep", key=f"own-run-{i:04d}"),
            requested_by="agent",
            agent_run_id=run,
        )
    assert svc.available_actions_for_run(S, case.id, run) == ["submit_diagnosis"]
    await svc.submit_diagnosis(
        S, case.id, abstain("r1_increased", "c1_increased"), submitted_by="agent", agent_run_id=run
    )
    assert svc.available_actions_for_run(S, case.id, run) == ["propose_repair"]
    await svc.propose_repair(
        S, case.id, RepairProposalInput(component_id="R1"), proposed_by="agent", agent_run_id=run
    )
    assert svc.available_actions_for_run(S, case.id, run) == ["propose_repair", "verify_repair"]
    await svc.verify_repair(S, case.id, requested_by="agent", agent_run_id=run)
    assert svc.available_actions_for_run(S, case.id, run) == []
    svc.release_agent(S, case.id, run)
    await svc.close_case(S, case.id)
    assert svc.available_actions_for_run(S, case.id, run) == []


async def test_run_ownership_is_checked_before_status(rig: Rig) -> None:
    """B-03: while a run owns the case every user mutation reports run_in_progress, whatever
    the status (the action may also be invalid in that status)."""
    svc = rig.service
    case = await new_case(rig)
    await svc.set_mode(S, case.id, "agent")
    svc.claim_for_agent(S, case.id, "run_order12345")
    user_calls = [
        svc.propose_repair(S, case.id, RepairProposalInput(component_id="R1"), proposed_by="user"),
        svc.verify_repair(S, case.id),
    ]
    for awaitable in user_calls:
        assert (await error_of(awaitable)).error.code == ErrorCode.RUN_IN_PROGRESS
    await svc.submit_diagnosis(
        S,
        case.id,
        abstain("r1_increased", "c1_increased"),
        submitted_by="agent",
        agent_run_id="run_order12345",
    )
    after_dx = [
        svc.run_experiment(S, case.id, req("output_voltage_sweep"), requested_by="user"),
        svc.submit_diagnosis(
            S, case.id, abstain("r1_increased", "c1_increased"), submitted_by="user"
        ),
        svc.verify_repair(S, case.id),
    ]
    for awaitable in after_dx:
        assert (await error_of(awaitable)).error.code == ErrorCode.RUN_IN_PROGRESS


async def test_owner_lost_in_a_restart_is_released_when_not_live(rig: Rig) -> None:
    """B-02: a case owned by a run that died with its process must not stay locked."""
    from challenges_testkit import build_rig

    case = await new_case(rig)
    await rig.service.set_mode(S, case.id, "agent")
    rig.service.claim_for_agent(S, case.id, "run_crashed123")
    # Same data files, fresh process state (the first process "crashed").
    restarted = build_rig(rig.settings, rig.simulator)
    try:
        assert restarted.service.get_case(S, case.id).active_run_id == "run_crashed123"
        restarted.service.register_run_liveness(lambda run_id: False)
        after = restarted.service.get_case(S, case.id)
        assert after.active_run_id is None
        assert "run_experiment" in after.available_actions
        resp = await restarted.service.run_experiment(
            S, case.id, req("output_voltage_sweep"), requested_by="user"
        )
        assert resp.case.budget.used == 1
        # A run claimed through THIS instance stays the owner until it is released.
        restarted.service.claim_for_agent(S, case.id, "run_alivehere1")
        assert restarted.service.get_case(S, case.id).active_run_id == "run_alivehere1"
        restarted.service.release_agent(S, case.id, "run_alivehere1")
        # A live run claimed by another instance is kept.
        rig.service.claim_for_agent(S, case.id, "run_elsewhere1")
        restarted.service.register_run_liveness(lambda run_id: run_id == "run_elsewhere1")
        assert restarted.service.get_case(S, case.id).active_run_id == "run_elsewhere1"
    finally:
        restarted.service.close()
        restarted.store.close()
