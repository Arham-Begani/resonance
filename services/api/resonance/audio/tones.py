"""Steady-state tone fitting (owner: audio & signal engineer).

Each tone's amplitude and phase come from a linear least-squares fit of

    v(t) ≈ c + Σ_k [a_k sin(2π f_k t) + b_k cos(2π f_k t)]

over an *analysis window* holding an integer number of common signal periods of the un-faded
steady-state clip. ``t`` is absolute simulated time, so phases share the source's reference
(every source tone is a zero-phase sine at t = 0). Amplitude = √(a² + b²) (peak volts),
phase = atan2(b, a) (degrees). Over whole common periods the regressors are orthogonal, so the
fit equals the exact DFT bin of each tone and is unaffected by the other tones or a DC offset.

The analysis window starts after the playback fade-in and ends before the fade-out, so the
fades (playback only) can never influence a measurement.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ..contracts.internal import FloatArray
from ..contracts.policy import DB_FLOOR
from .resample import uniform_times
from .signals import SignalError

MIN_ANALYSIS_PERIODS = 4


@dataclass(frozen=True)
class AnalysisWindow:
    """Sample range [start, start + length) of a clip used for every measurement."""

    start_index: int
    length: int
    periods: int
    sample_rate_hz: int

    @property
    def stop_index(self) -> int:
        return self.start_index + self.length

    @property
    def duration_s(self) -> float:
        return self.length / self.sample_rate_hz

    def halves(self) -> tuple[AnalysisWindow, AnalysisWindow]:
        """Two consecutive windows of ⌊periods/2⌋ whole periods each (steady-state check)."""
        half_periods = self.periods // 2
        half_len = round(self.length * half_periods / self.periods)
        first = AnalysisWindow(self.start_index, half_len, half_periods, self.sample_rate_hz)
        second = AnalysisWindow(
            self.start_index + half_len, half_len, half_periods, self.sample_rate_hz
        )
        return first, second


def analysis_window(
    n_samples: int, *, sample_rate_hz: int, fade_samples: int, common_period_s: float
) -> AnalysisWindow:
    """Largest whole number of common periods that fits between the two fades."""
    fade_n = max(fade_samples, 0)
    usable = n_samples - 2 * fade_n
    period_n = common_period_s * sample_rate_hz
    periods = math.floor(usable / period_n + 1e-9) if usable > 0 else 0
    if periods < MIN_ANALYSIS_PERIODS:
        raise SignalError("The audio clip is too short to analyse.")
    length = round(periods * period_n)
    return AnalysisWindow(
        start_index=fade_n, length=length, periods=periods, sample_rate_hz=sample_rate_hz
    )


@dataclass(frozen=True)
class ToneFit:
    frequency_hz: float
    amplitude_v: float
    phase_deg: float  # sine reference: v ≈ A · sin(2π f t + φ), t = absolute simulated time


@dataclass(frozen=True)
class FitResult:
    tones: tuple[ToneFit, ...]
    dc_v: float
    residual_rms_v: float

    def tone(self, frequency_hz: float) -> ToneFit:
        for fit in self.tones:
            if fit.frequency_hz == frequency_hz:
                return fit
        raise KeyError(frequency_hz)


def wrap_phase_deg(phase_deg: float) -> float:
    """Wrap to the half-open interval (−180, 180]."""
    wrapped = math.fmod(phase_deg, 360.0)
    if wrapped <= -180.0:
        wrapped += 360.0
    elif wrapped > 180.0:
        wrapped -= 360.0
    return wrapped


def wrap_phase_array_deg(phase_deg: FloatArray) -> FloatArray:
    """Vectorised ``wrap_phase_deg``."""
    wrapped: FloatArray = 180.0 - np.mod(180.0 - np.asarray(phase_deg, dtype=np.float64), 360.0)
    return wrapped


def gain_db(output_v: float, input_v: float) -> float:
    """20·log10(output/input), clamped at ``DB_FLOOR`` so no non-finite value reaches JSON."""
    if not input_v > 0.0:
        raise SignalError("The input tone was not found in the transient result.")
    if not output_v > 0.0:
        return DB_FLOOR
    return max(20.0 * math.log10(output_v / input_v), DB_FLOOR)


def fit_tones(
    samples_v: FloatArray,
    *,
    clip_start_s: float,
    window: AnalysisWindow,
    frequencies_hz: Sequence[float],
) -> FitResult:
    """Least-squares fit of DC + one sine/cosine pair per tone over ``window``.

    ``clip_start_s`` is the absolute simulated time of ``samples_v[0]``.
    """
    x = np.asarray(samples_v, dtype=np.float64)
    if window.stop_index > x.size:
        raise SignalError("The audio clip is shorter than its analysis window.")
    segment = x[window.start_index : window.stop_index]
    t = uniform_times(
        clip_start_s + window.start_index / window.sample_rate_hz,
        window.length,
        window.sample_rate_hz,
    )
    columns: list[FloatArray] = []
    for f in frequencies_hz:
        w = 2.0 * np.pi * f
        columns.append(np.sin(w * t))
        columns.append(np.cos(w * t))
    columns.append(np.ones_like(t))
    design = np.stack(columns, axis=1)
    coef, *_ = np.linalg.lstsq(design, segment, rcond=None)
    residual = segment - design @ coef
    fits = []
    for k, f in enumerate(frequencies_hz):
        a, b = float(coef[2 * k]), float(coef[2 * k + 1])
        fits.append(
            ToneFit(
                frequency_hz=float(f),
                amplitude_v=math.hypot(a, b),
                phase_deg=math.degrees(math.atan2(b, a)),
            )
        )
    return FitResult(
        tones=tuple(fits),
        dc_v=float(coef[-1]),
        residual_rms_v=float(np.sqrt(np.mean(residual * residual))),
    )
