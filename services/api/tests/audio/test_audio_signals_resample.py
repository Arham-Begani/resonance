"""Signal definition and adaptive → uniform resampling accuracy (synthetic, no ngspice)."""

from __future__ import annotations

import math

import numpy as np
import pytest
from audio_helpers import irregular_grid

from resonance.audio.resample import resample_uniform, uniform_times
from resonance.audio.signals import (
    MAX_TONE_HZ,
    MIN_POINTS_PER_PERIOD,
    SignalError,
    common_period_s,
    ideal_waveform,
    multi_tone_signal,
)
from resonance.audio.tones import analysis_window, fit_tones
from resonance.contracts.circuits import Tone
from resonance.contracts.policy import (
    AUDIO_CLIP_S,
    AUDIO_SAMPLE_RATE_HZ,
    DEFAULT_TONES,
    TRANSIENT_MAX_STEP_S,
    TRANSIENT_SETTLE_S,
)

RATE = AUDIO_SAMPLE_RATE_HZ
N_CLIP = round(AUDIO_CLIP_S * RATE)


def test_default_signal_common_period_is_25_ms() -> None:
    signal = multi_tone_signal(DEFAULT_TONES)
    assert signal.common_period_s == pytest.approx(0.025, abs=1e-15)
    t = np.linspace(0.0, 0.05, 997)
    np.testing.assert_allclose(
        ideal_waveform(DEFAULT_TONES, t),
        ideal_waveform(DEFAULT_TONES, t + signal.common_period_s),
        atol=1e-9,
    )


def test_common_period_examples_and_rejections() -> None:
    assert common_period_s([50.0, 60.0]) == pytest.approx(0.1)
    assert common_period_s([1000.0]) == pytest.approx(1e-3)
    tone = Tone(frequency_hz=441.5, amplitude_v=0.1, label="x", role="wanted")
    whistle = Tone(frequency_hz=4000.0, amplitude_v=0.1, label="y", role="interference")
    with pytest.raises(SignalError):  # gcd 0.5 Hz → 2 s period: cannot be analysed
        multi_tone_signal((tone, whistle))
    too_high = Tone(frequency_hz=15_000.0, amplitude_v=0.1, label="z", role="interference")
    with pytest.raises(SignalError):
        multi_tone_signal((too_high,))
    with pytest.raises(SignalError):
        multi_tone_signal((whistle, whistle))


def _sine(t: np.ndarray, f: float, a: float) -> np.ndarray:
    return a * np.sin(2 * np.pi * f * t)


def _max_error(f: float, a: float, method: str) -> float:
    stop = TRANSIENT_SETTLE_S + AUDIO_CLIP_S
    t = irregular_grid(stop, TRANSIENT_MAX_STEP_S)
    out = resample_uniform(
        t,
        _sine(t, f, a),
        start_s=TRANSIENT_SETTLE_S,
        n_samples=N_CLIP,
        sample_rate_hz=RATE,
        method=method,  # type: ignore[arg-type]
    )
    exact = _sine(uniform_times(TRANSIENT_SETTLE_S, N_CLIP, RATE), f, a)
    return float(np.max(np.abs(out - exact)))


def test_resampling_error_at_4khz_linear_vs_cubic() -> None:
    """Quantify interpolation error for the 0.3 V whistle at the policy max step (5 µs)."""
    a, f = 0.3, 4000.0
    w, h = 2 * math.pi * f, TRANSIENT_MAX_STEP_S
    linear_bound = h * h * w * w * a / 8.0  # |e| ≤ h²/8 · max|v''|
    linear = _max_error(f, a, "linear")
    cubic = _max_error(f, a, "cubic")
    print(
        f"\n4 kHz, {a} V, max step {h * 1e6:g} us: linear max error {linear:.3e} V "
        f"(bound {linear_bound:.3e}), cubic max error {cubic:.3e} V"
    )
    assert linear <= linear_bound
    assert linear > 0.25 * linear_bound  # the bound is realistic, not vacuous
    assert cubic < 2e-4  # < 0.07 % of the amplitude (measured ≈ 1.0e-4 V at 10 µs)
    assert cubic < linear / 10


def test_resampling_error_at_supported_bandwidth_limit() -> None:
    """At the bandwidth limit (≥ 20 simulator points per period) the cubic pointwise error
    stays below 0.1 % of the amplitude."""
    assert MAX_TONE_HZ * MIN_POINTS_PER_PERIOD * TRANSIENT_MAX_STEP_S == pytest.approx(1.0)
    cubic = _max_error(MAX_TONE_HZ, 1.0, "cubic")
    print(f"\n{MAX_TONE_HZ:g} Hz, 1 V: cubic max error {cubic:.3e} V")
    assert cubic < 1e-3


def test_cubic_resampling_preserves_tone_amplitudes() -> None:
    stop = TRANSIENT_SETTLE_S + AUDIO_CLIP_S
    t = irregular_grid(stop, TRANSIENT_MAX_STEP_S)
    v = ideal_waveform(DEFAULT_TONES, t)
    window = analysis_window(N_CLIP, sample_rate_hz=RATE, fade_samples=720, common_period_s=0.025)
    for method, limit_db in (("cubic", 1e-3), ("linear", 0.05)):
        samples = resample_uniform(
            t,
            v,
            start_s=TRANSIENT_SETTLE_S,
            n_samples=N_CLIP,
            sample_rate_hz=RATE,
            method=method,  # type: ignore[arg-type]
        )
        fit = fit_tones(
            samples,
            clip_start_s=TRANSIENT_SETTLE_S,
            window=window,
            frequencies_hz=[tn.frequency_hz for tn in DEFAULT_TONES],
        )
        for tone, got in zip(DEFAULT_TONES, fit.tones, strict=True):
            err_db = 20 * math.log10(got.amplitude_v / tone.amplitude_v)
            print(f"\n{method} {tone.frequency_hz:g} Hz amplitude bias {err_db:+.5f} dB")
            assert abs(err_db) < limit_db


def test_resampling_rejects_bad_input() -> None:
    t = np.linspace(0.0, 1.0, 1000)
    v = np.sin(t)
    with pytest.raises(SignalError):  # does not cover the clip
        resample_uniform(t, v, start_s=0.5, n_samples=48_000, sample_rate_hz=RATE)
    t_bad = t.copy()
    t_bad[10] = t_bad[9]
    with pytest.raises(SignalError):
        resample_uniform(t_bad, v, start_s=0.1, n_samples=100, sample_rate_hz=RATE)
    v_nan = v.copy()
    v_nan[5] = np.nan
    with pytest.raises(SignalError):
        resample_uniform(t, v_nan, start_s=0.1, n_samples=100, sample_rate_hz=RATE)
