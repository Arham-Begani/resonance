"""Two-tone transient with real ngspice: the source is exactly the tone sum, the time vector is
well formed, and steady-state tone gains roughly match the analytical transfer. Each family is
driven by its own policy signal (low-pass: 440 Hz + 4 kHz; high-pass: 50 Hz hum + 440 Hz).

This is a coarse physics check (2 % amplitude, 2° phase). The audio engineer owns the precise
0.5 dB tone-amplitude validation against AC results.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from resonance.circuits.revisions import build_revision, revision_to_circuit
from resonance.config import Settings
from resonance.contracts.circuits import TemplateId
from resonance.contracts.internal import TransientData, TransientRequest
from resonance.contracts.policy import (
    AUDIO_CLIP_S,
    TEMPLATE_TONES,
    TRANSIENT_MAX_STEP_S,
    TRANSIENT_SETTLE_S,
)
from resonance.simulation import analytic, create_simulator

pytestmark = pytest.mark.ngspice

AMP_REL_TOL = 0.02
PHASE_TOL_DEG = 2.0


def _fit_tones(t: np.ndarray, y: np.ndarray, freqs: list[float]) -> list[complex]:
    """Least-squares phasors (peak amplitude, sine reference) on non-uniform timepoints."""
    cols = [np.ones_like(t)]
    for f in freqs:
        cols += [np.sin(2 * np.pi * f * t), np.cos(2 * np.pi * f * t)]
    coef, *_ = np.linalg.lstsq(np.column_stack(cols), y, rcond=None)
    # a·sin(ωt) + b·cos(ωt) = A·sin(ωt + φ) with A·e^{jφ} = a + j·b
    return [complex(coef[1 + 2 * i], coef[2 + 2 * i]) for i in range(len(freqs))]


def _run(
    settings: Settings, template_id: TemplateId, r: float, c: float, stop_s: float
) -> TransientData:
    circuit = revision_to_circuit(
        build_revision(template_id, {"R1": r, "C1": c}, origin="user_edit")
    )
    request = TransientRequest(stop_s, TRANSIENT_MAX_STEP_S, probes=("v(in)", "v(out)", "i(V1)"))
    return asyncio.run(create_simulator(settings).transient(circuit, request))


@pytest.mark.parametrize(
    ("template_id", "r", "c", "stop_s"),
    [
        # Low-pass: 440 Hz tone + 4 kHz whistle.
        ("rc_lowpass", 1e3, 100e-9, TRANSIENT_SETTLE_S + AUDIO_CLIP_S),  # default, policy
        ("rc_lowpass", 390.0, 20.5e-9, 0.5),  # fc ≈ 19.9 kHz corner: τ ≈ 8 µs < max step
        ("rc_lowpass", 100e3, 79.5e-9, 0.5),  # fc ≈ 20 Hz corner: slowest allowed design
        # High-pass: 50 Hz mains hum + 440 Hz tone (the same τ bounds apply).
        ("rc_highpass", 10e3, 100e-9, TRANSIENT_SETTLE_S + AUDIO_CLIP_S),  # default, policy
        ("rc_highpass", 390.0, 20.5e-9, 0.5),  # fc ≈ 19.9 kHz: hum at ≈ −52 dB
        ("rc_highpass", 100e3, 79.5e-9, 0.5),  # fc ≈ 20 Hz corner: slowest allowed design
    ],
)
def test_two_tone_transient(
    settings: Settings, template_id: TemplateId, r: float, c: float, stop_s: float
) -> None:
    data = _run(settings, template_id, r, c, stop_s)
    t = data.time_s
    assert data.v_in is not None and data.v_out is not None and data.i_source is not None
    assert t[0] == 0.0 and np.isclose(t[-1], stop_s) and np.all(np.diff(t) > 0)
    assert np.max(np.diff(t)) <= TRANSIENT_MAX_STEP_S * (1 + 1e-6)
    assert t.size == data.v_in.size == data.v_out.size == data.i_source.size

    tones = [(tone.frequency_hz, tone.amplitude_v) for tone in TEMPLATE_TONES[template_id]]
    expected_in = sum(a * np.sin(2 * np.pi * f * t) for f, a in tones)
    assert np.max(np.abs(data.v_in - expected_in)) < 1e-6

    steady = t >= TRANSIENT_SETTLE_S
    freqs = [f for f, _ in tones]
    out = _fit_tones(t[steady], data.v_out[steady], freqs)
    cur = _fit_tones(t[steady], data.i_source[steady], freqs)
    for (f, a), v_ph, i_ph in zip(tones, out, cur, strict=True):
        h = complex(analytic.transfer(template_id, f, {"R1": r, "C1": c}))
        i_exp = complex(analytic.series_rc_source_current(f, r, c, v_source=a))
        assert abs(abs(v_ph) / a - abs(h)) <= AMP_REL_TOL * abs(h)
        assert (
            abs(analytic.wrap_phase_deg(np.degrees(np.angle(v_ph) - np.angle(h)))) <= PHASE_TOL_DEG
        )
        assert abs(abs(i_ph) - abs(i_exp)) <= AMP_REL_TOL * abs(i_exp)
        assert (
            abs(analytic.wrap_phase_deg(np.degrees(np.angle(i_ph) - np.angle(i_exp))))
            <= PHASE_TOL_DEG
        )


def test_transient_returns_only_requested_probes(settings: Settings) -> None:
    circuit = revision_to_circuit(build_revision("rc_lowpass", {}, origin="user_edit"))
    data = asyncio.run(
        create_simulator(settings).transient(circuit, TransientRequest(0.05, 1e-5, ("v(out)",)))
    )
    assert data.v_in is None and data.i_source is None and data.v_out is not None
