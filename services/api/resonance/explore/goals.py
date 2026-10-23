"""Filtering goals: defaults, validation, analytical feasibility (owner: audio & signal engineer).

Goals are *requested constraints*. ``evaluate_goal`` judges them against MEASURED gain from an
ngspice AC analysis at exactly each constraint frequency, and every result cites that
measurement (``EvidenceRef``). Feasibility here is an analytical prediction for the first-order
RC family, labeled as such; it is never used to decide whether a constraint passed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal

from ..circuits.templates import TemplateUnavailableError, get_template
from ..contracts.circuits import TemplateId
from ..contracts.common import EvidenceRef, QuantityName, new_id
from ..contracts.experiments import (
    ConstraintResult,
    FeasibilityAnalysis,
    Goal,
    GoalConstraint,
    GoalEvaluation,
    GoalInput,
)
from ..contracts.policy import TEMPLATE_DEFAULT_GOALS

# Closed QuantityName vocabulary: gain of V(out)/V(in) from the AC analysis at that frequency.
MEASURED_GAIN_QUANTITY: QuantityName = "output_gain_db"

DEFAULT_GOAL_TITLE = TEMPLATE_DEFAULT_GOALS["rc_lowpass"][0]


def default_goal(template_id: TemplateId = "rc_lowpass") -> Goal:
    """The template's opening goal (``policy.TEMPLATE_DEFAULT_GOALS``) with a fresh ID."""
    title, constraints = TEMPLATE_DEFAULT_GOALS[template_id]
    return Goal(
        id=new_id("goal"),
        title=title,
        origin="default",
        constraints=[
            GoalConstraint(
                kind="preserve" if kind == "preserve" else "attenuate",
                frequency_hz=frequency_hz,
                limit_db=limit_db,
                label=label,
            )
            for kind, frequency_hz, limit_db, label in constraints
        ],
    )


def goal_from_input(body: GoalInput, origin: Literal["user", "agent"]) -> Goal:
    return Goal(
        id=new_id("goal"),
        title=body.title,
        constraints=list(body.constraints),
        origin=origin,
    )


def predicted_attenuation_db(template_id: str, frequency_hz: float, cutoff_hz: float) -> float:
    """First-order RC attenuation (dB, ≥ 0) at ``frequency_hz`` for a cutoff (a prediction)."""
    if template_id == "rc_lowpass":
        ratio = frequency_hz / cutoff_hz
    elif template_id == "rc_highpass":
        ratio = cutoff_hz / frequency_hz
    else:
        raise ValueError(f"unknown template {template_id}")
    return 10.0 * math.log10(1.0 + ratio * ratio)


def constraint_margin_db(constraint: GoalConstraint, attenuation_db: float) -> float:
    """Room left (dB) by one constraint at this attenuation; negative = it fails by that much.

    preserve: limit − attenuation; attenuate: attenuation − limit (same rule as measured)."""
    if constraint.kind == "preserve":
        return constraint.limit_db - attenuation_db
    return attenuation_db - constraint.limit_db


def margin_rises_with_cutoff(template_id: str, constraint: GoalConstraint) -> bool:
    """Whether raising the cutoff increases this constraint's margin (it never stays flat
    for a positive limit): low-pass preserve / high-pass attenuate rise; the others fall."""
    lowpass = template_id == "rc_lowpass"
    return (constraint.kind == "preserve") == lowpass


def cutoff_interval(template_id: str, c: GoalConstraint) -> tuple[float, float]:
    """Cutoff range [lo, hi] (Hz, possibly 0 or ∞) for which one constraint holds."""
    return _cutoff_interval(template_id, c)


def supported_cutoffs(template_id: str) -> tuple[float, float]:
    """The template's supported cutoff range (revision creation enforces it)."""
    return _supported_cutoffs(template_id)


def _cutoff_interval(template_id: str, c: GoalConstraint) -> tuple[float, float]:
    """Cutoff range [lo, hi] (Hz) for which one constraint holds, first-order RC.

    Attenuation A(f) = 10·log10(1 + (f/fc)^2) for low-pass, 10·log10(1 + (fc/f)^2) for
    high-pass. Let k = 10^(limit/10) − 1 (≥ 0).
    """
    k = 10 ** (c.limit_db / 10.0) - 1.0
    f = c.frequency_hz
    if template_id == "rc_lowpass":
        # preserve: (f/fc)^2 ≤ k → fc ≥ f/√k ; attenuate: fc ≤ f/√k
        bound = math.inf if k == 0 else f / math.sqrt(k)
        return (bound, math.inf) if c.kind == "preserve" else (0.0, bound)
    if template_id == "rc_highpass":
        # preserve: (fc/f)^2 ≤ k → fc ≤ f·√k ; attenuate: fc ≥ f·√k
        bound = f * math.sqrt(k)
        return (0.0, bound) if c.kind == "preserve" else (bound, math.inf)
    raise ValueError(f"unknown template {template_id}")


