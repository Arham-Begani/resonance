"""Agent-visible views: an explicit ALLOW-LIST built from public data (verification V-01).

Agent tools never serialize ``PublicCase``/``Observation``/``ExperimentRecord`` directly; they
return only the dictionaries built here. Anything not listed below is never sent to the model:
in particular no ``reveal`` (case views refuse closed cases), no ``distinguishing_instruments``,
no hypothesis statuses outside ``assess_evidence``, no waveform arrays, no audio clips, no
artifact IDs or URLs, no settings echo, no seeds, files, logs or simulator details.

Numbers: every measured value is rounded to at most 4 significant figures (well inside the
declared evidence tolerances: 0.5 dB, 3°, 3 %); non-finite values become ``null``.

Measurement floors (``challenges.instruments``): a magnitude at or below the declared floor
(gain ≤ −120 dB, current ≤ 1e-6 mA) is rendered as the string ``"below floor"`` and its phase
as ``"undefined"`` — never as a number such as the −200 dB clamp or the published 0° — so the
model cannot quote a non-measurement as a level or a phase. The ``reference``/``note`` text of
every sweep and transient view explains the markers (constant text, identical for every hidden
condition). The same rule applies to the intended-design reference columns.

INVESTIGATE / HINT schemas
--------------------------
case_view (tool ``get_case``)::

    case_id, status, mode, hints_used,
    budget: {total, used, remaining},
    intended_design: {revision_id, label, template_id, title,
        nodes: [{id, label, role}],
        components: [{id, kind, value, value_si, unit, nodes, editable, allowed_range_si,
                      description}],            # description = trusted topology text
        source: {description, test_tones: [{label, role, frequency_hz, amplitude_v}]},
        analytical_prediction: {note, cutoff_hz, time_constant_s}},
        # label = view label + the template's trusted family/topology summary
        # (``family_text``), e.g. "… RC high-pass filter: a capacitor (C1) from Input to …"
    instruments: [{kind, title, description, returns,
                   frequency_grid: {points, start_hz, stop_hz} | null, probes, cost}],
    hypotheses: [{id, label}],
    evidence_tolerances: {gain_tolerance_db, phase_tolerance_deg, current_tolerance_pct,
                          current_abs_tolerance_ma, tone_gain_tolerance_db,
                          tone_phase_tolerance_deg, description},
    observations: [observation_brief | observation_view (symptom only)],
    available_actions: what THIS run may do now (CaseService.available_actions_for_run),
                       subset of [run_experiment, submit_diagnosis, propose_repair],
    diagnosis?: {diagnosis_id, conclusion, hypothesis_id, candidate_hypothesis_ids,
                 submitted_by, evidence_check_status},
    repair_proposal?: {repair_id, component_id, value, proposed_by},
    repair_verification?: {status, functional_recovery, max_gain_deviation_db,
                           max_phase_deviation_deg, worst_frequency_hz, summary}

observation_brief::

    observation_id, sequence, kind, probe?, status, counted, requested_by, summary

observation_view (tools ``read_observation``, ``run_experiment``) = observation_brief plus::

    sweep?: {quantity, magnitude_unit, reference, columns: [frequency_hz, magnitude,
             expected_magnitude, phase_deg, expected_phase_deg], rows: [[...] x 31]},
    transient?: {probe, reference, note,
                 tones: [{label, role, frequency_hz, amplitude, expected_amplitude,
                          amplitude_unit, phase_deg, expected_phase_deg, gain_db?,
                          expected_gain_db?}]},
    error?: learner-safe message
    # magnitude/amplitude/gain cells may be "below floor" and phase cells "undefined"

evidence_view (INVESTIGATE tool ``assess_evidence`` only)::

    hypothesis_status: [{id, status}], remaining: [id], statement

EXPLORE schemas (public session data only): ``revision_view``, ``experiment_view``
(tone gains, a ≤17-point subset of the measured AC sweep, goal evaluation, consistency check
names), ``comparison_view``, ``feasibility_view``, ``suggestion_view`` (an analytical
``DesignSuggestion``, labeled as a prediction) and ``learn_attempt_view`` (a recorded Learn
attempt: change, graded predictions with measured tone deltas, cited experiment IDs, and the
deterministic explanation/tradeoff; never its audio).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from ..challenges.hypotheses import current_measurable, gain_measurable
from ..challenges.instruments import CURRENT_FLOOR_MA, MEASUREMENT_FLOOR_DB
from ..circuits.templates import get_template
from ..contracts.circuits import Revision
from ..contracts.common import QuantityName
from ..contracts.experiments import (
    DesignSuggestion,
    ExperimentComparison,
    ExperimentRecord,
    FeasibilityAnalysis,
    Goal,
)
from ..contracts.investigation import CaseStatus, Observation, ObservedTone, PublicCase
from ..contracts.learn import LearnAttempt

AGENT_CASE_ACTIONS = ("run_experiment", "submit_diagnosis", "propose_repair")

# Primary quantity of a cited result (EvidenceRef.quantity uses a closed vocabulary).
EXPERIMENT_QUANTITY: QuantityName = "output_gain_db"

BELOW_FLOOR = "below floor"
"""Rendered in place of a magnitude at or below the declared measurement floor."""

UNDEFINED_PHASE = "undefined"
"""Rendered in place of the phase of a reading without a measurable magnitude."""

FLOOR_NOTE = (
    f'"{BELOW_FLOOR}" means no measurable signal (a gain at or below '
    f"{MEASUREMENT_FLOOR_DB:g} dB, or a current at or below {CURRENT_FLOOR_MA:g} mA); it is not "
    f'a level to quote. The phase of such a reading is "{UNDEFINED_PHASE}" and is never used '
    "as evidence."
)

Cell = float | str | None


class ClosedCaseError(ValueError):
    """Raised if a view of a closed case is requested (the reveal must never reach the agent)."""


def observation_quantity(obs: Observation) -> QuantityName:
    if obs.kind == "output_voltage_sweep":
        return "output_gain_db"
    if obs.kind == "source_current_sweep":
        return "source_current_ma"
    probe = obs.probe or (obs.transient.probe if obs.transient else None)
    return "tone_current_ma" if probe == "source_current" else "tone_gain_db"


def sig(value: float | None, digits: int = 4) -> float | None:
    """Round to ``digits`` significant figures; non-finite values become None."""
    if value is None or not math.isfinite(value):
        return None
    if value == 0:
        return 0.0
    return round(value, digits - 1 - int(math.floor(math.log10(abs(value)))))


# --------------------------------------------------------------------------------------
# Investigation
# --------------------------------------------------------------------------------------


def family_text(template_id: str) -> str:
    """Trusted family and topology text from the template graph (public), e.g. "RC high-pass
    filter: a capacitor (C1) from Input to Output and a resistor (R1) from Output to Ground.
    High frequencies pass; …". Identical for every hidden condition of a family."""
    template = get_template(template_id)
    summary = template.summary
    return f"{template.title}: {summary[:1].lower()}{summary[1:]}"


