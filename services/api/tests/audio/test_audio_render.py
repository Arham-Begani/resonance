"""Shared-gain rendering: no normalization, shared reduction, matched fades, valid WAV."""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from resonance.audio.render import (
    PCM_MAX,
    REDUCTION_TARGET_PEAK_FS,
    ClipSource,
    fade_envelope,
    fade_samples,
    low_tone_note,
    plan_shared_gain,
    render_comparison,
)
from resonance.audio.resample import uniform_times
from resonance.audio.tones import analysis_window, fit_tones
from resonance.contracts.policy import (
    AUDIO_CLIP_S,
    AUDIO_FADE_MS,
    AUDIO_SAMPLE_RATE_HZ,
    PLAYBACK_GAIN_FS_PER_V,
    TEMPLATE_TONES,
)
from resonance.store.artifacts import ArtifactStore
from resonance.store.db import DocumentStore

RATE = AUDIO_SAMPLE_RATE_HZ
N = round(AUDIO_CLIP_S * RATE)
SESSION = "s" * 40


@pytest.fixture
def artifacts(tmp_path: Path) -> Iterator[ArtifactStore]:
    store = DocumentStore(tmp_path / "db.sqlite3")
    yield ArtifactStore(tmp_path / "artifacts", store, max_count=50, max_bytes=50 * 1024 * 1024)
    store.close()


def _two_tone(scale: float = 1.0) -> np.ndarray:
    t = uniform_times(0.2, N, RATE)
    return scale * (0.6 * np.sin(2 * np.pi * 440 * t) + 0.3 * np.sin(2 * np.pi * 4000 * t))


