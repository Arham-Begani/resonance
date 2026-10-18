"""Previous-vs-current comparison audio: reuse vs shared re-render (synthetic, no ngspice)."""

from __future__ import annotations

import math
import wave
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from resonance.audio.capture import transient_settings
from resonance.audio.render import REDUCTION_TARGET_PEAK_FS, ClipSource, render_comparison
from resonance.audio.resample import uniform_times
from resonance.circuits.revisions import build_revision
from resonance.contracts.common import SimulatorInfo
from resonance.contracts.experiments import (
    AcSweepSettings,
    AudioComparison,
    ExperimentRecord,
    ExperimentSettings,
)
from resonance.contracts.policy import AUDIO_SAMPLE_RATE_HZ, DEFAULT_TONES
from resonance.explore.service import compare_experiments
from resonance.store.artifacts import ArtifactStore
from resonance.store.db import DocumentStore

SESSION = "s" * 40
N = 57_600


@pytest.fixture
def artifacts(tmp_path: Path) -> Iterator[ArtifactStore]:
    store = DocumentStore(tmp_path / "db.sqlite3")
    yield ArtifactStore(tmp_path / "art", store, max_count=50, max_bytes=50 * 1024 * 1024)
    store.close()


def _tone(peak_v: float) -> np.ndarray:
    t = uniform_times(0.2, N, AUDIO_SAMPLE_RATE_HZ)
    return peak_v / 0.9 * (0.6 * np.sin(2 * np.pi * 440 * t) + 0.3 * np.sin(2 * np.pi * 4000 * t))


def _record(record_id: str, revision_id: str, audio: AudioComparison) -> ExperimentRecord:
    return ExperimentRecord(
        id=record_id,
        revision_id=revision_id,
        status="succeeded",
        created_at=datetime.now(UTC),
        duration_ms=1,
        simulator=SimulatorInfo(version="47"),
        settings=ExperimentSettings(
            ac=AcSweepSettings(start_hz=10, stop_hz=1e5, points_per_decade=40, points=161),
            transient=transient_settings(DEFAULT_TONES),
        ),
        audio=audio,
    )


def _pcm(artifacts: ArtifactStore, artifact_id: str) -> np.ndarray:
    opened = artifacts.open(artifact_id, SESSION)
    assert opened is not None
    with wave.open(str(opened[0]), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)


def test_different_adjustments_are_re_rendered_together(artifacts: ArtifactStore) -> None:
    rev_a = build_revision("rc_lowpass", {}, origin="template_default")
    rev_b = build_revision("rc_lowpass", {"C1": 220e-9}, origin="user_edit", parent=rev_a)
    loud = render_comparison(
        [
            ClipSource("input", "Input", _tone(1.5), None),
            ClipSource("output", "Output", _tone(1.5), None),
        ],
        artifacts,
        SESSION,
    )
    soft = render_comparison(
        [
            ClipSource("input", "Input", _tone(0.9), None),
            ClipSource("output", "Output", _tone(0.5), None),
        ],
        artifacts,
        SESSION,
    )
    assert loud.gain_adjustment_db < 0 and soft.gain_adjustment_db == 0
    a = _record("exp_aaaaaaaaaaaa", rev_a.id, loud)
    b = _record("exp_bbbbbbbbbbbb", rev_b.id, soft)
    cmp = compare_experiments(
        artifacts=artifacts, session_id=SESSION, a=a, b=b, rev_a=rev_a, rev_b=rev_b
    )
    audio = cmp.audio
    assert audio is not None
    assert "re-rendered together at one shared gain" in audio.note
    assert audio.gain_adjustment_db == pytest.approx(
        20 * math.log10(REDUCTION_TARGET_PEAK_FS / 1.5), abs=1e-3
    )
    previous, current = audio.clips
    assert (previous.role, current.role) == ("previous", "output")
    assert previous.artifact_id != loud.clips[1].artifact_id  # new artifacts were written
    pa, pb = _pcm(artifacts, previous.artifact_id), _pcm(artifacts, current.artifact_id)
    assert pa[0] == 0.0 and pb[0] == 0.0  # fades kept, not applied twice
    body = slice(720, 720 + 46 * 1200)
    ratio_db = 20 * math.log10(np.sqrt(np.mean(pb[body] ** 2)) / np.sqrt(np.mean(pa[body] ** 2)))
    assert ratio_db == pytest.approx(20 * math.log10(0.5 / 1.5), abs=0.01)
    assert cmp.changes[0].component_id == "C1"


def test_missing_clips_make_audio_unavailable_not_an_error(artifacts: ArtifactStore) -> None:
    rev = build_revision("rc_lowpass", {}, origin="template_default")
    audio = render_comparison(
        [
            ClipSource("input", "Input", _tone(0.9), None),
            ClipSource("output", "Output", _tone(0.5), None),
        ],
        artifacts,
        "t" * 40,  # stored for another session: invisible here
    )
    a = _record("exp_aaaaaaaaaaaa", rev.id, audio)
    cmp = compare_experiments(
        artifacts=artifacts, session_id=SESSION, a=a, b=a, rev_a=rev, rev_b=rev
    )
    assert cmp.audio is None
    assert cmp.summary.endswith(
        "Audio for this comparison is unavailable; run the experiments again to listen."
    )
