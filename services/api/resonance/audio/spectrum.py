"""Magnitude spectra of simulated clips (owner: audio & signal engineer).

The DFT is taken over the analysis window, which holds an integer number of common signal
periods. With a rectangular window every demo tone then falls exactly on a DFT bin (no
leakage), so the spectral peaks equal the fitted tone amplitudes. Levels are single-sided RMS
in dBV (dB re 1 V RMS). For plotting, bins between ``SPECTRUM_MIN_HZ`` and ``SPECTRUM_MAX_HZ``
are reduced to at most ``SPECTRUM_MAX_POINTS`` log-spaced bands, each reporting its strongest
bin (peak hold), so tone peaks keep their exact frequency and level.
"""

from __future__ import annotations

import math

import numpy as np

from ..contracts.experiments import Spectrum
from ..contracts.internal import FloatArray
from .signals import SignalError
from .tones import AnalysisWindow

SPECTRUM_MIN_HZ = 20.0
SPECTRUM_MAX_HZ = 12_000.0
SPECTRUM_MAX_POINTS = 480
SPECTRUM_FLOOR_DBV = -120.0


def rms_spectrum_v(segment: FloatArray) -> FloatArray:
    """Single-sided RMS amplitude per bin (DC excluded from the RMS doubling)."""
    n = segment.size
    mag = np.abs(np.fft.rfft(segment)) / n
    rms: FloatArray = mag * math.sqrt(2.0)
    rms[0] = mag[0]
    if n % 2 == 0:
        rms[-1] = mag[-1]
    return rms


def _to_dbv(rms_v: FloatArray) -> FloatArray:
    floor = 10.0 ** (SPECTRUM_FLOOR_DBV / 20.0)
    dbv: FloatArray = 20.0 * np.log10(np.maximum(rms_v, floor))
    return dbv


def compute_spectrum(
    input_v: FloatArray, output_v: FloatArray, *, window: AnalysisWindow
) -> Spectrum:
    """Input and output spectra over ``window`` (both clips share one frequency axis)."""
    a = np.asarray(input_v, dtype=np.float64)
    b = np.asarray(output_v, dtype=np.float64)
    if a.shape != b.shape or window.stop_index > a.size:
        raise SignalError("The audio clips do not match their analysis window.")
    seg_in = a[window.start_index : window.stop_index]
    seg_out = b[window.start_index : window.stop_index]
    rms_in = rms_spectrum_v(seg_in)
    rms_out = rms_spectrum_v(seg_out)
    resolution = window.sample_rate_hz / window.length
    freqs = np.arange(rms_in.size, dtype=np.float64) * resolution

    first = int(np.searchsorted(freqs, SPECTRUM_MIN_HZ, side="left"))
    last = int(np.searchsorted(freqs, SPECTRUM_MAX_HZ, side="right"))
    if last - first < 2:
        raise SignalError("The audio clip is too short for a spectrum.")
    edges = np.geomspace(freqs[first], freqs[last - 1], SPECTRUM_MAX_POINTS + 1)
    bounds = np.searchsorted(freqs, edges, side="left")
    bounds[0], bounds[-1] = first, last
    strongest = np.maximum(rms_in, rms_out)
    picks: list[int] = []
    for lo, hi in zip(bounds[:-1], bounds[1:], strict=True):
        lo_i, hi_i = int(lo), int(hi)
        if hi_i <= lo_i:
            continue
        pick = lo_i + int(np.argmax(strongest[lo_i:hi_i]))
        if not picks or pick > picks[-1]:
            picks.append(pick)
    idx = np.asarray(picks, dtype=np.int64)
    return Spectrum(
        frequency_hz=[round(float(f), 4) for f in freqs[idx]],
        input_dbv=[round(float(x), 3) for x in _to_dbv(rms_in[idx])],
        output_dbv=[round(float(x), 3) for x in _to_dbv(rms_out[idx])],
        resolution_hz=resolution,
        method=(
            f"Rectangular window over {window.periods} whole signal periods "
            f"({window.duration_s:.3f} s), so each tone falls exactly on a DFT bin; "
            "single-sided RMS level in dB re 1 V RMS; strongest bin per log-spaced band "
            f"from {SPECTRUM_MIN_HZ:g} Hz to {SPECTRUM_MAX_HZ / 1000:g} kHz; levels below "
            f"{SPECTRUM_FLOOR_DBV:g} dBV are shown at {SPECTRUM_FLOOR_DBV:g} dBV. Residual "
            "components far below the tones are numerical artifacts of the simulator time "
            "step and resampling, not circuit behavior."
        ),
    )
