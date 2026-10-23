"""Real-ngspice audio checks for BOTH circuit families, each with its own two-tone signal.

Designs are derived from the template's validated bounds (``resonance.circuits.templates``):
the default, a goal-meeting design, the component-bound corners that satisfy the joint cutoff
constraint, the slowest and fastest allowed cutoffs reached with each component at its limit,
and the 2x / 0.5x fault variants of the slowest and fastest designs (hidden cases pass the
same capture pipeline). For every design:

* transient tone levels and phases (voltage and source current) agree with an AC analysis at
  exactly each tone frequency, including the high-pass 50 Hz hum
  (``TONE_AGREEMENT_TOLERANCE_DB`` = 0.5 dB; the measured worst case is printed);
* the fixed public settling time is sufficient (first/second half of the analysis window);
* the clips rendered at the shared gain do not clip, and the per-tone level ratio of the
  decoded 16-bit PCM (output / input) equals the measured gain, so attenuation stays audible
  at its true size.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import numpy as np
import pytest

from resonance.audio.capture import TwoToneCapture, run_two_tone_capture
from resonance.audio.render import ClipSource, read_wav_fs, render_comparison
from resonance.audio.tones import fit_tones
from resonance.circuits.templates import get_template, validate_values
from resonance.config import Settings
from resonance.contracts.circuits import SourceSpec, TemplateId
from resonance.contracts.internal import AcPoints, CircuitSpec
from resonance.contracts.policy import PLAYBACK_GAIN_FS_PER_V, TEMPLATE_TONES
from resonance.explore.experiments import (
    INPUT_AMPLITUDE_TOLERANCE_DB,
    STEADY_STATE_TOLERANCE_DB,
    TONE_AGREEMENT_TOLERANCE_DB,
    TONE_PHASE_TOLERANCE_DEG,
)
from resonance.simulation import create_simulator
from resonance.store.artifacts import ArtifactStore
from resonance.store.db import DocumentStore

pytestmark = pytest.mark.ngspice

# 16-bit PCM keeps even a −52 dB tone at ≈ 25 LSB, so its fitted level is exact to ~1e-4.
PCM_LEVEL_TOLERANCE_DB = 0.02

# (name, R1, C1) of a goal-meeting design per family (E12 values from ``explore.suggest``).
_GOAL_DESIGN = {"rc_lowpass": (1.8e3, 100e-9), "rc_highpass": (8.2e3, 100e-9)}


def _designs(template_id: TemplateId) -> list[tuple[str, float, float]]:
    t = get_template(template_id)
    r_b = t.component("R1").bounds
    c_b = t.component("C1").bounds
    assert r_b is not None and c_b is not None
    fc_lo, fc_hi = t.cutoff_min_hz * 1.001, t.cutoff_max_hz * 0.999
    defaults = t.default_values()

    def r_for(c: float, fc: float) -> float:
        return 1 / (2 * math.pi * c * fc)

    nominal = [
        ("default", defaults["R1"], defaults["C1"]),
        ("goal design", *_GOAL_DESIGN[template_id]),
        ("R min, C max", r_b.min_si, c_b.max_si),
        ("R max, C min", r_b.max_si, c_b.min_si),
        ("slowest, R max", r_b.max_si, r_for(r_b.max_si, fc_lo)),
        ("slowest, C max", r_for(c_b.max_si, fc_lo), c_b.max_si),
        ("fastest, R min", r_b.min_si, r_for(r_b.min_si, fc_hi)),
        ("fastest, C min", r_for(c_b.min_si, fc_hi), c_b.min_si),
    ]
    valid = []
    for name, r, c in nominal:
        try:
            validate_values(template_id, {"R1": r, "C1": c})
        except ValueError:
            continue  # a corner excluded by the joint cutoff constraint
        valid.append((name, r, c))
    slow = [d for d in valid if d[0].startswith("slowest")]
    fast = [d for d in valid if d[0].startswith("fastest")]
    assert len(slow) == 2 and len(fast) == 2
    faults = [(f"{n} x2 R (fault)", r * 2, c) for n, r, c in slow]
    faults += [(f"{n} x2 C (fault)", r, c * 2) for n, r, c in slow]
    faults += [(f"{n} x0.5 R (fault)", r * 0.5, c) for n, r, c in fast]
    faults += [(f"{n} x0.5 C (fault)", r, c * 0.5) for n, r, c in fast]
    return valid + faults


def _pcm_gains_db(
    capture: TwoToneCapture, artifacts: ArtifactStore, session: str
) -> tuple[list[float], float, bool, float]:
    """Render Input/Output at the shared gain, decode the WAVs, and fit each tone's level."""
    comparison = render_comparison(
        [
            ClipSource("input", "Input", capture.reference.samples_v),
            ClipSource("output", "Output", capture.node("out").samples_v),
        ],
        artifacts,
        session,
        tones=capture.tones,
    )
    fits = []
    for clip in comparison.clips:
        opened = artifacts.open(clip.artifact_id, session)
        assert opened is not None
        _rate, pcm = read_wav_fs(opened[0])
        fits.append(
            fit_tones(
                pcm,
                clip_start_s=capture.settings.settle_s,
                window=capture.window,
                frequencies_hz=capture.signal.frequencies_hz,
            )
        )
    gains = [
        20 * math.log10(out.amplitude_v / inp.amplitude_v)
        for inp, out in zip(fits[0].tones, fits[1].tones, strict=True)
    ]
    peak = max(c.peak_fs for c in comparison.clips)
    clipped = any(c.clipped for c in comparison.clips)
    return gains, peak, clipped, comparison.gain_adjustment_db


