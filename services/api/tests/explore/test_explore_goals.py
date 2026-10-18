"""Goal evaluation against measured gain, and labeled analytical feasibility."""

from __future__ import annotations

import pytest

from resonance.contracts.experiments import Goal, GoalConstraint
from resonance.explore.goals import (
    analyze_feasibility,
    default_goal,
    evaluate_goal,
    goal_frequencies,
)

EXP_ID = "exp_abcdefghijkl"


def test_measured_gains_decide_each_constraint_with_evidence() -> None:
    goal = default_goal()
    # Gains as an AC analysis would report them (values only illustrate the arithmetic).
    evaluation = evaluate_goal(
        goal,
        experiment_id=EXP_ID,
        template_id="rc_lowpass",
        measured_gain_db={440.0: -0.32, 4000.0: -8.64},
    )
    preserve, attenuate = evaluation.results
    assert preserve.passed and preserve.margin_db == pytest.approx(1.0 - 0.32)
    assert preserve.measured_attenuation_db == pytest.approx(0.32)
    assert not attenuate.passed and attenuate.margin_db == pytest.approx(8.64 - 12.0)
    assert not evaluation.passed
    for result in evaluation.results:
        assert result.evidence.result_id == EXP_ID
        assert result.evidence.quantity == "output_gain_db"
        assert result.evidence.frequency_hz == result.constraint.frequency_hz
    assert evaluation.feasibility.basis == "analytical_first_order_rc"
    assert evaluation.feasibility.explanation.startswith("Prediction:")


def test_boundary_values_pass_and_missing_measurements_never_pass() -> None:
    goal = default_goal()
    exact = evaluate_goal(
        goal,
        experiment_id=EXP_ID,
        template_id="rc_lowpass",
        measured_gain_db={440.0: -1.0, 4000.0: -12.0},
    )
    assert exact.passed and all(r.margin_db == 0.0 for r in exact.results)
    with pytest.raises(KeyError):
        evaluate_goal(
            goal,
            experiment_id=EXP_ID,
            template_id="rc_lowpass",
            measured_gain_db={440.0: -0.5},
        )


def test_default_goal_feasibility_range_is_a_labeled_prediction() -> None:
    analysis = analyze_feasibility(default_goal(), "rc_lowpass")
    assert analysis.feasible
    assert analysis.cutoff_range_hz is not None
    lo, hi = analysis.cutoff_range_hz
    # fc ≥ 440/√(10^0.1 − 1) and fc ≤ 4000/√(10^1.2 − 1)
    assert lo == pytest.approx(864.70, abs=0.01)
    assert hi == pytest.approx(1038.04, abs=0.01)
    assert "between 865 Hz and 1,038 Hz" in analysis.explanation


def test_infeasible_goal_is_explained() -> None:
    goal = Goal(
        id="goal_abcdefghijkl",
        title="Impossible",
        origin="user",
        constraints=[
            GoalConstraint(kind="preserve", frequency_hz=3000.0, limit_db=0.5, label="Tone"),
            GoalConstraint(kind="attenuate", frequency_hz=4000.0, limit_db=20.0, label="Whistle"),
        ],
    )
    analysis = analyze_feasibility(goal, "rc_lowpass")
    assert not analysis.feasible and analysis.cutoff_range_hz is None
    assert "tradeoff" in analysis.explanation
    assert goal_frequencies(goal) == (3000.0, 4000.0)
