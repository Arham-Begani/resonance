"""Two-tone transient capture: simulate → resample → fit (owner: audio & signal engineer).

STABLE INTERFACE, shared with the investigation evaluator for hidden circuits:

* ``run_two_tone_capture(simulator, circuit, nodes=..., cancel=...) -> TwoToneCapture``
  ``nodes`` ⊆ {"in", "out", "i_source"}: the input voltage, the output voltage, or the current
  delivered by V1 (simulator probe ``i(V1)``, delivered convention). v(in) is always captured as
  the reference. Voltages are kept in V; the source current is kept in **mA**
  (``NodeCapture.unit``), so its fitted amplitudes and waveform windows are in mA.
* ``capture.tone_gain_db(node, k)`` (voltages only) and ``capture.tone_phase_deg(node, k)``
  (any captured quantity; relative to the source voltage tone, wrapped to (−180, 180]).
* ``waveform_window(capture, node, duration_s=None) -> (start_s, samples)`` (≤
  ``WAVEFORM_WINDOW_MAX_SAMPLES``). The default length (``default_waveform_window_s``) is
  ``WAVEFORM_WINDOW_S`` or one full period of the lowest tone if that is longer (50 Hz hum →
  20 ms), capped by the sample limit; it depends only on the public tone set.
* ``render_comparison(clips, artifacts, session_id) -> AudioComparison`` with ``ClipSource``
  (voltages only: ``clip_source`` refuses the current, which is never played as audio).

Hidden-state rules: transient settings come ONLY from public policy constants (settle time,
clip length, maximum step, sample rate, fade) plus the circuit's source tones, which are the
public demo signal for every circuit. Nothing here reads ``circuit.values``, and nothing is
cached (a cache keyed by circuit content would be a timing oracle for hidden circuits). A
capture keeps only uniform-grid samples and fits for the requested quantities plus v(in); the
raw simulator time vector, its point count, and run durations are discarded.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np

from ..contracts.circuits import Tone
from ..contracts.experiments import ClipRole, TransientSettings
from ..contracts.internal import (
    CancelToken,
    CircuitSpec,
    FloatArray,
    Probe,
    Simulator,
    TransientData,
    TransientRequest,
)
from ..contracts.policy import (
    AUDIO_CLIP_S,
    AUDIO_FADE_MS,
    AUDIO_SAMPLE_RATE_HZ,
    TRANSIENT_MAX_STEP_S,
    TRANSIENT_SETTLE_S,
    WAVEFORM_WINDOW_MAX_SAMPLES,
)
from .render import ClipSource, fade_samples, render_comparison
from .resample import resample_uniform
from .signals import MultiToneSignal, SignalError, multi_tone_signal
from .tones import AnalysisWindow, FitResult, analysis_window, fit_tones, gain_db, wrap_phase_deg

__all__ = [
    "CaptureNode",
    "ClipSource",
    "NodeCapture",
    "TwoToneCapture",
    "WAVEFORM_WINDOW_S",
    "analyse_transient",
    "clip_source",
    "default_waveform_window_s",
    "render_comparison",
    "run_two_tone_capture",
    "transient_settings",
    "waveform_window",
]

CaptureNode = Literal["in", "out", "i_source"]
"""Captured quantity: V(in), V(out), or the current delivered by V1 (in mA)."""
CaptureUnit = Literal["V", "mA"]

_PROBE: dict[CaptureNode, Probe] = {"in": "v(in)", "out": "v(out)", "i_source": "i(V1)"}
_UNIT: dict[CaptureNode, CaptureUnit] = {"in": "V", "out": "V", "i_source": "mA"}
_SCALE: dict[CaptureNode, float] = {"in": 1.0, "out": 1.0, "i_source": 1000.0}  # A → mA

WAVEFORM_WINDOW_S = 0.010
# ngspice honours its maximum step to rounding; anything coarser invalidates the error bounds.
MAX_GAP_FACTOR = 1.5


def transient_settings(tones: list[Tone] | tuple[Tone, ...]) -> TransientSettings:
    """The fixed public settings of every two-tone capture."""
    return TransientSettings(
        tones=list(tones),
        settle_s=TRANSIENT_SETTLE_S,
        clip_s=AUDIO_CLIP_S,
        max_step_s=TRANSIENT_MAX_STEP_S,
        sample_rate_hz=AUDIO_SAMPLE_RATE_HZ,
        fade_ms=AUDIO_FADE_MS,
    )


@dataclass(frozen=True)
class NodeCapture:
    node: CaptureNode
    unit: CaptureUnit  # "V" for voltages, "mA" for the source current
    samples: FloatArray  # un-faded clip on the uniform grid (in ``unit``), from settle_s
    fit: FitResult  # over the whole analysis window (amplitudes in ``unit``)
    first_half: FitResult  # steady-state check
    second_half: FitResult
    peak: float  # max |sample| in ``unit``

    @property
    def samples_v(self) -> FloatArray:
        """Voltage samples (refuses the current, which is never treated as a voltage)."""
        if self.unit != "V":
            raise SignalError("The source current is not a voltage.")
        return self.samples

    def amplitude(self, index: int) -> float:
        """Fitted peak amplitude of tone ``index`` in ``unit``."""
        return self.fit.tones[index].amplitude_v

    def amplitude_v(self, index: int) -> float:
        if self.unit != "V":
            raise SignalError("The source current is not a voltage.")
        return self.amplitude(index)


@dataclass(frozen=True)
class SteadyStateDrift:
    """Change between the first and second half of the analysis window."""

    max_tone_change_db: float
    dc_change: float  # in the node's unit


@dataclass(frozen=True)
class TwoToneCapture:
    settings: TransientSettings
    signal: MultiToneSignal
    window: AnalysisWindow
    reference: NodeCapture  # v(in), always captured
    nodes: Mapping[CaptureNode, NodeCapture]  # exactly the requested quantities

    @property
    def tones(self) -> tuple[Tone, ...]:
        return self.signal.tones

    def node(self, node: CaptureNode) -> NodeCapture:
        if node == "in":
            return self.reference
        try:
            return self.nodes[node]
        except KeyError:
            raise SignalError("That quantity was not captured.") from None

    def tone_gain_db(self, node: CaptureNode, index: int) -> float:
        """20·log10(node amplitude / input amplitude) for tone ``index`` (voltages only)."""
        return gain_db(self.node(node).amplitude_v(index), self.reference.amplitude_v(index))

    def tone_phase_deg(self, node: CaptureNode, index: int) -> float:
        """Phase of ``node`` minus the source voltage phase for tone ``index``, (−180, 180]."""
        out = self.node(node).fit.tones[index].phase_deg
        ref = self.reference.fit.tones[index].phase_deg
        return wrap_phase_deg(out - ref)

    def steady_state_drift(self, node: CaptureNode) -> SteadyStateDrift:
        cap = self.node(node)
        worst = 0.0
        for a, b in zip(cap.first_half.tones, cap.second_half.tones, strict=True):
            if a.amplitude_v > 0.0 and b.amplitude_v > 0.0:
                worst = max(worst, abs(20.0 * math.log10(b.amplitude_v / a.amplitude_v)))
        return SteadyStateDrift(
            max_tone_change_db=worst,
            dc_change=abs(cap.second_half.dc_v - cap.first_half.dc_v),
        )


def _probes_for(nodes: tuple[CaptureNode, ...]) -> tuple[Probe, ...]:
    if not nodes:
        raise SignalError("Choose at least one quantity to capture.")
    if len(set(nodes)) != len(nodes) or any(n not in _PROBE for n in nodes):
        raise SignalError(
            "Only the input voltage, output voltage, or source current can be captured."
        )
    probes: list[Probe] = ["v(in)"]
    for node in ("out", "i_source"):
        if node in nodes:
            probes.append(_PROBE[node])
    return tuple(probes)


def _raw_values(data: TransientData, node: CaptureNode) -> FloatArray | None:
    if node == "in":
        return data.v_in
    if node == "out":
        return data.v_out
    return data.i_source


def _analyse_node(
    node: CaptureNode,
    data: TransientData,
    settings: TransientSettings,
    signal: MultiToneSignal,
    window: AnalysisWindow,
    n_samples: int,
) -> NodeCapture:
    values = _raw_values(data, node)
    if values is None:
        raise SignalError("The transient result is missing a requested quantity.")
    samples = _SCALE[node] * resample_uniform(
        data.time_s,
        values,
        start_s=settings.settle_s,
        n_samples=n_samples,
        sample_rate_hz=settings.sample_rate_hz,
        max_gap_s=MAX_GAP_FACTOR * settings.max_step_s,
    )
    freqs = signal.frequencies_hz
    first, second = window.halves()

    def fit(w: AnalysisWindow) -> FitResult:
        return fit_tones(samples, clip_start_s=settings.settle_s, window=w, frequencies_hz=freqs)

    return NodeCapture(
        node=node,
        unit=_UNIT[node],
        samples=samples,
        fit=fit(window),
        first_half=fit(first),
        second_half=fit(second),
        peak=float(np.max(np.abs(samples))),
    )


def analyse_transient(
    data: TransientData,
    *,
    nodes: tuple[CaptureNode, ...],
    settings: TransientSettings,
    signal: MultiToneSignal,
) -> TwoToneCapture:
    """Pure analysis step of ``run_two_tone_capture`` (exposed for tests)."""
    n_samples = round(settings.clip_s * settings.sample_rate_hz)
    window = analysis_window(
        n_samples,
        sample_rate_hz=settings.sample_rate_hz,
        fade_samples=fade_samples(settings.sample_rate_hz, settings.fade_ms),
        common_period_s=signal.common_period_s,
    )
    reference = _analyse_node("in", data, settings, signal, window, n_samples)
    captured: dict[CaptureNode, NodeCapture] = {}
    for node in nodes:
        if node == "in":
            captured["in"] = reference
        else:
            captured[node] = _analyse_node(node, data, settings, signal, window, n_samples)
    return TwoToneCapture(
        settings=settings, signal=signal, window=window, reference=reference, nodes=captured
    )


async def run_two_tone_capture(
    simulator: Simulator,
    circuit: CircuitSpec,
    *,
    nodes: tuple[CaptureNode, ...],
    cancel: CancelToken | None = None,
) -> TwoToneCapture:
    """Run one two-tone transient and analyse the requested quantities (+ v(in)).

    Simulator errors (``SimulationError`` subclasses) propagate unchanged; malformed results
    raise ``SignalError``. Both have learner-safe messages.
    """
    probes = _probes_for(nodes)
    signal = multi_tone_signal(circuit.source.tones)
    settings = transient_settings(signal.tones)
    request = TransientRequest(
        stop_s=settings.settle_s + settings.clip_s,
        max_step_s=settings.max_step_s,
        probes=probes,
    )
    data = await simulator.transient(circuit, request, cancel=cancel)
    return await asyncio.to_thread(
        analyse_transient, data, nodes=nodes, settings=settings, signal=signal
    )


def default_waveform_window_s(signal: MultiToneSignal, sample_rate_hz: int) -> float:
    """Oscilloscope window length: ``WAVEFORM_WINDOW_S``, or one whole period of the lowest
    tone when that is longer (so a 50 Hz hum shows a full cycle), at most
    ``WAVEFORM_WINDOW_MAX_SAMPLES`` samples. Depends only on the public tone set."""
    longest = WAVEFORM_WINDOW_MAX_SAMPLES / sample_rate_hz
    return min(max(WAVEFORM_WINDOW_S, 1.0 / signal.lowest_hz), longest)


def waveform_window(
    capture: TwoToneCapture, node: CaptureNode, *, duration_s: float | None = None
) -> tuple[float, list[float]]:
    """A short steady-state window for the oscilloscope view (values in the node's unit).

    It starts at the first whole common period after the fade-in, so windows from different
    captures (input, output, expected, observed) are time-aligned. ``start_s`` is measured
    from the start of the clip. At most ``WAVEFORM_WINDOW_MAX_SAMPLES`` samples; the default
    length is ``default_waveform_window_s``.
    """
    settings = capture.settings
    rate = settings.sample_rate_hz
    if duration_s is None:
        duration_s = default_waveform_window_s(capture.signal, rate)
    period = capture.signal.common_period_s
    fade_end_abs = settings.settle_s + capture.window.start_index / rate
    start_abs = math.ceil(fade_end_abs / period - 1e-9) * period
    start_idx = round((start_abs - settings.settle_s) * rate)
    length = round(duration_s * rate)
    samples = capture.node(node).samples
    if length < 1 or length > WAVEFORM_WINDOW_MAX_SAMPLES:
        raise SignalError("The requested waveform window is too long.")
    if start_idx < 0 or start_idx + length > samples.size:
        raise SignalError("The waveform window does not fit inside the clip.")
    window = samples[start_idx : start_idx + length]
    return start_idx / rate, [round(float(x), 7) for x in window]


def clip_source(
    capture: TwoToneCapture,
    node: CaptureNode,
    *,
    role: ClipRole,
    label: str,
    source_id: str | None,
) -> ClipSource:
    """A ``ClipSource`` for one captured VOLTAGE (the current is never played as audio)."""
    return ClipSource(
        role=role,
        label=label,
        samples_v=capture.node(node).samples_v,
        source_id=source_id,
    )