async def _measure(
    simulator: object,
    template_id: TemplateId,
    name: str,
    r: float,
    c: float,
    artifacts: ArtifactStore,
) -> dict[str, object]:
    tones = TEMPLATE_TONES[template_id]
    circuit = CircuitSpec(
        template_id=template_id,
        values={"R1": r, "C1": c},
        source=SourceSpec(tones=list(tones), description="two-tone test source"),
    )
    freqs = tuple(t.frequency_hz for t in tones)
    cap, ac = await asyncio.gather(
        run_two_tone_capture(simulator, circuit, nodes=("out", "i_source")),  # type: ignore[arg-type]
        simulator.ac(circuit, AcPoints(freqs)),  # type: ignore[attr-defined]
    )
    pcm_gains, peak, clipped, adjustment = _pcm_gains_db(cap, artifacts, "s" * 40)
    rows = []
    for k, f in enumerate(freqs):
        h = ac.v_out[k] / ac.v_in[k]
        y = ac.i_source[k] / ac.v_in[k]  # delivered A per volt
        tr_gain = cap.tone_gain_db("out", k)
        ac_gain = 20 * math.log10(abs(h))
        phase_err = (cap.tone_phase_deg("out", k) - math.degrees(float(np.angle(h))) + 180) % 360
        in_err = 20 * math.log10(cap.reference.amplitude_v(k) / tones[k].amplitude_v)
        ac_ma = abs(y) * tones[k].amplitude_v * 1000
        i_err = 20 * math.log10(cap.node("i_source").amplitude(k) / ac_ma)
        i_phase = cap.tone_phase_deg("i_source", k) - math.degrees(float(np.angle(y)))
        rows.append(
            {
                "f": f,
                "tr": tr_gain,
                "ac": ac_gain,
                "d": tr_gain - ac_gain,
                "p": phase_err - 180,
                "in_err": in_err,
                "di": i_err,
                "dp": (i_phase + 180) % 360 - 180,
                "pcm": pcm_gains[k],
            }
        )
    return {
        "name": name,
        "r": r,
        "c": c,
        "rows": rows,
        "drift": cap.steady_state_drift("out"),
        "i_drift": cap.steady_state_drift("i_source"),
        "peak": peak,
        "clipped": clipped,
        "adjustment": adjustment,
    }


