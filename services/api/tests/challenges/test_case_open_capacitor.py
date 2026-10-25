"""Open capacitor (``c1_disconnected``) and circuit families: analytical identifiability proofs
and the evidence rules for zero signals (no phase, measurement floors).

Proof obligations (milestone 2, before the condition enters the generator):
- (b) for EVERY drawable nominal design of BOTH families, the open capacitor is separated from
  every other hypothesis by the standardized symptom alone (≥ ``SYMPTOM_MIN_RATIO`` tolerances,
  on the family's own tones) and by every other instrument view (> ``SEPARABLE_MIN_RATIO``), so
  it is never confused with no_fault; an exact symptom observation of an open capacitor leaves
  it as the only remaining (supported) explanation;
- (c) the R-vs-C voltage ambiguity also holds for the high-pass family (see
  ``test_case_generator_hypotheses``) and the current sweep resolves it.
The measured side (real ngspice) is in ``test_case_families_ngspice.py``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np
import pytest
from challenges_testkit import SESSION_A, AnalyticTestSimulator, build_rig, seed_for

from resonance.api.errors import ApiException
from resonance.challenges import hypotheses
from resonance.challenges.evaluator import faults
from resonance.challenges.instruments import (
    CASE_FREQUENCY_GRID_HZ,
    CURRENT_FLOOR_MA,
    MEASUREMENT_FLOOR_DB,
    family_tones,
)
from resonance.config import Settings
from resonance.contracts.circuits import TemplateId
from resonance.contracts.common import SimulatorInfo, new_id
from resonance.contracts.internal import AcData
from resonance.contracts.investigation import (
    CaseExperimentRequest,
    DiagnosisSubmission,
    EvidenceCitation,
    EvidencePolicy,
    HypothesisId,
    InstrumentKind,
    Observation,
    ObservationSettings,
    ObservedTone,
    ObservedWaveform,
    RepairProposalInput,
    SweepObservation,
    TransientObservation,
    TransientProbe,
)

FAMILIES: tuple[TemplateId, ...] = ("rc_lowpass", "rc_highpass")
OPEN: HypothesisId = "c1_disconnected"
CASE_ID = new_id("case")


def _obs(
    template_id: TemplateId,
    nominal: dict[str, float],
    truth: HypothesisId,
    kind: InstrumentKind,
    probe: TransientProbe | None = None,
    *,
    phase_override: float | None = None,
) -> Observation:
    """An exact (analytical) observation of ``truth``. ``phase_override`` replaces every phase
    (to show that undefined phases never influence the evidence)."""
    tones = family_tones(template_id)
    preds = hypotheses.hypothesis_predictions(template_id, nominal, truth, kind, probe, tones=tones)
    mags = [p.value for p in preds[0::2]]
    phases = [phase_override if phase_override is not None else p.value for p in preds[1::2]]
    common = dict(
        id=new_id("obs"),
        case_id=CASE_ID,
        sequence=1,
        counted=True,
        requested_by="user",
        status="succeeded",
        created_at=datetime.now(UTC),
        simulator=SimulatorInfo(version="analytical-test-observation"),
        settings=ObservationSettings(),
        summary="analytical test observation",
    )
    if kind != InstrumentKind.TRANSIENT_CAPTURE:
        voltage = kind == InstrumentKind.OUTPUT_VOLTAGE_SWEEP
        sweep = SweepObservation(
            quantity="output_gain" if voltage else "source_current",
            frequency_hz=list(CASE_FREQUENCY_GRID_HZ),
            magnitude=mags,
            magnitude_unit="dB" if voltage else "mA",
            phase_deg=phases,
            expected_magnitude=mags,
            expected_phase_deg=phases,
        )
        return Observation(kind=kind.value, sweep=sweep, **common)  # type: ignore[arg-type]
    voltage = probe == "output_voltage"
    observed_tones = [
        ObservedTone(
            frequency_hz=t.frequency_hz,
            label=t.label,
            role=t.role,
            amplitude=t.amplitude_v * 10 ** (m / 20) if voltage else m,
            amplitude_unit="V" if voltage else "mA",
            gain_db=m if voltage else None,
            phase_deg=ph,
            expected_amplitude=1.0,
            expected_gain_db=0.0 if voltage else None,
            expected_phase_deg=0.0,
        )
        for t, m, ph in zip(tones, mags, phases, strict=True)
    ]
    transient = TransientObservation(
        probe=probe,  # type: ignore[arg-type]
        tones=observed_tones,
        waveform=ObservedWaveform(
            sample_rate_hz=48_000,
            start_s=0.0,
            unit="V" if voltage else "mA",
            input_v=[0.0],
            observed=[0.0],
            expected=[0.0],
        ),
    )
    return Observation(
        kind="symptom",
        probe=probe,
        transient=transient,
        **common,  # type: ignore[arg-type]
    )


def _status(
    template_id: TemplateId, nominal: dict[str, float], observations: Sequence[Observation]
) -> dict[str, str]:
    state = hypotheses.assess(
        template_id,
        nominal,
        observations,
        publish_distinguishing=False,
        tones=family_tones(template_id),
    )
    return {a.hypothesis_id: a.status for a in state.assessments}


def test_open_capacitor_predictions_per_family() -> None:
    """Low-pass: H = 1 (0 dB, 0°), I = 0. High-pass: H = 0 (dB floor, no phase), I = 0."""
    nominal = {"R1": 2200.0, "C1": 47e-9}
    for template_id in FAMILIES:
        tones = family_tones(template_id)
        for kind, probe in faults.INSTRUMENT_VIEWS:
            preds = hypotheses.hypothesis_predictions(
                template_id, nominal, OPEN, kind, probe, tones=tones
            )
            magnitudes, phases = preds[0::2], preds[1::2]
            voltage = magnitudes[0].unit == "dB"
            if voltage and template_id == "rc_lowpass":
                assert all(m.value == 0.0 for m in magnitudes)
                assert all(p.defined and p.value == 0.0 for p in phases)
            elif voltage:
                assert all(m.value == MEASUREMENT_FLOOR_DB for m in magnitudes)
                assert not any(p.defined for p in phases)
            else:
                assert all(m.value == 0.0 for m in magnitudes)
                assert all(math.isclose(m.tolerance, 1e-6) for m in magnitudes)
                assert not any(p.defined for p in phases)


def _min_separation(
    template_id: TemplateId, nominal: dict[str, float], kind: InstrumentKind, probe: str | None
) -> float:
    preds = faults.view_predictions(template_id, nominal, kind, probe)  # type: ignore[arg-type]
    others = [h for h in hypotheses.HYPOTHESIS_IDS if h != OPEN]
    return min(
        min(
            hypotheses.separation_ratio(preds[OPEN], preds[h]),
            hypotheses.separation_ratio(preds[h], preds[OPEN]),
        )
        for h in others
    )


@pytest.mark.parametrize("template_id", FAMILIES)
def test_open_capacitor_is_separable_for_every_drawable_design(template_id: TemplateId) -> None:
    """(b) For every drawable design: the symptom alone separates the open capacitor from every
    other hypothesis (no_fault included), and so does each instrument on its own."""
    worst: dict[str, float] = {}
    for r, c in faults.nominal_designs(template_id):
        nominal = {"R1": r, "C1": c}
        for kind, probe in faults.INSTRUMENT_VIEWS:
            ratio = _min_separation(template_id, nominal, kind, probe)
            view = f"{kind.value}:{probe}" if probe else kind.value
            worst[view] = min(worst.get(view, math.inf), ratio)
            symptom = kind == InstrumentKind.TRANSIENT_CAPTURE and probe == "output_voltage"
            limit = faults.SYMPTOM_MIN_RATIO if symptom else faults.SEPARABLE_MIN_RATIO
            assert ratio > limit, (template_id, r, c, view, ratio)
        # An exact open-capacitor symptom leaves only the open capacitor (supported) ...
        status = _status(
            template_id,
            nominal,
            [_obs(template_id, nominal, OPEN, InstrumentKind.TRANSIENT_CAPTURE, "output_voltage")],
        )
        assert status[OPEN] == "supported", (template_id, r, c, status)
        # ... and an exact no-fault symptom contradicts the open capacitor.
        status = _status(
            template_id,
            nominal,
            [
                _obs(
                    template_id,
                    nominal,
                    "no_fault",
                    InstrumentKind.TRANSIENT_CAPTURE,
                    "output_voltage",
                )
            ],
        )
        assert status[OPEN] == "contradicted" and status["no_fault"] == "supported"
    print(f"\n{template_id}: minimum open-capacitor separation (tolerances) per view: {worst}")


@pytest.mark.parametrize("template_id", FAMILIES)
def test_current_sweep_resolves_r_vs_c_in_both_families(template_id: TemplateId) -> None:
    """(c) After an exact output sweep of R1×2 the pair {R1×2, C1×2} remains (voltage cannot
    split it); adding an exact source-current sweep leaves only the truth."""
    for r, c in faults.nominal_designs(template_id):
        nominal = {"R1": r, "C1": c}
        for truth, twin in (("r1_increased", "c1_increased"), ("c1_decreased", "r1_decreased")):
            volt = _obs(template_id, nominal, truth, InstrumentKind.OUTPUT_VOLTAGE_SWEEP)  # type: ignore[arg-type]
            status = _status(template_id, nominal, [volt])
            assert status[truth] == status[twin] == "unresolved", (template_id, r, c, status)
            assert [h for h, s in status.items() if s != "contradicted"] == sorted(
                [truth, twin], key=hypotheses.HYPOTHESIS_IDS.index
            )
            cur = _obs(template_id, nominal, truth, InstrumentKind.SOURCE_CURRENT_SWEEP)  # type: ignore[arg-type]
            status = _status(template_id, nominal, [volt, cur])
            assert status[truth] == "supported" and status[twin] == "contradicted"


@pytest.mark.parametrize("phase", [0.0, 90.0, -170.0, 179.99])
def test_undefined_phases_never_decide_anything(phase: float) -> None:
    """A zero output (high-pass open capacitor) or zero current has no phase: whatever phase
    number accompanies it, every status is the same, and no check reports a phase."""
    nominal = {"R1": 1000.0, "C1": 1e-7}
    reference: dict[str, str] | None = None
    for template_id, kind, probe in (
        ("rc_highpass", InstrumentKind.TRANSIENT_CAPTURE, "output_voltage"),
        ("rc_highpass", InstrumentKind.OUTPUT_VOLTAGE_SWEEP, None),
        ("rc_lowpass", InstrumentKind.SOURCE_CURRENT_SWEEP, None),
        ("rc_highpass", InstrumentKind.TRANSIENT_CAPTURE, "source_current"),
    ):
        obs = _obs(template_id, nominal, OPEN, kind, probe, phase_override=phase)  # type: ignore[arg-type]
        state = hypotheses.assess(
            template_id,  # type: ignore[arg-type]
            nominal,
            [obs],
            publish_distinguishing=False,
            tones=family_tones(template_id),  # type: ignore[arg-type]
        )
        status = {a.hypothesis_id: a.status for a in state.assessments}
        assert status[OPEN] == "supported", (template_id, kind, status)
        assert state.remaining == [OPEN]
        for assessment in state.assessments:
            for check in assessment.checks:
                assert "phase" not in check.quantity, (template_id, kind, check)
        reference = reference or status
        assert status == reference


def test_floor_semantics_for_gains_and_currents() -> None:
    gain = hypotheses.Prediction(
        "output_gain_db", "dB", 20.0, MEASUREMENT_FLOOR_DB, 0.5, floor=MEASUREMENT_FLOOR_DB
    )
    assert gain.deviation(-200.0) == 0.0  # e.g. an old payload clamped at policy.DB_FLOOR
    assert gain.deviation(-150.0) == 0.0  # both below the floor: "no measurable output"
    assert gain.deviation(-60.0) == pytest.approx(60.0)
    assert gain.level(-200.0) == MEASUREMENT_FLOOR_DB and gain.level(-60.0) == -60.0
    current = hypotheses.Prediction(
        "source_current_ma", "mA", 20.0, 0.0, 1e-6, floor=CURRENT_FLOOR_MA
    )
    assert current.level(1e-17) == 0.0 and current.deviation(1e-17) == 0.0
    assert current.level(4e-4) == 4e-4 and current.deviation(4e-4) == pytest.approx(4e-4)
    assert not hypotheses.gain_measurable(MEASUREMENT_FLOOR_DB)
    assert hypotheses.gain_measurable(-60.0)
    assert not hypotheses.current_measurable(CURRENT_FLOOR_MA)
    assert hypotheses.current_measurable(4e-4)
    undefined = hypotheses.Prediction(
        "output_phase_deg", "deg", 20.0, 0.0, 3.0, angle=True, defined=False
    )
    defined = hypotheses.Prediction("output_phase_deg", "deg", 20.0, 90.0, 3.0, angle=True)
    assert not undefined.disjoint(defined) and not defined.disjoint(undefined)
    assert hypotheses.separation_ratio([undefined], [defined]) == 0.0


def test_open_capacitor_is_voltage_equivalent_only_to_itself() -> None:
    assert hypotheses.voltage_equivalent(OPEN, OPEN)
    for h in hypotheses.HYPOTHESIS_IDS:
        if h != OPEN:
            assert not hypotheses.voltage_equivalent(OPEN, h)
            assert not hypotheses.voltage_equivalent(h, OPEN)
    assert hypotheses.voltage_equivalent("r1_increased", "c1_increased")
    assert hypotheses.HYPOTHESES_BY_ID[OPEN].label == "C1 is disconnected (open)"
    assert hypotheses.hypothesis_disconnected(OPEN) == frozenset({"C1"})
    assert hypotheses.hypothesis_values({"R1": 1.0, "C1": 2.0}, OPEN) == {"R1": 1.0, "C1": 2.0}


class _NaNSimulator(AnalyticTestSimulator):
    """TEST DOUBLE: once ``broken`` is set, every AC result is NaN (a broken simulator)."""

    broken = False

    async def ac(self, circuit, sweep, *, cancel=None):  # type: ignore[no-untyped-def]
        data = await super().ac(circuit, sweep, cancel=cancel)
        if not self.broken:
            return data
        nan = np.full_like(data.frequency_hz, np.nan, dtype=np.complex128)
        return AcData(data.frequency_hz, nan, nan, nan)


@pytest.mark.parametrize("instrument", ["output_voltage_sweep", "source_current_sweep"])
async def test_non_finite_simulator_output_is_a_failure_not_a_zero_signal(
    settings: Settings, instrument: str
) -> None:
    """The zero-phasor convention must never turn NaN into "no measurable output/current"."""
    simulator = _NaNSimulator()
    rig = build_rig(settings, simulator)
    try:
        case = await rig.service.harness_create_case(SESSION_A, seed_for("no_fault"))
        await rig.service.set_mode(SESSION_A, case.id, "guide")
        simulator.broken = True
        with pytest.raises(ApiException) as info:
            await rig.service.run_experiment(
                SESSION_A,
                case.id,
                CaseExperimentRequest.model_validate(
                    {"instrument": instrument, "client_request_id": "nan-check-1"}
                ),
                requested_by="user",
            )
        assert info.value.error.code.value == "internal_error"
        after = rig.service.get_case(SESSION_A, case.id)
        assert after.observations[-1].status == "failed"
        assert after.observations[-1].sweep is None
        assert after.budget.used == 0
    finally:
        rig.service.close()
        rig.store.close()


async def test_published_floor_values_are_exact_and_arithmetically_consistent(
    settings: Settings,
) -> None:
    """M2-02/M2-03: a high-pass open capacitor publishes its silence ON the declared floors
    (gain = -120 dB, amplitudes and currents exactly 0, phase 0), every evidence check reports
    observed and predicted on the scale its deviation uses, a still-silent repair reports a
    deviation consistent with the published points, and the floors are in EvidencePolicy."""
    assert EvidencePolicy.model_fields["measurement_floor_db"].default == MEASUREMENT_FLOOR_DB
    assert EvidencePolicy.model_fields["current_floor_ma"].default == CURRENT_FLOOR_MA
    rig = build_rig(settings, AnalyticTestSimulator())
    s = SESSION_A
    try:
        seed = seed_for(OPEN, (1000.0, 100e-9), "rc_highpass")
        case = await rig.service.harness_create_case(s, seed, "rc_highpass")
        assert case.policy.measurement_floor_db == MEASUREMENT_FLOOR_DB
        assert case.policy.current_floor_ma == CURRENT_FLOOR_MA
        await rig.service.set_mode(s, case.id, "guide")
        plan = [
            ("output_voltage_sweep", None),
            ("source_current_sweep", None),
            ("transient_capture", "source_current"),
        ]
        for i, (instrument, probe) in enumerate(plan):
            case = (
                await rig.service.run_experiment(
                    s,
                    case.id,
                    CaseExperimentRequest.model_validate(
                        {
                            "instrument": instrument,
                            "probe": probe,
                            "client_request_id": f"floor-check-{i}",
                        }
                    ),
                    requested_by="user",
                )
            ).case
        for obs in case.observations:
            if obs.sweep is not None:
                floor_value = MEASUREMENT_FLOOR_DB if obs.sweep.magnitude_unit == "dB" else 0.0
                assert set(obs.sweep.magnitude) == {floor_value}, obs.kind
                assert set(obs.sweep.phase_deg) == {0.0}
                assert min(obs.sweep.expected_magnitude) > floor_value  # the reference is real
            else:
                assert obs.transient is not None
                for tone in obs.transient.tones:
                    assert (tone.amplitude, tone.phase_deg) == (0.0, 0.0), tone
                    if tone.gain_db is not None:
                        assert tone.gain_db == MEASUREMENT_FLOOR_DB
        checks = [c for a in case.evidence.assessments for c in a.checks]
        assert any(c.unit == "dB" and c.deviation > 50 for c in checks)  # e.g. no_fault
        for check in checks:
            assert math.isclose(
                abs(check.observed - check.predicted), check.deviation, abs_tol=1e-9
            ), check
            if check.unit == "dB":
                assert check.observed >= MEASUREMENT_FLOOR_DB
                assert check.predicted >= MEASUREMENT_FLOOR_DB
        symptom = case.observations[0]
        await rig.service.submit_diagnosis(
            s,
            case.id,
            DiagnosisSubmission(
                conclusion="fault",
                hypothesis_id="r1_increased",  # wrong on purpose: the repair stays silent
                citations=[EvidenceCitation(observation_id=symptom.id, quantity="tone_gain_db")],
                explanation="test",
            ),
            submitted_by="user",
        )
        await rig.service.propose_repair(
            s, case.id, RepairProposalInput(component_id="R1"), proposed_by="user"
        )
        case = await rig.service.verify_repair(s, case.id)
        repair = case.repair
        assert repair is not None and repair.functional_recovery is False
        assert {p.repaired_gain_db for p in repair.points} == {MEASUREMENT_FLOOR_DB}
        worst = max(abs(p.repaired_gain_db - p.expected_gain_db) for p in repair.points)
        assert repair.max_gain_deviation_db == pytest.approx(worst, abs=1e-3)
        assert repair.max_gain_deviation_db is not None and repair.max_gain_deviation_db < 120
        assert repair.max_phase_deviation_deg == 0.0  # undefined phases are not compared
        assert "no measurable output" in repair.summary
    finally:
        rig.service.close()
        rig.store.close()
