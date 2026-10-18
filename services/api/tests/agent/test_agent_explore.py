"""EXPLORE runs: goals, previewed (never applied) proposals, simulations restricted to the
learner's own revisions, and separation from investigation tools."""

from __future__ import annotations

from typing import Any

import pytest
from agent_testkit import (
    SESSION,
    FakeCaseService,
    ScriptedProvider,
    completion,
    last_tool_result,
    make_service,
    wait_finished,
)

from resonance.agent.service import AgentService
from resonance.api.errors import ApiException
from resonance.circuits.revisions import build_revision
from resonance.contracts.agent import EditProposal, ExploreRunRequest
from resonance.contracts.circuits import Revision, Tone
from resonance.contracts.common import SimulatorInfo, new_id
from resonance.contracts.experiments import (
    AcSweepSettings,
    ExperimentComparison,
    ExperimentRecord,
    ExperimentSettings,
    ToneLevel,
    TransientSettings,
)
from resonance.contracts.internal import CancelToken
from resonance.simulation import analytic
from resonance.store.db import DocumentStore, utcnow
from resonance.store.session_state import load_session_state


class FakeExplore:
    """TEST-ONLY ExplorePort double that records requests and returns fixed records."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self.runs: list[tuple[str, str | None]] = []

    async def run_experiment(
        self, session_id: str, revision_id: str, goal_id: str | None, cancel: CancelToken | None
    ) -> ExperimentRecord:
        self.runs.append((revision_id, goal_id))
        record = ExperimentRecord(
            id=new_id("exp"),
            revision_id=revision_id,
            status="succeeded",
            created_at=utcnow(),
            duration_ms=10,
            simulator=SimulatorInfo(version="47"),
            settings=ExperimentSettings(
                ac=AcSweepSettings(start_hz=10, stop_hz=1e5, points_per_decade=40, points=161),
                transient=TransientSettings(
                    tones=[Tone(frequency_hz=440, amplitude_v=0.6, label="Tone", role="wanted")],
                    settle_s=0.2,
                    clip_s=1.2,
                    max_step_s=5e-6,
                    sample_rate_hz=48_000,
                    fade_ms=15,
                ),
            ),
            tones=[
                ToneLevel(
                    frequency_hz=4000.0,
                    label="Whistle",
                    role="interference",
                    input_amplitude_v=0.3,
                    output_amplitude_v=0.11,
                    transient_gain_db=-8.7,
                    ac_gain_db=-8.69,
                    agreement_db=-0.01,
                    phase_deg=-68.0,
                )
            ],
        )
        self.store.put("experiment", record.id, session_id, record)
        return record

    async def compare(self, session_id: str, a_id: str, b_id: str) -> ExperimentComparison:
        raise NotImplementedError("not used in these tests")


def _revision(store: DocumentStore, origin: str = "template_default") -> Revision:
    rev = build_revision(
        "rc_lowpass",
        {"R1": 1000.0, "C1": 1e-7},
        origin=origin,  # type: ignore[arg-type]
        frozen=origin == "case_intended",
    )
    store.put("revision", rev.id, SESSION, rev)
    return rev


async def _run(service: AgentService, revision: Revision, message: str = "Help me.") -> str:
    run = await service.start_explore_run(
        SESSION, ExploreRunRequest(revision_id=revision.id, message=message)
    )
    await wait_finished(service, run.id)
    return run.id


def _events(service: AgentService, run_id: str) -> list[Any]:
    return list(service.handle_for(SESSION, run_id).events)


async def test_propose_values_previews_without_applying(settings, store) -> None:
    rev = _revision(store)
    provider = ScriptedProvider(
        [
            completion(
                None,
                [
                    (
                        "propose_values",
                        {
                            "changes": [{"component_id": "C1", "value": "220 nF"}],
                            "rationale": "A larger capacitor lowers the cutoff.",
                        },
                    )
                ],
            ),
            completion("Prediction: the whistle drops further. Apply it to measure."),
        ]
    )
    service = make_service(settings, store, provider, explore=FakeExplore(store))
    run_id = await _run(service, rev, "Make the whistle quieter.")

    proposals = store.list_docs("proposal", SESSION, EditProposal)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.status == "pending" and proposal.base_revision_id == rev.id
    assert proposal.changes[0].to_value_si == pytest.approx(2.2e-7)
    assert proposal.changes[0].to_display == "220 nF"
    whistle = next(p for p in proposal.predicted if p.frequency_hz == 4000.0)
    expected = float(analytic.gain_db(analytic.lowpass_transfer(4000.0, 1000.0, 2.2e-7)))
    assert whistle.predicted_gain_db == pytest.approx(expected)
    assert whistle.basis == "analytical_first_order_rc"
    assert proposal.predicted_goal_pass is not None  # evaluated against the default goal
    # Nothing was applied: the session still has exactly one revision.
    assert len(store.list_docs("revision", SESSION, Revision)) == 1
    kinds = [e.type for e in _events(service, run_id)]
    assert "proposal" in kinds
    result = last_tool_result(provider.requests[1])
    assert result["applied"] is False and "not measurements" in result["note"]


async def test_invalid_proposal_values_are_reported_to_the_model(settings, store) -> None:
    rev = _revision(store)
    provider = ScriptedProvider(
        [
            completion(
                None,
                [
                    (
                        "propose_values",
                        {
                            "changes": [{"component_id": "R1", "value": "4.7 nF"}],
                            "rationale": "wrong unit",
                        },
                    )
                ],
            ),
            completion("That value is not valid for a resistor."),
        ]
    )
    service = make_service(settings, store, provider, explore=FakeExplore(store))
    await _run(service, rev)
    assert store.list_docs("proposal", SESSION, EditProposal) == []
    result = last_tool_result(provider.requests[1])
    assert result["ok"] is False and result["error"]["code"] == "validation_error"


async def test_set_goal_records_an_agent_goal(settings, store) -> None:
    rev = _revision(store)
    goal = {
        "title": "Keep 440 Hz, cut 4 kHz by 20 dB",
        "constraints": [
            {"kind": "preserve", "frequency_hz": 440, "limit_db": 1, "label": "Tone"},
            {"kind": "attenuate", "frequency_hz": 4000, "limit_db": 20, "label": "Whistle"},
        ],
    }
    provider = ScriptedProvider(
        [completion(None, [("set_goal", goal)]), completion("That goal is not reachable.")]
    )
    service = make_service(settings, store, provider, explore=FakeExplore(store))
    await _run(service, rev)
    state = load_session_state(store, SESSION)
    assert state.goal.origin == "agent" and state.goal.title == goal["title"]
    feasibility = last_tool_result(provider.requests[1])["feasibility"]
    assert feasibility["feasible"] is False and "prediction" in feasibility["basis"]


async def test_explore_experiments_only_on_the_learners_revisions(settings, store) -> None:
    rev = _revision(store)
    case_rev = _revision(store, origin="case_intended")
    explore = FakeExplore(store)
    provider = ScriptedProvider(
        [
            completion(None, [("run_experiment", {"revision_id": case_rev.id})]),
            completion(None, [("run_experiment", {"revision_id": new_id("rev")})]),
            completion(None, [("run_experiment", {"revision_id": rev.id})]),
            lambda body: completion(
                f"The whistle is 8.7 dB quieter [{last_tool_result(body)['experiment_id']}]."
            ),
        ]
    )
    service = make_service(settings, store, provider, explore=explore)
    run_id = await _run(service, rev)
    assert [r for r, _ in explore.runs] == [rev.id]
    goal_id = load_session_state(store, SESSION).goal.id
    assert explore.runs[0][1] == goal_id
    results = [e for e in _events(service, run_id) if e.type == "tool_result"]
    assert [r.ok for r in results] == [False, False, True]
    message = [e for e in _events(service, run_id) if e.type == "message"][-1]
    assert message.citations[0].result_id == results[2].experiment_id
    assert message.citations[0].quantity == "output_gain_db"


async def test_explore_run_cannot_use_investigation_tools(settings, store) -> None:
    rev = _revision(store)
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            completion(None, [("get_case", {})]),
            completion(None, [("run_experiment", {"instrument": "source_current_sweep"})]),
            completion(None, [("submit_diagnosis", {"conclusion": "no_fault"})]),
            completion("I can only work on your own circuit here."),
        ]
    )
    service = make_service(settings, store, provider, cases, explore=FakeExplore(store))
    run_id = await _run(service, rev)
    assert provider.tool_names() == {
        "inspect_circuit",
        "set_goal",
        "propose_values",
        "run_experiment",
        "compare_experiments",
    }
    assert cases.calls == [], "the case service was never touched"
    results = [e for e in _events(service, run_id) if e.type == "tool_result"]
    assert not any(r.ok for r in results)


async def test_one_explore_run_per_session(settings, store) -> None:
    from agent_testkit import Hang

    rev = _revision(store)
    provider = ScriptedProvider([Hang(30)])
    service = make_service(settings, store, provider, explore=FakeExplore(store))
    first = await service.start_explore_run(
        SESSION, ExploreRunRequest(revision_id=rev.id, message="Hello")
    )
    with pytest.raises(ApiException) as info:
        await service.start_explore_run(
            SESSION, ExploreRunRequest(revision_id=rev.id, message="Again")
        )
    assert info.value.error.code == "run_in_progress"
    await service.cancel(SESSION, first.id)
