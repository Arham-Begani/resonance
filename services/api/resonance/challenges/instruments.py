"""The three declared investigation instruments (public).

Each instrument states exactly what it returns. Settings are fixed public constants from
``contracts.policy``; nothing here depends on a case's hidden state.
"""

from __future__ import annotations

import math
from typing import cast

from ..audio.capture import transient_settings
from ..contracts.experiments import AcSweepSettings, TransientSettings
from ..contracts.investigation import InstrumentKind, InstrumentSpec, TransientProbe
from ..contracts.policy import (
    CASE_SWEEP_POINTS_PER_DECADE,
    CASE_SWEEP_START_HZ,
    CASE_SWEEP_STOP_HZ,
    CASE_TRANSIENT_PROBES,
    DEFAULT_TONES,
)


def _declared_grid() -> tuple[float, ...]:
    decades = math.log10(CASE_SWEEP_STOP_HZ / CASE_SWEEP_START_HZ)
    count = math.floor(decades * CASE_SWEEP_POINTS_PER_DECADE + 1e-9) + 1
    return tuple(
        CASE_SWEEP_START_HZ * 10 ** (k / CASE_SWEEP_POINTS_PER_DECADE) for k in range(count)
    )


CASE_FREQUENCY_GRID_HZ: tuple[float, ...] = _declared_grid()
"""20 Hz–20 kHz, 10 points per decade (31 points). The only frequencies a sweep returns."""

TRANSIENT_PROBES: tuple[TransientProbe, ...] = cast(
    tuple[TransientProbe, ...], tuple(CASE_TRANSIENT_PROBES)
)


def sweep_settings() -> AcSweepSettings:
    """Echo of the fixed public sweep settings."""
    return AcSweepSettings(
        start_hz=CASE_SWEEP_START_HZ,
        stop_hz=CASE_SWEEP_STOP_HZ,
        points_per_decade=CASE_SWEEP_POINTS_PER_DECADE,
        points=len(CASE_FREQUENCY_GRID_HZ),
        source_ac_magnitude_v=1.0,
    )


def case_transient_settings() -> TransientSettings:
    """Echo of the fixed public two-tone capture settings."""
    return transient_settings(DEFAULT_TONES)


_TONE_TEXT = " + ".join(f"{t.label.lower()} ({t.frequency_hz:g} Hz)" for t in DEFAULT_TONES)


INSTRUMENTS: tuple[InstrumentSpec, ...] = (
    InstrumentSpec(
        kind=InstrumentKind.OUTPUT_VOLTAGE_SWEEP,
        title="Output response sweep",
        description=(
            "Drives the input with a 1 V sine at 31 fixed frequencies from 20 Hz to 20 kHz "
            "(10 per decade) and measures the output voltage relative to the input: gain in dB "
            "and phase in degrees. The same sweep of the intended design is returned as the "
            "reference."
        ),
        returns=["output_gain_db", "output_phase_deg"],
        frequency_hz=list(CASE_FREQUENCY_GRID_HZ),
        cost=1,
    ),
    InstrumentSpec(
        kind=InstrumentKind.SOURCE_CURRENT_SWEEP,
        title="Source current sweep",
        description=(
            "Drives the input with a 1 V sine at the same 31 frequencies and measures the current "
            "the source delivers into the circuit: magnitude in mA and phase in degrees relative "
            "to the source voltage. The same sweep of the intended design is returned as the "
            "reference."
        ),
        returns=["source_current_ma", "source_current_phase_deg"],
        frequency_hz=list(CASE_FREQUENCY_GRID_HZ),
        cost=1,
    ),
    InstrumentSpec(
        kind=InstrumentKind.TRANSIENT_CAPTURE,
        title="Two-tone capture",
        description=(
            f"Plays the two-tone test signal ({_TONE_TEXT}) and records one quantity over time "
            "after a fixed settling period: the output voltage, or the current the source "
            "delivers. Output voltage repeats the standardized symptom measurement exactly (same "
            "quantity and settings, so it adds no new information); it returns each tone's "
            "amplitude (V), gain relative to the source (dB) and phase, a short waveform window, "
            "and Expected-vs-Observed audio clips at one shared playback gain. Source current "
            "returns each tone's current (mA) and phase relative to the source voltage, and a "
            "short waveform window (current is shown, never played as audio). The intended "
            "design's values are returned as the reference."
        ),
        returns=[
            "tone_amplitude_v",
            "tone_gain_db",
            "tone_phase_deg",
            "tone_current_ma",
            "tone_current_phase_deg",
        ],
        probes=list(TRANSIENT_PROBES),
        cost=1,
    ),
)

INSTRUMENTS_BY_KIND: dict[InstrumentKind, InstrumentSpec] = {i.kind: i for i in INSTRUMENTS}


def instrument_specs() -> list[InstrumentSpec]:
    return [spec.model_copy(deep=True) for spec in INSTRUMENTS]
