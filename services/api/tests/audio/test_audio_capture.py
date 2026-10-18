"""Two-tone capture pipeline against the analytic TEST DOUBLE (no ngspice).

Real-simulator agreement is covered by ``test_audio_ngspice.py``.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest
from audio_helpers import AnalyticRcTestDouble, make_circuit

from resonance.audio.capture import (
    WAVEFORM_WINDOW_S,
    clip_source,
    run_two_tone_capture,
    transient_settings,
    waveform_window,
)
from resonance.audio.resample import uniform_times
from resonance.audio.signals import SignalError, ideal_waveform
from resonance.contracts.policy import (
    AUDIO_CLIP_S,
    AUDIO_FADE_MS,
    AUDIO_SAMPLE_RATE_HZ,
    DEFAULT_TONES,
    TRANSIENT_MAX_STEP_S,
    TRANSIENT_SETTLE_S,
    WAVEFORM_WINDOW_MAX_SAMPLES,
)


def _analytic_gain_db(f: float, r: float, c: float, template: str) -> tuple[float, float]:
    jwrc = 1j * 2 * math.pi * f * r * c
    h = 1 / (1 + jwrc) if template == "rc_lowpass" else jwrc / (1 + jwrc)
    return 20 * math.log10(abs(h)), math.degrees(np.angle(h))


@pytest.mark.parametrize(
    ("template", "r", "c"),
    [("rc_lowpass", 1e3, 100e-9), ("rc_lowpass", 4.7e3, 470e-9), ("rc_highpass", 1e3, 100e-9)],
)
async def test_capture_matches_closed_form_response(
    analytic_double: AnalyticRcTestDouble, template: str, r: float, c: float
) -> None:
    circuit = make_circuit(r, c, template)
    cap = await run_two_tone_capture(analytic_double, circuit, nodes=("in", "out"))
    for k, tone in enumerate(DEFAULT_TONES):
        g, p = _analytic_gain_db(tone.frequency_hz, r, c, template)
        assert cap.tone_gain_db("out", k) == pytest.approx(g, abs=0.01)
        assert cap.tone_phase_deg("out", k) == pytest.approx(p, abs=0.05)
        assert cap.reference.amplitude_v(k) == pytest.approx(tone.amplitude_v, rel=1e-4)
    drift = cap.steady_state_drift("out")
    assert drift.max_tone_change_db < 0.01 and drift.dc_change < 1e-4


async def test_capture_request_uses_only_public_constants(
    analytic_double: AnalyticRcTestDouble,
) -> None:
    """Hidden circuits pass through here: the request and settings must not depend on values."""
    a = await run_two_tone_capture(analytic_double, make_circuit(1e3, 100e-9), nodes=("out",))
    b = await run_two_tone_capture(analytic_double, make_circuit(2e3, 47e-9), nodes=("out",))
    req_a, req_b = analytic_double.transient_requests
    assert req_a == req_b
    assert req_a.stop_s == pytest.approx(TRANSIENT_SETTLE_S + AUDIO_CLIP_S)
    assert req_a.max_step_s == TRANSIENT_MAX_STEP_S
    assert req_a.probes == ("v(in)", "v(out)")
    assert a.settings == b.settings == transient_settings(DEFAULT_TONES)
    assert a.settings.sample_rate_hz == AUDIO_SAMPLE_RATE_HZ
    assert a.settings.fade_ms == AUDIO_FADE_MS
    # Nothing but uniform-grid samples and fits are kept (no raw time vector or point count).
    fields = {f.name for f in dataclasses.fields(a)}
    assert fields == {"settings", "signal", "window", "reference", "nodes"}
    assert set(a.nodes) == {"out"}


async def test_capture_of_input_only_requests_only_the_input(
    analytic_double: AnalyticRcTestDouble,
) -> None:
    cap = await run_two_tone_capture(analytic_double, make_circuit(), nodes=("in",))
    assert analytic_double.transient_requests[0].probes == ("v(in)",)
    assert set(cap.nodes) == {"in"}
    with pytest.raises(SignalError):
        cap.node("out")
    with pytest.raises(SignalError):
        await run_two_tone_capture(analytic_double, make_circuit(), nodes=())


async def test_waveform_window_is_period_aligned_steady_state(
    analytic_double: AnalyticRcTestDouble,
) -> None:
    cap = await run_two_tone_capture(analytic_double, make_circuit(), nodes=("in", "out"))
    start_s, values = waveform_window(cap, "in")
    assert start_s == pytest.approx(0.025)  # first whole 25 ms period after the 15 ms fade
    assert len(values) == round(WAVEFORM_WINDOW_S * AUDIO_SAMPLE_RATE_HZ)
    t = uniform_times(TRANSIENT_SETTLE_S + start_s, len(values), AUDIO_SAMPLE_RATE_HZ)
    # Cubic resampling error at the 10 us policy step is ~1e-4 V (test_audio_signals_resample).
    np.testing.assert_allclose(values, ideal_waveform(DEFAULT_TONES, t), atol=2e-4)
    out_start, out_values = waveform_window(cap, "out")
    assert out_start == start_s and len(out_values) == len(values)


async def test_source_current_capture_in_ma_without_output_voltage(
    analytic_double: AnalyticRcTestDouble,
) -> None:
    r, c = 1e3, 100e-9
    cap = await run_two_tone_capture(analytic_double, make_circuit(r, c), nodes=("i_source",))
    assert analytic_double.transient_requests[0].probes == ("v(in)", "i(V1)")
    assert set(cap.nodes) == {"i_source"}  # v(out) was neither requested nor kept
    current = cap.node("i_source")
    assert current.unit == "mA"
    for k, tone in enumerate(DEFAULT_TONES):
        w = 2 * math.pi * tone.frequency_hz
        y = 1j * w * c / (1 + 1j * w * r * c)  # delivered current per volt (A/V)
        assert current.amplitude(k) == pytest.approx(abs(y) * tone.amplitude_v * 1000, rel=1e-3)
        assert cap.tone_phase_deg("i_source", k) == pytest.approx(
            math.degrees(np.angle(y)), abs=0.05
        )
    with pytest.raises(SignalError):
        cap.tone_gain_db("i_source", 0)  # a current has no voltage gain
    with pytest.raises(SignalError):
        clip_source(cap, "i_source", role="observed", label="x", source_id=None)  # never audio
    start_s, values = waveform_window(cap, "i_source")
    assert start_s == pytest.approx(0.025)
    assert max(abs(v) for v in values) == pytest.approx(current.peak, rel=0.05)


async def test_waveform_window_is_capped(analytic_double: AnalyticRcTestDouble) -> None:
    cap = await run_two_tone_capture(analytic_double, make_circuit(), nodes=("out",))
    longest = WAVEFORM_WINDOW_MAX_SAMPLES / AUDIO_SAMPLE_RATE_HZ
    _, values = waveform_window(cap, "out", duration_s=longest)
    assert len(values) == WAVEFORM_WINDOW_MAX_SAMPLES
    with pytest.raises(SignalError):
        waveform_window(cap, "out", duration_s=2 * longest)
