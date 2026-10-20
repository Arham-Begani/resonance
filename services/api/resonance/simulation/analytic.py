"""Analytical first-order RC equations (owner: circuit & simulation engineer).

These are INDEPENDENT CHECKS and PREDICTIONS (tests, hypothesis predictions, proposal previews,
``Revision.prediction``). They are never substituted for an ngspice result.

Model assumptions: ideal voltage source (zero output impedance), ideal linear R and C, no load
on ``out``, no parasitics or tolerances, temperature-independent values. An open (disconnected)
capacitor is an ideal open circuit: no leakage, no stray capacitance.

Conventions:
- ``f`` may be a float or an array (Hz); results are NumPy arrays (0-d for scalars).
- Source current uses the DELIVERED convention: positive current leaves V1's + terminal into
  the circuit, i.e. ``I = V / Z_in``. For a 1 V source it equals the input admittance.
- Phases are wrapped to (−180°, 180°]. A zero phasor (an open-capacitor high-pass output, any
  open-capacitor current) has NO defined phase; ``phase_deg`` then returns 0° (NumPy's
  ``angle(0)``), so callers must not compare phases where the magnitude is zero.

Functions (stable names; other modules import them):

- ``time_constant_s(r, c)`` → τ = RC;  ``cutoff_hz(r, c)`` → fc = 1/(2πRC) (both families).
- ``lowpass_transfer(f, r, c)``  — R1 in→out, C1 out→gnd:  H(jω) = 1 / (1 + jωRC)
- ``highpass_transfer(f, r, c)`` — C1 in→out, R1 out→gnd:  H(jω) = jωRC / (1 + jωRC)
- ``series_rc_source_current(f, r, c, v_source=1)`` — both families present the same
  series-RC input impedance Z = R + 1/(jωC), so I(jω) = V · jωC / (1 + jωRC).
- ``open_capacitor_transfer(template_id, f)`` — C1 removed: low-pass H = 1 exactly (no current
  flows, so R1 drops no voltage); high-pass H = 0 exactly (nothing drives the output; R1 holds
  it at ground).
- ``open_capacitor_source_current(f, v_source=1)`` — C1 removed: I = 0 for both families
  (the series path through C1 is broken).
- ``transfer(template_id, f, values, disconnected=frozenset())`` and
  ``source_current(template_id, f, values, v_source=1, disconnected=frozenset())`` — dispatch
  by template and connection state (``disconnected`` may only be empty or ``{"C1"}``, mirroring
  ``circuits.netlist.DISCONNECTABLE``; ``values`` may then omit C1).
- ``gain_db(h, floor_db=None)`` — 20·log10|h| without warnings; |h| = 0 gives −inf unless a
  floor is given (pass ``policy.DB_FLOOR`` for anything that reaches JSON).
- ``phase_deg(h)``, ``wrap_phase_deg(deg)``.

Identifiability note: H depends on R and C only through the product RC, so doubling R and
doubling C give identical voltage transfer in BOTH families; the source current separates them
(|I| → V/R at high frequency, → ωCV at low frequency; the ratio I(C×k)/I(R×k) is exactly k at
every frequency). An open capacitor is not voltage-equivalent to any finite R/C drift: the
low-pass output becomes flat at 0 dB and the high-pass output vanishes, with zero source current.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Set

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


_TEMPLATES = ("rc_lowpass", "rc_highpass")


def open_capacitor_transfer(template_id: TemplateId, f: ArrayLike) -> ComplexArray:
    """V(out)/V(in) with C1 removed: 1 for the low-pass, 0 for the high-pass (exactly)."""
    shape = np.shape(np.asarray(f, dtype=np.float64))
    if template_id == "rc_lowpass":
        return np.ones(shape, dtype=np.complex128)
    if template_id == "rc_highpass":
        return np.zeros(shape, dtype=np.complex128)
    raise ValueError("unknown template")


def open_capacitor_source_current(f: ArrayLike, v_source: float = 1.0) -> ComplexArray:
    """Delivered source current with C1 removed: 0 A for both families (series path open)."""
    del v_source  # no current flows for any source amplitude
    return np.zeros(np.shape(np.asarray(f, dtype=np.float64)), dtype=np.complex128)


def _c1_open(template_id: TemplateId, disconnected: Set[str]) -> bool:
    if template_id not in _TEMPLATES:
        raise ValueError("unknown template")
    if not set(disconnected) <= {"C1"}:
        raise ValueError("only C1 can be disconnected")
    return "C1" in disconnected


def transfer(
    template_id: TemplateId,
    f: ArrayLike,
    values: Mapping[str, float],
    disconnected: Set[str] = frozenset(),
) -> ComplexArray:
    """V(out)/V(in) for a template's R1/C1 values and connection state."""
    if _c1_open(template_id, disconnected):
        return open_capacitor_transfer(template_id, f)
    r, c = float(values["R1"]), float(values["C1"])
    if template_id == "rc_lowpass":
        return lowpass_transfer(f, r, c)
    return highpass_transfer(f, r, c)


def source_current(
    template_id: TemplateId,
    f: ArrayLike,
    values: Mapping[str, float],
    v_source: float = 1.0,
    disconnected: Set[str] = frozenset(),
) -> ComplexArray:
    """Delivered source current (A) for a template's R1/C1 values, source amplitude, and
    connection state."""
    if _c1_open(template_id, disconnected):
        return open_capacitor_source_current(f, v_source)
    return series_rc_source_current(f, float(values["R1"]), float(values["C1"]), v_source)


def gain_db(h: ArrayLike, floor_db: float | None = None) -> FloatArray:
    """20·log10|h|. |h| = 0 gives −inf (no warning) unless ``floor_db`` clamps it."""
    with np.errstate(divide="ignore"):
        db = np.asarray(20.0 * np.log10(np.abs(np.asarray(h))), dtype=np.float64)
    if floor_db is not None:
        db = np.asarray(np.maximum(db, float(floor_db)), dtype=np.float64)
    return db


def wrap_phase_deg(deg: ArrayLike) -> FloatArray:
    """Wrap angles to (−180, 180]."""
    d = np.asarray(deg, dtype=np.float64)
    wrapped = -((-d + 180.0) % 360.0 - 180.0)
    return np.asarray(wrapped, dtype=np.float64)


def phase_deg(h: ArrayLike) -> FloatArray:
    return wrap_phase_deg(np.degrees(np.angle(np.asarray(h))))
