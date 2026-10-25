"""The three declared investigation instruments (public).

Each instrument states exactly what it returns. Settings are fixed public constants from
``contracts.policy`` plus the case's public two-tone signal (the intended revision's own tones,
``policy.TEMPLATE_TONES``); nothing here depends on a case's hidden state.

Measurement floors (public, declared in every instrument description): a first-order RC filter
with an open capacitor can produce an exactly zero output (high-pass) or zero source current
(both families). A zero phasor has no phase, so:

- an output gain at or below ``MEASUREMENT_FLOOR_DB`` (−120 dB) means "no measurable output";
- a current magnitude at or below ``CURRENT_FLOOR_MA`` (1e-6 mA = 1 nA) means "no measurable
  current";
- such a reading is published on the floor so every displayed number is arithmetically
  consistent (finding M2-02/M2-03): gains are clamped at −120 dB, below-floor amplitudes and
  currents are published as exactly 0, and the (undefined) phase as 0°; the phase is never
  evaluated as evidence. Both floors are also published in ``EvidencePolicy``
  (``measurement_floor_db``, ``current_floor_ma``) so clients can render "no measurable output".

Every non-zero response the case designs can produce is far above these floors (gain ≥ −60 dB,
current ≥ 4e-4 mA), so the floors only ever classify true zeros.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import cast

from ..audio.capture import transient_settings
from ..contracts.circuits import TemplateId, Tone
from ..contracts.experiments import AcSweepSettings, TransientSettings
from ..contracts.investigation import InstrumentKind, InstrumentSpec, TransientProbe
from ..contracts.policy import (
    CASE_SWEEP_POINTS_PER_DECADE,
    CASE_SWEEP_START_HZ,
    CASE_SWEEP_STOP_HZ,
    CASE_TRANSIENT_PROBES,
    DEFAULT_TONES,
    TEMPLATE_TONES,
)

MEASUREMENT_FLOOR_DB = -120.0
"""Output gains at or below this are 'no measurable output'; their phase is undefined."""

CURRENT_FLOOR_MA = 1e-6
"""Current magnitudes (mA) at or below this are 'no measurable current'; phase undefined."""


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


def family_tones(template_id: TemplateId) -> tuple[Tone, ...]:
    """The public two-tone test signal of a circuit family."""
    return TEMPLATE_TONES[template_id]


def sweep_settings() -> AcSweepSettings:
    """Echo of the fixed public sweep settings."""
    return AcSweepSettings(
        start_hz=CASE_SWEEP_START_HZ,
        stop_hz=CASE_SWEEP_STOP_HZ,
        points_per_decade=CASE_SWEEP_POINTS_PER_DECADE,
        points=len(CASE_FREQUENCY_GRID_HZ),
        source_ac_magnitude_v=1.0,
    )


def case_transient_settings(tones: Sequence[Tone] = DEFAULT_TONES) -> TransientSettings:
    """Echo of the fixed public two-tone capture settings for the case's tones."""
    return transient_settings(tuple(tones))


_FLOOR_TEXT = (
    f"A gain at or below {MEASUREMENT_FLOOR_DB:g} dB (or a current at or below "
    f"{CURRENT_FLOOR_MA * 1e6:g} nA) means no measurable signal: it is reported as "
    f"{MEASUREMENT_FLOOR_DB:g} dB (an amplitude or current of 0), its phase is undefined, is "
    "reported as 0°, and is never used as evidence."
)


def _tone_text(tones: Sequence[Tone]) -> str:
    return " + ".join(f"{t.label.lower()} ({t.frequency_hz:g} Hz)" for t in tones)


def instrument_specs(tones: Sequence[Tone] = DEFAULT_TONES) -> list[InstrumentSpec]:
    """The declared instruments for a case whose test signal is ``tones``."""
    return [
        InstrumentSpec(
            kind=InstrumentKind.OUTPUT_VOLTAGE_SWEEP,
            title="Output response sweep",
            description=(
                "Drives the input with a 1 V sine at 31 fixed frequencies from 20 Hz to 20 kHz "
                "(10 per decade) and measures the output voltage relative to the input: gain in "
                "dB and phase in degrees. The same sweep of the intended design is returned as "
                f"the reference. {_FLOOR_TEXT}"
            ),
            returns=["output_gain_db", "output_phase_deg"],
            frequency_hz=list(CASE_FREQUENCY_GRID_HZ),
            cost=1,
        ),
        InstrumentSpec(
            kind=InstrumentKind.SOURCE_CURRENT_SWEEP,
            title="Source current sweep",
            description=(
                "Drives the input with a 1 V sine at the same 31 frequencies and measures the "
                "current the source delivers into the circuit: magnitude in mA and phase in "
                "degrees relative to the source voltage. The same sweep of the intended design "
                f"is returned as the reference. {_FLOOR_TEXT}"
            ),
            returns=["source_current_ma", "source_current_phase_deg"],
            frequency_hz=list(CASE_FREQUENCY_GRID_HZ),
            cost=1,
        ),
        InstrumentSpec(
            kind=InstrumentKind.TRANSIENT_CAPTURE,
            title="Two-tone capture",
            description=(
                f"Plays the two-tone test signal ({_tone_text(tones)}) and records one quantity "
                "over time after a fixed settling period: the output voltage, or the current the "
                "source delivers. Output voltage repeats the standardized symptom measurement "
                "exactly (same quantity and settings, so it adds no new information); it returns "
                "each tone's amplitude (V), gain relative to the source (dB) and phase, a short "
                "waveform window, and Expected-vs-Observed audio clips at one shared playback "
                "gain. Source current returns each tone's current (mA) and phase relative to the "
                "source voltage, and a short waveform window (current is shown, never played as "
                f"audio). The intended design's values are returned as the reference. "
                f"{_FLOOR_TEXT}"
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
    ]


INSTRUMENTS: tuple[InstrumentSpec, ...] = tuple(instrument_specs())
"""The instrument kinds and returned quantities (descriptions use the low-pass tones; cases use
``instrument_specs(case tones)``)."""

INSTRUMENTS_BY_KIND: dict[InstrumentKind, InstrumentSpec] = {i.kind: i for i in INSTRUMENTS}
