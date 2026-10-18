"""Deterministic text built only from sanitized, measured numbers (public)."""

from __future__ import annotations

from collections.abc import Sequence

from ..circuits.units import format_frequency
from ..contracts.investigation import (
    ObservedTone,
    SweepObservation,
    TransientObservation,
    VerificationPoint,
)
from .hypotheses import wrap_deg


def _signed(x: float, digits: int = 1) -> str:
    return f"{x:+.{digits}f}"


def sweep_summary(sweep: SweepObservation) -> str:
    n = len(sweep.frequency_hz)
    first, last = sweep.frequency_hz[0], sweep.frequency_hz[-1]
    span = f"{format_frequency(first)} to {format_frequency(last)}"
    phase_diffs = [
        abs(float(wrap_deg(p - e)))
        for p, e in zip(sweep.phase_deg, sweep.expected_phase_deg, strict=True)
    ]
    k_ph = max(range(n), key=lambda i: phase_diffs[i])
    phase_text = (
        f" Largest phase difference: {phase_diffs[k_ph]:.1f}° at "
        f"{format_frequency(sweep.frequency_hz[k_ph])}."
    )
    if sweep.quantity == "output_gain":
        diffs = [m - e for m, e in zip(sweep.magnitude, sweep.expected_magnitude, strict=True)]
        k = max(range(n), key=lambda i: abs(diffs[i]))
        return (
            f"Output gain measured at {n} frequencies from {span}. Largest difference from the "
            f"intended design: {_signed(diffs[k])} dB at {format_frequency(sweep.frequency_hz[k])} "
            f"(measured {sweep.magnitude[k]:.1f} dB, intended {sweep.expected_magnitude[k]:.1f} "
            f"dB).{phase_text}"
        )
    rel = [
        (m - e) / e * 100.0 if e > 0 else 0.0
        for m, e in zip(sweep.magnitude, sweep.expected_magnitude, strict=True)
    ]
    k = max(range(n), key=lambda i: abs(rel[i]))
    return (
        f"Source current measured at {n} frequencies from {span} with a 1 V source. Largest "
        f"difference from the intended design: {_signed(rel[k], 0)}% at "
        f"{format_frequency(sweep.frequency_hz[k])} (measured {sweep.magnitude[k]:.4g} mA, "
        f"intended {sweep.expected_magnitude[k]:.4g} mA).{phase_text}"
    )


def _tone_text(tone: ObservedTone) -> str:
    name = f"{tone.label} ({format_frequency(tone.frequency_hz)})"
    if tone.gain_db is not None and tone.expected_gain_db is not None:
        return (
            f"{name} {tone.gain_db:.1f} dB, {tone.phase_deg:.0f}° (intended "
            f"{tone.expected_gain_db:.1f} dB, {tone.expected_phase_deg:.0f}°)"
        )
    return (
        f"{name} {tone.amplitude:.4g} {tone.amplitude_unit}, {tone.phase_deg:.0f}° (intended "
        f"{tone.expected_amplitude:.4g} {tone.amplitude_unit}, {tone.expected_phase_deg:.0f}°)"
    )


def transient_summary(transient: TransientObservation, *, symptom: bool) -> str:
    what = "output voltage" if transient.probe == "output_voltage" else "source current"
    prefix = "Standardized symptom: two-tone capture" if symptom else "Two-tone capture"
    tones = "; ".join(_tone_text(t) for t in transient.tones)
    return f"{prefix} of the {what}. {tones}."


def failed_summary(retried: bool) -> str:
    retry = ", even after one automatic retry" if retried else ""
    return (
        f"The simulator could not complete this measurement{retry}. No experiment budget was used."
    )


CANCELLED_SUMMARY = "Cancelled before the measurement finished. No experiment budget was used."


def repair_summary(
    component_id: str,
    display_value: str,
    points: Sequence[VerificationPoint],
    recovered: bool,
    gain_tol_db: float,
    phase_tol_deg: float,
) -> str:
    worst = max(points, key=lambda p: abs(p.repaired_gain_db - p.expected_gain_db))
    worst_gain = worst.repaired_gain_db - worst.expected_gain_db
    head = f"Repair applied: {component_id} set to {display_value}; nothing else was changed."
    if recovered:
        return (
            f"{head} The output response matches the intended design within ±{gain_tol_db:g} dB "
            f"and ±{phase_tol_deg:g}° at all {len(points)} frequencies (largest gain difference "
            f"{_signed(worst_gain)} dB at {format_frequency(worst.frequency_hz)})."
        )
    outside = sum(1 for p in points if not p.within_tolerance)
    return (
        f"{head} The output response does not match the intended design: {outside} of "
        f"{len(points)} frequencies are outside ±{gain_tol_db:g} dB / ±{phase_tol_deg:g}° "
        f"(largest gain difference {_signed(worst_gain)} dB at "
        f"{format_frequency(worst.frequency_hz)})."
    )
