"""The hidden-state evaluator: the only code that builds or reads hidden circuits.

Boundary contract (docs/contracts.md "Hidden/public boundary", V-04):
- Hidden state (seed, fault, actual values) lives only in ``EvaluatorStore`` (its own SQLite
  file) and in local variables of this module. The evaluator is never attached to
  ``app.state`` and is not reachable from any agent tool; ``CaseService`` holds it privately.
- Every result leaving this module is a public contract model containing ONLY the declared
  quantities of an instrument (``SweepObservation`` / ``TransientObservation``), each paired
  with the same quantity simulated on the public intended design. No netlists, actual values,
  seeds, fault labels, simulator logs, timings, or point counts leave.
- Simulation settings are fixed public constants (``contracts.policy``) and the public demo
  source; nothing is derived from hidden values. Frequencies are the declared grid.
- Hidden-circuit runs never read or write a cache and are never short-circuited by comparison
  with another circuit. The only reuse is the per-case cache of intended-design REFERENCE
  results, keyed by case ID and instrument view, so the simulation work of an instrument
  depends only on the (public) history of the case, never on hidden values.
- Audio clips go through the shared-gain renderer with random artifact IDs; clip labels are
  fixed strings. Source current is never rendered as audio.
- Every measured value (hidden runs AND the matching intended reference) is quantized at this
  boundary to the declared resolutions in ``evaluator.quantize`` (A-01): full-precision solver
  round-off would otherwise fingerprint voltage-equivalent hidden circuits.
- A zero phasor has no phase: where a magnitude is at or below the declared measurement floor
  (``instruments.MEASUREMENT_FLOOR_DB`` / ``CURRENT_FLOOR_MA``, e.g. an open capacitor) the
  published phase is 0 degrees by convention, for hidden and reference runs alike, instead of
  solver noise. dB values are clamped at ``policy.DB_FLOOR``.
- An open component (``c1_disconnected``) is simulated by removing it from the netlist
  (``CircuitSpec.disconnected``); a repair that replaces it reconnects it.
- Simulator errors propagate as ``SimulationError`` with a fixed learner-safe message; any other
  failure becomes ``EvaluatorFailure`` (generic message). Logs record the case ID, instrument,
  and status only.
- ``reveal`` works only after ``mark_closed``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from ...audio.capture import (
    CaptureNode,
    ClipSource,
    TwoToneCapture,
    render_comparison,
    run_two_tone_capture,
    waveform_window,
)
from ...audio.signals import SignalError
from ...contracts.circuits import SourceSpec, TemplateId
from ...contracts.common import SimulatorInfo
from ...contracts.experiments import AudioComparison, ClipRole
from ...contracts.internal import (
    AcSweep,
    CancelToken,
    CircuitSpec,
    Probe,
    SimulationCancelled,
    SimulationError,
    SimulationFailed,
    SimulationTimeout,
    Simulator,
    SimulatorUnavailable,
)
from ...contracts.investigation import (
    HypothesisId,
    InstrumentKind,
    ObservedTone,
    ObservedWaveform,
    SweepObservation,
    TransientObservation,
    TransientProbe,
)
from ...contracts.policy import (
    CASE_SWEEP_POINTS_PER_DECADE,
    CASE_SWEEP_START_HZ,
    CASE_SWEEP_STOP_HZ,
    DB_FLOOR,
    WAVEFORM_WINDOW_MAX_SAMPLES,
)
from ...store.artifacts import ArtifactStore
from ..instruments import (
    CASE_FREQUENCY_GRID_HZ,
    CURRENT_FLOOR_MA,
    MEASUREMENT_FLOOR_DB,
    TRANSIENT_PROBES,
)
from . import faults
from .quantize import q_db, q_peak, q_phase, q_series, q_sig
from .store import EvaluatorStore, HiddenCase

log = logging.getLogger("resonance.challenges.evaluator")

EXPECTED_LABEL = "Expected (intended design)"
OBSERVED_LABEL = "Observed"
REPAIRED_LABEL = "Repaired"
REFERENCE_CACHE_MAX = 64

SweepQuantity = Literal["output_gain", "source_current"]
_CAPTURE_NODE: dict[TransientProbe, CaptureNode] = {
    "output_voltage": "out",
    "source_current": "i_source",
}


class EvaluatorFailure(Exception):
    """A non-simulator failure inside the evaluator. ``str()`` is generic by construction."""

    def __init__(self) -> None:
        super().__init__("The measurement could not be completed.")


class UnknownCase(Exception):
    def __init__(self) -> None:
        super().__init__("Case was not found.")


@dataclass(frozen=True)
class IntendedDesign:
    """Public: the nominal design of a new case (becomes the frozen intended revision)."""

    template_id: TemplateId
    values: dict[str, float]


@dataclass(frozen=True)
class RepairMeasurement:
    """Public measurements of the repaired circuit vs the intended design."""

    sweep: SweepObservation  # output_gain: magnitude/phase = repaired, expected_* = intended
    transient: TransientObservation  # output_voltage: observed = repaired


@dataclass(frozen=True)
class RevealData:
    hypothesis_id: HypothesisId
    component_id: str | None
    multiplier: float | None
    nominal_value_si: float | None
    actual_value_si: float | None  # None for no fault and for an open (disconnected) part
    disconnected: bool
    seed: str
    generator_version: str


@dataclass(frozen=True)
class _SweepResult:
    magnitude: list[float]
    phase_deg: list[float]


async def _pair[T, U](a: Awaitable[T], b: Awaitable[U]) -> tuple[T, U]:
    """Run two simulations concurrently; if either fails, cancel and await the other."""
    ta = asyncio.ensure_future(a)
    tb = asyncio.ensure_future(b)
    try:
        ra = await ta
        rb = await tb
    except BaseException:
        for task in (ta, tb):
            task.cancel()
        await asyncio.gather(ta, tb, return_exceptions=True)
        raise
    return ra, rb


class _ReferenceCache[V]:
    """LRU of intended-design reference results keyed by (case ID, view). Public data only."""

    def __init__(self, max_entries: int) -> None:
        self._data: OrderedDict[tuple[str, str], V] = OrderedDict()
        self._max = max_entries

    async def with_hidden(
        self,
        key: tuple[str, str],
        reference: Callable[[], Awaitable[V]],
        hidden: Callable[[], Awaitable[V]],
    ) -> tuple[V, V]:
        """Always run the hidden circuit; reuse the reference if this case already has it."""
        cached = self._data.get(key)
        if cached is not None:
            self._data.move_to_end(key)
            return await hidden(), cached
        observed, ref = await _pair(hidden(), reference())
        self._data[key] = ref
        while len(self._data) > self._max:
            self._data.popitem(last=False)
        return observed, ref

    def forget(self, case_id: str) -> None:
        for key in [k for k in self._data if k[0] == case_id]:
            del self._data[key]


def _db(x: float) -> float:
    """Clamp at the declared floor, then quantize to the declared dB resolution."""
    return q_db(max(DB_FLOOR, float(x)))


class Evaluator:
    def __init__(self, store: EvaluatorStore, simulator: Simulator, artifacts: ArtifactStore):
        self._store = store
        self._sim = simulator
        self._artifacts = artifacts
        self._sweep_refs: _ReferenceCache[_SweepResult] = _ReferenceCache(REFERENCE_CACHE_MAX)
        self._capture_refs: _ReferenceCache[TwoToneCapture] = _ReferenceCache(REFERENCE_CACHE_MAX)

    def close(self) -> None:
        self._store.close()

    # ---------------------------------------------------------------- creation / lifecycle

    def create_case(self, case_id: str, template_id: TemplateId = "rc_lowpass") -> IntendedDesign:
        """Create hidden state for a new case from a fresh random seed."""
        return self.create_case_from_seed(case_id, faults.new_seed(), template_id)

    def create_case_from_seed(
        self, case_id: str, seed: str, template_id: TemplateId = "rc_lowpass"
    ) -> IntendedDesign:
        """In-process only (private evaluation harness and reproduction from a published seed).
        Never exposed over HTTP: ``POST /api/cases`` accepts no seed."""
        design = faults.generate(seed, template_id)
        self._store.insert(
            HiddenCase(
                case_id=case_id,
                seed=seed,
                generator_version=faults.GENERATOR_VERSION,
                template_id=template_id,
                nominal=dict(design.nominal),
                actual=design.actual,
                fault=design.fault,
                closed=False,
            )
        )
        log.info("case %s hidden state created", case_id)
        return IntendedDesign(template_id=template_id, values=dict(design.nominal))

    def discard(self, case_id: str) -> None:
        """Remove hidden state for a case that never became public (creation failed)."""
        self._store.delete(case_id)
        self._forget(case_id)

    def mark_closed(self, case_id: str) -> None:
        self._store.mark_closed(case_id)
        self._forget(case_id)
        log.info("case %s closed", case_id)

    def reveal(self, case_id: str) -> RevealData:
        case = self._case(case_id)
        if not case.closed:
            raise PermissionError("The answer is only available after the case is closed.")
        comp = case.fault.component_id
        open_part = case.fault.disconnected
        return RevealData(
            hypothesis_id=case.fault.hypothesis_id,
            component_id=comp,
            multiplier=case.fault.multiplier,
            nominal_value_si=case.nominal[comp] if comp else None,
            actual_value_si=case.actual[comp] if comp and not open_part else None,
            disconnected=open_part,
            seed=case.seed,
            generator_version=case.generator_version,
        )

    def simulator_info(self) -> SimulatorInfo:
        try:
            return self._sim.info()
        except SimulationError:
            return SimulatorInfo(version="unavailable")

    def _case(self, case_id: str) -> HiddenCase:
        case = self._store.get(case_id)
        if case is None:
            raise UnknownCase()
        return case

    def _forget(self, case_id: str) -> None:
        self._sweep_refs.forget(case_id)
        self._capture_refs.forget(case_id)

    # ---------------------------------------------------------------- measurements

    async def observe(
        self,
        case_id: str,
        session_id: str,
        *,
        source: SourceSpec,
        instrument: InstrumentKind,
        probe: TransientProbe | None,
        observation_id: str,
        cancel: CancelToken | None = None,
    ) -> SweepObservation | TransientObservation:
        """Run one declared instrument on the hidden circuit (and the intended reference)."""
        case = self._case(case_id)
        hidden = CircuitSpec(
            template_id=case.template_id,
            values=case.actual,
            source=source,
            disconnected=case.disconnected,
        )
        intended = CircuitSpec(template_id=case.template_id, values=case.nominal, source=source)
        result: SweepObservation | TransientObservation
        try:
            _check_cancel(cancel)
            if instrument == InstrumentKind.OUTPUT_VOLTAGE_SWEEP:
                result = await self._sweep_obs(case_id, hidden, intended, "output_gain", cancel)
            elif instrument == InstrumentKind.SOURCE_CURRENT_SWEEP:
                result = await self._sweep_obs(case_id, hidden, intended, "source_current", cancel)
            else:
                if probe not in TRANSIENT_PROBES:
                    raise EvaluatorFailure()
                result = await self._transient_obs(
                    case_id,
                    hidden,
                    intended,
                    probe,
                    session_id=session_id,
                    observed_role="observed",
                    observed_label=OBSERVED_LABEL,
                    source_id=observation_id,
                    cancel=cancel,
                )
        except BaseException as exc:
            raise _sanitize(exc, case_id, instrument.value) from None
        log.info("case %s %s succeeded", case_id, instrument.value)
        return result

    async def measure_repair(
        self,
        case_id: str,
        session_id: str,
        *,
        source: SourceSpec,
        component_id: Literal["R1", "C1"],
        value_si: float,
        source_id: str,
        cancel: CancelToken | None = None,
    ) -> RepairMeasurement:
        """Apply ONLY the proposed component change to the hidden circuit and measure it
        against the intended design (declared grid sweep + two-tone output capture)."""
        case = self._case(case_id)
        repaired_values = dict(case.actual)
        repaired_values[component_id] = float(value_si)
        # Replacing a part with its nominal value also reconnects it if it was open.
        repaired = CircuitSpec(
            template_id=case.template_id,
            values=repaired_values,
            source=source,
            disconnected=case.disconnected - {component_id},
        )
        intended = CircuitSpec(template_id=case.template_id, values=case.nominal, source=source)
        try:
            _check_cancel(cancel)
            sweep = await self._sweep_obs(case_id, repaired, intended, "output_gain", cancel)
            transient = await self._transient_obs(
                case_id,
                repaired,
                intended,
                "output_voltage",
                session_id=session_id,
                observed_role="repaired",
                observed_label=REPAIRED_LABEL,
                source_id=source_id,
                cancel=cancel,
            )
        except BaseException as exc:
            raise _sanitize(exc, case_id, "repair") from None
        log.info("case %s repair measurement succeeded", case_id)
        return RepairMeasurement(sweep=sweep, transient=transient)

    # ---------------------------------------------------------------- sweeps

    async def _sweep(
        self, circuit: CircuitSpec, quantity: SweepQuantity, cancel: CancelToken | None
    ) -> _SweepResult:
        probes: tuple[Probe, ...] = ("v(in)", "v(out)") if quantity == "output_gain" else ("i(V1)",)
        sweep = AcSweep(
            start_hz=CASE_SWEEP_START_HZ,
            stop_hz=CASE_SWEEP_STOP_HZ,
            points_per_decade=CASE_SWEEP_POINTS_PER_DECADE,
            probes=probes,
        )
        data = await self._sim.ac(circuit, sweep, cancel=cancel)
        freqs = np.asarray(data.frequency_hz, dtype=np.float64)
        grid = np.asarray(CASE_FREQUENCY_GRID_HZ, dtype=np.float64)
        if freqs.shape != grid.shape or not np.allclose(freqs, grid, rtol=1e-6, atol=0.0):
            raise EvaluatorFailure()
        if quantity == "output_gain":
            if data.v_out is None or data.v_in is None:
                raise EvaluatorFailure()
            with np.errstate(divide="ignore", invalid="ignore"):
                h = np.asarray(data.v_out) / np.asarray(data.v_in)
            # A broken simulator (NaN/inf, or a zero input) is a failure, never a reading: check
            # the raw response BEFORE the dB floor and the zero-phasor phase convention, which
            # would otherwise turn NaN into "no measurable output".
            _check_finite_complex(h)
            with np.errstate(divide="ignore"):
                magnitude = [_db(x) for x in 20.0 * np.log10(np.abs(h))]
            measurable = [m > MEASUREMENT_FLOOR_DB for m in magnitude]
        else:
            if data.i_source is None:
                raise EvaluatorFailure()
            h = np.asarray(data.i_source) / circuit.source.ac_magnitude_v
            _check_finite_complex(h)
            magnitude = [q_sig(x) for x in np.abs(h) * 1e3]
            measurable = [m > CURRENT_FLOOR_MA for m in magnitude]
        phase = [
            q_phase(x) if ok else 0.0
            for x, ok in zip(np.degrees(np.angle(h)), measurable, strict=True)
        ]
        _check_finite(magnitude + phase)
        return _SweepResult(magnitude=magnitude, phase_deg=phase)

    async def _sweep_obs(
        self,
        case_id: str,
        circuit: CircuitSpec,
        intended: CircuitSpec,
        quantity: SweepQuantity,
        cancel: CancelToken | None,
    ) -> SweepObservation:
        observed, expected = await self._sweep_refs.with_hidden(
            (case_id, quantity),
            lambda: self._sweep(intended, quantity, cancel),
            lambda: self._sweep(circuit, quantity, cancel),
        )
        return SweepObservation(
            quantity=quantity,
            frequency_hz=list(CASE_FREQUENCY_GRID_HZ),
            magnitude=observed.magnitude,
            magnitude_unit="dB" if quantity == "output_gain" else "mA",
            phase_deg=observed.phase_deg,
            expected_magnitude=expected.magnitude,
            expected_phase_deg=expected.phase_deg,
        )

    # ---------------------------------------------------------------- transients

    async def _capture(
        self, circuit: CircuitSpec, probe: TransientProbe, cancel: CancelToken | None
    ) -> TwoToneCapture:
        """Two-tone capture of V(out) or of the delivered source current (mA); v(in) is always
        captured as the phase reference."""
        return await run_two_tone_capture(
            self._sim, circuit, nodes=(_CAPTURE_NODE[probe],), cancel=cancel
        )

    async def _transient_obs(
        self,
        case_id: str,
        circuit: CircuitSpec,
        intended: CircuitSpec,
        probe: TransientProbe,
        *,
        session_id: str,
        observed_role: ClipRole,
        observed_label: str,
        source_id: str,
        cancel: CancelToken | None,
    ) -> TransientObservation:
        observed, expected = await self._capture_refs.with_hidden(
            (case_id, probe),
            lambda: self._capture(intended, probe, cancel),
            lambda: self._capture(circuit, probe, cancel),
        )
        _check_cancel(cancel)
        voltage = probe == "output_voltage"
        node = _CAPTURE_NODE[probe]
        start_s, input_v = waveform_window(expected, "in")
        _, observed_series = waveform_window(observed, node)
        _, expected_series = waveform_window(expected, node)
        cap = WAVEFORM_WINDOW_MAX_SAMPLES
        audio = None
        if voltage:
            audio = await asyncio.to_thread(
                render_comparison,
                [
                    ClipSource(
                        role="expected",
                        label=EXPECTED_LABEL,
                        samples_v=expected.node("out").samples_v,
                        source_id=source_id,
                    ),
                    ClipSource(
                        role=observed_role,
                        label=observed_label,
                        samples_v=observed.node("out").samples_v,
                        source_id=source_id,
                    ),
                ],
                self._artifacts,
                session_id,
                # Public tones (identical for the intended and hidden circuits) so low-hum
                # disclosures appear on high-pass case audio too.
                tones=intended.source.tones,
            )
        return TransientObservation(
            probe=probe,
            tones=_observed_tones(observed, expected, probe),
            waveform=ObservedWaveform(
                sample_rate_hz=expected.settings.sample_rate_hz,
                start_s=start_s,
                unit="V" if voltage else "mA",
                input_v=q_series(input_v[:cap]),
                observed=q_series(observed_series[:cap]),
                expected=q_series(expected_series[:cap]),
            ),
            audio=_quantized_audio(audio) if audio is not None else None,
        )


def _observed_tones(
    observed: TwoToneCapture, expected: TwoToneCapture, probe: TransientProbe
) -> list[ObservedTone]:
    node = _CAPTURE_NODE[probe]
    voltage = probe == "output_voltage"

    def level(capture: TwoToneCapture, k: int) -> tuple[float, float | None, float]:
        """(amplitude, gain_db or None, phase) with the floor convention for zero phasors."""
        raw_amplitude = capture.node(node).amplitude(k)
        raw_phase = capture.tone_phase_deg(node, k)
        _check_finite([raw_amplitude, raw_phase])  # NaN is a failure, never "no signal"
        amplitude = q_sig(raw_amplitude)
        gain = _db(capture.tone_gain_db(node, k)) if voltage else None
        measurable = (
            gain > MEASUREMENT_FLOOR_DB if gain is not None else amplitude > CURRENT_FLOOR_MA
        )
        phase = q_phase(raw_phase) if measurable else 0.0
        return amplitude, gain, phase

    tones: list[ObservedTone] = []
    for k, tone in enumerate(expected.tones):
        amplitude, gain, phase = level(observed, k)
        expected_amplitude, expected_gain, expected_phase = level(expected, k)
        tones.append(
            ObservedTone(
                frequency_hz=tone.frequency_hz,
                label=tone.label,
                role=tone.role,
                amplitude=amplitude,
                amplitude_unit="V" if voltage else "mA",
                gain_db=gain,
                phase_deg=phase,
                expected_amplitude=expected_amplitude,
                expected_gain_db=expected_gain,
                expected_phase_deg=expected_phase,
            )
        )
    _check_finite([t.amplitude for t in tones] + [t.phase_deg for t in tones])
    return tones


def _quantized_audio(audio: AudioComparison) -> AudioComparison:
    """Clip peaks and the shared gain adjustment at the declared resolutions (A-01)."""
    return audio.model_copy(
        update={
            "clips": [c.model_copy(update={"peak_fs": q_peak(c.peak_fs)}) for c in audio.clips],
            "gain_adjustment_db": q_db(audio.gain_adjustment_db),
        }
    )


def _check_finite(values: Sequence[float]) -> None:
    if not bool(np.all(np.isfinite(np.asarray(values, dtype=np.float64)))):
        raise EvaluatorFailure()


def _check_finite_complex(values: object) -> None:
    if not bool(np.all(np.isfinite(np.asarray(values, dtype=np.complex128)))):
        raise EvaluatorFailure()


def _check_cancel(cancel: CancelToken | None) -> None:
    if cancel is not None and cancel.cancelled:
        raise SimulationCancelled("The simulation was cancelled.")


def _sanitize(exc: BaseException, case_id: str, what: str) -> BaseException:
    """Map any failure inside a hidden run to a learner-safe exception; log status only."""
    if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt | SystemExit):
        log.info("case %s %s interrupted", case_id, what)
        return exc
    if isinstance(exc, SimulationCancelled):
        log.info("case %s %s cancelled", case_id, what)
        return SimulationCancelled("The simulation was cancelled.")
    if isinstance(exc, SimulationError):
        log.warning("case %s %s simulator error (%s)", case_id, what, type(exc).__name__)
        message = "The simulator could not complete this run."
        fresh: SimulationError
        if isinstance(exc, SimulatorUnavailable):
            fresh = SimulatorUnavailable(message)
        elif isinstance(exc, SimulationTimeout):
            fresh = SimulationTimeout(message)
        else:
            fresh = SimulationFailed(message)
            fresh.retryable = exc.retryable
        return fresh
    if isinstance(exc, SignalError):
        log.warning("case %s %s analysis error", case_id, what)
        return SimulationFailed("The simulator could not complete this run.")
    log.error("case %s %s internal error (%s)", case_id, what, type(exc).__name__)
    return EvaluatorFailure()