def revision_view(revision: Revision, *, label: str) -> dict[str, Any]:
    return {
        "revision_id": revision.id,
        "label": f"{label} {family_text(revision.template_id)}",
        "template_id": revision.template_id,
        "title": revision.title,
        "nodes": [{"id": n.id, "label": n.label, "role": n.role.value} for n in revision.nodes],
        "components": [
            {
                "id": c.id,
                "kind": c.kind.value,
                "value": c.display_value,
                "value_si": sig(c.value_si, 6) if c.value_si is not None else None,
                "unit": c.unit,
                "nodes": list(c.nodes),
                "editable": c.editable,
                "allowed_range_si": [c.bounds.min_si, c.bounds.max_si] if c.bounds else None,
                "description": c.description,
            }
            for c in revision.components
        ],
        "source": {
            "description": revision.source.description,
            "test_tones": [
                {
                    "label": t.label,
                    "role": t.role,
                    "frequency_hz": t.frequency_hz,
                    "amplitude_v": t.amplitude_v,
                }
                for t in revision.source.tones
            ],
        },
        "analytical_prediction": {
            "note": "Prediction from first-order RC equations, not a measurement.",
            "cutoff_hz": sig(revision.prediction.cutoff_hz),
            "time_constant_s": sig(revision.prediction.time_constant_s),
        },
    }


def observation_brief(obs: Observation) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "observation_id": obs.id,
        "sequence": obs.sequence,
        "kind": obs.kind,
        "status": obs.status,
        "counted": obs.counted,
        "requested_by": obs.requested_by,
        "summary": obs.summary,
    }
    if obs.probe is not None:
        brief["probe"] = obs.probe
    return brief


def _measured(value: float, measurable: bool) -> Cell:
    """A magnitude cell: the rounded number, or the floor marker (never the −200 dB clamp)."""
    return sig(value) if measurable else BELOW_FLOOR


def _phase(value: float, measurable: bool) -> Cell:
    """A phase cell: only meaningful where its magnitude was measurable."""
    return sig(value) if measurable else UNDEFINED_PHASE


