"""Agent-visible views (allow-list), tool schemas, and text hygiene helpers."""

from __future__ import annotations

import json
import math

import pytest
from agent_testkit import SECRET_SEED, SESSION, WAVEFORM_MARKER, FakeCaseService

from resonance.agent import views
from resonance.agent.case_tools import CaseRunContext, hint_toolset, investigate_toolset
from resonance.agent.explore_tools import ExploreRunContext, explore_toolset
from resonance.agent.loop import cap_arguments
from resonance.agent.text import check_citations, strip_reasoning
from resonance.contracts.internal import CancelToken
from resonance.contracts.investigation import InstrumentKind

FORBIDDEN_KEYS = {
    "reveal",
    "seed",
    "distinguishing_instruments",
    "assessments",
    "audio",
    "waveform",
    "artifact_id",
    "url",
    "settings",
}


def _keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, sub in value.items():
            found.add(key)
            found |= _keys(sub)
    elif isinstance(value, list):
        for sub in value:
            found |= _keys(sub)
    return found


def test_case_view_is_an_allow_list() -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    view = views.case_view(cases.public(), run_actions=["run_experiment", "cancel_agent"])
    text = json.dumps(view, allow_nan=False)  # also proves there is no NaN/Infinity
    assert not (_keys(view) & FORBIDDEN_KEYS)
    assert str(WAVEFORM_MARKER) not in text
    assert "art_SECRETARTIFACT01" not in text and "/api/artifacts" not in text
    assert "STATEMENT-MARKER" not in text, "evidence statements only via assess_evidence"
    assert "hypothesis_status" not in view and "remaining" not in view
    assert view["available_actions"] == ["run_experiment"], "only actions the agent has tools for"
    assert set(view) >= {
        "case_id",
        "status",
        "mode",
        "hints_used",
        "budget",
        "intended_design",
        "instruments",
        "hypotheses",
        "evidence_tolerances",
        "observations",
    }
    symptom = view["observations"][0]
    assert symptom["kind"] == "symptom"
    tone = symptom["transient"]["tones"][1]
    assert set(tone) >= {"amplitude", "expected_amplitude", "gain_db", "expected_phase_deg"}
    components = {c["id"]: c for c in view["intended_design"]["components"]}
    assert components["R1"]["value"] == "1 kΩ" and "Output" in components["C1"]["description"]


def test_observation_view_rounds_sweeps_to_four_significant_figures() -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    obs = cases._sweep(InstrumentKind.SOURCE_CURRENT_SWEEP, None, "user")
    view = views.observation_view(obs)
    rows = view["sweep"]["rows"]
    assert len(rows) == 31
    for row in rows:
        for value in row:
            digits = f"{abs(value):.15g}".replace(".", "").lstrip("0").rstrip("0")
            assert len(digits) <= 4, value


def test_evidence_view_has_statuses_but_never_distinguishing_instruments() -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    view = views.evidence_view(cases.public())
    assert set(view) == {"hypothesis_status", "remaining", "statement"}
    assert "distinguishing" not in json.dumps(view)


def test_views_refuse_closed_cases() -> None:
    import asyncio

    cases = FakeCaseService(SESSION, mode="agent")
    closed = asyncio.run(cases.close_case(SESSION, cases.case_id))
    assert closed.reveal is not None and closed.reveal.seed == SECRET_SEED
    with pytest.raises(views.ClosedCaseError):
        views.case_view(closed, run_actions=[])
    with pytest.raises(views.ClosedCaseError):
        views.evidence_view(closed)


def test_sig_rounding() -> None:
    assert views.sig(1234.5678) == 1235.0
    assert views.sig(0.000123456) == 0.0001235
    assert views.sig(-38.2716) == -38.27
    assert views.sig(0.0) == 0.0
    assert views.sig(math.inf) is None and views.sig(math.nan) is None


def test_tool_schemas_are_self_contained_and_sets_are_disjoint() -> None:
    case_ctx = CaseRunContext(SESSION, "case_x", "run_xxxxxxxx", None, CancelToken())  # type: ignore[arg-type]
    explore_ctx = ExploreRunContext(SESSION, "run_x", "rev_x", None, None, None, CancelToken())  # type: ignore[arg-type]
    sets = {
        "investigate": investigate_toolset(case_ctx),
        "hint": hint_toolset(case_ctx),
        "explore": explore_toolset(explore_ctx),
    }
    assert set(sets["hint"].names) == {"get_case", "read_observation"}
    assert set(sets["explore"].names) == {
        "inspect_circuit",
        "set_goal",
        "propose_values",
        "run_experiment",
        "compare_experiments",
    }
    assert "assess_evidence" in sets["investigate"].names
    assert not set(sets["explore"].names) & {"get_case", "submit_diagnosis", "propose_repair"}
    for toolset in sets.values():
        for schema in toolset.schemas():
            text = json.dumps(schema)
            assert "$ref" not in text and "$defs" not in text
            assert schema["function"]["parameters"]["type"] == "object"
    diagnosis = next(
        s for s in sets["investigate"].schemas() if s["function"]["name"] == "submit_diagnosis"
    )
    params = diagnosis["function"]["parameters"]
    quantity = params["properties"]["citations"]["items"]["properties"]["quantity"]
    assert "tone_gain_db" in quantity["enum"]


@pytest.mark.parametrize(
    ("raw", "visible"),
    [
        ("<think>secret</think>Answer", "Answer"),
        ("secret plan</think>\nAnswer", "Answer"),
        ("Answer<think>unfinished secret", "Answer"),
        ("<THINKING>x</THINKING> A <reasoning>y</reasoning>B", "A B"),
        (None, ""),
    ],
)
def test_strip_reasoning(raw: str | None, visible: str) -> None:
    assert strip_reasoning(raw) == visible


def test_check_citations_marks_unknown_ids() -> None:
    known = {"obs_abcdefgh1234": "output_gain_db"}
    checked = check_citations(
        "Gain fell [obs_abcdefgh1234]; current [obs_zzzzzzzz9999] and [exp_qqqqqqqq0000].",
        known,  # type: ignore[arg-type]
    )
    assert [c.result_id for c in checked.citations] == ["obs_abcdefgh1234"]
    assert checked.unverified == ["obs_zzzzzzzz9999", "exp_qqqqqqqq0000"]
    assert checked.text.count("[unverified citation]") == 2


def test_cap_arguments_limits_event_size() -> None:
    args = {"explanation": "x" * 1200, "citations": [{"note": "n" * 240} for _ in range(8)]}
    capped = cap_arguments(args)
    assert len(json.dumps(capped).encode()) <= 2048
    assert capped["explanation"].endswith("…")
    small = {"instrument": "source_current_sweep"}
    assert cap_arguments(small) == small
