"""High-pass cases and the open capacitor on REAL ngspice (proof obligations (a) and (c)).

(a) An open capacitor measured by the real instruments matches its analytical prediction in
    both families (low-pass: 0 dB and 0°, no source current; high-pass: no measurable output,
    no source current), the symptom alone leaves it as the only (supported) explanation, and
    replacing C1 with its nominal part reconnects it (functional recovery) while replacing R1
    does not. Run at the lowest- and highest-cutoff drawable designs of each family, where the
    margins are thinnest.
(c) For the high-pass family, R1×2 and C1×2 give identical output-voltage measurements (both
    stay unresolved, an abstention is consistent) and the source-current sweep resolves them.

``RESONANCE_OPEN_C_ALL_DESIGNS=1`` additionally runs the open-capacitor symptom for EVERY
drawable design of both families (≈ 150 cases; several minutes).
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from challenges_testkit import SESSION_A, Rig, seed_for

from resonance.challenges import hypotheses
from resonance.challenges.evaluator import faults
from resonance.challenges.instruments import CURRENT_FLOOR_MA, MEASUREMENT_FLOOR_DB
from resonance.contracts.circuits import TemplateId
from resonance.contracts.investigation import (
    CaseExperimentRequest,
    DiagnosisSubmission,
    EvidenceCitation,
    InstrumentKind,
    PublicCase,
    RepairProposalInput,
)

pytestmark = pytest.mark.ngspice

OPEN = "c1_disconnected"


def _by_cutoff(template_id: TemplateId) -> list[tuple[float, float]]:
    return sorted(
        faults.nominal_designs(template_id),
        key=lambda d: faults.cutoff_hz({"R1": d[0], "C1": d[1]}),
    )


def _extremes(template_id: TemplateId) -> tuple[tuple[float, float], tuple[float, float]]:
    designs = _by_cutoff(template_id)
    return designs[0], designs[-1]


# (family, design, repaired component, restores)
OPEN_SCENARIOS = [
    ("rc_lowpass", _extremes("rc_lowpass")[0], "C1", True),
    ("rc_lowpass", _extremes("rc_lowpass")[1], "R1", False),
    ("rc_highpass", _extremes("rc_highpass")[0], "R1", False),
    ("rc_highpass", _extremes("rc_highpass")[1], "C1", True),
]


def _status(case: PublicCase) -> dict[str, str]:
    return {a.hypothesis_id: a.status for a in case.evidence.assessments}


def _truth_margin(case: PublicCase, truth: str) -> float:
    (assessment,) = [a for a in case.evidence.assessments if a.hypothesis_id == truth]
    assert assessment.status != "contradicted", assessment
    return max((c.deviation / c.tolerance for c in assessment.checks), default=0.0)


def _req(instrument: str, key: str, probe: str | None = None) -> CaseExperimentRequest:
    return CaseExperimentRequest.model_validate(
        {"instrument": instrument, "probe": probe, "client_request_id": key}
    )


@pytest.mark.parametrize(("template_id", "nominal", "repair", "restores"), OPEN_SCENARIOS)
async def test_open_capacitor_case_on_real_ngspice(
    ngspice_rig: Rig,
    template_id: TemplateId,
    nominal: tuple[float, float],
    repair: str,
    restores: bool,
) -> None:
    svc, s = ngspice_rig.service, f"{SESSION_A}open"
    case = await svc.harness_create_case(s, seed_for(OPEN, nominal, template_id), template_id)
    assert case.intended_revision.template_id == template_id
    values = {c.id: c.value_si for c in case.intended_revision.components if c.id in ("R1", "C1")}
    assert (values["R1"], values["C1"]) == nominal

    # The standardized symptom alone identifies the open capacitor.
    assert case.evidence.remaining == [OPEN], case.evidence.remaining
    assert _status(case)[OPEN] == "supported"
    assert _truth_margin(case, OPEN) <= 0.25
    symptom = case.observations[0]
    assert symptom.transient is not None and symptom.transient.audio is not None
    for tone in symptom.transient.tones:
        assert tone.gain_db is not None
        if template_id == "rc_lowpass":  # C1 open: R1 carries no current, V(out) = V(in)
            assert abs(tone.gain_db) <= 0.05 and abs(tone.phase_deg) <= 0.5, tone
        else:  # C1 open: nothing drives the output
            assert tone.gain_db <= MEASUREMENT_FLOOR_DB and tone.phase_deg == 0.0, tone
            assert tone.amplitude == 0.0
    if template_id == "rc_highpass":
        assert "no measurable output" in symptom.summary
        observed = [c for c in symptom.transient.audio.clips if c.role == "observed"]
        assert observed and observed[0].peak_fs == 0.0  # an honest silent clip, not normalized

    # Both current instruments measure no current (phase undefined → reported as 0°).
    await svc.set_mode(s, case.id, "guide")
    sweep = await svc.run_experiment(s, case.id, _req("source_current_sweep", "open-cur-1"),
                                     requested_by="user")
    assert sweep.observation.sweep is not None
    assert max(sweep.observation.sweep.magnitude) <= CURRENT_FLOOR_MA
    assert set(sweep.observation.sweep.phase_deg) == {0.0}
    assert "no measurable current" in sweep.observation.summary
    tran = await svc.run_experiment(
        s, case.id, _req("transient_capture", "open-cur-2", "source_current"),
        requested_by="user",
    )
    assert tran.observation.transient is not None
    assert all(t.amplitude <= CURRENT_FLOOR_MA for t in tran.observation.transient.tones)
    assert tran.case.evidence.remaining == [OPEN]
    assert _truth_margin(tran.case, OPEN) <= 0.25

    # Diagnosis, then the repair: replacing C1 reconnects it; replacing R1 does not.
    case = await svc.submit_diagnosis(
        s,
        case.id,
        DiagnosisSubmission(
            conclusion="fault",
            hypothesis_id=OPEN,
            citations=[EvidenceCitation(observation_id=symptom.id, quantity="tone_gain_db")],
            explanation="The symptom matches an open capacitor.",
        ),
        submitted_by="user",
    )
    assert case.diagnosis is not None and case.diagnosis.evidence_check.status == "supported"
    await svc.propose_repair(
        s, case.id, RepairProposalInput(component_id=repair), proposed_by="user"  # type: ignore[arg-type]
    )
    case = await svc.verify_repair(s, case.id)
    assert case.repair is not None and case.repair.functional_recovery is restores
    assert case.repair.max_gain_deviation_db is not None
    if restores:
        assert case.repair.max_gain_deviation_db < 0.01
        assert case.repair.max_phase_deviation_deg is not None
        assert case.repair.max_phase_deviation_deg < 0.05
    else:
        assert case.repair.max_gain_deviation_db > 3.0
    closed = await svc.close_case(s, case.id)
    assert closed.reveal is not None
    fault = closed.reveal.fault
    assert (fault.hypothesis_id, fault.component_id, fault.actual_value_si) == (OPEN, "C1", None)
    assert fault.nominal_value_si == nominal[1] and "not connected" in fault.description
    outcome = closed.reveal.outcome
    assert outcome.exact_identification == "correct"
    assert outcome.functional_recovery == ("restored" if restores else "not_restored")
    print(
        f"\n[open C] {template_id} {nominal}: symptom margin {_truth_margin(tran.case, OPEN):.3f} "
        f"tol; repair {repair} recovered={case.repair.functional_recovery} "
        f"(max dev {case.repair.max_gain_deviation_db:.3f} dB)"
    )


async def test_highpass_r_vs_c_ambiguity_and_current_resolves_it(ngspice_rig: Rig) -> None:
    svc = ngspice_rig.service
    nominal = (2200.0, 47e-9)  # fc ≈ 1.54 kHz, drawable in the high-pass family
    assert nominal in faults.nominal_designs("rc_highpass")
    sweeps = {}
    for i, truth in enumerate(("r1_increased", "c1_increased")):
        s = f"{SESSION_A}hp{i}"
        case = await svc.harness_create_case(
            s, seed_for(truth, nominal, "rc_highpass"), "rc_highpass"  # type: ignore[arg-type]
        )
        assert case.evidence.remaining == ["r1_increased", "c1_increased"]  # symptom only
        await svc.set_mode(s, case.id, "guide")
        volt = await svc.run_experiment(s, case.id, _req("output_voltage_sweep", f"hp-volt-{i}0"),
                                        requested_by="user")
        assert volt.case.evidence.remaining == ["r1_increased", "c1_increased"]
        assert _status(volt.case)[truth] == "unresolved"
        assert volt.case.evidence.distinguishing_instruments is None  # withheld while observing
        sweeps[truth] = volt.observation.sweep
        if truth == "r1_increased":
            cur = await svc.run_experiment(
                s, case.id, _req("source_current_sweep", f"hp-curr-{i}0"), requested_by="user"
            )
            assert cur.case.evidence.remaining == ["r1_increased"]
            assert _status(cur.case)["r1_increased"] == "supported"
            assert _truth_margin(cur.case, "r1_increased") <= 0.25
        else:
            case = await svc.submit_diagnosis(
                s,
                case.id,
                DiagnosisSubmission(
                    conclusion="insufficient_evidence",
                    candidate_hypothesis_ids=["r1_increased", "c1_increased"],
                    suggested_instrument=InstrumentKind.SOURCE_CURRENT_SWEEP,
                    explanation="Voltage alone cannot separate R1 from C1.",
                ),
                submitted_by="user",
            )
            assert case.diagnosis is not None
            assert case.diagnosis.evidence_check.status == "consistent_abstention"
            assert case.evidence.distinguishing_instruments == [
                InstrumentKind.SOURCE_CURRENT_SWEEP,
                InstrumentKind.TRANSIENT_CAPTURE,
            ]
        await svc.close_case(s, case.id)
    a, b = sweeps["r1_increased"], sweeps["c1_increased"]
    assert a is not None and b is not None
    assert np.max(np.abs(np.subtract(a.magnitude, b.magnitude))) < 1e-3  # tolerance 0.5 dB
    assert np.max(np.abs(np.subtract(a.phase_deg, b.phase_deg))) < 1e-2  # tolerance 3°
    predicted = hypotheses.hypothesis_predictions(
        "rc_highpass",
        {"R1": nominal[0], "C1": nominal[1]},
        "r1_increased",
        InstrumentKind.OUTPUT_VOLTAGE_SWEEP,
    )
    worst = max(p.deviation(m) for p, m in zip(predicted[0::2], a.magnitude, strict=True))
    assert worst < 0.05, worst


async def test_highpass_no_fault_control_is_supported_by_the_symptom(ngspice_rig: Rig) -> None:
    svc, s = ngspice_rig.service, f"{SESSION_A}hpnf"
    low, _ = _extremes("rc_highpass")
    case = await svc.harness_create_case(s, seed_for("no_fault", low, "rc_highpass"), "rc_highpass")
    assert case.evidence.remaining == ["no_fault"]
    assert _status(case)["no_fault"] == "supported"
    assert _truth_margin(case, "no_fault") <= 0.25
    tones = [t.frequency_hz for t in case.intended_revision.source.tones]
    assert tones == [50.0, 440.0]
    await svc.close_case(s, case.id)


@pytest.mark.skipif(
    os.environ.get("RESONANCE_OPEN_C_ALL_DESIGNS") != "1",
    reason="exhaustive run: set RESONANCE_OPEN_C_ALL_DESIGNS=1",
)
@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
async def test_open_capacitor_symptom_for_every_drawable_design(
    ngspice_rig: Rig, template_id: TemplateId
) -> None:
    svc = ngspice_rig.service
    worst_truth, worst_other = 0.0, float("inf")
    for i, nominal in enumerate(faults.nominal_designs(template_id)):
        s = f"{SESSION_A}all{i}"
        case = await svc.harness_create_case(s, seed_for(OPEN, nominal, template_id), template_id)
        assert case.evidence.remaining == [OPEN], (template_id, nominal, case.evidence.remaining)
        worst_truth = max(worst_truth, _truth_margin(case, OPEN))
        for a in case.evidence.assessments:
            if a.hypothesis_id != OPEN:
                worst_other = min(worst_other, max(c.deviation / c.tolerance for c in a.checks))
        await svc.close_case(s, case.id)
    print(
        f"\n[open C, all designs] {template_id}: truth worst {worst_truth:.3f} tol; closest other "
        f"hypothesis {worst_other:.2f} tol away"
    )
    assert worst_truth <= 0.25
