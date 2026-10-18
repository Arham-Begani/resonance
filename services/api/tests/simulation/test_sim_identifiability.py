"""Identifiability with real ngspice: R×k and C×k are indistinguishable by voltage transfer
alone, but the source current separates them (ratio exactly k at every frequency, because
I = jωC / (1 + jωRC) and RC is equal in both variants)."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from resonance.config import Settings
from resonance.contracts.circuits import SourceSpec
from resonance.contracts.internal import AcData, AcSweep, CircuitSpec
from resonance.contracts.policy import (
    CASE_SWEEP_POINTS_PER_DECADE,
    CASE_SWEEP_START_HZ,
    CASE_SWEEP_STOP_HZ,
    DEFAULT_TONES,
)
from resonance.simulation import create_simulator

pytestmark = pytest.mark.ngspice

SOURCE = SourceSpec(tones=list(DEFAULT_TONES), description="test")
GRID = AcSweep(CASE_SWEEP_START_HZ, CASE_SWEEP_STOP_HZ, CASE_SWEEP_POINTS_PER_DECADE)
NOMINAL = {"R1": 2200.0, "C1": 47e-9}  # fc ≈ 1.54 kHz


def _run(settings: Settings, r_mult: float, c_mult: float) -> AcData:
    values = {"R1": NOMINAL["R1"] * r_mult, "C1": NOMINAL["C1"] * c_mult}
    circuit = CircuitSpec(template_id="rc_lowpass", values=values, source=SOURCE)
    return asyncio.run(create_simulator(settings).ac(circuit, GRID))


@pytest.mark.parametrize("k", [2.0, 0.5])
def test_voltage_transfer_identical_but_current_distinguishes(settings: Settings, k: float) -> None:
    r_fault = _run(settings, k, 1.0)
    c_fault = _run(settings, 1.0, k)
    nominal = _run(settings, 1.0, 1.0)
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
