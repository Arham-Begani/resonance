"""Real ngspice AC results versus independent analytical RC equations.

Assumptions (stated, not modeled): ideal voltage source, ideal linear R and C, no load on
``out``, no parasitics or tolerances; temperature (27 °C default) does not affect ideal R/C.
Tolerances (CLAUDE.md physics gate): |H| and |I| within 1 % relative (with a tiny absolute
floor), phase within 1° using wrapped differences, on a log grid spanning fc/100 … 100·fc
(four decades, 20 points/decade) for every combination. For the high-pass, |H| ≈ f/fc falls to
≈ 0.01 at fc/100, so the 1 % relative bound (≈ 1e-4) still governs there; the 1e-9 absolute
floor only matters for values that are effectively zero.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import numpy as np
import pytest

from resonance.circuits.revisions import build_revision, revision_to_circuit
from resonance.config import Settings
from resonance.contracts.circuits import TemplateId
from resonance.contracts.internal import AcData, AcPoints, AcSweep, CircuitSpec
from resonance.simulation import analytic, create_simulator

pytestmark = pytest.mark.ngspice

REL_TOL = 0.01
ABS_TOL_H = 1e-9  # |H| absolute floor (unitless)
ABS_TOL_I = 1e-12  # |I| absolute floor (A)
PHASE_TOL_DEG = 1.0

# Spans the per-component bounds (100 Ω–100 kΩ, 1 nF–10 µF) and the 20 Hz–20 kHz cutoff range.
COMBINATIONS = [
    (100.0, 10e-6),  # fc ≈ 159 Hz (R min, C max)
    (100e3, 1e-9),  # fc ≈ 1.59 kHz (R max, C min)
    (100e3, 68e-9),  # fc ≈ 23.4 Hz (near the lower cutoff limit)
    (390.0, 22e-9),  # fc ≈ 18.5 kHz (near the upper cutoff limit)
    (1e3, 100e-9),  # template default, fc ≈ 1.59 kHz
]


# High-pass designs: the same bounds and cutoff range, including the four corners.
HIGHPASS_COMBINATIONS = [
    (100.0, 10e-6),  # fc ≈ 159 Hz (R min, C max)
    (100e3, 1e-9),  # fc ≈ 1.59 kHz (R max, C min)
    (100e3, 79.5e-9),  # fc ≈ 20.02 Hz (lower cutoff corner, R max)
    (100.0, 80e-9),  # fc ≈ 19.9 kHz (upper cutoff corner, R min)
    (8.2e3, 1e-9),  # fc ≈ 19.4 kHz (upper cutoff corner, C min)
    (10e3, 100e-9),  # template default, fc ≈ 159 Hz
]


def _circuit(r: float, c: float, template_id: TemplateId = "rc_lowpass") -> CircuitSpec:
    # build_revision enforces the template's bounds and joint cutoff range for every design.
    return revision_to_circuit(build_revision(template_id, {"R1": r, "C1": c}, origin="user_edit"))


def _phase_err(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.abs(analytic.wrap_phase_deg(np.degrees(np.angle(a)) - np.degrees(np.angle(b))))


def _max_errors(
    measured: np.ndarray, expected: np.ndarray, abs_floor: float
) -> tuple[float, float]:
    mag_err = np.abs(np.abs(measured) - np.abs(expected))
    allowed = np.maximum(REL_TOL * np.abs(expected), abs_floor)
    assert np.all(mag_err <= allowed)
    rel = float(np.max(mag_err / np.abs(expected)))
    phase = float(np.max(_phase_err(measured, expected)))
    assert phase <= PHASE_TOL_DEG
    return rel, phase


def _sweep(settings: Settings, circuit: CircuitSpec, sweep: AcSweep | AcPoints) -> AcData:
    return asyncio.run(create_simulator(settings).ac(circuit, sweep))


@pytest.mark.parametrize(("r", "c"), COMBINATIONS)
def test_lowpass_matches_analytical_gain_phase_and_current(
    settings: Settings, r: float, c: float, record_property: Callable[[str, object], None]
) -> None:
    fc = analytic.cutoff_hz(r, c)
    sweep = AcSweep(start_hz=fc / 100, stop_hz=fc * 100, points_per_decade=20)
    data = _sweep(settings, _circuit(r, c), sweep)

    assert data.v_in is not None and data.v_out is not None and data.i_source is not None
    assert data.frequency_hz.size == 81
    assert np.allclose(
        data.frequency_hz, np.geomspace(sweep.start_hz, sweep.stop_hz, 81), rtol=1e-9
    )
    assert np.allclose(data.v_in, 1.0, rtol=0, atol=1e-12)  # ideal 1 V, 0° source

    h = data.v_out / data.v_in
    h_rel, h_phase = _max_errors(h, analytic.lowpass_transfer(data.frequency_hz, r, c), ABS_TOL_H)
    i_rel, i_phase = _max_errors(
        data.i_source, analytic.series_rc_source_current(data.frequency_hz, r, c), ABS_TOL_I
    )
    record_property("max_errors", (h_rel, h_phase, i_rel, i_phase))
    # Delivered convention: at low frequency the capacitor's current leads the source voltage.
    assert np.angle(data.i_source[0], deg=True) > 80.0


@pytest.mark.parametrize(("r", "c"), HIGHPASS_COMBINATIONS)
def test_highpass_matches_analytical_gain_phase_and_current(
    settings: Settings, r: float, c: float, record_property: Callable[[str, object], None]
) -> None:
    fc = analytic.cutoff_hz(r, c)
    sweep = AcSweep(start_hz=fc / 100, stop_hz=fc * 100, points_per_decade=20)
    data = _sweep(settings, _circuit(r, c, "rc_highpass"), sweep)

    assert data.v_in is not None and data.v_out is not None and data.i_source is not None
    assert data.frequency_hz.size == 81
    assert np.allclose(data.v_in, 1.0, rtol=0, atol=1e-12)

    h = data.v_out / data.v_in
    expected_h = analytic.highpass_transfer(data.frequency_hz, r, c)
    h_rel, h_phase = _max_errors(h, expected_h, ABS_TOL_H)
    i_rel, i_phase = _max_errors(
        data.i_source, analytic.series_rc_source_current(data.frequency_hz, r, c), ABS_TOL_I
    )
    record_property("max_errors", (h_rel, h_phase, i_rel, i_phase))
    # Shape sanity independent of the formula: ≈ +20 dB/decade far below fc, flat above, and
    # −3 dB with +45° at fc (the 41st point of the grid is exactly fc).
    assert np.isclose(data.frequency_hz[40], fc, rtol=1e-9)
    assert abs(20 * np.log10(abs(h[40])) + 10 * np.log10(2)) < 0.01
    assert abs(np.angle(h[40], deg=True) - 45.0) < 0.1
    assert abs(20 * np.log10(abs(h[20]) / abs(h[0])) - 20.0) < 0.1  # fc/100 → fc/10
    assert abs(abs(h[-1]) - 1.0) < 1e-3


def test_exact_points_return_requested_frequencies(settings: Settings) -> None:
    r, c = 1e3, 100e-9
    data = _sweep(settings, _circuit(r, c), AcPoints((440.0, 4000.0, 1234.5)))
    assert data.frequency_hz.tolist() == [440.0, 4000.0, 1234.5]
    assert data.v_out is not None
    _max_errors(data.v_out, analytic.lowpass_transfer(data.frequency_hz, r, c), ABS_TOL_H)


def test_only_requested_probes_are_returned(settings: Settings) -> None:
    data = _sweep(settings, _circuit(1e3, 100e-9), AcSweep(20.0, 20e3, 10, probes=("i(V1)",)))
    assert data.v_in is None and data.v_out is None and data.i_source is not None
    assert data.frequency_hz.size == 31
