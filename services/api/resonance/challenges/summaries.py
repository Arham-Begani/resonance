"""Deterministic text built only from sanitized, measured numbers (public).

Readings at or below the declared measurement floors (``instruments.MEASUREMENT_FLOOR_DB``,
``instruments.CURRENT_FLOOR_MA``) are described as "no measurable output/current" and their
(undefined) phases are never quoted.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..circuits.units import format_frequency
from ..contracts.investigation import (
    ObservedTone,
    SweepObservation,
    TransientObservation,
    VerificationPoint,
)
from .hypotheses import current_measurable, gain_measurable, wrap_deg
from .instruments import CURRENT_FLOOR_MA, MEASUREMENT_FLOOR_DB


def _signed(x: float, digits: int = 1) -> str:
    return f"{x:+.{digits}f}"


def _phase_text(sweep: SweepObservation, measurable: Sequence[bool]) -> str:
    comparable = [
        i
        for i in range(len(sweep.frequency_hz))
        if measurable[i] and _expected_measurable(sweep, i)
    ]
    if not comparable:
        return " Phase is not defined where nothing measurable was observed."
    diffs = {
        i: abs(float(wrap_deg(sweep.phase_deg[i] - sweep.expected_phase_deg[i])))
        for i in comparable
    }
    k = max(comparable, key=lambda i: diffs[i])
    return (
        f" Largest phase difference: {diffs[k]:.1f}° at {format_frequency(sweep.frequency_hz[k])}."
    )


def _expected_measurable(sweep: SweepObservation, i: int) -> bool:
    e = sweep.expected_magnitude[i]
    return gain_measurable(e) if sweep.quantity == "output_gain" else current_measurable(e)


def sweep_summary(sweep: SweepObservation) -> str:
    n = len(sweep.frequency_hz)
    first, last = sweep.frequency_hz[0], sweep.frequency_hz[-1]
    span = f"{format_frequency(first)} to {format_frequency(last)}"
    if sweep.quantity == "output_gain":
        measurable = [gain_measurable(m) for m in sweep.magnitude]
        if not any(measurable):
            return (
                f"Output gain measured at {n} frequencies from {span}: no measurable output at "
                f"any of them (at or below {MEASUREMENT_FLOOR_DB:g} dB). The intended design "
                f"ranges from {min(sweep.expected_magnitude):.1f} dB to "
                f"{max(sweep.expected_magnitude):.1f} dB. Phase is not defined without an "
                "output."
            )
        floor = MEASUREMENT_FLOOR_DB
        diffs = [
            max(m, floor) - max(e, floor)
            for m, e in zip(sweep.magnitude, sweep.expected_magnitude, strict=True)
        ]
        k = max(range(n), key=lambda i: abs(diffs[i]))
        return (
            f"Output gain measured at {n} frequencies from {span}. Largest difference from the "
            f"intended design: {_signed(diffs[k])} dB at {format_frequency(sweep.frequency_hz[k])} "
            f"(measured {sweep.magnitude[k]:.1f} dB, intended {sweep.expected_magnitude[k]:.1f} "
            f"dB).{_phase_text(sweep, measurable)}"
        )
    measurable = [current_measurable(m) for m in sweep.magnitude]
    if not any(measurable):
        return (
            f"Source current measured at {n} frequencies from {span} with a 1 V source: no "
            f"measurable current at any of them (at or below {CURRENT_FLOOR_MA * 1e6:g} nA). The "
            f"intended design draws {min(sweep.expected_magnitude):.4g} mA to "
            f"{max(sweep.expected_magnitude):.4g} mA. Phase is not defined without a current."
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
        f"intended {sweep.expected_magnitude[k]:.4g} mA).{_phase_text(sweep, measurable)}"
    )


def _tone_text(tone: ObservedTone) -> str:
    name = f"{tone.label} ({format_frequency(tone.frequency_hz)})"
    if tone.gain_db is not None and tone.expected_gain_db is not None:
        intended = f"intended {tone.expected_gain_db:.1f} dB, {tone.expected_phase_deg:.0f}°"
        if not gain_measurable(tone.gain_db):
            return f"{name} no measurable output ({intended})"
        return f"{name} {tone.gain_db:.1f} dB, {tone.phase_deg:.0f}° ({intended})"
    intended = (
        f"intended {tone.expected_amplitude:.4g} {tone.amplitude_unit}, "
        f"{tone.expected_phase_deg:.0f}°"
    )
    if not current_measurable(tone.amplitude):
        return f"{name} no measurable current ({intended})"
    return f"{name} {tone.amplitude:.4g} {tone.amplitude_unit}, {tone.phase_deg:.0f}° ({intended})"


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
    if not any(gain_measurable(p.repaired_gain_db) for p in points):
        return (
            f"{head} The output response does not match the intended design: there is still no "
            f"measurable output at any of the {len(points)} frequencies."
        )
    return (
        f"{head} The output response does not match the intended design: {outside} of "
        f"{len(points)} frequencies are outside ±{gain_tol_db:g} dB / ±{phase_tol_deg:g}° "
        f"(largest gain difference {_signed(worst_gain)} dB at "
        f"{format_frequency(worst.frequency_hz)})."
    )