def _tone_measurable(amplitude: float, gain_db: float | None) -> bool:
    """Output-voltage tones carry a gain (dB); source-current tones an amplitude in mA."""
    return gain_measurable(gain_db) if gain_db is not None else current_measurable(amplitude)


def _tone_view(tone: ObservedTone) -> dict[str, Any]:
    seen = _tone_measurable(tone.amplitude, tone.gain_db)
    expected = _tone_measurable(tone.expected_amplitude, tone.expected_gain_db)
    view: dict[str, Any] = {
        "label": tone.label,
        "role": tone.role,
        "frequency_hz": sig(tone.frequency_hz),
        "amplitude": _measured(tone.amplitude, seen),
        "expected_amplitude": _measured(tone.expected_amplitude, expected),
        "amplitude_unit": tone.amplitude_unit,
        "phase_deg": _phase(tone.phase_deg, seen),
        "expected_phase_deg": _phase(tone.expected_phase_deg, expected),
    }
    if tone.gain_db is not None:
        view["gain_db"] = _measured(tone.gain_db, seen)
    if tone.expected_gain_db is not None:
        view["expected_gain_db"] = _measured(tone.expected_gain_db, expected)
    return view


def observation_view(obs: Observation) -> dict[str, Any]:
    view = observation_brief(obs)
    if obs.sweep is not None:
        s = obs.sweep
        measurable = gain_measurable if s.quantity == "output_gain" else current_measurable
        rows: list[list[Cell]] = []
        for f, m, em, p, ep in zip(
            s.frequency_hz,
            s.magnitude,
            s.expected_magnitude,
            s.phase_deg,
            s.expected_phase_deg,
            strict=True,
        ):
            seen, expected = measurable(m), measurable(em)
            rows.append(
                [
                    sig(f),
                    _measured(m, seen),
                    _measured(em, expected),
                    _phase(p, seen),
                    _phase(ep, expected),
                ]
            )
        view["sweep"] = {
            "quantity": s.quantity,
            "magnitude_unit": s.magnitude_unit,
            "reference": "expected_* columns are the same measurement of the intended design. "
            + FLOOR_NOTE,
            "columns": [
                "frequency_hz",
                "magnitude",
                "expected_magnitude",
                "phase_deg",
                "expected_phase_deg",
            ],
            "rows": rows,
        }
    if obs.transient is not None:
        t = obs.transient
        view["transient"] = {
            "probe": t.probe,
            "reference": "expected_* fields are the same measurement of the intended design",
            "tones": [_tone_view(tone) for tone in t.tones],
            "note": "The waveform window and any audio clips are shown to the learner and "
            f"omitted here. {FLOOR_NOTE}",
        }
    if obs.error is not None:
        view["error"] = obs.error.message
    return view


def budget_view(case: PublicCase) -> dict[str, int]:
    b = case.budget
    return {"total": b.total, "used": b.used, "remaining": b.remaining}


def evidence_view(case: PublicCase) -> dict[str, Any]:
    """Deterministic hypothesis statuses (only via the INVESTIGATE tool ``assess_evidence``)."""
    _refuse_closed(case)
    return {
        "hypothesis_status": [
            {"id": a.hypothesis_id, "status": a.status} for a in case.evidence.assessments
        ],
        "remaining": list(case.evidence.remaining),
        "statement": case.evidence.statement,
    }


def _refuse_closed(case: PublicCase) -> None:
    if case.status == CaseStatus.CLOSED or case.reveal is not None:
        raise ClosedCaseError("closed cases are never shown to the agent")


def agent_actions(actions: Sequence[str]) -> list[str]:
    """The run's own permitted actions, limited to those the agent has tools for."""
    return [a for a in actions if a in AGENT_CASE_ACTIONS]


