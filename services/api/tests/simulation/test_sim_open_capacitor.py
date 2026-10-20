"""Open capacitor (``CircuitSpec.disconnected == {"C1"}``) with real ngspice, both families.

Removing C1 must leave a well-posed circuit (no floating node, no singular matrix) and give the
physically right answers for an ideal open circuit:

- low-pass: no current flows, so R1 drops no voltage → V(out) = V(in), I(V1) = 0;
- high-pass: nothing drives the output; R1 holds it at ground → V(out) = 0, I(V1) = 0.

Observed with ngspice 47 (recorded here, asserted with tight tolerances below): AC gives exactly
V(out) = 1 + 0j (low-pass) / 0 + 0j (high-pass) and I(V1) = 0 + 0j; transient high-pass output
and current are exactly 0, transient low-pass |V(out) − V(in)| ≈ 1e-16 V and |I| ≈ 1e-19 A
(round-off). Zero currents are returned as +0.0, so their computed phase is 0° rather than the
spurious ±180° of −0.0. A zero output is −inf dB; consumers clamp at ``policy.DB_FLOOR``.
"""

from __future__ import annotations

import asyncio
import dataclasses

import numpy as np
import pytest

from resonance.circuits.revisions import build_revision, revision_to_circuit
from resonance.config import Settings
from resonance.contracts.circuits import TemplateId
from resonance.contracts.internal import AcData, AcPoints, AcSweep, CircuitSpec, TransientRequest
from resonance.contracts.policy import (
    CASE_SWEEP_POINTS_PER_DECADE,
    CASE_SWEEP_START_HZ,
    CASE_SWEEP_STOP_HZ,
    DB_FLOOR,
    TRANSIENT_MAX_STEP_S,
)
from resonance.simulation import analytic, create_simulator

pytestmark = pytest.mark.ngspice

GRID = AcSweep(CASE_SWEEP_START_HZ, CASE_SWEEP_STOP_HZ, CASE_SWEEP_POINTS_PER_DECADE)
OPEN = frozenset({"C1"})
V_ATOL = 1e-12  # V (AC, 1 V source); observed: exact
I_ATOL = 1e-15  # A (AC); observed: exact 0
TRAN_V_ATOL = 1e-9  # V; observed ≤ 1.2e-16
TRAN_I_ATOL = 1e-12  # A; observed ≤ 1.1e-19

# Default designs and the R-min/C-max design (the largest current when C1 IS connected).
DESIGNS = [
    ("rc_lowpass", 1e3, 100e-9),
    ("rc_lowpass", 100.0, 10e-6),
    ("rc_highpass", 10e3, 100e-9),
    ("rc_highpass", 100.0, 10e-6),
]


def _open_circuit(template_id: TemplateId, r: float, c: float) -> CircuitSpec:
    connected = revision_to_circuit(
        build_revision(template_id, {"R1": r, "C1": c}, origin="user_edit")
    )
    return dataclasses.replace(connected, disconnected=OPEN)


def _ac(settings: Settings, circuit: CircuitSpec, analysis: AcSweep | AcPoints) -> AcData:
    return asyncio.run(create_simulator(settings).ac(circuit, analysis))


def _check_ac(template_id: TemplateId, data: AcData) -> None:
    assert data.v_in is not None and data.v_out is not None and data.i_source is not None
    f = data.frequency_hz
    assert np.allclose(data.v_in, 1.0, rtol=0, atol=V_ATOL)
    expected_out = analytic.transfer(template_id, f, {"R1": 1.0}, disconnected=OPEN)
    assert np.all(np.abs(data.v_out - expected_out) <= V_ATOL)
    assert np.all(np.abs(data.i_source) <= I_ATOL)
    assert np.all(analytic.source_current(template_id, f, {}, disconnected=OPEN) == 0)
    # No negative zeros: the phase of a zero current must not come out as ±180°.
    assert np.all(analytic.phase_deg(data.i_source) == 0.0)
    gains = analytic.gain_db(data.v_out / data.v_in, floor_db=DB_FLOOR)
    assert np.all(np.isfinite(gains))
    if template_id == "rc_lowpass":
        assert np.allclose(gains, 0.0, atol=1e-9)
        assert np.allclose(analytic.phase_deg(data.v_out), 0.0, atol=1e-9)
    else:
        assert np.all(gains == DB_FLOOR)


@pytest.mark.parametrize(("template_id", "r", "c"), DESIGNS)
def test_open_capacitor_ac_on_the_case_grid(
    settings: Settings, template_id: TemplateId, r: float, c: float
) -> None:
    data = _ac(settings, _open_circuit(template_id, r, c), GRID)
    assert data.frequency_hz.size == 31
    _check_ac(template_id, data)


@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
def test_open_capacitor_exact_tone_points_and_value_free_spec(
    settings: Settings, template_id: TemplateId
) -> None:
    circuit = _open_circuit(template_id, 2200.0, 47e-9)
    tones = tuple(t.frequency_hz for t in circuit.source.tones)
    data = _ac(settings, circuit, AcPoints(tones))
    assert data.frequency_hz.tolist() == list(tones)
    _check_ac(template_id, data)
    # The open part's value may be omitted from the spec; results are identical.
    value_free = dataclasses.replace(circuit, values={"R1": 2200.0})
    same = _ac(settings, value_free, AcPoints(tones))
    assert same.v_out is not None and data.v_out is not None
    assert np.array_equal(same.v_out, data.v_out)


@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
def test_open_capacitor_two_tone_transient(settings: Settings, template_id: TemplateId) -> None:
    circuit = _open_circuit(template_id, 1e3, 100e-9)
    request = TransientRequest(0.3, TRANSIENT_MAX_STEP_S, probes=("v(in)", "v(out)", "i(V1)"))
    data = asyncio.run(create_simulator(settings).transient(circuit, request))
    t = data.time_s
    assert data.v_in is not None and data.v_out is not None and data.i_source is not None
    assert t[0] == 0.0 and np.isclose(t[-1], 0.3) and np.all(np.diff(t) > 0)
    expected_in = sum(
        tone.amplitude_v * np.sin(2 * np.pi * tone.frequency_hz * t)
        for tone in circuit.source.tones
    )
    assert np.max(np.abs(data.v_in - expected_in)) < 1e-6
    if template_id == "rc_lowpass":
        assert np.max(np.abs(data.v_out - data.v_in)) <= TRAN_V_ATOL
    else:
        assert np.max(np.abs(data.v_out)) <= TRAN_V_ATOL
    assert np.max(np.abs(data.i_source)) <= TRAN_I_ATOL
