"""Identifiability with real ngspice, for BOTH families: R×k and C×k are indistinguishable by
voltage transfer alone (low-pass H = 1/(1+jωRC), high-pass H = jωRC/(1+jωRC): both depend on
R and C only through RC), but the source current separates them (ratio exactly k at every
frequency, because both present the series-RC input I = jωC / (1 + jωRC) and RC is equal in
both variants)."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from resonance.config import Settings
from resonance.contracts.circuits import SourceSpec, TemplateId
from resonance.contracts.internal import AcData, AcSweep, CircuitSpec
from resonance.contracts.policy import (
    CASE_SWEEP_POINTS_PER_DECADE,
    CASE_SWEEP_START_HZ,
    CASE_SWEEP_STOP_HZ,
    TEMPLATE_TONES,
)
from resonance.simulation import create_simulator

pytestmark = pytest.mark.ngspice

GRID = AcSweep(CASE_SWEEP_START_HZ, CASE_SWEEP_STOP_HZ, CASE_SWEEP_POINTS_PER_DECADE)
NOMINAL = {"R1": 2200.0, "C1": 47e-9}  # fc ≈ 1.54 kHz


def _run(settings: Settings, template_id: TemplateId, r_mult: float, c_mult: float) -> AcData:
    values = {"R1": NOMINAL["R1"] * r_mult, "C1": NOMINAL["C1"] * c_mult}
    source = SourceSpec(tones=list(TEMPLATE_TONES[template_id]), description="test")
    circuit = CircuitSpec(template_id=template_id, values=values, source=source)
    return asyncio.run(create_simulator(settings).ac(circuit, GRID))


@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
@pytest.mark.parametrize("k", [2.0, 0.5])
def test_voltage_transfer_identical_but_current_distinguishes(
    settings: Settings, template_id: TemplateId, k: float
) -> None:
    r_fault = _run(settings, template_id, k, 1.0)
    c_fault = _run(settings, template_id, 1.0, k)
    nominal = _run(settings, template_id, 1.0, 1.0)
    for d in (r_fault, c_fault, nominal):
        assert d.v_in is not None and d.v_out is not None and d.i_source is not None
    assert r_fault.frequency_hz.size == 31

    h_r = r_fault.v_out / r_fault.v_in  # type: ignore[operator]
    h_c = c_fault.v_out / c_fault.v_in  # type: ignore[operator]
    h_n = nominal.v_out / nominal.v_in  # type: ignore[operator]
    # Voltage-only: equal to numerical precision at every grid point ...
    assert np.allclose(h_r, h_c, rtol=1e-9, atol=1e-15)
    # ... while both clearly differ from the nominal design (the fault IS visible).
    assert np.max(np.abs(20 * np.log10(np.abs(h_r) / np.abs(h_n)))) > 3.0

    # Source current: I = jωC / (1 + jωRC). Both variants share RC, so
    # I_C-fault / I_R-fault = (k·C) / C = k at every frequency.
    ratio = np.abs(c_fault.i_source) / np.abs(r_fault.i_source)  # type: ignore[arg-type]
    assert np.allclose(ratio, k, rtol=1e-9)
    relative_difference = np.abs(ratio - 1.0)
    assert np.all(relative_difference >= 0.4)  # ≥ 40 % everywhere, incl. the top of the grid
    phase_diff = np.angle(c_fault.i_source / r_fault.i_source, deg=True)  # type: ignore[operator]
    assert np.allclose(phase_diff, 0.0, atol=1e-6)  # same RC ⇒ same phase; magnitude separates


def test_families_differ_for_the_same_values(settings: Settings) -> None:
    """Same R/C, different topology: the voltage transfers are complementary (H_lp + H_hp = 1)
    while the source currents are identical (same series-RC input impedance)."""
    low = _run(settings, "rc_lowpass", 1.0, 1.0)
    high = _run(settings, "rc_highpass", 1.0, 1.0)
    assert low.v_out is not None and high.v_out is not None
    assert np.allclose(low.v_out + high.v_out, 1.0, rtol=0, atol=1e-9)
    assert np.allclose(low.i_source, high.i_source, rtol=1e-9, atol=0)  # type: ignore[arg-type]