def case_view(case: PublicCase, *, run_actions: Sequence[str] = ()) -> dict[str, Any]:
    """``run_actions`` must come from ``CaseService.available_actions_for_run`` (agent tools
    always pass it): the public ``available_actions`` describe what the *learner* may do and
    are empty for the agent while its run owns the case (verification B-04)."""
    _refuse_closed(case)
    policy = case.policy
    view: dict[str, Any] = {
        "case_id": case.id,
        "status": case.status.value,
        "mode": case.mode,
        "hints_used": case.hints_used,
        "budget": budget_view(case),
        "intended_design": revision_view(
            case.intended_revision,
            label="Intended design with nominal values (the hidden circuit may differ).",
        ),
        "instruments": [
            {
                "kind": spec.kind.value,
                "title": spec.title,
                "description": spec.description,
                "returns": list(spec.returns),
                "frequency_grid": (
                    {
                        "points": len(spec.frequency_hz),
                        "start_hz": sig(min(spec.frequency_hz)),
                        "stop_hz": sig(max(spec.frequency_hz)),
                    }
                    if spec.frequency_hz
                    else None
                ),
                "probes": list(spec.probes) if spec.probes else None,
                "cost": spec.cost,
            }
            for spec in case.instruments
        ],
        "hypotheses": [{"id": h.id, "label": h.label} for h in case.hypotheses],
        "evidence_tolerances": {
            "gain_tolerance_db": policy.gain_tolerance_db,
            "phase_tolerance_deg": policy.phase_tolerance_deg,
            "current_tolerance_pct": policy.current_tolerance_pct,
            "current_abs_tolerance_ma": policy.current_abs_tolerance_ma,
            "tone_gain_tolerance_db": policy.tone_gain_tolerance_db,
            "tone_phase_tolerance_deg": policy.tone_phase_tolerance_deg,
            "description": policy.description,
        },
        # The standardized symptom is small and always relevant: its numbers are inline.
        "observations": [
            observation_view(o) if o.kind == "symptom" else observation_brief(o)
            for o in case.observations
        ],
        "available_actions": agent_actions(run_actions),
    }
    if case.diagnosis is not None:
        dx = case.diagnosis
        view["diagnosis"] = {
            "diagnosis_id": dx.id,
            "conclusion": dx.submission.conclusion.value,
            "hypothesis_id": dx.submission.hypothesis_id,
            "candidate_hypothesis_ids": list(dx.submission.candidate_hypothesis_ids),
            "submitted_by": dx.submitted_by,
            "evidence_check_status": dx.evidence_check.status,
        }
    if case.repair_proposal is not None:
        rp = case.repair_proposal
        view["repair_proposal"] = {
            "repair_id": rp.id,
            "component_id": rp.component_id,
            "value": rp.display_value,
            "proposed_by": rp.proposed_by,
        }
    if case.repair is not None:
        rv = case.repair
        silent = sum(1 for p in rv.points if not gain_measurable(p.repaired_gain_db))
        view["repair_verification"] = {
            "status": rv.status,
            "functional_recovery": rv.functional_recovery,
            # A deviation from a clamped non-measurement is not a number to quote.
            "max_gain_deviation_db": (
                f"not defined: no measurable output at {silent} of {len(rv.points)} frequencies"
                if silent
                else sig(rv.max_gain_deviation_db)
            ),
            "max_phase_deviation_deg": sig(rv.max_phase_deviation_deg),
            "worst_frequency_hz": sig(rv.worst_frequency_hz),
            "summary": rv.summary,
        }
    return view


# --------------------------------------------------------------------------------------
# Explore
# --------------------------------------------------------------------------------------


def feasibility_view(feasibility: FeasibilityAnalysis) -> dict[str, Any]:
    return {
        "basis": "analytical prediction (first-order RC), not a measurement",
        "feasible": feasibility.feasible,
        "cutoff_range_hz": (
            [sig(feasibility.cutoff_range_hz[0]), sig(feasibility.cutoff_range_hz[1])]
            if feasibility.cutoff_range_hz
            else None
        ),
        "explanation": feasibility.explanation,
    }


def experiment_view(record: ExperimentRecord, *, max_response_points: int = 17) -> dict[str, Any]:
    view: dict[str, Any] = {
        "experiment_id": record.id,
        "revision_id": record.revision_id,
        "status": record.status,
    }
    if record.error is not None:
        view["error"] = record.error.message
    if record.tones:
        view["tones"] = [
            {
                "label": t.label,
                "role": t.role,
                "frequency_hz": sig(t.frequency_hz),
                "input_amplitude_v": sig(t.input_amplitude_v),
                "output_amplitude_v": sig(t.output_amplitude_v),
                "measured_gain_db": sig(t.ac_gain_db),
                "transient_gain_db": sig(t.transient_gain_db),
            }
            for t in record.tones
        ]
    if record.response is not None and record.response.frequency_hz:
        r = record.response
        step = max(1, math.ceil(len(r.frequency_hz) / max_response_points))
        idx = list(range(0, len(r.frequency_hz), step))
        if idx[-1] != len(r.frequency_hz) - 1:
            idx.append(len(r.frequency_hz) - 1)
        view["frequency_response"] = {
            "note": f"{len(idx)} of {len(r.frequency_hz)} measured AC points.",
            "columns": ["frequency_hz", "gain_db", "phase_deg", "source_current_ma"],
            "rows": [
                [
                    sig(r.frequency_hz[i]),
                    sig(r.gain_db[i]),
                    sig(r.phase_deg[i]),
                    sig(r.source_current_ma[i]),
                ]
                for i in idx
            ],
        }
    if record.goal is not None:
        g = record.goal
        view["goal_evaluation"] = {
            "goal_id": g.goal_id,
            "passed": g.passed,
            "constraints": [
                {
                    "label": c.constraint.label,
                    "kind": c.constraint.kind,
                    "frequency_hz": c.constraint.frequency_hz,
                    "limit_db": c.constraint.limit_db,
                    "measured_attenuation_db": sig(c.measured_attenuation_db),
                    "passed": c.passed,
                    "margin_db": sig(c.margin_db),
                }
                for c in g.results
            ],
        }
    if record.checks:
        view["consistency_checks"] = [{"name": c.name, "passed": c.passed} for c in record.checks]
    return view


