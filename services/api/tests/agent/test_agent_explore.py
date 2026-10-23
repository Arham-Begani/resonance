"""EXPLORE runs: goals, previewed (never applied) proposals, simulations restricted to the
learner's own revisions, and separation from investigation tools."""

from __future__ import annotations

import json
import math
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
from resonance.circuits.revisions import build_revision, create_revision, revision_values
from resonance.contracts.agent import EditProposal, ExploreRunRequest
from resonance.contracts.circuits import Revision, RevisionCreateRequest, Tone
from resonance.contracts.common import EvidenceRef, SimulatorInfo, new_id
from resonance.contracts.experiments import (
    AcSweepSettings,
    AudioClip,
    AudioComparison,
    ExperimentComparison,
    ExperimentRecord,
    ExperimentSettings,
    ToneLevel,
    TransientSettings,
)
from resonance.contracts.internal import CancelToken
from resonance.contracts.learn import (
    GradedPrediction,
    LearnAttempt,
    LearnChange,
    TonePredictionInput,
)
from resonance.contracts.policy import TEMPLATE_TONES
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
        "suggest_design",
        "propose_values",
        "run_experiment",
        "compare_experiments",
        "read_learn_attempt",
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


# --------------------------------------------------------------------------------------
# Milestone 2: design suggestions, family-switch proposals, Learn attempts
# --------------------------------------------------------------------------------------


def _lowpass_db(f: float, r: float, c: float) -> float:
    """Independent first-order RC low-pass magnitude (not the project's analytic module)."""
    w = 2.0 * math.pi * f
    return -10.0 * math.log10(1.0 + (w * r * c) ** 2)


def _highpass_db(f: float, r: float, c: float) -> float:
    """Independent first-order RC high-pass magnitude: |H| = wRC / sqrt(1 + (wRC)^2)."""
    x = 2.0 * math.pi * f * r * c
    return 20.0 * math.log10(x / math.sqrt(1.0 + x * x))


async def test_suggest_design_is_a_labeled_prediction_that_changes_nothing(settings, store) -> None:
    rev = _revision(store)  # low-pass 1 kΩ / 100 nF; the session has the default low-pass goal
    provider = ScriptedProvider(
        [
            completion(None, [("suggest_design", {"vary": "R1"})]),
            completion("Prediction: a standard resistor meets the goal. Apply it to measure."),
        ]
    )
    service = make_service(settings, store, provider, explore=FakeExplore(store))
    run_id = await _run(service, rev, "Which resistor meets the goal?")

    result = last_tool_result(provider.requests[1])
    assert result["basis"].startswith("analytical prediction") and result["applied"] is False
    assert result["revision_id"] == rev.id and result["template_id"] == "rc_lowpass"
    assert result["goal"]["goal_id"] == load_session_state(store, SESSION).goal.id
    values = {v["component_id"]: v for v in result["values"]}
    assert values["C1"]["series"] == "unchanged" and values["C1"]["value"] == "100 nF"
    assert values["R1"]["series"] == "E12"
    r1 = values["R1"]["value_si"]
    assert result["predicted"], result
    for p in result["predicted"]:  # numbers survive the compact view (4 significant figures)
        assert p["predicted_gain_db"] == pytest.approx(
            _lowpass_db(p["frequency_hz"], r1, 1e-7), abs=0.01
        )
    assert result["predicted_goal_pass"] == all(p["passes"] for p in result["predicted"])
    assert result["feasible"] is True and result["predicted_goal_pass"] is True
    # A suggestion is a preview: no proposal, no revision, no simulation.
    assert store.list_docs("proposal", SESSION, EditProposal) == []
    assert len(store.list_docs("revision", SESSION, Revision)) == 1
    tool_results = [e for e in _events(service, run_id) if e.type == "tool_result"]
    assert tool_results[0].ok and "Prediction" in tool_results[0].summary


async def test_suggest_design_reports_the_predicted_tradeoff_for_an_infeasible_goal(
    settings, store
) -> None:
    rev = _revision(store)
    impossible = {
        "title": "Keep 440 Hz exactly, cut 1 kHz by 20 dB",
        "constraints": [
            {"kind": "preserve", "frequency_hz": 440, "limit_db": 0.1, "label": "Tone"},
            {"kind": "attenuate", "frequency_hz": 1000, "limit_db": 20, "label": "Buzz"},
        ],
    }
    provider = ScriptedProvider(
        [
            completion(None, [("set_goal", impossible)]),
            completion(None, [("suggest_design", {"vary": "C1"})]),
            completion("Prediction: this family cannot do both; here is the closest tradeoff."),
        ]
    )
    service = make_service(settings, store, provider, explore=FakeExplore(store))
    await _run(service, rev)
    result = last_tool_result(provider.requests[2])
    assert result["goal"]["title"] == impossible["title"]
    assert result["feasible"] is False and result["predicted_goal_pass"] is False
    assert result["tradeoff"].startswith("Prediction:")
    assert {v["component_id"]: v["series"] for v in result["values"]} == {
        "C1": "E12",
        "R1": "unchanged",
    }