def _read(artifacts: ArtifactStore, artifact_id: str) -> tuple[wave._wave_params, np.ndarray]:
    opened = artifacts.open(artifact_id, SESSION)
    assert opened is not None
    path, media_type = opened
    assert media_type == "audio/wav"
    with wave.open(str(path), "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    return params, np.frombuffer(frames, dtype="<i2").astype(np.float64)


def _window():  # type: ignore[no-untyped-def]
    return analysis_window(
        N, sample_rate_hz=RATE, fade_samples=fade_samples(RATE), common_period_s=0.025
    )


def test_minus_10_db_output_is_exactly_10_db_quieter_in_pcm(artifacts: ArtifactStore) -> None:
    x_in = _two_tone()
    x_out = _two_tone(10 ** (-10 / 20))
    cmp = render_comparison(
        [ClipSource("input", "Input", x_in, None), ClipSource("output", "Output", x_out, None)],
        artifacts,
        SESSION,
    )
    assert cmp.playback_gain_fs_per_v == PLAYBACK_GAIN_FS_PER_V
    assert cmp.gain_adjustment_db == 0.0
    assert "normalized" in cmp.note
    _, pcm_in = _read(artifacts, cmp.clips[0].artifact_id)
    _, pcm_out = _read(artifacts, cmp.clips[1].artifact_id)
    w = _window()
    seg_in = pcm_in[w.start_index : w.stop_index]
    seg_out = pcm_out[w.start_index : w.stop_index]
    rms_ratio_db = 20 * math.log10(np.sqrt(np.mean(seg_out**2)) / np.sqrt(np.mean(seg_in**2)))
    assert rms_ratio_db == pytest.approx(-10.0, abs=0.005)
    # The PCM level is the voltage times the shared gain: nothing was scaled up or down.
    fit_in = fit_tones(pcm_in / PCM_MAX, clip_start_s=0.2, window=w, frequencies_hz=(440.0, 4000.0))
    assert fit_in.tones[0].amplitude_v == pytest.approx(0.6 * PLAYBACK_GAIN_FS_PER_V, rel=1e-4)
    assert cmp.clips[0].peak_fs == pytest.approx(np.max(np.abs(x_in)), rel=1e-6)
    assert cmp.clips[1].peak_fs == pytest.approx(cmp.clips[0].peak_fs * 10 ** (-0.5), rel=1e-6)


def test_quiet_clip_is_not_normalized(artifacts: ArtifactStore) -> None:
    quiet = _two_tone(0.01)
    cmp = render_comparison([ClipSource("output", "Output", quiet, None)], artifacts, SESSION)
    assert cmp.clips[0].peak_fs == pytest.approx(float(np.max(np.abs(quiet))), rel=1e-6)
    assert cmp.clips[0].peak_fs < 0.01


def test_clipping_is_detected_and_one_shared_reduction_is_disclosed(
    artifacts: ArtifactStore,
) -> None:
    loud = _two_tone(1.5 / 0.9)  # peak ≈ 1.5 V → 1.5 FS at the standard gain
    soft = _two_tone(0.5 / 0.9)
    plan = plan_shared_gain([float(np.max(np.abs(loud))), float(np.max(np.abs(soft)))])
    assert plan.would_clip == (True, False)
    cmp = render_comparison(
        [ClipSource("input", "Loud", loud, None), ClipSource("output", "Soft", soft, None)],
        artifacts,
        SESSION,
    )
    expected_db = 20 * math.log10(REDUCTION_TARGET_PEAK_FS / float(np.max(np.abs(loud))))
    assert cmp.gain_adjustment_db == pytest.approx(expected_db)
    assert cmp.gain_adjustment_db < 0
    assert "Loud" in cmp.note and f"{-expected_db:.1f} dB" in cmp.note
    assert not any(c.clipped for c in cmp.clips)
    assert cmp.clips[0].peak_fs == pytest.approx(REDUCTION_TARGET_PEAK_FS, rel=1e-6)
    # The same factor was applied to both clips: their ratio is unchanged.
    _, pcm_loud = _read(artifacts, cmp.clips[0].artifact_id)
    _, pcm_soft = _read(artifacts, cmp.clips[1].artifact_id)
    w = _window()
    ratio = np.sqrt(np.mean(pcm_soft[w.start_index : w.stop_index] ** 2)) / np.sqrt(
        np.mean(pcm_loud[w.start_index : w.stop_index] ** 2)
    )
    assert ratio == pytest.approx(0.5 / 1.5, rel=1e-4)


def test_fades_are_matched_raised_cosines_outside_the_analysis_window(
    artifacts: ArtifactStore,
) -> None:
    a = _two_tone()
    b = _two_tone(0.2)
    cmp = render_comparison(
        [ClipSource("input", "A", a, None), ClipSource("output", "B", b, None)], artifacts, SESSION
    )
    assert cmp.fade_ms == AUDIO_FADE_MS
    m = fade_samples(RATE, AUDIO_FADE_MS)
    env = fade_envelope(N, RATE, AUDIO_FADE_MS)
    assert env[0] == 0.0 and env[-1] == 0.0
    np.testing.assert_allclose(env[:m], 0.5 * (1 - np.cos(np.pi * np.arange(m) / m)))
    np.testing.assert_allclose(env[-m:], env[:m][::-1])
    assert np.all(env[m:-m] == 1.0)
    w = _window()
    assert w.start_index >= m and w.stop_index <= N - m  # fades never touch measurements
    for clip, x in zip(cmp.clips, (a, b), strict=True):
        _, pcm = _read(artifacts, clip.artifact_id)
        expected = np.rint(x * env * PLAYBACK_GAIN_FS_PER_V * PCM_MAX)
        np.testing.assert_array_equal(pcm, expected)


def test_wav_is_plain_mono_int16_pcm(artifacts: ArtifactStore) -> None:
    cmp = render_comparison(
        [ClipSource("input", "Input", _two_tone(), "exp_x")], artifacts, SESSION
    )
    clip = cmp.clips[0]
    assert clip.url == f"/api/artifacts/{clip.artifact_id}"
    assert clip.sample_rate_hz == RATE and clip.duration_s == pytest.approx(AUDIO_CLIP_S)
    assert clip.source_id == "exp_x" and clip.role == "input" and not clip.clipped
    params, pcm = _read(artifacts, clip.artifact_id)
    assert (params.nchannels, params.sampwidth, params.framerate, params.nframes) == (1, 2, RATE, N)
    assert pcm.size == N
    path, _ = artifacts.open(clip.artifact_id, SESSION)  # type: ignore[misc]
    raw = path.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    chunks, pos = [], 12
    while pos < len(raw):
        cid, size = raw[pos : pos + 4], struct.unpack("<I", raw[pos + 4 : pos + 8])[0]
        chunks.append(cid)
        pos += 8 + size + (size & 1)
    assert chunks == [b"fmt ", b"data"]  # no metadata chunks
    assert len(raw) == 44 + 2 * N
    # Session-scoped: another session cannot open the artifact.
    assert artifacts.open(clip.artifact_id, "t" * 40) is None


def test_invalid_clips_are_rejected(artifacts: ArtifactStore) -> None:
    from resonance.audio.signals import SignalError

    with pytest.raises(SignalError):
        render_comparison([], artifacts, SESSION)
    bad = _two_tone()
    bad[3] = np.inf
    with pytest.raises(SignalError):
        render_comparison([ClipSource("input", "x", bad, None)], artifacts, SESSION)


def test_low_hum_is_disclosed_as_possibly_inaudible_only_when_present(
    artifacts: ArtifactStore,
) -> None:
    assert low_tone_note(TEMPLATE_TONES["rc_lowpass"]) is None
    note = low_tone_note(TEMPLATE_TONES["rc_highpass"])
    assert note is not None and "Mains hum (50 Hz)" in note and "inaudible" in note
    clip = ClipSource("output", "Output", _two_tone(0.5))
    with_hum = render_comparison([clip], artifacts, SESSION, tones=TEMPLATE_TONES["rc_highpass"])
    assert with_hum.note.endswith(note)
    without = render_comparison([clip], artifacts, SESSION, tones=TEMPLATE_TONES["rc_lowpass"])
    assert "inaudible" not in without.note
    assert with_hum.note.startswith(without.note)  # the disclosure only adds text
