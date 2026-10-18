"""Adaptive ngspice timepoints → uniform audio grid (owner: audio & signal engineer).

ngspice returns the solution at its own adaptive timepoints (≤ ``TRANSIENT_MAX_STEP_S`` apart,
with occasional much shorter steps near breakpoints). Audio, tone fits, and spectra need a
uniform grid, so the waveform is interpolated at ``AUDIO_SAMPLE_RATE_HZ``.

Interpolation choice (deliberate, quantified in ``tests/audio/test_audio_signals_resample.py``
for a 0.3 V, 4 kHz tone at the 10 µs policy step):

* **Linear** (``np.interp``) error is bounded by h²/8 · max|v''|: measured 2.4e-3 V (0.8 % of
  the amplitude, bound 2.37e-3 V). It systematically shrinks peaks (−0.03 dB fitted amplitude
  bias) and adds interpolation distortion only ≈ 42 dB below the tone.
* **Cubic Hermite with three-point derivatives** (the derivative of the parabola through each
  point and its two neighbours, valid on non-uniform grids) has O(h³) error: measured 1.0e-4 V
  (0.03 %), 23× lower, with −0.0002 dB amplitude bias. It is not monotonicity-preserving, so it
  could overshoot at a corner, but the analysed window starts after the settling period where
  the signal is a smooth sum of sines.

The cubic method is the default. Linear is kept for the error comparison test.
"""

from __future__ import annotations

from typing import Literal

import numpy as np

from ..contracts.internal import FloatArray
from .signals import SignalError

InterpolationMethod = Literal["cubic", "linear"]
DEFAULT_METHOD: InterpolationMethod = "cubic"
METHOD_DESCRIPTION = "cubic Hermite interpolation with three-point derivatives"


def uniform_times(start_s: float, n_samples: int, sample_rate_hz: int) -> FloatArray:
    """Absolute times of a uniform grid: start_s + k / sample_rate_hz, k = 0 … n_samples − 1."""
    return start_s + np.arange(n_samples, dtype=np.float64) / float(sample_rate_hz)


def _validate(time_s: FloatArray, values: FloatArray, t_first: float, t_last: float) -> None:
    if time_s.ndim != 1 or values.shape != time_s.shape or time_s.size < 4:
        raise SignalError("The transient result has an unexpected shape.")
    if not (np.all(np.isfinite(time_s)) and np.all(np.isfinite(values))):
        raise SignalError("The transient result contains invalid numbers.")
    if not np.all(np.diff(time_s) > 0):
        raise SignalError("The transient result timepoints are not increasing.")
    # Allow a sub-nanosecond tolerance for the end point of the simulated interval.
    if time_s[0] > t_first + 1e-12 or time_s[-1] < t_last - 1e-9:
        raise SignalError("The transient result does not cover the requested clip.")


def _cubic_hermite(time_s: FloatArray, values: FloatArray, query_s: FloatArray) -> FloatArray:
    h = np.diff(time_s)
    slope = np.diff(values) / h
    deriv = np.empty_like(values)
    # Derivative of the parabola through (i-1, i, i+1): second-order on non-uniform grids.
    deriv[1:-1] = (h[1:] * slope[:-1] + h[:-1] * slope[1:]) / (h[:-1] + h[1:])
    deriv[0] = slope[0]
    deriv[-1] = slope[-1]
    idx = np.searchsorted(time_s, query_s, side="right") - 1
    idx = np.clip(idx, 0, time_s.size - 2)
    hi = h[idx]
    s = (query_s - time_s[idx]) / hi
    s = np.clip(s, 0.0, 1.0)
    one_minus = 1.0 - s
    h00 = (1.0 + 2.0 * s) * one_minus * one_minus
    h10 = s * one_minus * one_minus
    h01 = s * s * (3.0 - 2.0 * s)
    h11 = s * s * (s - 1.0)
    result: FloatArray = (
        h00 * values[idx]
        + h10 * hi * deriv[idx]
        + h01 * values[idx + 1]
        + h11 * hi * deriv[idx + 1]
    )
    return result


def resample_uniform(
    time_s: FloatArray,
    values: FloatArray,
    *,
    start_s: float,
    n_samples: int,
    sample_rate_hz: int,
    method: InterpolationMethod = DEFAULT_METHOD,
    max_gap_s: float | None = None,
) -> FloatArray:
    """Interpolate an adaptive-timestep waveform onto ``uniform_times(start_s, n, rate)``.

    Only the simulator points around the requested interval are used. Raises ``SignalError``
    if the data do not cover the interval, are malformed, or (when ``max_gap_s`` is given)
    contain a gap between timepoints larger than that inside the interval — the error bounds
    above assume the simulator's maximum step was respected.
    """
    if n_samples < 1 or sample_rate_hz <= 0:
        raise SignalError("The audio grid is empty.")
    t = np.asarray(time_s, dtype=np.float64)
    v = np.asarray(values, dtype=np.float64)
    query = uniform_times(start_s, n_samples, sample_rate_hz)
    _validate(t, v, float(query[0]), float(query[-1]))
    # Keep two neighbours beyond each end so edge derivatives use interior formulas.
    lo = max(int(np.searchsorted(t, query[0], side="right")) - 3, 0)
    hi = min(int(np.searchsorted(t, query[-1], side="left")) + 3, t.size)
    t_win, v_win = t[lo:hi], v[lo:hi]
    if t_win.size < 2:
        raise SignalError("The transient result does not cover the requested clip.")
    if max_gap_s is not None and float(np.max(np.diff(t_win))) > max_gap_s:
        raise SignalError("The transient result is too coarse to resample accurately.")
    if method == "linear":
        out: FloatArray = np.interp(query, t_win, v_win)
        return out
    if t_win.size < 3:
        out = np.interp(query, t_win, v_win)
        return out
    return _cubic_hermite(t_win, v_win, query)
