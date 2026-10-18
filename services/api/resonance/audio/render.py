"""Shared-gain audio rendering for comparisons (owner: audio & signal engineer).

Honesty rules implemented here:

* Every clip in a comparison is multiplied by the same global ``PLAYBACK_GAIN_FS_PER_V``.
  Nothing is normalized per clip and nothing is compressed or limited, so a tone that the
  circuit attenuates by 10 dB is exactly 10 dB quieter in the PCM data.
* Clipping is detected before encoding. If any clip would exceed full scale at the shared gain,
  ONE reduction is applied to every clip of the comparison and disclosed in
  ``AudioComparison.gain_adjustment_db`` and ``note``.
* Matched raised-cosine fades of ``AUDIO_FADE_MS`` at both ends of every clip prevent clicks.
  They are applied to the playback copy only; measurements use the un-faded steady state, and
  the analysis window (``tones.analysis_window``) lies strictly between the fades.
* Clips are 16-bit PCM mono WAV written by the standard library: RIFF/fmt/data chunks only,
  no metadata. Artifact IDs are random (``ArtifactStore.put``).
"""

from __future__ import annotations

import io
import math
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np

from ..contracts.common import new_id
from ..contracts.experiments import AudioClip, AudioComparison, ClipRole
from ..contracts.internal import FloatArray
from ..contracts.policy import AUDIO_FADE_MS, AUDIO_SAMPLE_RATE_HZ, PLAYBACK_GAIN_FS_PER_V
from ..store.artifacts import ArtifactStore
from .signals import SignalError

FULL_SCALE_FS = 1.0
# When a shared reduction is unavoidable, the loudest clip is brought to this peak.
REDUCTION_TARGET_PEAK_FS = 0.95
PCM_MAX = 32767  # symmetric int16 scaling: +1.0 FS → 32767, −1.0 FS → −32767
MAX_CLIPS = 4


class ClipSource(NamedTuple):
    """One clip to render: un-faded samples in volts on the uniform audio grid."""

    role: ClipRole
    label: str
    samples_v: FloatArray
    source_id: str | None = None


@dataclass(frozen=True)
class GainPlan:
    playback_gain_fs_per_v: float
    adjustment_db: float  # ≤ 0; shared by every clip
    nominal_peaks_fs: tuple[float, ...]  # peaks at the standard gain, before any reduction

    @property
    def factor(self) -> float:
        """Volts → full-scale multiplier actually applied to every clip."""
        return self.playback_gain_fs_per_v * 10.0 ** (self.adjustment_db / 20.0)

    @property
    def would_clip(self) -> tuple[bool, ...]:
        return tuple(p > FULL_SCALE_FS for p in self.nominal_peaks_fs)


def plan_shared_gain(
    peaks_v: Sequence[float], *, playback_gain_fs_per_v: float = PLAYBACK_GAIN_FS_PER_V
) -> GainPlan:
    """Decide the single gain for a comparison from each clip's peak voltage."""
    if not peaks_v:
        raise SignalError("A comparison needs at least one clip.")
    nominal = tuple(float(p) * playback_gain_fs_per_v for p in peaks_v)
    worst = max(nominal)
    adjustment = 0.0
    if worst > FULL_SCALE_FS:
        adjustment = 20.0 * math.log10(REDUCTION_TARGET_PEAK_FS / worst)
    return GainPlan(
        playback_gain_fs_per_v=playback_gain_fs_per_v,
        adjustment_db=adjustment,
        nominal_peaks_fs=nominal,
    )


def fade_samples(sample_rate_hz: int, fade_ms: float = AUDIO_FADE_MS) -> int:
    """Fade length in samples; shared with the analysis window so they never overlap."""
    return math.ceil(fade_ms * sample_rate_hz / 1000.0 - 1e-9)


def fade_envelope(
    n_samples: int, sample_rate_hz: int, fade_ms: float = AUDIO_FADE_MS
) -> FloatArray:
    """Raised-cosine fade-in and fade-out of ``fade_ms`` (identical for every clip)."""
    env = np.ones(n_samples, dtype=np.float64)
    m = min(fade_samples(sample_rate_hz, fade_ms), n_samples // 2)
    if m > 0:
        ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(m, dtype=np.float64) / m))
        env[:m] = ramp
        env[n_samples - m :] = ramp[::-1]
    return env


def pcm16(samples_fs: FloatArray) -> bytes:
    """Little-endian int16 PCM. Values beyond ±1 FS are limited (callers detect this first)."""
    scaled = np.rint(np.asarray(samples_fs, dtype=np.float64) * PCM_MAX)
    return np.clip(scaled, -PCM_MAX, PCM_MAX).astype("<i2").tobytes()


def wav_bytes(pcm: bytes, sample_rate_hz: int) -> bytes:
    """Mono 16-bit WAV with only the RIFF, fmt, and data chunks."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate_hz)
        w.writeframes(pcm)
    return buf.getvalue()


def read_wav_fs(path: Path) -> tuple[int, FloatArray]:
    """Read a clip written by ``wav_bytes`` back as full-scale floats (for re-rendering)."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SignalError("A stored audio clip has an unexpected format.")
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    data = np.frombuffer(frames, dtype="<i2").astype(np.float64) / PCM_MAX
    return rate, data


