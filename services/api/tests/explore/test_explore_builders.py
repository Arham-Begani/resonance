"""Deterministic record/comparison builders (no simulator)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from resonance.audio.capture import transient_settings
from resonance.circuits.revisions import build_revision
from resonance.contracts.common import SimulatorInfo
from resonance.contracts.experiments import (
    AcSweepSettings,
    ExperimentRecord,
    ExperimentSettings,
    ToneLevel,
)
from resonance.contracts.internal import AcData
from resonance.contracts.policy import DB_FLOOR, DEFAULT_TONES
from resonance.explore.experiments import (
    comparison_summary,
    component_changes,
    exact_points,
    frequency_response,
    tone_deltas,
)


def test_frequency_response_wraps_phase_converts_current_and_clamps_db() -> None:
    f = np.array([100.0, 1000.0, 10_000.0])
    v_in = np.full(3, 1.0 + 0j)
    v_out = np.array([np.exp(1j * math.radians(200.0)), 0.5 + 0j, 0j])
    i_src = np.array([1e-3 + 0j, -2e-3j, 0j])
    resp = frequency_response(AcData(frequency_hz=f, v_in=v_in, v_out=v_out, i_source=i_src))
    assert resp.phase_deg[0] == pytest.approx(-160.0)
    assert resp.gain_db[1] == pytest.approx(20 * math.log10(0.5))
    assert resp.gain_db[2] == DB_FLOOR  # an exact zero never becomes -inf in JSON
    assert resp.source_current_ma[:2] == pytest.approx([1.0, 2.0])
    assert resp.source_current_phase_deg[1] == pytest.approx(-90.0)
    assert all(math.isfinite(x) for x in resp.gain_db + resp.phase_deg)
    points = exact_points(AcData(frequency_hz=f, v_in=v_in, v_out=v_out, i_source=i_src))
    assert points[1000.0].gain_db == pytest.approx(-6.0206, abs=1e-4)


def _record(record_id: str, revision_id: str, gains: tuple[float, float]) -> ExperimentRecord:
    tones = [
        ToneLevel(
            frequency_hz=t.frequency_hz,
            label=t.label,
            role=t.role,
            input_amplitude_v=t.amplitude_v,
            output_amplitude_v=t.amplitude_v * 10 ** (g / 20),
            transient_gain_db=g,
            ac_gain_db=g,
            agreement_db=0.0,
            phase_deg=0.0,
        )
        for t, g in zip(DEFAULT_TONES, gains, strict=True)
    ]
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
        tones=tones,
    )


def test_comparison_lists_changes_and_measured_tone_deltas() -> None:
    rev_a = build_revision("rc_lowpass", {}, origin="template_default")
    rev_b = build_revision("rc_lowpass", {"R1": 1600.0}, origin="user_edit", parent=rev_a)
    a = _record("exp_aaaaaaaaaaaa", rev_a.id, (-0.32, -8.64))
    b = _record("exp_bbbbbbbbbbbb", rev_b.id, (-0.78, -12.35))
    changes = component_changes(rev_a, rev_b)
    assert [(c.component_id, c.from_value_si, c.to_value_si) for c in changes] == [
        ("R1", 1000.0, 1600.0)
    ]
    assert changes[0].from_display == "1 kΩ" and changes[0].to_display == "1.6 kΩ"
    deltas = tone_deltas(a, b)
    assert [d.delta_db for d in deltas] == pytest.approx([-0.46, -3.71])
    summary = comparison_summary(a, b, rev_a, rev_b, changes, deltas)
    assert summary == (
        "Changed R1 from 1 kΩ to 1.6 kΩ. Measured AC gain: Musical tone (440 Hz) -0.32 dB → "
        "-0.78 dB (0.5 dB more attenuation); Whistle (4 kHz) -8.64 dB → -12.35 dB "
        "(3.7 dB more attenuation)."
    )
    same = comparison_summary(a, a, rev_a, rev_a, [], tone_deltas(a, a))
    assert same.startswith("Both experiments used the same component values.")
    assert "no measurable change" in same
