"""Agent-visible views: an explicit ALLOW-LIST built from public data (verification V-01).

Agent tools never serialize ``PublicCase``/``Observation``/``ExperimentRecord`` directly; they
return only the dictionaries built here. Anything not listed below is never sent to the model:
in particular no ``reveal`` (case views refuse closed cases), no ``distinguishing_instruments``,
no hypothesis statuses outside ``assess_evidence``, no waveform arrays, no audio clips, no
artifact IDs or URLs, no settings echo, no seeds, files, logs or simulator details.

Numbers: every measured value is rounded to at most 4 significant figures (well inside the
declared evidence tolerances: 0.5 dB, 3°, 3 %); non-finite values become ``null``.

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

evidence_view (INVESTIGATE tool ``assess_evidence`` only)::

    hypothesis_status: [{id, status}], remaining: [id], statement

EXPLORE schemas (public session data only): ``revision_view``, ``experiment_view``
(tone gains, a ≤17-point subset of the measured AC sweep, goal evaluation, consistency check
names), ``comparison_view`` and ``feasibility_view``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from ..contracts.circuits import Revision
from ..contracts.common import QuantityName
from ..contracts.experiments import ExperimentComparison, ExperimentRecord, FeasibilityAnalysis
from ..contracts.investigation import CaseStatus, Observation, ObservedTone, PublicCase

AGENT_CASE_ACTIONS = ("run_experiment", "submit_diagnosis", "propose_repair")

# Primary quantity of a cited result (EvidenceRef.quantity uses a closed vocabulary).
EXPERIMENT_QUANTITY: QuantityName = "output_gain_db"


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


def revision_view(revision: Revision, *, label: str) -> dict[str, Any]:
    return {
        "revision_id": revision.id,
        "label": label,
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


def _tone_view(tone: ObservedTone) -> dict[str, Any]:
    view: dict[str, Any] = {
        "label": tone.label,
        "role": tone.role,
        "frequency_hz": sig(tone.frequency_hz),
        "amplitude": sig(tone.amplitude),
        "expected_amplitude": sig(tone.expected_amplitude),
        "amplitude_unit": tone.amplitude_unit,
        "phase_deg": sig(tone.phase_deg),
        "expected_phase_deg": sig(tone.expected_phase_deg),
    }
    if tone.gain_db is not None:
        view["gain_db"] = sig(tone.gain_db)
    if tone.expected_gain_db is not None:
        view["expected_gain_db"] = sig(tone.expected_gain_db)
    return view


def observation_view(obs: Observation) -> dict[str, Any]:
    view = observation_brief(obs)
    if obs.sweep is not None:
        s = obs.sweep
        view["sweep"] = {
            "quantity": s.quantity,
            "magnitude_unit": s.magnitude_unit,
            "reference": "expected_* columns are the same measurement of the intended design",
            "columns": [
                "frequency_hz",
                "magnitude",
                "expected_magnitude",
                "phase_deg",
                "expected_phase_deg",
            ],
            "rows": [
                [sig(f), sig(m), sig(em), sig(p), sig(ep)]
                for f, m, em, p, ep in zip(
                    s.frequency_hz,
                    s.magnitude,
                    s.expected_magnitude,
                    s.phase_deg,
                    s.expected_phase_deg,
                    strict=True,
                )
            ],
        }
    if obs.transient is not None:
        t = obs.transient
        view["transient"] = {
            "probe": t.probe,
            "reference": "expected_* fields are the same measurement of the intended design",
            "tones": [_tone_view(tone) for tone in t.tones],
            "note": "The waveform window and any audio clips are shown to the learner and "
            "omitted here.",
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
        view["repair_verification"] = {
            "status": rv.status,
            "functional_recovery": rv.functional_recovery,
            "max_gain_deviation_db": sig(rv.max_gain_deviation_db),
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
