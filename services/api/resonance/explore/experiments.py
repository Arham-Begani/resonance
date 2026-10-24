"""Pure builders for explore experiment records and comparisons (owner: audio & signal engineer).

Everything here turns simulator data and captures into contract models deterministically;
orchestration (simulation, artifacts, persistence) lives in ``explore.service``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ..audio.capture import TwoToneCapture
from ..audio.tones import gain_db, wrap_phase_array_deg, wrap_phase_deg
from ..contracts.circuits import ComponentChange, Revision, Tone
from ..contracts.experiments import (
    AudioComparison,
    ConsistencyCheck,
    ExperimentRecord,
    FrequencyResponse,
    ToneDelta,
    ToneLevel,
)
from ..contracts.internal import AcData, ComplexArray
from ..contracts.policy import DB_FLOOR

# Transient-vs-AC cross-check tolerances (CLAUDE.md "Audio correctness" / verification gates).
TONE_AGREEMENT_TOLERANCE_DB = 0.5
TONE_PHASE_TOLERANCE_DEG = 1.0
# The fitted input must reproduce the declared source tones (ideal source, exact resampling).
INPUT_AMPLITUDE_TOLERANCE_DB = 0.05
INPUT_PHASE_TOLERANCE_DEG = 0.5
# Settling check: fits over the first and second half of the analysis window must agree.
STEADY_STATE_TOLERANCE_DB = 0.05
STEADY_STATE_DC_TOLERANCE_V = 1e-3


@dataclass(frozen=True)
class ExactPoint:
    """Exact-frequency AC measurement (1 V source)."""

    frequency_hz: float
    gain_db: float
    phase_deg: float
    source_current_ma: float
    source_current_phase_deg: float


def _transfer(ac: AcData) -> tuple[ComplexArray, ComplexArray]:
    if ac.v_in is None or ac.v_out is None or ac.i_source is None:
        raise ValueError("AC result is missing a probe")
    v_in = ac.v_in
    if np.any(np.abs(v_in) == 0.0):
        raise ValueError("AC source voltage is zero")
    h: ComplexArray = ac.v_out / v_in
    # Delivered current per volt of source: equals |I(V1)| for the 1 V AC source.
    y: ComplexArray = ac.i_source / v_in
    return h, y


def _db(x: np.ndarray) -> np.ndarray:
    """20·log10|x| clamped at ``DB_FLOOR`` (V-18: no non-finite value may reach JSON)."""
    floor = 10.0 ** (DB_FLOOR / 20.0)
    return 20.0 * np.log10(np.maximum(np.abs(x), floor))


def frequency_response(ac: AcData) -> FrequencyResponse:
    h, y = _transfer(ac)
    return FrequencyResponse(
        frequency_hz=[float(f) for f in ac.frequency_hz],
        gain_db=[float(g) for g in _db(h)],
        phase_deg=[float(p) for p in wrap_phase_array_deg(np.degrees(np.angle(h)))],
        source_current_ma=[float(i) for i in np.abs(y) * 1000.0],
        source_current_phase_deg=[float(p) for p in wrap_phase_array_deg(np.degrees(np.angle(y)))],
    )


def exact_points(ac: AcData) -> dict[float, ExactPoint]:
    h, y = _transfer(ac)
    gains = _db(h)
    phases = wrap_phase_array_deg(np.degrees(np.angle(h)))
    i_phases = wrap_phase_array_deg(np.degrees(np.angle(y)))
    return {
        float(f): ExactPoint(
            frequency_hz=float(f),
            gain_db=float(gains[k]),
            phase_deg=float(phases[k]),
            source_current_ma=float(abs(y[k]) * 1000.0),
            source_current_phase_deg=float(i_phases[k]),
        )
        for k, f in enumerate(ac.frequency_hz)
    }


def lookup_point(points: dict[float, ExactPoint], frequency_hz: float) -> ExactPoint:
    """Find an exact-frequency point, tolerating float round-trips through the simulator."""
    if frequency_hz in points:
        return points[frequency_hz]
    for f, point in points.items():
        if math.isclose(f, frequency_hz, rel_tol=1e-9, abs_tol=1e-9):
            return point
    raise KeyError(frequency_hz)


def tone_levels(capture: TwoToneCapture, points: dict[float, ExactPoint]) -> list[ToneLevel]:
    levels: list[ToneLevel] = []
    for k, tone in enumerate(capture.tones):
        transient_gain = capture.tone_gain_db("out", k)
        ac_gain = lookup_point(points, tone.frequency_hz).gain_db
        levels.append(
            ToneLevel(
                frequency_hz=tone.frequency_hz,
                label=tone.label,
                role=tone.role,
                input_amplitude_v=capture.reference.amplitude_v(k),
                output_amplitude_v=capture.node("out").amplitude_v(k),
                transient_gain_db=transient_gain,
                ac_gain_db=ac_gain,
                agreement_db=transient_gain - ac_gain,
                phase_deg=capture.tone_phase_deg("out", k),
            )
        )
    return levels


def format_hz(frequency_hz: float) -> str:
    if frequency_hz >= 1000.0:
        return f"{frequency_hz / 1000.0:g} kHz"
    return f"{frequency_hz:g} Hz"


def _tone_name(tone: Tone | ToneLevel) -> str:
    return f"{tone.label} ({format_hz(tone.frequency_hz)})"


def consistency_checks(
    capture: TwoToneCapture,
    levels: Sequence[ToneLevel],
    points: dict[float, ExactPoint],
    audio: AudioComparison | None,
) -> list[ConsistencyCheck]:
    checks: list[ConsistencyCheck] = []

    worst = max(levels, key=lambda lv: abs(lv.agreement_db))
    checks.append(
        ConsistencyCheck(
            name="tone_gain_agreement",
            passed=all(abs(lv.agreement_db) <= TONE_AGREEMENT_TOLERANCE_DB for lv in levels),
            detail=(
                "Tone gains fitted from the transient waveform match the AC analysis at the same "
                f"frequencies within {abs(worst.agreement_db):.3f} dB (worst: "
                f"{_tone_name(worst)}; tolerance {TONE_AGREEMENT_TOLERANCE_DB:g} dB)."
            ),
        )
    )

    phase_errors = [
        abs(wrap_phase_deg(lv.phase_deg - lookup_point(points, lv.frequency_hz).phase_deg))
        for lv in levels
    ]
    checks.append(
        ConsistencyCheck(
            name="tone_phase_agreement",
            passed=all(e <= TONE_PHASE_TOLERANCE_DEG for e in phase_errors),
            detail=(
                "Tone phases fitted from the transient waveform match the AC analysis within "
                f"{max(phase_errors):.3f}° (tolerance {TONE_PHASE_TOLERANCE_DEG:g}°)."
            ),
        )
    )

    amp_errors: list[float] = []
    phase_in_errors: list[float] = []
    for k, tone in enumerate(capture.tones):
        fit = capture.reference.fit.tones[k]
        amp_errors.append(abs(gain_db(fit.amplitude_v, tone.amplitude_v)))
        phase_in_errors.append(abs(wrap_phase_deg(fit.phase_deg)))
    checks.append(
        ConsistencyCheck(
            name="input_matches_source",
            passed=max(amp_errors) <= INPUT_AMPLITUDE_TOLERANCE_DB
            and max(phase_in_errors) <= INPUT_PHASE_TOLERANCE_DEG,
            detail=(
                "The simulated input reproduces the declared source tones within "
                f"{max(amp_errors):.4f} dB and {max(phase_in_errors):.3f}° (tolerance "
                f"{INPUT_AMPLITUDE_TOLERANCE_DB:g} dB, {INPUT_PHASE_TOLERANCE_DEG:g}°)."
            ),
        )
    )

    drift = capture.steady_state_drift("out")
    checks.append(
        ConsistencyCheck(
            name="steady_state",
            passed=drift.max_tone_change_db <= STEADY_STATE_TOLERANCE_DB
            and drift.dc_change <= STEADY_STATE_DC_TOLERANCE_V,
            detail=(
                f"After discarding {capture.settings.settle_s * 1000:g} ms of settling, output "
                "tone levels in the first and second half of the analysed clip differ by "
                f"{drift.max_tone_change_db:.4f} dB and the DC level by "
                f"{drift.dc_change * 1000:.3f} mV (tolerance {STEADY_STATE_TOLERANCE_DB:g} dB, "
                f"{STEADY_STATE_DC_TOLERANCE_V * 1000:g} mV)."
            ),
        )
    )

    if audio is not None:
        clipped = [c.label for c in audio.clips if c.clipped]
        peak = max(c.peak_fs for c in audio.clips)
        if clipped:
            detail = f"Clipping detected in: {', '.join(clipped)}."
        elif audio.gain_adjustment_db < 0.0:
            detail = (
                f"No clipping after a shared {-audio.gain_adjustment_db:.1f} dB reduction of "
                f"every clip; loudest peak {peak:.2f} of full scale."
            )
        else:
            detail = (
                f"No clipping at the standard shared gain; loudest peak {peak:.2f} of full scale."
            )
        checks.append(ConsistencyCheck(name="no_clipping", passed=not clipped, detail=detail))
    return checks


# --------------------------------------------------------------------------------------
# Comparisons
# --------------------------------------------------------------------------------------


def component_changes(a: Revision, b: Revision) -> list[ComponentChange]:
    """Value differences between two revisions of the SAME circuit family.

    Across families the topology differs (C1 is the shunt element of a low-pass but the series
    element of a high-pass), so per-component diffs would mislead and none are reported, as
    for family-switch revisions; ``comparison_summary`` states both designs instead."""
    if a.template_id != b.template_id:
        return []
    by_id = {c.id: c for c in a.components}
    changes: list[ComponentChange] = []
    for comp in b.components:
        before = by_id.get(comp.id)
        if before is None or before.value_si is None or comp.value_si is None:
            continue
        if before.value_si != comp.value_si:
            changes.append(
                ComponentChange(
                    component_id=comp.id,
                    from_value_si=before.value_si,
                    to_value_si=comp.value_si,
                    from_display=before.display_value or f"{before.value_si:g}",
                    to_display=comp.display_value or f"{comp.value_si:g}",
                )
            )
    return changes


def tone_deltas(a: ExperimentRecord, b: ExperimentRecord) -> list[ToneDelta]:
    """Measured AC gain at each demo tone present in both experiments (b − a)."""
    b_by_freq = {t.frequency_hz: t for t in b.tones}
    deltas: list[ToneDelta] = []
    for ta in a.tones:
        tb = b_by_freq.get(ta.frequency_hz)
        if tb is None:
            continue
        deltas.append(
            ToneDelta(
                frequency_hz=ta.frequency_hz,
                label=ta.label,
                a_gain_db=ta.ac_gain_db,
                b_gain_db=tb.ac_gain_db,
                delta_db=tb.ac_gain_db - ta.ac_gain_db,
            )
        )
    return deltas


_TEMPLATE_NAMES = {"rc_lowpass": "RC low-pass", "rc_highpass": "RC high-pass"}


def _design_text(rev: Revision) -> str:
    values = ", ".join(
        f"{c.id} = {c.display_value}" for c in rev.components if c.editable and c.display_value
    )
    name = _TEMPLATE_NAMES.get(rev.template_id, rev.template_id)
    return f"{name} ({values})" if values else name


def _delta_phrase(delta_db: float) -> str:
    # Two decimals, matching Learn's explanation and grading resolution (finding M2-01).
    if abs(delta_db) < 0.005:
        return "no measurable change"
    if delta_db < 0:
        return f"{-delta_db:.2f} dB more attenuation"
    return f"{delta_db:.2f} dB less attenuation"


def comparison_summary(
    a: ExperimentRecord,
    b: ExperimentRecord,
    rev_a: Revision,
    rev_b: Revision,
    changes: Sequence[ComponentChange],
    deltas: Sequence[ToneDelta],
) -> str:
    """Deterministic text built only from recorded numbers and revision data."""
    parts: list[str] = []
    if rev_a.template_id != rev_b.template_id:
        parts.append(
            f"Circuit changed from {_design_text(rev_a)} to {_design_text(rev_b)}; each uses its "
            "own test signal, so only tones present in both are compared."
        )
    if changes:
        listed = "; ".join(
            f"{c.component_id} from {c.from_display} to {c.to_display}" for c in changes
        )
        parts.append(f"Changed {listed}.")
    elif rev_a.template_id == rev_b.template_id:
        parts.append("Both experiments used the same component values.")
    if deltas:
        parts.append(
            "Measured AC gain: "
            + "; ".join(
                f"{d.label} ({format_hz(d.frequency_hz)}) {d.a_gain_db:.2f} dB → "
                f"{d.b_gain_db:.2f} dB ({_delta_phrase(d.delta_db)})"
                for d in deltas
            )
            + "."
        )
    if a.goal is not None and b.goal is not None and a.goal.goal_id == b.goal.goal_id:
        status = {True: "met", False: "not met"}
        parts.append(f"Goal: {status[a.goal.passed]} → {status[b.goal.passed]}.")
    return " ".join(parts)
