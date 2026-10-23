"""Deterministic multi-tone test signal (owner: audio & signal engineer).

The demo input is a sum of sines that all start at t = 0 with zero phase (see
``contracts.circuits.SourceSpec``)::

    v_in(t) = Σ_k A_k · sin(2π f_k t)

Because every tone frequency is an integer multiple of their greatest common divisor, the sum
repeats exactly every *common period* ``1 / gcd(f_k)``: 440 Hz + 4 kHz (low-pass demo) → 40 Hz →
25 ms; 50 Hz + 440 Hz (high-pass demo) → 10 Hz → 100 ms. Analysing an integer number of common
periods makes the tones exactly orthogonal, so tone fits and rectangular-window spectra have no
leakage. Nothing in the audio pipeline assumes a particular tone set: every capture uses the
tones of the circuit's own source (``Revision.source.tones``, one set per template in
``policy.TEMPLATE_TONES``).

Supported signal bandwidth
--------------------------
Tones must lie in [``MIN_TONE_HZ``, ``MAX_TONE_HZ``]. The upper limit comes from the transient
pipeline, not from the 48 kHz audio grid (Nyquist 24 kHz): every tone must get at least
``MIN_POINTS_PER_PERIOD`` = 20 simulator points per period at ``TRANSIENT_MAX_STEP_S``, i.e.
ω·h ≤ 0.31 (``MAX_TONE_HZ`` = 5 kHz at the 10 µs policy step). Within that limit:

* the cubic Hermite resampler's pointwise error stays below 0.1 % of the tone amplitude
  (quantified in ``tests/audio/test_audio_signals_resample.py``);
* trapezoidal integration warps frequency by ≈ (ωh)²/12 (≤ 0.8 %), i.e. < 0.1 dB of gain error
  on a first-order slope, well inside the 0.5 dB transient-vs-AC tolerance (the real-ngspice
  tests measure ≈ 0.04 dB at 4 kHz);
* an RC network is linear, so the output contains no frequencies the input does not: no
  anti-aliasing filter is needed before the 48 kHz grid.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ..contracts.circuits import Tone
from ..contracts.internal import FloatArray
from ..contracts.policy import TRANSIENT_MAX_STEP_S

MIN_TONE_HZ = 20.0
MIN_POINTS_PER_PERIOD = 20
MAX_TONE_HZ = 1.0 / (MIN_POINTS_PER_PERIOD * TRANSIENT_MAX_STEP_S)
# Frequencies are resolved on a 1 mHz grid to compute an exact common period.
_FREQ_RESOLUTION_HZ = 1e-3
# The analysis window must hold several whole common periods (gcd ≥ 10 Hz): the 1.2 s clip
# minus its two 15 ms fades then holds ≥ 11 whole periods (the high-pass 50 Hz + 440 Hz signal
# sits exactly at this limit).
MAX_COMMON_PERIOD_S = 0.1


class SignalError(ValueError):
    """A signal or simulator result cannot be analysed. ``str()`` is learner-safe."""


@dataclass(frozen=True)
class MultiToneSignal:
    tones: tuple[Tone, ...]
    common_period_s: float

    @property
    def frequencies_hz(self) -> tuple[float, ...]:
        return tuple(t.frequency_hz for t in self.tones)

    @property
    def amplitudes_v(self) -> tuple[float, ...]:
        return tuple(t.amplitude_v for t in self.tones)

    @property
    def lowest_hz(self) -> float:
        return min(self.frequencies_hz)

    @property
    def peak_v(self) -> float:
        """Upper bound of |v_in(t)| (the tones may never align exactly)."""
        return float(sum(self.amplitudes_v))


def common_period_s(frequencies_hz: Sequence[float]) -> float:
    """Exact repetition period of a sum of tones: 1 / gcd(frequencies)."""
    if not frequencies_hz:
        raise SignalError("At least one tone is required.")
    units: list[int] = []
    for f in frequencies_hz:
        scaled = f / _FREQ_RESOLUTION_HZ
        rounded = round(scaled)
        if rounded <= 0 or abs(scaled - rounded) > 1e-6 * max(1.0, abs(scaled)):
            raise SignalError("Tone frequencies must be positive multiples of 1 mHz.")
        units.append(int(rounded))
    g = 0
    for u in units:
        g = math.gcd(g, u)
    return 1.0 / (g * _FREQ_RESOLUTION_HZ)


def multi_tone_signal(tones: Sequence[Tone]) -> MultiToneSignal:
    """Validate a tone set against the supported bandwidth and compute its common period.

    There is deliberately no default tone set: callers pass the circuit's own source tones."""
    if not 1 <= len(tones) <= 4:
        raise SignalError("A test signal has between one and four tones.")
    freqs = [t.frequency_hz for t in tones]
    if len(set(freqs)) != len(freqs):
        raise SignalError("Tone frequencies must be distinct.")
    for f in freqs:
        if not MIN_TONE_HZ <= f <= MAX_TONE_HZ:
            raise SignalError(
                f"Tone frequencies must be between {MIN_TONE_HZ:g} Hz and {MAX_TONE_HZ:.0f} Hz."
            )
    period = common_period_s(freqs)
    if period > MAX_COMMON_PERIOD_S:
        raise SignalError("These tone frequencies do not repeat often enough to analyse.")
    return MultiToneSignal(tones=tuple(tones), common_period_s=period)


def ideal_waveform(tones: Sequence[Tone], time_s: FloatArray) -> FloatArray:
    """The ideal source voltage Σ A_k sin(2π f_k t) at absolute simulated times ``time_s``."""
    t = np.asarray(time_s, dtype=np.float64)
    out = np.zeros_like(t)
    for tone in tones:
        out += tone.amplitude_v * np.sin(2.0 * np.pi * tone.frequency_hz * t)
    return out