async def test_family_switch_proposal_uses_the_target_family_and_is_never_applied(
    settings, store
) -> None:
    rev = _revision(store)  # low-pass
    provider = ScriptedProvider(
        [
            completion(
                None,
                [
                    (
                        "propose_values",
                        {
                            "template_id": "rc_highpass",
                            "changes": [
                                {"component_id": "R1", "value": "3.3k"},
                                {"component_id": "C1", "value": "100n"},
                            ],
                            "rationale": "A high-pass filter removes low hum.",
                        },
                    )
                ],
            ),
            completion("Prediction: the hum gets quieter. Apply it to measure."),
        ]
    )
    service = make_service(settings, store, provider, explore=FakeExplore(store))
    run_id = await _run(service, rev, "Remove the 50 Hz hum.")

    proposals = store.list_docs("proposal", SESSION, EditProposal)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.template_id == "rc_highpass" and proposal.base_revision_id == rev.id
    assert proposal.status == "pending"
    changes = {c.component_id: c for c in proposal.changes}
    assert set(changes) == {"R1", "C1"}, "a family switch lists both components"
    assert changes["R1"].from_value_si == 1000.0
    assert changes["R1"].to_value_si == pytest.approx(3.3e3)
    assert changes["C1"].to_value_si == pytest.approx(1e-7)
    # Predictions use the TARGET family's equations and its own test tones.
    predicted = {p.frequency_hz: p for p in proposal.predicted}
    assert {t.frequency_hz for t in TEMPLATE_TONES["rc_highpass"]} <= set(predicted)
    for p in proposal.predicted:
        assert p.basis == "analytical_first_order_rc"
        assert p.predicted_gain_db == pytest.approx(_highpass_db(p.frequency_hz, 3.3e3, 1e-7))
    result = last_tool_result(provider.requests[1])
    assert result["family_switch"] is True and result["applied"] is False
    assert result["template_id"] == "rc_highpass" and "not measurements" in result["note"]
    assert any(e.type == "proposal" for e in _events(service, run_id))
    # Never applied by the agent: the session still has only the original revision ...
    assert len(store.list_docs("revision", SESSION, Revision)) == 1
    # ... and the learner can apply it: a high-pass revision linked to the base for undo.
    applied = create_revision(
        store,
        SESSION,
        RevisionCreateRequest(
            parent_id=rev.id, origin="agent_proposal_applied", proposal_id=proposal.id
        ),
    )
    assert applied.template_id == "rc_highpass" and applied.parent_id == rev.id
    assert revision_values(applied) == pytest.approx({"R1": 3.3e3, "C1": 1e-7})
    stored = store.get("proposal", proposal.id, SESSION, EditProposal)
    assert stored is not None and stored.status == "applied"


async def test_family_switch_must_list_both_components(settings, store) -> None:
    rev = _revision(store)
    provider = ScriptedProvider(
        [
            completion(
                None,
                [
                    (
                        "propose_values",
                        {
                            "template_id": "rc_highpass",
                            "changes": [{"component_id": "R1", "value": "3.3k"}],
                            "rationale": "Only one value.",
                        },
                    )
                ],
            ),
            completion(
                None,
                [
                    (
                        "propose_values",
                        {
                            "template_id": "rc_lowpass",  # the current family: not a switch
                            "changes": [{"component_id": "R1", "value": "2.2k"}],
                            "rationale": "Lower the cutoff.",
                        },
                    )
                ],
            ),
            completion("Prediction: R1 = 2.2 kΩ lowers the cutoff."),
        ]
    )
    service = make_service(settings, store, provider, explore=FakeExplore(store))
    run_id = await _run(service, rev)
    results = [e for e in _events(service, run_id) if e.type == "tool_result"]
    assert [r.ok for r in results] == [False, True]
    assert results[0].error is not None and results[0].error.code == "validation_error"
    assert "both R1 and C1" in results[0].error.message
    proposals = store.list_docs("proposal", SESSION, EditProposal)
    assert len(proposals) == 1 and proposals[0].template_id is None
    assert [c.component_id for c in proposals[0].changes] == ["R1"]


