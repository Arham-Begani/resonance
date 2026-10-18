"""Identifiability, honest ambiguity, no-fault controls, and repair verification on REAL ngspice.

Doubling R1 and doubling C1 give the same R·C, so every output-voltage measurement is identical
and both explanations must stay ``unresolved``; the source current (|I| → V/R at high frequency)
separates them. Observed-vs-predicted numbers and timings are printed (run with ``-s``).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable

import numpy as np
import pytest
from challenges_testkit import SESSION_A, Rig, seed_for

from resonance.challenges import hypotheses
from resonance.contracts.investigation import (
    CaseExperimentRequest,
    DiagnosisSubmission,
    EvidenceCitation,
    InstrumentKind,
    ObservationResponse,
    PublicCase,
    RepairProposalInput,
)

pytestmark = pytest.mark.ngspice

NOMINAL = (2200.0, 47e-9)  # fc ≈ 1.54 kHz, a drawable design
TIMINGS: dict[str, list[float]] = {}


async def timed[T](label: str, awaitable: Awaitable[T]) -> T:
    start = time.perf_counter()
    result = await awaitable
    TIMINGS.setdefault(label, []).append(time.perf_counter() - start)
    return result


def session(i: int) -> str:
    return f"{SESSION_A}{i}"


async def open_case(rig: Rig, fault: str, i: int) -> PublicCase:
    case = await timed(
        "create_case (symptom)",
        rig.service.harness_create_case(session(i), seed_for(fault, NOMINAL)),  # type: ignore[arg-type]
    )
    assert (
        case.intended_revision.components[1].value_si,
        case.intended_revision.components[2].value_si,
    ) == NOMINAL
    return await rig.service.set_mode(session(i), case.id, "guide")


async def measure(
    rig: Rig, case: PublicCase, i: int, instrument: str, probe: str | None = None
) -> ObservationResponse:
    request = CaseExperimentRequest.model_validate(
        {
            "instrument": instrument,
            "probe": probe,
            "client_request_id": f"{instrument[:12]}-{probe or 'none'}-{i}",
        }
    )
    return await timed(
        f"{instrument}{':' + probe if probe else ''}",
        rig.service.run_experiment(session(i), case.id, request, requested_by="user"),
    )


def status_of(case: PublicCase) -> dict[str, str]:
    return {a.hypothesis_id: a.status for a in case.evidence.assessments}


def assert_true_hypothesis_well_inside_tolerance(case: PublicCase, truth: str) -> None:
    """The measured (ngspice) data sit far inside the declared tolerance of the truth."""
    (assessment,) = [a for a in case.evidence.assessments if a.hypothesis_id == truth]
    for check in assessment.checks:
        assert check.deviation <= 0.25 * check.tolerance, check


def cite(obs_id: str, quantity: str) -> list[EvidenceCitation]:
    return [EvidenceCitation(observation_id=obs_id, quantity=quantity)]  # type: ignore[arg-type]


async def test_r_vs_c_identifiability_ambiguity_and_repair(ngspice_rig: Rig) -> None:
    rig = ngspice_rig
    svc = rig.service
    nominal = {"R1": NOMINAL[0], "C1": NOMINAL[1]}

    # --- A: R1 doubled. Voltage leaves {R1×2, C1×2}; the current sweep resolves it. -----------
    a = await open_case(rig, "r1_increased", 1)
    assert a.evidence.remaining == ["r1_increased", "c1_increased"]  # from the symptom alone
    a_volt = await measure(rig, a, 1, "output_voltage_sweep")
    assert a_volt.case.evidence.remaining == ["r1_increased", "c1_increased"]
    assert status_of(a_volt.case)["r1_increased"] == "unresolved"
    assert status_of(a_volt.case)["c1_increased"] == "unresolved"
    a_cur = await measure(rig, a, 1, "source_current_sweep")
    assert a_cur.case.evidence.remaining == ["r1_increased"]
    assert status_of(a_cur.case) == {
        "no_fault": "contradicted",
        "r1_increased": "supported",
        "r1_decreased": "contradicted",
        "c1_increased": "contradicted",
        "c1_decreased": "contradicted",
    }
    assert_true_hypothesis_well_inside_tolerance(a_cur.case, "r1_increased")
    a_case = await svc.submit_diagnosis(
        session(1),
        a.id,
        DiagnosisSubmission(
            conclusion="fault",
            hypothesis_id="r1_increased",
            citations=cite(a_cur.observation.id, "source_current_ma"),
            explanation="High-frequency current is half the reference.",
        ),
        submitted_by="user",
    )
    assert a_case.diagnosis is not None and a_case.diagnosis.evidence_check.status == "supported"
    await svc.propose_repair(
        session(1), a.id, RepairProposalInput(component_id="R1"), proposed_by="user"
    )
    a_case = await timed("verify_repair", svc.verify_repair(session(1), a.id))
    assert a_case.repair is not None and a_case.repair.functional_recovery is True
    assert a_case.repair.max_gain_deviation_db is not None
    assert a_case.repair.max_gain_deviation_db < 0.01
    a_closed = await svc.close_case(session(1), a.id)
    assert a_closed.reveal is not None
    assert a_closed.reveal.outcome.exact_identification == "correct"
    assert a_closed.reveal.outcome.functional_recovery == "restored"
    assert a_closed.reveal.outcome.unsupported_certainty is False

    # --- B: C1 doubled, same nominal. Identical voltage data; honest abstention. -------------
    b = await open_case(rig, "c1_increased", 2)
    b_volt = await measure(rig, b, 2, "output_voltage_sweep")
    va, vb = a_volt.observation.sweep, b_volt.observation.sweep
    assert va is not None and vb is not None
    gain_diff = np.max(np.abs(np.subtract(va.magnitude, vb.magnitude)))
    phase_diff = np.max(np.abs(np.subtract(va.phase_deg, vb.phase_deg)))
    assert gain_diff < 1e-3 and phase_diff < 1e-2  # equal; tolerance is 0.5 dB / 3°
    assert b_volt.case.evidence.remaining == ["r1_increased", "c1_increased"]
    assert status_of(b_volt.case)["c1_increased"] == "unresolved"
    assert b_volt.case.evidence.distinguishing_instruments is None  # withheld while observing
    b_case = await svc.submit_diagnosis(
        session(2),
        b.id,
        DiagnosisSubmission(
            conclusion="insufficient_evidence",
            candidate_hypothesis_ids=["r1_increased", "c1_increased"],
            suggested_instrument=InstrumentKind.SOURCE_CURRENT_SWEEP,
            explanation="Voltage alone cannot separate R1 from C1.",
        ),
        submitted_by="user",
    )
    assert b_case.diagnosis is not None
    assert b_case.diagnosis.evidence_check.status == "consistent_abstention"
    assert b_case.evidence.distinguishing_instruments == [
        InstrumentKind.SOURCE_CURRENT_SWEEP,
        InstrumentKind.TRANSIENT_CAPTURE,
    ]
    await svc.propose_repair(
        session(2), b.id, RepairProposalInput(component_id="C1"), proposed_by="user"
    )
    b_case = await svc.verify_repair(session(2), b.id)
    assert b_case.repair is not None and b_case.repair.functional_recovery is True
    b_closed = await svc.close_case(session(2), b.id)
    assert b_closed.reveal is not None
    assert b_closed.reveal.outcome.exact_identification == "abstained"
    assert b_closed.reveal.outcome.true_cause_among_candidates is True
    assert b_closed.reveal.outcome.functional_recovery == "restored"

    # --- C: C1 doubled; a single-fault claim from voltage-only data is "ambiguous". -----------
    c = await open_case(rig, "c1_increased", 3)
    c_volt = await measure(rig, c, 3, "output_voltage_sweep")
    c_case = await svc.submit_diagnosis(
        session(3),
        c.id,
        DiagnosisSubmission(
            conclusion="fault",
            hypothesis_id="r1_increased",
            citations=cite(c_volt.observation.id, "output_gain_db"),
            explanation="The cutoff halved.",
        ),
        submitted_by="user",
    )
    assert c_case.diagnosis is not None and c_case.diagnosis.evidence_check.status == "ambiguous"
    await svc.propose_repair(
        session(3), c.id, RepairProposalInput(component_id="R1"), proposed_by="user"
    )
    c_case = await svc.verify_repair(session(3), c.id)  # R1 is already nominal: no recovery
    assert c_case.repair is not None and c_case.repair.functional_recovery is False
    c_closed = await svc.close_case(session(3), c.id)
    assert c_closed.reveal is not None
    assert c_closed.reveal.outcome.exact_identification == "incorrect"
    assert c_closed.reveal.outcome.unsupported_certainty is True
    assert c_closed.reveal.outcome.functional_recovery == "not_restored"

    # --- D: the transient source-current probe also separates R1 from C1. -------------------
    d = await open_case(rig, "c1_increased", 4)
    d_cur = await measure(rig, d, 4, "transient_capture", "source_current")
    assert d_cur.case.evidence.remaining == ["c1_increased"]
    assert_true_hypothesis_well_inside_tolerance(d_cur.case, "c1_increased")
    d_out = await measure(rig, d, 4, "transient_capture", "output_voltage")
    assert d_out.case.evidence.remaining == ["c1_increased"]
    await svc.close_case(session(4), d.id)

    # --- Report observed vs predicted (printed with -s). -------------------------------------
    cur_a, cur_b_pred = (
        a_cur.observation.sweep,
        hypotheses.predictions(
            "rc_lowpass",
            hypotheses.hypothesis_values(nominal, "c1_increased"),
            InstrumentKind.SOURCE_CURRENT_SWEEP,
        ),
    )
    assert cur_a is not None
    print("\nR-vs-C ambiguity (nominal 2.2 kOhm / 47 nF):")
    for k in (0, 15, 30):
        f = va.frequency_hz[k]
        pred = float(
            hypotheses.analytic.gain_db(
                hypotheses.transfer(
                    "rc_lowpass", hypotheses.hypothesis_values(nominal, "r1_increased"), [f]
                )
            )[0]
        )
        print(
            f"  {f:8.1f} Hz output gain: R1x2 {va.magnitude[k]:9.4f} dB, "
            f"C1x2 {vb.magnitude[k]:9.4f} dB, predicted (both) {pred:9.4f} dB"
        )
    for k in (0, 15, 30):
        f = cur_a.frequency_hz[k]
        pred_r = hypotheses.predictions(
            "rc_lowpass",
            hypotheses.hypothesis_values(nominal, "r1_increased"),
            InstrumentKind.SOURCE_CURRENT_SWEEP,
        )[2 * k].value
        print(
            f"  {f:8.1f} Hz |I|: observed (R1x2 case) {cur_a.magnitude[k]:.6f} mA, predicted "
            f"R1x2 {pred_r:.6f} mA, predicted C1x2 {cur_b_pred[2 * k].value:.6f} mA"
        )
    for label, values in TIMINGS.items():
        print(f"  timing {label}: " + ", ".join(f"{v:.2f}s" for v in values))


async def test_no_fault_control_is_supported_by_the_symptom(ngspice_rig: Rig) -> None:
    rig = ngspice_rig
    case = await open_case(rig, "no_fault", 9)
    assert case.evidence.remaining == ["no_fault"]
    assert status_of(case)["no_fault"] == "supported"
    assert_true_hypothesis_well_inside_tolerance(case, "no_fault")
    symptom = case.observations[0]
    case = await rig.service.submit_diagnosis(
        session(9),
        case.id,
        DiagnosisSubmission(
            conclusion="no_fault",
            citations=cite(symptom.id, "tone_gain_db"),
            explanation="Both tone gains match the intended design.",
        ),
        submitted_by="user",
    )
    assert case.diagnosis is not None and case.diagnosis.evidence_check.status == "supported"
    closed = await rig.service.close_case(session(9), case.id)
    assert closed.reveal is not None
    assert closed.reveal.outcome.exact_identification == "correct"
    assert closed.reveal.outcome.functional_recovery == "not_needed"
    assert closed.reveal.outcome.experiments_used == 0