def comparison_view(cmp: ExperimentComparison) -> dict[str, Any]:
    return {
        "a_id": cmp.a_id,
        "b_id": cmp.b_id,
        "changes": [
            {"component_id": c.component_id, "from": c.from_display, "to": c.to_display}
            for c in cmp.changes
        ],
        "tones": [
            {
                "label": t.label,
                "frequency_hz": sig(t.frequency_hz),
                "a_gain_db": sig(t.a_gain_db),
                "b_gain_db": sig(t.b_gain_db),
                "delta_db": sig(t.delta_db),
            }
            for t in cmp.tones
        ],
        "summary": cmp.summary,
    }


def suggestion_view(suggestion: DesignSuggestion, goal: Goal) -> dict[str, Any]:
    """An analytical goal-to-design preview (``explore.suggest``), labeled as a prediction."""
    s = suggestion
    return {
        "basis": "analytical prediction (first-order RC), not a measurement",
        "applied": False,
        "revision_id": s.revision_id,
        "template_id": s.template_id,
        "goal": {"goal_id": goal.id, "title": goal.title},
        "feasible": s.feasible,
        "target_cutoff_hz": sig(s.target_cutoff_hz),
        "values": [
            {
                "component_id": v.component_id,
                "value": v.display,
                "value_si": sig(v.value_si, 6),
                "series": v.series,
            }
            for v in s.values
        ],
        "predicted_cutoff_hz": sig(s.predicted_cutoff_hz),
        "predicted": [
            {
                "label": p.constraint_label,
                "frequency_hz": sig(p.frequency_hz),
                "predicted_gain_db": sig(p.predicted_gain_db),
                "passes": p.passes,
                "margin_db": sig(p.margin_db),
            }
            for p in s.predicted
        ],
        "predicted_goal_pass": s.predicted_goal_pass,
        "explanation": s.explanation,
        "tradeoff": s.tradeoff,
        "note": "Prediction only; nothing was changed. To offer these values to the learner, "
        "call propose_values with them; the learner applies a proposal and an experiment "
        "measures it.",
    }


def learn_attempt_view(attempt: LearnAttempt) -> dict[str, Any]:
    """A recorded Learn attempt: graded from MEASURED tone gains (experiment IDs cited)."""
    a = attempt
    return {
        "attempt_id": a.id,
        "template_id": a.template_id,
        # The learner's chosen change (public), e.g. C1 × 2; named "factor" so it is never
        # confused with a hidden fault's multiplier, which no agent view contains.
        "change": {"component_id": a.change.component_id, "factor": a.change.multiplier},
        "base_revision_id": a.base_revision_id,
        "new_revision_id": a.new_revision_id,
        "base_experiment_id": a.base_experiment_id,
        "new_experiment_id": a.new_experiment_id,
        "learner_reasoning": a.reasoning,
        "graded": [
            {
                "label": g.label,
                "role": g.role,
                "frequency_hz": sig(g.frequency_hz),
                "predicted": g.predicted,
                "observed": g.observed,
                "measured_delta_db": sig(g.delta_db),
                "correct": g.correct,
            }
            for g in a.graded
        ],
        "score": a.score,
        "total": a.total,
        "comparison_summary": a.comparison.summary,
        "explanation": a.explanation,
        "tradeoff": a.tradeoff,
        "note": "measured_delta_db is the measured tone gain of the new experiment minus the "
        "base experiment (ngspice, exact tone frequency). learner_reasoning is the learner's "
        "own text. Audio clips are shown to the learner and omitted here.",
    }