def _learn_attempt(rev: Revision, base_exp: str, new_exp: str, *, score: int) -> LearnAttempt:
    clip = AudioClip(
        artifact_id="art_SECRETLEARNCLIP1",
        url="/api/artifacts/art_SECRETLEARNCLIP1",
        role="output",
        label="Output",
        duration_s=1.2,
        sample_rate_hz=48_000,
        peak_fs=0.4,
        clipped=False,
    )
    return LearnAttempt(
        id=new_id("learn"),
        template_id="rc_lowpass",
        base_revision_id=rev.id,
        new_revision_id=rev.id,
        base_experiment_id=base_exp,
        new_experiment_id=new_exp,
        change=LearnChange(component_id="C1", multiplier=2.0),
        predictions=[TonePredictionInput(frequency_hz=4000.0, expectation="quieter")],
        reasoning="A bigger capacitor shorts more treble.",
        graded=[
            GradedPrediction(
                frequency_hz=4000.0,
                label="Whistle",
                role="interference",
                predicted="quieter",
                observed="quieter",
                delta_db=-5.123456,
                correct=True,
                evidence=[EvidenceRef(result_id=new_exp, quantity="output_gain_db")],
            )
        ],
        score=score,
        total=1,
        comparison=ExperimentComparison(
            a_id=base_exp,
            b_id=new_exp,
            a_revision_id=rev.id,
            b_revision_id=rev.id,
            changes=[],
            tones=[],
            audio=AudioComparison(
                id=new_id("cmp"),
                clips=[clip],
                playback_gain_fs_per_v=1.0,
                fade_ms=15.0,
                note="Shared gain.",
            ),
            summary="The whistle is 5.1 dB quieter.",
        ),
        explanation="Doubling C1 halved the cutoff, so the whistle fell by 5.1 dB.",
        tradeoff="The musical tone also fell slightly.",
        created_at=utcnow(),
    )


async def test_read_learn_attempt_returns_measured_results_without_audio(settings, store) -> None:
    rev = _revision(store)
    explore = FakeExplore(store)
    base_exp = (await explore.run_experiment(SESSION, rev.id, None, None)).id
    new_exp = (await explore.run_experiment(SESSION, rev.id, None, None)).id
    older = _learn_attempt(rev, base_exp, new_exp, score=0)
    latest = _learn_attempt(rev, base_exp, new_exp, score=1)
    store.put("learn_attempt", older.id, SESSION, older)
    store.put("learn_attempt", latest.id, SESSION, latest)
    provider = ScriptedProvider(
        [
            completion(None, [("read_learn_attempt", {})]),
            completion(None, [("read_learn_attempt", {"attempt_id": new_id("learn")})]),
            completion(None, [("read_learn_attempt", {"attempt_id": older.id})]),
            lambda body: completion(
                f"The whistle fell by about 5 dB [{_first_tool_result(body)['new_experiment_id']}]."
            ),
        ]
    )
    service = make_service(settings, store, provider, explore=explore)
    run_id = await _run(service, rev, "Why did that happen?")

    first = last_tool_result(provider.requests[1])
    assert first["attempt_id"] == latest.id, "no ID means the latest attempt"
    assert first["change"] == {"component_id": "C1", "factor": 2.0}
    assert first["graded"][0]["measured_delta_db"] == -5.123
    assert first["explanation"] == latest.explanation and first["score"] == 1
    text = json.dumps(first)
    assert "art_SECRETLEARNCLIP1" not in text and "/api/artifacts" not in text
    assert not ({"audio", "clips", "url", "artifact_id"} & set(_all_keys(first)))
    assert last_tool_result(provider.requests[2])["error"]["code"] == "not_found"
    assert last_tool_result(provider.requests[3])["attempt_id"] == older.id
    message = [e for e in _events(service, run_id) if e.type == "message"][-1]
    assert [c.result_id for c in message.citations] == [new_exp], "measured claim is cited"
    assert len(store.list_docs("revision", SESSION, Revision)) == 1


def _first_tool_result(body: dict[str, Any]) -> dict[str, Any]:
    content = next(m["content"] for m in body["messages"] if m["role"] == "tool")
    result: dict[str, Any] = json.loads(content)
    return result


def _all_keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _all_keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _all_keys(v)}
    return set()
