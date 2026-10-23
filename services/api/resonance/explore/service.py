"""Explore experiment orchestration (owner: audio & signal engineer).

``run_explore_experiment`` produces one complete ``ExperimentRecord`` for a PUBLIC session
revision: an AC sweep (gain, phase, source current), an exact-frequency AC analysis at the demo
tones and goal frequencies, a two-tone transient capture at ``in`` and ``out``, tone levels
cross-checked against AC, consistency checks, a waveform window, a spectrum, an Input/Output
audio comparison with the shared gain, and an optional goal evaluation on the measured gain.

Failures raise the mapped ``ApiException``; nothing is written unless the whole bundle
succeeded, so earlier valid records are never overwritten or deleted.
``measure_explore_experiment`` is the same bundle without persisting the record, for callers
(Learn) that must store several documents only once every measurement has succeeded.

Every frequency-dependent choice follows the revision's own test signal
(``revision.source.tones``): exact AC points, tone fits, the common analysis period, the
spectrum bins, the oscilloscope window, and the audio disclosure note.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from datetime import datetime
from typing import Any

from ..api.errors import ApiException, not_found, simulation_error_to_api
from ..audio.capture import (
    ClipSource,
    TwoToneCapture,
    run_two_tone_capture,
    waveform_window,
)
from ..audio.render import (
    low_tone_note,
    read_wav_fs,
    render_comparison,
    render_faded_comparison,
)
from ..audio.signals import SignalError
from ..audio.spectrum import compute_spectrum
from ..contracts.circuits import Revision, Tone
from ..contracts.common import ErrorCode, SimulatorInfo, new_id
from ..contracts.experiments import (
    AcSweepSettings,
    AudioClip,
    AudioComparison,
    ExperimentComparison,
    ExperimentRecord,
    ExperimentSettings,
    Goal,
    WaveformWindow,
)
from ..contracts.internal import (
    AcData,
    AcPoints,
    AcSweep,
    CancelToken,
    CircuitSpec,
    SimulationCancelled,
    SimulationError,
    Simulator,
)
from ..contracts.policy import (
    EXPLORE_SWEEP_POINTS_PER_DECADE,
    EXPLORE_SWEEP_START_HZ,
    EXPLORE_SWEEP_STOP_HZ,
)
from ..store.artifacts import ArtifactStore
from ..store.db import DocumentStore, utcnow
from . import experiments as build
from .goals import evaluate_goal, goal_frequencies

log = logging.getLogger("resonance.explore")

MAX_EXACT_FREQUENCIES = 16  # AcPoints limit


def explore_sweep() -> AcSweep:
    return AcSweep(
        start_hz=EXPLORE_SWEEP_START_HZ,
        stop_hz=EXPLORE_SWEEP_STOP_HZ,
        points_per_decade=EXPLORE_SWEEP_POINTS_PER_DECADE,
    )


def exact_frequencies(circuit: CircuitSpec, goal: Goal | None) -> tuple[float, ...]:
    """Demo tone frequencies followed by any additional goal frequencies (deduplicated)."""
    freqs: list[float] = []
    for f in [t.frequency_hz for t in circuit.source.tones] + list(
        goal_frequencies(goal) if goal else ()
    ):
        if f not in freqs:
            freqs.append(f)
    if len(freqs) > MAX_EXACT_FREQUENCIES:
        raise ApiException(
            ErrorCode.VALIDATION_ERROR, "The goal has too many frequencies to measure."
        )
    return tuple(freqs)


def _analysis_failed() -> ApiException:
    return ApiException(
        ErrorCode.SIMULATION_FAILED,
        "The simulation finished but its result could not be analysed.",
        retryable=True,
    )


async def _run_concurrently(jobs: list[Awaitable[Any]], cancel: CancelToken) -> list[Any]:
    """Run simulator jobs together; on the first failure cancel the rest and re-raise it."""
    tasks = [asyncio.ensure_future(j) for j in jobs]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except asyncio.CancelledError:
        cancel.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    failures: list[BaseException] = []
    for task in done:
        exc = task.exception()
        if exc is not None:
            failures.append(exc)
    if failures:
        cancel.cancel()
        settled = await asyncio.gather(*pending, return_exceptions=True)
        failures += [r for r in settled if isinstance(r, BaseException)]
        # Prefer a real failure over the cancellations it triggered.
        real = [e for e in failures if not isinstance(e, SimulationCancelled)]
        raise (real or failures)[0]
    return [t.result() for t in tasks]


def _assemble(
    *,
    experiment_id: str,
    revision: Revision,
    session_id: str,
    artifacts: ArtifactStore,
    sweep: AcSweep,
    sweep_data: AcData,
    points_data: AcData,
    capture: TwoToneCapture,
    goal: Goal | None,
    started: float,
    created_at: datetime,
    simulator_info: SimulatorInfo,
) -> ExperimentRecord:
    response = build.frequency_response(sweep_data)
    points = build.exact_points(points_data)
    levels = build.tone_levels(capture, points)
    start_s, input_v = waveform_window(capture, "in")
    _, output_v = waveform_window(capture, "out")
    waveform = WaveformWindow(
        sample_rate_hz=capture.settings.sample_rate_hz,
        start_s=start_s,
        input_v=input_v,
        output_v=output_v,
    )
    spectrum = compute_spectrum(
        capture.reference.samples_v, capture.node("out").samples_v, window=capture.window
    )
    audio = render_comparison(
        [
            ClipSource("input", "Input", capture.reference.samples_v, experiment_id),
            ClipSource("output", "Output", capture.node("out").samples_v, experiment_id),
        ],
        artifacts,
        session_id,
        tones=capture.tones,
    )
    evaluation = None
    if goal is not None:
        evaluation = evaluate_goal(
            goal,
            experiment_id=experiment_id,
            template_id=revision.template_id,
            measured_gain_db={
                c.frequency_hz: build.lookup_point(points, c.frequency_hz).gain_db
                for c in goal.constraints
            },
        )
    return ExperimentRecord(
        id=experiment_id,
        revision_id=revision.id,
        status="succeeded",
        created_at=created_at,
        duration_ms=round((time.perf_counter() - started) * 1000),
        simulator=simulator_info,
        settings=ExperimentSettings(
            ac=AcSweepSettings(
                start_hz=sweep.start_hz,
                stop_hz=sweep.stop_hz,
                points_per_decade=sweep.points_per_decade,
                points=len(response.frequency_hz),
            ),
            transient=capture.settings,
        ),
        response=response,
        tones=levels,
        waveform=waveform,
        spectrum=spectrum,
        audio=audio,
        goal=evaluation,
        checks=build.consistency_checks(capture, levels, points, audio),
    )


async def run_explore_experiment(
    *,
    simulator: Simulator,
    store: DocumentStore,
    artifacts: ArtifactStore,
    session_id: str,
    revision: Revision,
    circuit: CircuitSpec,
    goal: Goal | None = None,
    cancel: CancelToken | None = None,
) -> ExperimentRecord:
    """Run, assemble, and persist the explore bundle for one public session revision.

    Refuses a frozen revision (the intended design of an open case) for every caller, so no
    path into explore can measure a case design outside the case instruments.
    """
    record = await measure_explore_experiment(
        simulator=simulator,
        artifacts=artifacts,
        session_id=session_id,
        revision=revision,
        circuit=circuit,
        goal=goal,
        cancel=cancel,
    )
    store.put("experiment", record.id, session_id, record)
    return record


async def measure_explore_experiment(
    *,
    simulator: Simulator,
    artifacts: ArtifactStore,
    session_id: str,
    revision: Revision,
    circuit: CircuitSpec,
    goal: Goal | None = None,
    cancel: CancelToken | None = None,
) -> ExperimentRecord:
    """Run and assemble the explore bundle WITHOUT persisting the record.

    The caller must persist the returned record (kind ``"experiment"``) before citing it.
    Audio artifacts are already written (session-scoped, random IDs). Same refusals and error
    mapping as ``run_explore_experiment``.
    """
    if revision.frozen:
        raise ApiException(
            ErrorCode.STALE_REVISION,
            "This design belongs to an open investigation. Measure it with the case "
            "instruments instead.",
            field="revision_id",
        )
    token = cancel or CancelToken()
    started = time.perf_counter()
    created_at = utcnow()
    experiment_id = new_id("exp")
    sweep = explore_sweep()
    points = AcPoints(frequencies_hz=exact_frequencies(circuit, goal))
    try:
        simulator_info = simulator.info()  # fail fast (503) before starting any job
        sweep_data, points_data, capture = await _run_concurrently(
            [
                simulator.ac(circuit, sweep, cancel=token),
                simulator.ac(circuit, points, cancel=token),
                run_two_tone_capture(simulator, circuit, nodes=("in", "out"), cancel=token),
            ],
            token,
        )
        if token.cancelled:
            raise SimulationCancelled("The simulation was cancelled.")
        record = await asyncio.to_thread(
            _assemble,
            experiment_id=experiment_id,
            revision=revision,
            session_id=session_id,
            artifacts=artifacts,
            sweep=sweep,
            sweep_data=sweep_data,
            points_data=points_data,
            capture=capture,
            goal=goal,
            started=started,
            created_at=created_at,
            simulator_info=simulator_info,
        )
    except SimulationError as exc:
        log.info("explore experiment %s: %s", experiment_id, type(exc).__name__)
        raise simulation_error_to_api(exc) from None
    except (SignalError, ValueError, KeyError) as exc:
        log.warning("explore experiment %s analysis failed: %s", experiment_id, type(exc).__name__)
        raise _analysis_failed() from None
    if token.cancelled:
        # The client left while results were being assembled: keep nothing half-reported.
        raise simulation_error_to_api(SimulationCancelled("The simulation was cancelled."))
    return record


def get_experiment(store: DocumentStore, session_id: str, experiment_id: str) -> ExperimentRecord:
    record = store.get("experiment", experiment_id, session_id, ExperimentRecord)
    if record is None:
        raise not_found("Experiment")
    return record


# --------------------------------------------------------------------------------------
# Comparisons
# --------------------------------------------------------------------------------------


def _output_clip(record: ExperimentRecord) -> AudioClip | None:
    if record.audio is None:
        return None
    for clip in record.audio.clips:
        if clip.role == "output":
            return clip
    return None


def _comparison_tones(a: ExperimentRecord, b: ExperimentRecord) -> list[Tone]:
    """The test-signal tones of both experiments (they differ across circuit families)."""
    tones: list[Tone] = []
    for tone in [*a.settings.transient.tones, *b.settings.transient.tones]:
        if all(t.frequency_hz != tone.frequency_hz for t in tones):
            tones.append(tone)
    return tones


def _comparison_audio(
    a: ExperimentRecord,
    b: ExperimentRecord,
    artifacts: ArtifactStore,
    session_id: str,
) -> AudioComparison | None:
    """Previous (A) vs current (B) output clips at one shared gain."""
    tones = _comparison_tones(a, b)
    clip_a, clip_b = _output_clip(a), _output_clip(b)
    if clip_a is None or clip_b is None or a.audio is None or b.audio is None:
        return None
    opened_a = artifacts.open(clip_a.artifact_id, session_id)
    opened_b = artifacts.open(clip_b.artifact_id, session_id)
    if opened_a is None or opened_b is None:
        return None  # evicted from the bounded artifact directory
    label_a, label_b = "Previous output", "Current output"
    same_gain = (
        a.audio.playback_gain_fs_per_v == b.audio.playback_gain_fs_per_v
        and a.audio.gain_adjustment_db == 0.0
        and b.audio.gain_adjustment_db == 0.0
        and a.audio.fade_ms == b.audio.fade_ms
        and clip_a.sample_rate_hz == clip_b.sample_rate_hz
    )
    if same_gain:
        reuse = (
            "These are the recorded output clips of both experiments, reused unchanged: both "
            "were rendered at the same shared gain with no reduction."
        )
        low = low_tone_note(tones)
        return AudioComparison(
            id=new_id("cmp"),
            clips=[
                clip_a.model_copy(update={"role": "previous", "label": label_a}),
                clip_b.model_copy(update={"role": "output", "label": label_b}),
            ],
            playback_gain_fs_per_v=a.audio.playback_gain_fs_per_v,
            gain_adjustment_db=0.0,
            fade_ms=a.audio.fade_ms,
            note=(
                f"{reuse} Every clip uses the same playback gain (1 V maps to "
                f"{a.audio.playback_gain_fs_per_v * 100:.0f}% of full scale) and the same "
                "volume control; no clip is normalized or compressed, so level differences are "
                f"real. Each clip has {a.audio.fade_ms:g} ms raised-cosine fades at both ends "
                "to prevent clicks (playback only; measurements use the un-faded steady state)."
                + (f" {low}" if low else "")
            ),
        )
    # Different gains or a reduction: recover volts from the stored PCM and render both again
    # at one new shared gain. Gain and fades are linear, so this equals re-rendering the
    # original samples (to 16-bit rounding).
    rate_a, fs_a = read_wav_fs(opened_a[0])
    rate_b, fs_b = read_wav_fs(opened_b[0])
    if rate_a != rate_b:
        return None
    volts_a = fs_a / (a.audio.playback_gain_fs_per_v * 10 ** (a.audio.gain_adjustment_db / 20))
    volts_b = fs_b / (b.audio.playback_gain_fs_per_v * 10 ** (b.audio.gain_adjustment_db / 20))
    return render_faded_comparison(
        [
            ClipSource("previous", label_a, volts_a, a.id),
            ClipSource("output", label_b, volts_b, b.id),
        ],
        artifacts,
        session_id,
        playback_gain_fs_per_v=b.audio.playback_gain_fs_per_v,
        sample_rate_hz=rate_a,
        fade_ms=b.audio.fade_ms,
        extra_note=(
            "The two experiments' clips were rendered with different gain adjustments, so "
            "both were re-rendered together at one shared gain."
        ),
        tones=tones,
    )


def compare_experiments(
    *,
    artifacts: ArtifactStore,
    session_id: str,
    a: ExperimentRecord,
    b: ExperimentRecord,
    rev_a: Revision,
    rev_b: Revision,
) -> ExperimentComparison:
    """Deterministic comparison of two recorded experiments (A = before, B = after)."""
    changes = build.component_changes(rev_a, rev_b)
    deltas = build.tone_deltas(a, b)
    try:
        audio = _comparison_audio(a, b, artifacts, session_id)
    except (SignalError, OSError, ValueError) as exc:
        log.warning("comparison audio unavailable: %s", type(exc).__name__)
        audio = None
    summary = build.comparison_summary(a, b, rev_a, rev_b, changes, deltas)
    if audio is None:
        summary += " Audio for this comparison is unavailable; run the experiments again to listen."
    return ExperimentComparison(
        a_id=a.id,
        b_id=b.id,
        a_revision_id=a.revision_id,
        b_revision_id=b.revision_id,
        changes=changes,
        tones=deltas,
        audio=audio,
        summary=summary,
    )