@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
async def test_tone_levels_match_ac_across_template_corners(
    template_id: TemplateId, settings: Settings, tmp_path: Path
) -> None:
    simulator = create_simulator(settings)
    store = DocumentStore(tmp_path / "pcm.sqlite3")
    artifacts = ArtifactStore(tmp_path / "pcm", store, max_count=8, max_bytes=8 * 1024 * 1024)
    designs = _designs(template_id)
    # One design at a time (its transient and AC run together): the simulator's queue has a
    # bounded wait, so flooding it with every design at once would time out by design.
    try:
        results = [
            await _measure(simulator, template_id, n, r, c, artifacts) for n, r, c in designs
        ]
    finally:
        store.close()
    worst = worst_i = worst_pcm = 0.0
    worst_by_tone: dict[float, float] = {}
    print(f"\n{template_id}: tones {[t.frequency_hz for t in TEMPLATE_TONES[template_id]]}")
    for res in results:
        fc = 1 / (2 * math.pi * res["r"] * res["c"])  # type: ignore[operator]
        rows: list[dict[str, float]] = res["rows"]  # type: ignore[assignment]
        for row in rows:
            worst = max(worst, abs(row["d"]))
            worst_i = max(worst_i, abs(row["di"]))
            worst_pcm = max(worst_pcm, abs(row["pcm"] - row["tr"]))
            worst_by_tone[row["f"]] = max(worst_by_tone.get(row["f"], 0.0), abs(row["d"]))
        detail = "  ".join(
            f"{row['f']:g} Hz: tr {row['tr']:+.3f} / ac {row['ac']:+.3f} dB "
            f"(d {row['d']:+.4f} dB, {row['p']:+.3f} deg; PCM {row['pcm'] - row['tr']:+.4f}), "
            f"I d {row['di']:+.4f} dB {row['dp']:+.3f} deg"
            for row in rows
        )
        print(
            f"{res['name']:<28} R={res['r']:.4g} C={res['c']:.4g} fc={fc:8.1f} Hz "
            f"peak {res['peak']:.3f} FS  {detail}"
        )
        for row in rows:
            key = (res["name"], row["f"])
            assert abs(row["d"]) <= TONE_AGREEMENT_TOLERANCE_DB, (key, row["d"])
            assert abs(row["p"]) <= TONE_PHASE_TOLERANCE_DEG, (key, row["p"])
            assert abs(row["in_err"]) <= INPUT_AMPLITUDE_TOLERANCE_DB, (key, row["in_err"])
            assert abs(row["di"]) <= TONE_AGREEMENT_TOLERANCE_DB, (key, row["di"])
            assert abs(row["dp"]) <= TONE_PHASE_TOLERANCE_DEG, (key, row["dp"])
            # Honest audio: the PCM level ratio IS the measured gain (shared gain, no
            # normalization, no compression), within 16-bit quantization.
            assert abs(row["pcm"] - row["tr"]) <= PCM_LEVEL_TOLERANCE_DB, (key, row["pcm"])
        # Settling (fixed public TRANSIENT_SETTLE_S) is sufficient even for the slowest faults.
        for drift_key in ("drift", "i_drift"):
            drift = res[drift_key]
            assert drift.max_tone_change_db <= STEADY_STATE_TOLERANCE_DB, (res["name"], drift_key)  # type: ignore[attr-defined]
        # Peak sum of the tones is 0.9 V: a passive filter never clips at the shared gain.
        assert not res["clipped"] and res["adjustment"] == 0.0, res["name"]
        assert res["peak"] <= 0.9 * PLAYBACK_GAIN_FS_PER_V + 1e-3, res["name"]  # type: ignore[operator]
    per_tone = ", ".join(f"{f:g} Hz {d:.4f} dB" for f, d in sorted(worst_by_tone.items()))
    print(
        f"worst tone agreement: voltage {worst:.4f} dB ({per_tone}), source current "
        f"{worst_i:.4f} dB; worst PCM-vs-fit level {worst_pcm:.4f} dB"
    )
