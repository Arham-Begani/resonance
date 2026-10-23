"""Goal-to-design suggestions (owner: audio & signal engineer).

``suggest_design(revision, goal, vary) -> DesignSuggestion`` is an ANALYTICAL preview built
from first-order RC equations and labeled as a prediction. Nothing here is a measurement: the
learner applies the suggestion as an ordinary revision and measures it with ngspice.

Algorithm (template = the revision's circuit family):

1. Feasible cutoffs: the intersection of every constraint's cutoff interval with the
   template's supported range (``goals.analyze_feasibility``, the same analysis the workbench
   shows).
2. Target cutoff: if that interval is non-empty, its geometric centre √(lo·hi), the middle on
   the logarithmic frequency axis on which first-order responses are symmetric. Otherwise the
   best compromise: the supported cutoff that maximises the smallest predicted constraint
   margin. Every margin is strictly monotonic in the cutoff, so that maximum lies where the
   weakest rising and weakest falling margins cross (bisection in log f) or at a range end.
3. Ideal value of the varied component with the other kept: R1 = 1/(2π·fc·C1) or
   C1 = 1/(2π·fc·R1).
4. Standard part: the ``NEIGHBOURS`` E12 values closest to the ideal (log distance) among those
   inside the component's bounds whose design stays inside the supported cutoff range. The
   winner passes every constraint with the largest minimum margin; if none passes, it has the
   smallest worst shortfall; ties go to the value closer to the ideal. Because the passing
   values form one contiguous range around the ideal, checking the nearest neighbours on each
   side cannot miss a passing E12 value.
5. A ``tradeoff`` statement names each failing constraint and its predicted shortfall in dB
   whenever the goal is infeasible or the chosen standard part misses a limit.

Model assumptions are those of ``simulation.analytic``: ideal source, ideal R and C, no load,
no parasitics, no component tolerances.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ..api.errors import ApiException
from ..circuits.revisions import revision_values
from ..circuits.templates import get_template, validate_values
from ..circuits.units import ValueParseError, format_frequency, format_value
from ..contracts.circuits import Revision, TemplateId
from ..contracts.common import ErrorCode
from ..contracts.experiments import (
    DesignSuggestion,
    Goal,
    GoalConstraint,
    PredictedGoalTone,
    SuggestedValue,
)
from ..contracts.policy import DB_FLOOR, E12_SERIES
from ..simulation import analytic
from .goals import (
    analyze_feasibility,
    constraint_margin_db,
    margin_rises_with_cutoff,
    predicted_attenuation_db,
    supported_cutoffs,
)

NEIGHBOURS = 4
_BISECTION_STEPS = 80

_FAMILY = {"rc_lowpass": "low-pass", "rc_highpass": "high-pass"}
_OTHER: dict[str, Literal["R1", "C1"]] = {"R1": "C1", "C1": "R1"}


@dataclass(frozen=True)
class _Candidate:
    value_si: float
    r_ohm: float
    c_farad: float
    cutoff_hz: float
    predicted: tuple[PredictedGoalTone, ...]

    @property
    def passes(self) -> bool:
        return all(p.passes for p in self.predicted)

    @property
    def min_margin_db(self) -> float:
        return min(p.margin_db for p in self.predicted)


def _stale() -> ApiException:
    return ApiException(
        ErrorCode.STALE_REVISION,
        "This design belongs to an open investigation. Suggestions are made only for your own "
        "workbench revisions.",
        field="revision_id",
    )


def _predict(
    template_id: TemplateId, constraints: Sequence[GoalConstraint], r_ohm: float, c_farad: float
) -> tuple[PredictedGoalTone, ...]:
    values = {"R1": r_ohm, "C1": c_farad}
    out: list[PredictedGoalTone] = []
    for c in constraints:
        h = analytic.transfer(template_id, c.frequency_hz, values)
        gain = float(analytic.gain_db(h, floor_db=DB_FLOOR))
        margin = constraint_margin_db(c, -gain)
        out.append(
            PredictedGoalTone(
                constraint_label=c.label,
                frequency_hz=c.frequency_hz,
                predicted_gain_db=gain,
                passes=margin >= 0.0,
                margin_db=margin,
            )
        )
    return tuple(out)


def _worst_margin(template_id: str, group: Sequence[GoalConstraint], cutoff_hz: float) -> float:
    return min(
        constraint_margin_db(c, predicted_attenuation_db(template_id, c.frequency_hz, cutoff_hz))
        for c in group
    )


def best_compromise_cutoff(
    template_id: str, constraints: Sequence[GoalConstraint], lo_hz: float, hi_hz: float
) -> float:
    """Cutoff in [lo, hi] that maximises the smallest predicted margin (see module doc)."""
    rising = [c for c in constraints if margin_rises_with_cutoff(template_id, c)]
    falling = [c for c in constraints if not margin_rises_with_cutoff(template_id, c)]
    if not rising:
        return lo_hz  # every margin falls as the cutoff rises
    if not falling:
        return hi_hz

    def gap(fc: float) -> float:  # strictly increasing in fc
        return _worst_margin(template_id, rising, fc) - _worst_margin(template_id, falling, fc)

    if gap(lo_hz) >= 0.0:
        return lo_hz
    if gap(hi_hz) <= 0.0:
        return hi_hz
    a, b = math.log(lo_hz), math.log(hi_hz)
    for _ in range(_BISECTION_STEPS):
        mid = 0.5 * (a + b)
        if gap(math.exp(mid)) < 0.0:
            a = mid
        else:
            b = mid
    return math.exp(0.5 * (a + b))


def e12_values(min_si: float, max_si: float) -> list[float]:
    """Every E12 value within [min_si, max_si] (inclusive, exact decimal values)."""
    first = math.floor(math.log10(min_si)) - 1
    last = math.ceil(math.log10(max_si)) + 1
    values: list[float] = []
    for exponent in range(first, last + 1):
        for mantissa in E12_SERIES:
            value = float(f"{mantissa}e{exponent}")
            if min_si * (1 - 1e-12) <= value <= max_si * (1 + 1e-12):
                values.append(value)
    return values


def _candidates(
    template_id: TemplateId,
    vary: Literal["R1", "C1"],
    fixed_si: float,
    ideal_si: float,
    constraints: Sequence[GoalConstraint],
) -> list[_Candidate]:
    template = get_template(template_id)
    bounds = template.component(vary).bounds
    assert bounds is not None
    valid: list[float] = []
    for value in e12_values(bounds.min_si, bounds.max_si):
        values: dict[str, float] = {vary: value, _OTHER[vary]: fixed_si}
        try:
            validate_values(template_id, values)
        except ValueParseError:
            continue  # outside the supported cutoff range with the other component kept
        valid.append(value)
    valid.sort(key=lambda v: (abs(math.log(v / ideal_si)), v))
    out: list[_Candidate] = []
    for value in valid[:NEIGHBOURS]:
        r = value if vary == "R1" else fixed_si
        c = fixed_si if vary == "R1" else value
        out.append(
            _Candidate(
                value_si=value,
                r_ohm=r,
                c_farad=c,
                cutoff_hz=analytic.cutoff_hz(r, c),
                predicted=_predict(template_id, constraints, r, c),
            )
        )
    return out


def _choose(candidates: Sequence[_Candidate], ideal_si: float) -> _Candidate:
    """Pass every constraint with the largest minimum margin; else the smallest shortfall."""
    return max(
        candidates,
        key=lambda cand: (
            cand.passes,
            round(cand.min_margin_db, 9),
            -abs(math.log(cand.value_si / ideal_si)),
        ),
    )


def _db(x: float) -> str:
    return f"{x:.2f} dB"


def _constraint_phrase(c: GoalConstraint) -> str:
    if c.kind == "preserve":
        return f"lose at most {c.limit_db:g} dB"
    return f"reduce by at least {c.limit_db:g} dB"


def _tone_name(label: str, frequency_hz: float) -> str:
    return f"{label} ({format_frequency(frequency_hz)})"


def _predictions_text(
    constraints: Sequence[GoalConstraint], predicted: Sequence[PredictedGoalTone]
) -> str:
    parts = []
    for c, p in zip(constraints, predicted, strict=True):
        verdict = "passes" if p.passes else "fails"
        parts.append(
            f"{_tone_name(c.label, c.frequency_hz)} {_db(p.predicted_gain_db)} "
            f"({_constraint_phrase(c)}: {verdict}, margin {p.margin_db:+.2f} dB)"
        )
    return "; ".join(parts)


def _shortfalls_text(
    constraints: Sequence[GoalConstraint], predicted: Sequence[PredictedGoalTone]
) -> str:
    parts = []
    for c, p in zip(constraints, predicted, strict=True):
        if p.passes:
            continue
        attenuation = -p.predicted_gain_db
        need = "at most" if c.kind == "preserve" else "at least"
        parts.append(
            f"{_tone_name(c.label, c.frequency_hz)} misses by {-p.margin_db:.2f} dB "
            f"(predicted attenuation {attenuation:.2f} dB; the goal asks for {need} "
            f"{c.limit_db:g} dB)"
        )
    return "; ".join(parts)


def _unit(component_id: str) -> Literal["ohm", "farad"]:
    return "ohm" if component_id == "R1" else "farad"


def suggest_design(
    revision: Revision, goal: Goal, *, vary: Literal["R1", "C1"]
) -> DesignSuggestion:
    """Analytical goal-to-design preview for ``revision``'s template (see module doc)."""
    if revision.frozen:
        raise _stale()
    template_id = revision.template_id
    family = _FAMILY[template_id]
    constraints = list(goal.constraints)
    current = revision_values(revision)
    fixed_id = _OTHER[vary]
    fixed_si = current[fixed_id]
    bounds = get_template(template_id).component(vary).bounds
    assert bounds is not None

    feasibility = analyze_feasibility(goal, template_id)
    supported_lo, supported_hi = supported_cutoffs(template_id)
    if feasibility.feasible and feasibility.cutoff_range_hz is not None:
        lo, hi = feasibility.cutoff_range_hz
        target = math.sqrt(lo * hi)
        target_text = (
            f"The goal needs a cutoff between {format_frequency(lo)} and "
            f"{format_frequency(hi)}; the target is {format_frequency(target)}, the centre of "
            "that range on a logarithmic scale."
        )
    else:
        target = best_compromise_cutoff(template_id, constraints, supported_lo, supported_hi)
        target_text = (
            f"No first-order RC {family} cutoff in this lab's supported range "
            f"({format_frequency(supported_lo)} to {format_frequency(supported_hi)}) meets "
            f"every constraint at once. The best compromise is a cutoff of "
            f"{format_frequency(target)}, where the worst predicted shortfall is smallest."
        )

    ideal = 1.0 / (2.0 * math.pi * target * fixed_si)
    unit = _unit(vary)
    fixed_text = f"{fixed_id} = {format_value(fixed_si, _unit(fixed_id))}"
    candidates = _candidates(template_id, vary, fixed_si, ideal, constraints)
    if not candidates:  # cannot happen for a valid revision; kept so failure stays explicit
        raise ApiException(
            ErrorCode.CONFLICT,
            f"No standard {vary} value keeps this circuit inside its supported range with "
            f"{fixed_text}. Try choosing {fixed_id} instead.",
            field="vary",
        )
    chosen = _choose(candidates, ideal)

    in_bounds = bounds.min_si * (1 - 1e-12) <= ideal <= bounds.max_si * (1 + 1e-12)
    ideal_text = f"Keeping {fixed_text}, {vary} would ideally be {format_value(ideal, unit)}"
    if not in_bounds:
        ideal_text += (
            f", outside the supported {format_value(bounds.min_si, unit)} to "
            f"{format_value(bounds.max_si, unit)}"
        )
    explanation = (
        "Prediction from first-order RC equations, not a measurement. "
        f"{target_text} {ideal_text}; the standard E12 value "
        f"{format_value(chosen.value_si, unit)} gives a predicted cutoff of "
        f"{format_frequency(chosen.cutoff_hz)}: "
        f"{_predictions_text(constraints, chosen.predicted)}. "
        "Apply it as a revision and run an experiment to measure it."
    )

    tradeoff: str | None = None
    if not chosen.passes:
        r_text = format_value(chosen.r_ohm, "ohm")
        c_text = format_value(chosen.c_farad, "farad")
        design = (
            f"R1 = {r_text}, C1 = {c_text}, predicted cutoff {format_frequency(chosen.cutoff_hz)}"
        )
        shortfalls = _shortfalls_text(constraints, chosen.predicted)
        if not feasibility.feasible:
            tradeoff = (
                f"Prediction: the closest achievable design ({design}) still falls short. "
                f"{shortfalls}. Relax a limit, or apply this design and measure the tradeoff."
            )
        elif not in_bounds:
            tradeoff = (
                f"Prediction: with {fixed_text} the closest supported standard value "
                f"({design}) misses the goal. {shortfalls}. Try choosing {fixed_id} instead."
            )
        else:
            tradeoff = (
                f"Prediction: rounding to the nearest standard part ({design}) misses the "
                f"goal. {shortfalls}. The exact value {vary} = {format_value(ideal, unit)} "
                f"would meet every constraint; try choosing {fixed_id} instead or accept the "
                "small shortfall."
            )

    return DesignSuggestion(
        template_id=template_id,
        revision_id=revision.id,
        feasible=feasibility.feasible,
        target_cutoff_hz=target,
        values=[
            SuggestedValue(
                component_id=vary,
                value_si=chosen.value_si,
                display=format_value(chosen.value_si, unit),
                series="E12",
            ),
            SuggestedValue(
                component_id=fixed_id,
                value_si=fixed_si,
                display=format_value(fixed_si, _unit(fixed_id)),
                series="unchanged",
            ),
        ],
        predicted_cutoff_hz=chosen.cutoff_hz,
        predicted=list(chosen.predicted),
        predicted_goal_pass=chosen.passes,
        explanation=explanation,
        tradeoff=tradeoff,
    )