def _supported_cutoffs(template_id: str) -> tuple[float, float]:
    """The cutoff range this lab accepts for the template (revision creation enforces it)."""
    try:
        template = get_template(template_id)
    except TemplateUnavailableError:
        return 0.0, math.inf
    return template.cutoff_min_hz, template.cutoff_max_hz


def analyze_feasibility(goal: Goal, template_id: str) -> FeasibilityAnalysis:
    lo, hi = 0.0, math.inf
    for c in goal.constraints:
        a, b = _cutoff_interval(template_id, c)
        lo, hi = max(lo, a), min(hi, b)
    family = "low-pass" if template_id == "rc_lowpass" else "high-pass"
    supported_lo, supported_hi = _supported_cutoffs(template_id)
    if lo <= hi and hi > 0 and (lo > supported_hi or hi < supported_lo):
        # Achievable by the ideal formula, but only with a cutoff this lab does not accept.
        return FeasibilityAnalysis(
            template_id=template_id,
            feasible=False,
            cutoff_range_hz=None,
            explanation=(
                f"Prediction: meeting every constraint would need a first-order RC {family} "
                f"cutoff outside this lab's supported range ({supported_lo:,.0f} Hz to "
                f"{supported_hi:,.0f} Hz). Relax a limit or accept a measured tradeoff."
            ),
        )
    lo, hi = max(lo, supported_lo), min(hi, supported_hi)
    if lo <= hi and lo < math.inf and hi > 0:
        rng = (lo, hi) if math.isfinite(hi) else (lo, 1e9)
        if lo > 0 and math.isfinite(hi):
            where = f"between {lo:,.0f} Hz and {hi:,.0f} Hz"
        elif lo > 0:
            where = f"at or above {lo:,.0f} Hz"
        elif math.isfinite(hi):
            where = f"at or below {hi:,.0f} Hz"
        else:
            where = "anywhere"
        return FeasibilityAnalysis(
            template_id=template_id,
            feasible=True,
            cutoff_range_hz=rng,
            explanation=(
                f"Prediction: a first-order RC {family} filter meets every constraint when its "
                f"cutoff is {where}. Measure to confirm."
            ),
        )
    return FeasibilityAnalysis(
        template_id=template_id,
        feasible=False,
        cutoff_range_hz=None,
        explanation=(
            f"Prediction: no first-order RC {family} cutoff satisfies every constraint at once. "
            "Relax a limit or accept a measured tradeoff."
        ),
    )


def goal_frequencies(goal: Goal) -> tuple[float, ...]:
    """Distinct constraint frequencies, in order of first appearance."""
    seen: list[float] = []
    for c in goal.constraints:
        if c.frequency_hz not in seen:
            seen.append(c.frequency_hz)
    return tuple(seen)


def evaluate_constraint(
    constraint: GoalConstraint, measured_gain_db: float, evidence: EvidenceRef
) -> ConstraintResult:
    """preserve: attenuation ≤ limit. attenuate: attenuation ≥ limit. Margin > 0 = room left."""
    attenuation = -measured_gain_db
    margin = constraint_margin_db(constraint, attenuation)
    return ConstraintResult(
        constraint=constraint,
        measured_gain_db=measured_gain_db,
        measured_attenuation_db=attenuation,
        passed=margin >= 0.0,
        margin_db=margin,
        evidence=evidence,
    )


def evaluate_goal(
    goal: Goal,
    *,
    experiment_id: str,
    template_id: str,
    measured_gain_db: Mapping[float, float],
) -> GoalEvaluation:
    """Evaluate every constraint against measured AC gain at exactly its frequency.

    ``measured_gain_db`` maps frequency (Hz) → 20·log10|V(out)/V(in)| from an ngspice AC
    analysis recorded under ``experiment_id``. A missing frequency is an error, never a pass.
    """
    results: list[ConstraintResult] = []
    for c in goal.constraints:
        if c.frequency_hz not in measured_gain_db:
            raise KeyError(f"no measurement at {c.frequency_hz} Hz")
        evidence = EvidenceRef(
            result_id=experiment_id,
            quantity=MEASURED_GAIN_QUANTITY,
            frequency_hz=c.frequency_hz,
        )
        results.append(evaluate_constraint(c, measured_gain_db[c.frequency_hz], evidence))
    return GoalEvaluation(
        goal_id=goal.id,
        experiment_id=experiment_id,
        passed=all(r.passed for r in results),
        results=results,
        feasibility=analyze_feasibility(goal, template_id),
    )
