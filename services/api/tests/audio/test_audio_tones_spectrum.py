"""Tone fitting, analysis window, phase wrapping, and spectrum (synthetic, no ngspice)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from resonance.audio.render import fade_samples
from resonance.audio.resample import uniform_times
from resonance.audio.spectrum import SPECTRUM_FLOOR_DBV, SPECTRUM_MAX_POINTS, compute_spectrum
from resonance.audio.tones import (
    analysis_window,
    fit_tones,
    wrap_phase_array_deg,
    wrap_phase_deg,
)
from resonance.contracts.policy import AUDIO_CLIP_S, AUDIO_FADE_MS, AUDIO_SAMPLE_RATE_HZ

RATE = AUDIO_SAMPLE_RATE_HZ
N = round(AUDIO_CLIP_S * RATE)
START = 0.2
FREQS = (440.0, 4000.0)


def _window():  # type: ignore[no-untyped-def]
    return analysis_window(
        N,
        sample_rate_hz=RATE,
        fade_samples=fade_samples(RATE, AUDIO_FADE_MS),
        common_period_s=0.025,
    )


def test_analysis_window_holds_whole_periods_between_the_fades() -> None:
    w = _window()
    fade = fade_samples(RATE, AUDIO_FADE_MS)
    assert fade == 720
    assert w.start_index == fade
    assert w.stop_index <= N - fade
    assert w.length == w.periods * 1200  # 25 ms common period = 1200 samples
    assert w.periods == 46
    first, second = w.halves()
    assert first.length == second.length == 23 * 1200
    assert second.stop_index <= w.stop_index


def _two_tone(
    t: np.ndarray, amps: tuple[float, float], phases_deg: tuple[float, float]
) -> np.ndarray:
    out = np.zeros_like(t)
    for f, a, p in zip(FREQS, amps, phases_deg, strict=True):
        out += a * np.sin(2 * np.pi * f * t + math.radians(p))
    return out


def test_fit_recovers_amplitude_and_phase_with_dc_and_noise() -> None:
    t = uniform_times(START, N, RATE)
    rng = np.random.default_rng(3)
    amps, phases = (0.55, 0.031), (-12.5, -84.0)
    x = _two_tone(t, amps, phases) + 0.07 + rng.normal(0.0, 1e-3, N)
    fit = fit_tones(x, clip_start_s=START, window=_window(), frequencies_hz=FREQS)
    for got, a, p in zip(fit.tones, amps, phases, strict=True):
        assert 20 * math.log10(got.amplitude_v / a) == pytest.approx(0.0, abs=0.003)
        assert wrap_phase_deg(got.phase_deg - p) == pytest.approx(0.0, abs=0.05)
    assert fit.dc_v == pytest.approx(0.07, abs=1e-4)
    assert fit.residual_rms_v == pytest.approx(1e-3, rel=0.05)


def test_fit_is_exact_without_noise() -> None:
    t = uniform_times(START, N, RATE)
    x = _two_tone(t, (0.6, 0.3), (0.0, 0.0))
    fit = fit_tones(x, clip_start_s=START, window=_window(), frequencies_hz=FREQS)
    assert fit.tones[0].amplitude_v == pytest.approx(0.6, rel=1e-10)
    assert fit.tones[1].amplitude_v == pytest.approx(0.3, rel=1e-10)
    assert abs(fit.tones[1].phase_deg) < 1e-7


def test_phase_wrapping_is_half_open() -> None:
    for raw, wrapped in (
        (180.0, 180.0),
        (-180.0, 180.0),
        (540.0, 180.0),
        (-190.0, 170.0),
        (0.0, 0.0),
    ):
        assert wrap_phase_deg(raw) == pytest.approx(wrapped)
    arr = wrap_phase_array_deg(np.array([180.0, -180.0, 190.0, -359.0]))
    np.testing.assert_allclose(arr, [180.0, 180.0, -170.0, 1.0])


def test_spectrum_places_tones_on_exact_bins() -> None:
    t = uniform_times(START, N, RATE)
    x_in = _two_tone(t, (0.6, 0.3), (0.0, 0.0))
    x_out = _two_tone(t, (0.6 * 0.9, 0.3 * 0.1), (-10.0, -80.0))
    spec = compute_spectrum(x_in, x_out, window=_window())
    assert len(spec.frequency_hz) <= SPECTRUM_MAX_POINTS
    assert all(b > a for a, b in zip(spec.frequency_hz, spec.frequency_hz[1:], strict=False))
    assert spec.resolution_hz == pytest.approx(RATE / (46 * 1200))
    for f, a_in, a_out in ((440.0, 0.6, 0.54), (4000.0, 0.3, 0.03)):
        k = spec.frequency_hz.index(f)
        assert spec.input_dbv[k] == pytest.approx(20 * math.log10(a_in / math.sqrt(2)), abs=1e-3)
        assert spec.output_dbv[k] == pytest.approx(20 * math.log10(a_out / math.sqrt(2)), abs=1e-3)
    # Away from the tones there is no leakage: only the display floor.
    k = min(range(len(spec.frequency_hz)), key=lambda i: abs(spec.frequency_hz[i] - 1000.0))
    assert spec.input_dbv[k] == pytest.approx(SPECTRUM_FLOOR_DBV)
