"""Design suggestions: analytical goal-to-E12 previews for both circuit families.

Expected numbers are recomputed here from the closed-form first-order attenuation
A(f) = 10·log10(1 + (f/fc)²) (low-pass) or 10·log10(1 + (fc/f)²) (high-pass), independently
of the implementation. No ngspice: suggestions are predictions; measuring them is the
ordinary experiment path.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resonance.api.errors import ApiException
from resonance.app import create_app
from resonance.circuits.revisions import build_revision
from resonance.circuits.templates import validate_values
from resonance.config import Settings
from resonance.contracts.common import ErrorCode
from resonance.contracts.experiments import Goal, GoalConstraint
from resonance.contracts.policy import E12_SERIES
from resonance.explore.goals import default_goal
from resonance.explore.suggest import suggest_design

# Analytical feasible cutoffs of the default goals (policy.TEMPLATE_DEFAULT_GOALS).
FEASIBLE = {"rc_lowpass": (864.70, 1038.04), "rc_highpass": (192.68, 223.92)}


def _attenuation(template: str, f: float, fc: float) -> float:
    ratio = f / fc if template == "rc_lowpass" else fc / f
    return 10 * math.log10(1 + ratio * ratio)


def _margin(template: str, c: GoalConstraint, fc: float) -> float:
    a = _attenuation(template, c.frequency_hz, fc)
    return c.limit_db - a if c.kind == "preserve" else a - c.limit_db


def _goal(*constraints: tuple[str, float, float, str]) -> Goal:
    return Goal(
        id="goal_abcdefghijkl",
        title="Test goal",
        origin="user",
        constraints=[
            GoalConstraint(kind=k, frequency_hz=f, limit_db=lim, label=lab)
            for k, f, lim, lab in constraints
        ],
    )


def _all_e12(lo: float, hi: float) -> list[float]:
    return [
        float(f"{m}e{e}")
        for e in range(-12, 7)
        for m in E12_SERIES
        if lo * (1 - 1e-12) <= float(f"{m}e{e}") <= hi * (1 + 1e-12)
    ]


@pytest.mark.parametrize(
    ("template", "vary", "expected_display"),
    [
        ("rc_lowpass", "R1", "1.8 kΩ"),
        ("rc_lowpass", "C1", "180 nF"),
        ("rc_highpass", "R1", "8.2 kΩ"),
        ("rc_highpass", "C1", "82 nF"),
    ],
)
def test_default_goal_suggestion_is_the_only_passing_e12_part(
    template: str, vary: str, expected_display: str
) -> None:
    revision = build_revision(template, {}, origin="template_default")  # type: ignore[arg-type]
    goal = default_goal(template)  # type: ignore[arg-type]
    s = suggest_design(revision, goal, vary=vary)  # type: ignore[arg-type]
    lo, hi = FEASIBLE[template]
    assert s.feasible and s.basis == "analytical_first_order_rc"
    assert s.template_id == template and s.revision_id == revision.id
    assert lo <= s.target_cutoff_hz <= hi
    assert s.target_cutoff_hz == pytest.approx(math.sqrt(lo * hi), rel=1e-4)
    varied, kept = s.values
    assert (varied.component_id, varied.series, varied.display) == (vary, "E12", expected_display)
    other = "C1" if vary == "R1" else "R1"
    kept_si = next(float(c.value_si or 0.0) for c in revision.components if c.id == other)
    assert (kept.component_id, kept.series, kept.value_si) == (other, "unchanged", kept_si)
    r = varied.value_si if vary == "R1" else kept.value_si
    c = kept.value_si if vary == "R1" else varied.value_si
    fc = 1 / (2 * math.pi * r * c)
    assert s.predicted_cutoff_hz == pytest.approx(fc, rel=1e-12)
    assert lo <= fc <= hi
    for constraint, p in zip(goal.constraints, s.predicted, strict=True):
        assert p.constraint_label == constraint.label
        assert p.frequency_hz == constraint.frequency_hz
        assert -p.predicted_gain_db == pytest.approx(
            _attenuation(template, constraint.frequency_hz, fc), abs=1e-9
        )
        assert p.margin_db == pytest.approx(_margin(template, constraint, fc), abs=1e-9)
        assert p.passes
    assert s.predicted_goal_pass and s.tradeoff is None
    assert s.explanation.startswith("Prediction from first-order RC equations, not a measurement")
    assert expected_display in s.explanation
    # Brute force over every in-range E12 value: the suggestion is the only passing part.
    bounds = next(comp.bounds for comp in revision.components if comp.id == vary)
    assert bounds is not None
    passing = []
    for value in _all_e12(bounds.min_si, bounds.max_si):
        rr, cc = (value, kept_si) if vary == "R1" else (kept_si, value)
        try:
            validate_values(template, {"R1": rr, "C1": cc})
        except ValueError:
            continue
        f = 1 / (2 * math.pi * rr * cc)
        if all(_margin(template, k, f) >= 0 for k in goal.constraints):
            passing.append(value)
    assert passing == [varied.value_si]


def test_rounding_miss_reports_the_shortfall_of_the_closest_standard_part() -> None:
    """Feasible cutoffs 916.9–1000.6 Hz lie between the E12 neighbours 1.8 kΩ (884 Hz) and
    1.5 kΩ (1061 Hz) with C1 = 100 nF, so rounding must miss and say by how much."""
    goal = _goal(("preserve", 440.0, 0.9, "Musical tone"), ("attenuate", 4000.0, 12.3, "Whistle"))
    revision = build_revision("rc_lowpass", {}, origin="template_default")
    s = suggest_design(revision, goal, vary="R1")
    assert s.feasible and not s.predicted_goal_pass
    assert s.values[0].display == "1.8 kΩ"  # smaller worst shortfall than 1.5 kΩ
    fc = 1 / (2 * math.pi * 1.8e3 * 100e-9)
    shortfall = _attenuation("rc_lowpass", 440.0, fc) - 0.9
    assert s.tradeoff is not None
    assert "rounding to the nearest standard part" in s.tradeoff
    assert f"Musical tone (440 Hz) misses by {shortfall:.2f} dB" in s.tradeoff
    assert "Whistle" not in s.tradeoff  # it passes, so it is not named as a shortfall
    assert [p.passes for p in s.predicted] == [False, True]


@pytest.mark.parametrize(
    ("template", "constraints"),
    [
        (
            "rc_lowpass",
            (("preserve", 440.0, 1.0, "Musical tone"), ("attenuate", 600.0, 12.0, "Whistle")),
        ),
        (
            "rc_highpass",
            (("preserve", 440.0, 0.5, "Musical tone"), ("attenuate", 100.0, 20.0, "Mains hum")),
        ),
    ],
)
def test_infeasible_goal_gets_the_best_compromise_and_a_tradeoff(
    template: str, constraints: tuple[tuple[str, float, float, str], ...]
) -> None:
    goal = _goal(*constraints)
    revision = build_revision(template, {}, origin="template_default")  # type: ignore[arg-type]
    s = suggest_design(revision, goal, vary="R1")
    assert not s.feasible and not s.predicted_goal_pass
    # The compromise target equalises the two (negative) margins.
    m = [_margin(template, c, s.target_cutoff_hz) for c in goal.constraints]
    assert m[0] < 0 and m[0] == pytest.approx(m[1], abs=1e-6)
    assert s.tradeoff is not None and s.tradeoff.startswith("Prediction:")
    fc = s.predicted_cutoff_hz
    for c, p in zip(goal.constraints, s.predicted, strict=True):
        assert not p.passes
        shortfall = -_margin(template, c, fc)
        assert f"{c.label} ({c.frequency_hz:g} Hz) misses by {shortfall:.2f} dB" in s.tradeoff
    assert "Relax a limit" in s.tradeoff


def test_ideal_value_outside_the_component_range_points_to_the_other_component() -> None:
    revision = build_revision("rc_lowpass", {"R1": 100.0, "C1": 10e-6}, origin="user_edit")
    s = suggest_design(revision, default_goal("rc_lowpass"), vary="R1")
    assert s.values[0].display == "100 Ω" and not s.predicted_goal_pass
    assert "outside the supported 100 Ω to 100 kΩ" in s.explanation
    assert s.tradeoff is not None and "Try choosing C1 instead" in s.tradeoff
    fixed = suggest_design(revision, default_goal("rc_lowpass"), vary="C1")
    assert fixed.values[0].display == "1.8 µF" and fixed.predicted_goal_pass


def test_frozen_case_design_is_refused() -> None:
    frozen = build_revision("rc_highpass", {}, origin="case_intended", frozen=True)
    with pytest.raises(ApiException) as info:
        suggest_design(frozen, default_goal("rc_highpass"), vary="R1")
    assert info.value.error.code == ErrorCode.STALE_REVISION and info.value.status == 409


def test_suggest_route_scopes_revisions_to_the_session(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        resonance_data_dir=tmp_path / "data",
        web_dist_dir=tmp_path / "no-web",
        nebius_api_key=None,
        nebius_model=None,
        resonance_dev_agent_fixture=False,
    )
    with TestClient(create_app(settings)) as client:
        rev = client.post("/api/revisions", json={"template_id": "rc_highpass"}).json()
        title, goal = "Keep the musical tone. Remove the mains hum.", default_goal("rc_highpass")
        constraints = [c.model_dump() for c in goal.constraints]
        put = client.put("/api/workbench/goal", json={"title": title, "constraints": constraints})
        assert put.status_code == 200, put.text
        ok = client.post("/api/workbench/goal/suggest", json={"revision_id": rev["id"]})
        assert ok.status_code == 200, ok.text
        assert ok.json()["values"][0]["display"] == "8.2 kΩ"
        frozen = build_revision("rc_lowpass", {}, origin="case_intended", frozen=True)
        session = client.cookies.get("rs_session")
        client.app.state.store.put("revision", frozen.id, session, frozen)  # type: ignore[attr-defined]
        refused = client.post("/api/workbench/goal/suggest", json={"revision_id": frozen.id})
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "stale_revision"
        client.cookies.clear()
        other = client.post("/api/workbench/goal/suggest", json={"revision_id": rev["id"]})
        assert other.status_code == 404
        assert other.json()["error"]["code"] == "not_found"
