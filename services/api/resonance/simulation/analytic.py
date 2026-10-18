"""Analytical first-order RC equations (owner: circuit & simulation engineer).

These are INDEPENDENT CHECKS and PREDICTIONS (tests, hypothesis predictions, proposal previews,
``Revision.prediction``). They are never substituted for an ngspice result.

Model assumptions: ideal voltage source (zero output impedance), ideal linear R and C, no load
on ``out``, no parasitics or tolerances, temperature-independent values.

Conventions:
- ``f`` may be a float or an array (Hz); results are NumPy arrays (0-d for scalars).
- Source current uses the DELIVERED convention: positive current leaves V1's + terminal into
  the circuit, i.e. ``I = V / Z_in``. For a 1 V source it equals the input admittance.
- Low-pass (R1 in→out, C1 out→gnd):  H(jω) = 1 / (1 + jωRC)
- High-pass (C1 in→out, R1 out→gnd): H(jω) = jωRC / (1 + jωRC)   (not offered as a template yet)
- Both families present the same series-RC input impedance Z = R + 1/(jωC), so
  I(jω) = V · jωC / (1 + jωRC).

Identifiability note: H depends on R and C only through the product RC, so doubling R and
doubling C give identical voltage transfer; the source current separates them
(|I| → V/R at high frequency, → ωCV at low frequency).
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np
from numpy.typing import ArrayLike

from ..contracts.circuits import TemplateId
from ..contracts.internal import ComplexArray, FloatArray


def time_constant_s(r_ohm: float, c_farad: float) -> float:
    return float(r_ohm) * float(c_farad)


def cutoff_hz(r_ohm: float, c_farad: float) -> float:
    """−3 dB frequency fc = 1 / (2πRC) of a first-order RC filter."""
    return 1.0 / (2.0 * math.pi * time_constant_s(r_ohm, c_farad))


def _omega(f: ArrayLike) -> FloatArray:
    return 2.0 * np.pi * np.asarray(f, dtype=np.float64)


def lowpass_transfer(f: ArrayLike, r_ohm: float, c_farad: float) -> ComplexArray:
    jwrc = 1j * _omega(f) * r_ohm * c_farad
    return np.asarray(1.0 / (1.0 + jwrc), dtype=np.complex128)


def highpass_transfer(f: ArrayLike, r_ohm: float, c_farad: float) -> ComplexArray:
    jwrc = 1j * _omega(f) * r_ohm * c_farad
    return np.asarray(jwrc / (1.0 + jwrc), dtype=np.complex128)


def series_rc_source_current(
    f: ArrayLike, r_ohm: float, c_farad: float, v_source: float = 1.0
) -> ComplexArray:
    """Current delivered by an ideal source of amplitude ``v_source`` into series R + C."""
    w = _omega(f)
    return np.asarray(
        v_source * (1j * w * c_farad) / (1.0 + 1j * w * r_ohm * c_farad), dtype=np.complex128
    )


def transfer(template_id: TemplateId, f: ArrayLike, values: Mapping[str, float]) -> ComplexArray:
    """V(out)/V(in) for a template's R1/C1 values."""
    r, c = float(values["R1"]), float(values["C1"])
    if template_id == "rc_lowpass":
        return lowpass_transfer(f, r, c)
    if template_id == "rc_highpass":
        return highpass_transfer(f, r, c)
    raise ValueError("unknown template")


def source_current(
    template_id: TemplateId, f: ArrayLike, values: Mapping[str, float], v_source: float = 1.0
) -> ComplexArray:
    """Delivered source current (A) for a template's R1/C1 values and source amplitude."""
    if template_id not in ("rc_lowpass", "rc_highpass"):
        raise ValueError("unknown template")
    return series_rc_source_current(f, float(values["R1"]), float(values["C1"]), v_source)


def gain_db(h: ArrayLike) -> FloatArray:
    return np.asarray(20.0 * np.log10(np.abs(np.asarray(h))), dtype=np.float64)


def wrap_phase_deg(deg: ArrayLike) -> FloatArray:
    """Wrap angles to (−180, 180]."""
    d = np.asarray(deg, dtype=np.float64)
    wrapped = -((-d + 180.0) % 360.0 - 180.0)
    return np.asarray(wrapped, dtype=np.float64)


def phase_deg(h: ArrayLike) -> FloatArray:
    return wrap_phase_deg(np.degrees(np.angle(np.asarray(h))))