def _validate_clips(clips: Sequence[ClipSource]) -> list[FloatArray]:
    if not 1 <= len(clips) <= MAX_CLIPS:
        raise SignalError("A comparison has between one and four clips.")
    arrays = []
    for clip in clips:
        x = np.asarray(clip.samples_v, dtype=np.float64)
        if x.ndim != 1 or x.size == 0 or not np.all(np.isfinite(x)):
            raise SignalError("An audio clip contains invalid samples.")
        arrays.append(x)
    return arrays


def _gain_note(
    plan: GainPlan, clips: Sequence[ClipSource], fade_ms: float, faded_already: bool
) -> str:
    gain = plan.playback_gain_fs_per_v
    parts = [
        f"Every clip uses the same playback gain (1 V maps to {gain * 100:.0f}% of full scale) "
        "and the same volume control; no clip is normalized or compressed, so level "
        "differences are real."
    ]
    if plan.adjustment_db < 0.0:
        loud = [c.label for c, hot in zip(clips, plan.would_clip, strict=True) if hot]
        parts.append(
            f"At that gain {', '.join(loud)} would exceed full scale, so every clip was "
            f"reduced together by {-plan.adjustment_db:.1f} dB; relative levels are unchanged."
        )
    fade_text = (
        f"Each clip keeps its {fade_ms:g} ms raised-cosine fades"
        if faded_already
        else f"Each clip has {fade_ms:g} ms raised-cosine fades"
    )
    parts.append(
        f"{fade_text} at both ends to prevent clicks (playback only; measurements use the "
        "un-faded steady state)."
    )
    return " ".join(parts)


def _store(
    clips: Sequence[ClipSource],
    final_fs: Sequence[FloatArray],
    plan: GainPlan,
    *,
    artifacts: ArtifactStore,
    session_id: str,
    sample_rate_hz: int,
    fade_ms: float,
    note: str,
) -> AudioComparison:
    rendered: list[AudioClip] = []
    for clip, x in zip(clips, final_fs, strict=True):
        peak = float(np.max(np.abs(x)))
        artifact_id = artifacts.put(session_id, wav_bytes(pcm16(x), sample_rate_hz), "audio/wav")
        rendered.append(
            AudioClip(
                artifact_id=artifact_id,
                url=artifacts.url_for(artifact_id),
                role=clip.role,
                label=clip.label,
                source_id=clip.source_id,
                duration_s=x.size / sample_rate_hz,
                sample_rate_hz=sample_rate_hz,
                peak_fs=peak,
                clipped=peak > FULL_SCALE_FS,
            )
        )
    return AudioComparison(
        id=new_id("cmp"),
        clips=rendered,
        playback_gain_fs_per_v=plan.playback_gain_fs_per_v,
        gain_adjustment_db=plan.adjustment_db,
        fade_ms=fade_ms,
        note=note,
    )


def render_comparison(
    clips: Sequence[ClipSource],
    artifacts: ArtifactStore,
    session_id: str,
    *,
    playback_gain_fs_per_v: float = PLAYBACK_GAIN_FS_PER_V,
    sample_rate_hz: int = AUDIO_SAMPLE_RATE_HZ,
    fade_ms: float = AUDIO_FADE_MS,
    extra_note: str | None = None,
) -> AudioComparison:
    """Render un-faded voltage clips with ONE shared gain, matched fades, and WAV artifacts."""
    arrays = _validate_clips(clips)
    plan = plan_shared_gain(
        [float(np.max(np.abs(x))) for x in arrays], playback_gain_fs_per_v=playback_gain_fs_per_v
    )
    final = [x * plan.factor * fade_envelope(x.size, sample_rate_hz, fade_ms) for x in arrays]
    note = _gain_note(plan, clips, fade_ms, faded_already=False)
    if extra_note:
        note = f"{extra_note} {note}"
    return _store(
        clips,
        final,
        plan,
        artifacts=artifacts,
        session_id=session_id,
        sample_rate_hz=sample_rate_hz,
        fade_ms=fade_ms,
        note=note,
    )


def render_faded_comparison(
    clips: Sequence[ClipSource],
    artifacts: ArtifactStore,
    session_id: str,
    *,
    playback_gain_fs_per_v: float,
    sample_rate_hz: int,
    fade_ms: float,
    extra_note: str | None = None,
) -> AudioComparison:
    """Like ``render_comparison`` for clips recovered from earlier faded playback copies.

    The samples (volts) already carry the matched fades, so none are added. Because gain and
    fades are both linear, the result is identical (to PCM rounding) to rendering the original
    un-faded samples at the new shared gain.
    """
    arrays = _validate_clips(clips)
    plan = plan_shared_gain(
        [float(np.max(np.abs(x))) for x in arrays], playback_gain_fs_per_v=playback_gain_fs_per_v
    )
    final = [x * plan.factor for x in arrays]
    note = _gain_note(plan, clips, fade_ms, faded_already=True)
    if extra_note:
        note = f"{extra_note} {note}"
    return _store(
        clips,
        final,
        plan,
        artifacts=artifacts,
        session_id=session_id,
        sample_rate_hz=sample_rate_hz,
        fade_ms=fade_ms,
        note=note,
    )
