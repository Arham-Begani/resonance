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
    HypothesisId,
    InstrumentKind,
    Observation,
    ObservationSettings,
    ObservedTone,
    ObservedWaveform,
    SweepObservation,
    TransientObservation,
    TransientProbe,
)
from resonance.contracts.policy import DB_FLOOR

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
        kind="symptom", probe=probe, transient=transient, **common  # type: ignore[arg-type]
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
                assert all(m.value == DB_FLOOR for m in magnitudes)
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
        "output_gain_db", "dB", 20.0, DB_FLOOR, 0.5, floor=MEASUREMENT_FLOOR_DB
    )
    assert gain.deviation(-200.0) == 0.0
    assert gain.deviation(-150.0) == 0.0  # both below the floor: "no measurable output"
    assert gain.deviation(-60.0) == pytest.approx(60.0)
    assert not hypotheses.gain_measurable(MEASUREMENT_FLOOR_DB)
    assert hypotheses.gain_measurable(-60.0)
    assert not hypotheses.current_measurable(CURRENT_FLOOR_MA)
    assert hypotheses.current_measurable(4e-4)
    undefined = hypotheses.Prediction("output_phase_deg", "deg", 20.0, 0.0, 3.0, angle=True,
                                      defined=False)
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
