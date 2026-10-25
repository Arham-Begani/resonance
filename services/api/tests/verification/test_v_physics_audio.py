"""Independent verification, lens C: physics, audio honesty, Explore goals, units, hypotheses.

Owner: independent verification engineer (lens C).
Report: ``docs/verification/review-C-physics-audio.md``.

Every expected number here comes from analytical first-order RC formulas written in THIS file
(voltage divider and series-RC admittance). ``resonance.simulation.analytic`` is deliberately not
imported. What is tested:

1. ngspice AC gain/phase and delivered source current vs the independent formulas for a seeded
   random sample of designs across the template bounds plus the bound corners, each with its
   2x/0.5x R and C variants, on a log grid around each variant's own cutoff (1 % / 1 deg).
2. The Explore bundle over HTTP at the bound corners and around the default goal: transient tone
   levels vs exact AC (0.5 dB, 1 deg); settle sufficiency.
3. Audio honesty: the WAV files are downloaded and decoded here, the tones are fitted from the
   PCM, and the PCM output/input ratio must equal the reported tone gain (0.1 dB); identical
   format/length/fades; the compare endpoint's clips sit at one shared gain; an over-range
   comparison (only reachable through the renderer: a passive RC with the fixed 0.9 V source
   cannot exceed full scale) gets one shared, disclosed reduction.
4. Goal pass/fail against measured gain, and the feasibility interval (finding C-01).
5. Units parser and netlist safety through the public API (finding C-03).
6. Hypothesis evaluation: an independent reference partition for every drawable nominal design,
   the service's ``assess`` on synthetic observations, and real-ngspice cases at the extreme
   drawable designs with correct and wrong repairs.
7. Spectrum peaks vs fitted tone levels and dB floors (no non-finite JSON).

The AI is not involved anywhere in this file. Data lives under pytest's tmp_path.
Run from services/api:

    RESONANCE_REQUIRE_NGSPICE=1 uv run pytest tests/verification/test_v_physics_audio.py
"""

from __future__ import annotations

import asyncio
import cmath
import io
import json
import math
import random
import re
import sys
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

_TESTS = Path(__file__).resolve().parents[1]
if str(_TESTS / "challenges") not in sys.path:
    sys.path.insert(0, str(_TESTS / "challenges"))

from challenges_testkit import build_rig, seed_for  # noqa: E402

from resonance.app import create_app  # noqa: E402
from resonance.audio.render import ClipSource, render_comparison  # noqa: E402
from resonance.challenges import hypotheses  # noqa: E402
from resonance.challenges.evaluator import faults  # noqa: E402
from resonance.circuits.netlist import compile_ac, compile_transient  # noqa: E402
from resonance.circuits.revisions import revision_to_circuit  # noqa: E402
from resonance.circuits.templates import default_source  # noqa: E402
from resonance.config import Settings  # noqa: E402
from resonance.contracts import policy  # noqa: E402
from resonance.contracts.circuits import Revision  # noqa: E402
from resonance.contracts.common import SimulatorInfo, new_id  # noqa: E402
from resonance.contracts.internal import AcSweep, CircuitSpec, TransientRequest  # noqa: E402
from resonance.contracts.investigation import (  # noqa: E402
    CaseExperimentRequest,
    DiagnosisSubmission,
    EvidenceCitation,
    Observation,
    ObservationSettings,
    ObservedTone,
    ObservedWaveform,
    PublicCase,
    RepairProposalInput,
    SweepObservation,
    TransientObservation,
)
from resonance.simulation import create_simulator  # noqa: E402
from resonance.store.artifacts import ArtifactStore  # noqa: E402
from resonance.store.db import DocumentStore  # noqa: E402

# ======================================================================================
# Independent analytical model (ideal source, ideal R and C, no load)
# ======================================================================================

TWO_PI = 2.0 * math.pi


def h_lowpass(f: float, r: float, c: float) -> complex:
    """V(out)/V(in): capacitor impedance over the series R + C divider."""
    zc = 1.0 / (1j * TWO_PI * f * c)
    return zc / (r + zc)


def i_delivered(f: float, r: float, c: float, v: float = 1.0) -> complex:
    """Current leaving the source's + terminal into series R + C (amperes)."""
    return v / (r + 1.0 / (1j * TWO_PI * f * c))


def db(x: complex | float) -> float:
    return 20.0 * math.log10(abs(x))


def deg(z: complex) -> float:
    return math.degrees(cmath.phase(z))


def angle_diff(a: float, b: float) -> float:
    """|a − b| wrapped into [0, 180]."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def fc_of(r: float, c: float) -> float:
    return 1.0 / (TWO_PI * r * c)


VARIANTS: dict[str, tuple[float, float]] = {
    "no_fault": (1.0, 1.0),
    "r1_increased": (2.0, 1.0),
    "r1_decreased": (0.5, 1.0),
    "c1_increased": (1.0, 2.0),
    "c1_decreased": (1.0, 0.5),
}
VOLTAGE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("no_fault",),
    ("r1_increased", "c1_increased"),
    ("r1_decreased", "c1_decreased"),
)
CASE_GRID = [20.0 * 10.0 ** (k / 10.0) for k in range(31)]  # 20 Hz–20 kHz, 10/decade
TONES = [(440.0, 0.6), (4000.0, 0.3)]  # the declared demo signal (contracts/policy.py)

# Declared verification gates (CLAUDE.md "Meaningful verification").
GAIN_REL_TOL = 0.01
PHASE_TOL_DEG = 1.0
TONE_TOL_DB = 0.5
PCM_TOL_DB = 0.1

# Audio grid constants restated from the contract (policy) so the decoder is independent.
FS = 48_000
CLIP_N = 57_600  # 1.2 s
FADE_N = 720  # 15 ms
PERIOD_N = 1_200  # 25 ms = 1 / gcd(440, 4000)
WINDOW = (FADE_N, FADE_N + 46 * PERIOD_N)  # whole periods strictly between the fades
SETTLE_S = 0.2


def _settings(root: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        resonance_data_dir=root / "data",
        web_dist_dir=root / "no-web",
        nebius_api_key=None,
        nebius_model=None,
        resonance_dev_agent_fixture=False,
    )


# ======================================================================================
# PCM helpers (independent WAV decoding and tone fitting)
# ======================================================================================


@dataclass
class Wav:
    channels: int
    sample_width: int
    rate: int
    frames: int
    raw: bytes
    pcm: np.ndarray  # int16 values as float64


def decode_wav(raw: bytes) -> Wav:
    with wave.open(io.BytesIO(raw), "rb") as w:
        params = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())
        frames = w.readframes(w.getnframes())
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float64)
    return Wav(*params, raw=raw, pcm=pcm)


def fit_pcm(pcm: np.ndarray, *, start: int = WINDOW[0], stop: int = WINDOW[1]) -> np.ndarray:
    """Least-squares [a440, b440, a4k, b4k, dc] over samples [start, stop) (local time)."""
    seg = pcm[start:stop]
    t = np.arange(seg.size) / FS
    cols = []
    for f, _ in TONES:
        cols += [np.sin(TWO_PI * f * t), np.cos(TWO_PI * f * t)]
    cols.append(np.ones_like(t))
    coef, *_ = np.linalg.lstsq(np.stack(cols, axis=1), seg, rcond=None)
    return coef


def amplitudes(coef: np.ndarray) -> list[float]:
    return [math.hypot(coef[2 * k], coef[2 * k + 1]) for k in range(len(TONES))]


def fade_env(n: int = CLIP_N, m: int = FADE_N) -> np.ndarray:
    env = np.ones(n)
    ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(m) / m))
    env[:m] = ramp
    env[n - m :] = ramp[::-1]
    return env


def steady_model(pcm: np.ndarray) -> np.ndarray:
    """The steady-state two-tone fit of a clip, evaluated over the whole clip (PCM units)."""
    coef = fit_pcm(pcm)
    t = (np.arange(CLIP_N) - WINDOW[0]) / FS
    out = np.full(CLIP_N, coef[-1])
    for k, (f, _) in enumerate(TONES):
        out += coef[2 * k] * np.sin(TWO_PI * f * t) + coef[2 * k + 1] * np.cos(TWO_PI * f * t)
    return out


def strict_json(text: str) -> Any:
    def reject(token: str) -> Any:
        raise AssertionError(f"non-finite number in JSON: {token}")

    return json.loads(text, parse_constant=reject)


# ======================================================================================
# 1. AC physics vs independent formulas (real ngspice)
# ======================================================================================

AC_SEED = 20261018
R_BOUNDS = (100.0, 100e3)
C_BOUNDS = (1e-9, 10e-6)
CORNERS = [
    (100.0, 10e-6),  # R min, C max (fc 159 Hz)
    (100e3, 1e-9),  # R max, C min (fc 1.59 kHz)
    (100e3, 79.5e-9),  # fc 20.02 Hz: lowest allowed cutoff
    (100.0, 79.6e-9),  # fc 19.99 kHz: highest allowed cutoff
]


def random_designs(n: int, seed: int = AC_SEED) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    out: list[tuple[float, float]] = []
    while len(out) < n:
        r = 10.0 ** rng.uniform(math.log10(R_BOUNDS[0]), math.log10(R_BOUNDS[1]))
        c = 10.0 ** rng.uniform(math.log10(C_BOUNDS[0]), math.log10(C_BOUNDS[1]))
        if 20.0 <= fc_of(r, c) <= 20_000.0:
            out.append((r, c))
    return out


@pytest.mark.ngspice
async def test_ac_gain_phase_and_current_match_independent_formulas(tmp_path: Path) -> None:
    """|H| and |I| within 1 % (tiny absolute floors), phases within 1 deg wrapped, for designs
    across the bounds AND their 2x/0.5x fault variants, around each variant's own cutoff."""
    sim = create_simulator(_settings(tmp_path))
    sim.info()
    source = default_source("rc_lowpass")
    jobs = []
    for r0, c0 in random_designs(6) + CORNERS:
        for mr, mc in VARIANTS.values():
            r, c = r0 * mr, c0 * mc
            fc = fc_of(r, c)
            sweep = AcSweep(start_hz=fc / 30.0, stop_hz=fc * 30.0, points_per_decade=10)
            spec = CircuitSpec("rc_lowpass", {"R1": r, "C1": c}, source)
            jobs.append((r, c, sim.ac(spec, sweep)))
    results = await asyncio.gather(*(j[2] for j in jobs))
    worst = {"h_rel": 0.0, "h_deg": 0.0, "i_rel": 0.0, "i_deg": 0.0}
    for (r, c, _), data in zip(jobs, results, strict=True):
        assert data.v_in is not None and data.v_out is not None and data.i_source is not None
        assert data.frequency_hz.size == 30
        for k, f in enumerate(data.frequency_hz):
            h = complex(data.v_out[k] / data.v_in[k])
            i = complex(data.i_source[k])
            he, ie = h_lowpass(f, r, c), i_delivered(f, r, c)
            h_rel = abs(abs(h) - abs(he)) / abs(he)
            i_rel = abs(abs(i) - abs(ie)) / abs(ie)
            assert abs(abs(h) - abs(he)) <= max(GAIN_REL_TOL * abs(he), 1e-9), (r, c, f)
            assert abs(abs(i) - abs(ie)) <= max(GAIN_REL_TOL * abs(ie), 1e-12), (r, c, f)
            assert angle_diff(deg(h), deg(he)) <= PHASE_TOL_DEG, (r, c, f)
            assert angle_diff(deg(i), deg(ie)) <= PHASE_TOL_DEG, (r, c, f)
            assert 0.0 < deg(i) < 90.0, "delivered current must lead the source by 0..90 deg"
            worst["h_rel"] = max(worst["h_rel"], h_rel)
            worst["i_rel"] = max(worst["i_rel"], i_rel)
            worst["h_deg"] = max(worst["h_deg"], angle_diff(deg(h), deg(he)))
            worst["i_deg"] = max(worst["i_deg"], angle_diff(deg(i), deg(ie)))
    print(f"\n[C1] {len(jobs)} AC runs x 30 points; worst errors: {worst}")


# ======================================================================================
# 2/3/4/7. Explore bundle over HTTP (real ngspice)
# ======================================================================================

STRADDLE_C = 100e-9
# Cutoffs straddling both default-goal limits (fc ≥ 864.70 Hz and fc ≤ 1038.04 Hz).
STRADDLE_FC = {"fc855": 855.0, "fc875": 875.0, "fc1030": 1030.0, "fc1045": 1045.0}
EXPECTED_STRADDLE = {  # (440 Hz preserved ≤ 1 dB, 4 kHz attenuated ≥ 12 dB)
    "fc855": (False, True),
    "fc875": (True, True),
    "fc1030": (True, True),
    "fc1045": (True, False),
}
CORNER_RUNS = {
    "fc_min": ("100k", "79.5n"),
    "fc_max": ("100", "79.6n"),
    "r_min_c_max": ("100", "10u"),
    "r_max_c_min": ("100k", "1n"),
}


@dataclass
class Run:
    name: str
    r: float
    c: float
    record: dict[str, Any]
    raw_text: str
    wavs: dict[str, Wav]


@dataclass
class Bundle:
    client: TestClient
    goal: dict[str, Any]
    workbench: dict[str, Any]
    runs: dict[str, Run]
    comparison: dict[str, Any]
    comparison_wavs: dict[str, Wav]


def _download(client: TestClient, url: str) -> Wav:
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/wav")
    return decode_wav(resp.content)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Bundle]:
    root = tmp_path_factory.mktemp("verif_c_explore")
    with TestClient(create_app(_settings(root))) as client:
        workbench = strict_json(client.get("/api/workbench").text)
        goal = workbench["goal"]
        runs: dict[str, Run] = {}
        designs: list[tuple[str, Any, Any]] = [(n, r, c) for n, (r, c) in CORNER_RUNS.items()]
        designs += [
            (n, 1.0 / (TWO_PI * fc * STRADDLE_C), STRADDLE_C) for n, fc in STRADDLE_FC.items()
        ]
        for name, r_in, c_in in designs:
            rev = client.post(
                "/api/revisions",
                json={
                    "template_id": "rc_lowpass",
                    "values": [
                        {"component_id": "R1", "value": r_in},
                        {"component_id": "C1", "value": c_in},
                    ],
                },
            )
            assert rev.status_code == 201, rev.text
            values = {c["id"]: c["value_si"] for c in rev.json()["components"] if c["value_si"]}
            resp = client.post(
                "/api/experiments", json={"revision_id": rev.json()["id"], "goal_id": goal["id"]}
            )
            assert resp.status_code == 201, resp.text
            record = strict_json(resp.text)
            wavs = {
                clip["role"]: _download(client, clip["url"]) for clip in record["audio"]["clips"]
            }
            runs[name] = Run(name, values["R1"], values["C1"], record, resp.text, wavs)
        cmp_resp = client.get(
            "/api/experiments/compare",
            params={"a": runs["fc_min"].record["id"], "b": runs["fc_max"].record["id"]},
        )
        assert cmp_resp.status_code == 200, cmp_resp.text
        comparison = strict_json(cmp_resp.text)
        cmp_wavs = {c["role"]: _download(client, c["url"]) for c in comparison["audio"]["clips"]}
        yield Bundle(client, goal, workbench, runs, comparison, cmp_wavs)


@pytest.mark.ngspice
def test_explore_sweep_matches_formulas_and_floors(bundle: Bundle) -> None:
    """The recorded 10 Hz–100 kHz response (161 points) equals the formulas; no non-finite or
    below-floor values anywhere in the record."""
    for run in bundle.runs.values():
        resp = run.record["response"]
        f = resp["frequency_hz"]
        assert len(f) == 161 and math.isclose(f[0], 10.0) and math.isclose(f[-1], 1e5)
        for k, fk in enumerate(f):
            he, ie = h_lowpass(fk, run.r, run.c), i_delivered(fk, run.r, run.c)
            assert abs(10 ** (resp["gain_db"][k] / 20) - abs(he)) <= GAIN_REL_TOL * abs(he)
            assert angle_diff(resp["phase_deg"][k], deg(he)) <= PHASE_TOL_DEG
            assert abs(resp["source_current_ma"][k] - 1e3 * abs(ie)) <= GAIN_REL_TOL * 1e3 * abs(ie)
            assert angle_diff(resp["source_current_phase_deg"][k], deg(ie)) <= PHASE_TOL_DEG
        assert min(resp["gain_db"]) >= policy.DB_FLOOR
        # strict_json already rejected NaN/Infinity in the whole record.


@pytest.mark.ngspice
def test_transient_tone_levels_match_exact_ac(bundle: Bundle) -> None:
    """Tone gains fitted from the transient agree with the exact transfer function within 0.5 dB
    (phase 1 deg) at every bound corner; the AC point is exact; the input reproduces the source."""
    worst_db = worst_deg = 0.0
    for run in bundle.runs.values():
        rec = run.record
        assert [round(t["frequency_hz"], 6) for t in rec["tones"]] == [440.0, 4000.0]
        for tone, (f, amp) in zip(rec["tones"], TONES, strict=True):
            exact = h_lowpass(f, run.r, run.c)
            assert abs(tone["ac_gain_db"] - db(exact)) <= 1e-4
            err = tone["transient_gain_db"] - db(exact)
            assert abs(err) <= TONE_TOL_DB, (run.name, f, err)
            assert (
                abs(tone["agreement_db"] - (tone["transient_gain_db"] - tone["ac_gain_db"])) < 1e-9
            )
            assert angle_diff(tone["phase_deg"], deg(exact)) <= PHASE_TOL_DEG
            assert abs(db(tone["input_amplitude_v"] / amp)) <= 0.05
            out_err = db(tone["output_amplitude_v"] / (amp * abs(exact)))
            assert abs(out_err) <= TONE_TOL_DB
            worst_db = max(worst_db, abs(err))
            worst_deg = max(worst_deg, angle_diff(tone["phase_deg"], deg(exact)))
        checks = {c["name"]: c["passed"] for c in rec["checks"]}
        assert checks == {
            "tone_gain_agreement": True,
            "tone_phase_agreement": True,
            "input_matches_source": True,
            "steady_state": True,
            "no_clipping": True,
        }, (run.name, checks)
    print(f"\n[C2] worst transient-vs-exact tone error {worst_db:.4f} dB, {worst_deg:.3f} deg")


@pytest.mark.ngspice
def test_settling_is_sufficient_for_the_slowest_allowed_design(bundle: Bundle) -> None:
    """Analytically, the decaying term left at the fixed settle time is far below 1 LSB for the
    slowest allowed cutoff (20 Hz); measured, the two halves of the analysed PCM agree."""
    tau_max = 1.0 / (TWO_PI * 20.0)
    residual_v = sum(
        a * abs(h_lowpass(f, 1.0, tau_max)) * abs(math.sin(cmath.phase(h_lowpass(f, 1.0, tau_max))))
        for f, a in TONES
    ) * math.exp(-SETTLE_S / tau_max)
    assert residual_v < 1e-6  # 1 LSB of 16-bit full scale is 3.05e-5 V at the 1 FS/V gain
    run = bundle.runs["fc_min"]
    assert fc_of(run.r, run.c) < 20.1
    pcm = run.wavs["output"].pcm
    half = 23 * PERIOD_N
    first = amplitudes(fit_pcm(pcm, start=WINDOW[0], stop=WINDOW[0] + half))
    second = amplitudes(fit_pcm(pcm, start=WINDOW[0] + half, stop=WINDOW[1]))
    for a, b in zip(first, second, strict=True):
        assert abs(db(b / a)) < 0.01
    dc_fs = fit_pcm(pcm)[-1] / 32767
    assert abs(dc_fs) < 1e-4


@pytest.mark.ngspice
def test_wav_pcm_levels_equal_measured_tone_gains(bundle: Bundle) -> None:
    """Decoded PCM proves one shared gain and no per-clip normalization: output/input tone ratio
    in the PCM equals the reported transient tone gain (0.1 dB), absolute PCM levels equal the
    reported amplitudes x the shared gain, and every clip has the same format and length."""
    worst = 0.0
    for run in bundle.runs.values():
        audio = run.record["audio"]
        gain = audio["playback_gain_fs_per_v"]
        assert gain == policy.PLAYBACK_GAIN_FS_PER_V and audio["gain_adjustment_db"] == 0.0
        assert set(run.wavs) == {"input", "output"}
        for wav in run.wavs.values():
            assert (wav.channels, wav.sample_width, wav.rate, wav.frames) == (1, 2, FS, CLIP_N)
            assert (
                wav.raw[:4] == b"RIFF"
                and wav.raw[8:16] == b"WAVEfmt "
                and wav.raw[36:40] == b"data"
            )
            assert len(wav.raw) == 44 + 2 * CLIP_N  # RIFF + fmt + data only, no metadata chunks
        a_in = amplitudes(fit_pcm(run.wavs["input"].pcm))
        a_out = amplitudes(fit_pcm(run.wavs["output"].pcm))
        for k, tone in enumerate(run.record["tones"]):
            pcm_gain = db(a_out[k] / a_in[k])
            assert abs(pcm_gain - tone["transient_gain_db"]) <= PCM_TOL_DB, (run.name, k)
            assert abs(pcm_gain - tone["ac_gain_db"]) <= TONE_TOL_DB
            assert abs(db(a_out[k] / 32767 / (gain * tone["output_amplitude_v"]))) <= PCM_TOL_DB
            assert abs(db(a_in[k] / 32767 / (gain * TONES[k][1]))) <= PCM_TOL_DB
            worst = max(worst, abs(pcm_gain - tone["transient_gain_db"]))
    print(f"\n[C3] worst |PCM gain − reported tone gain| = {worst:.5f} dB")


@pytest.mark.ngspice
def test_clips_carry_identical_matched_fades_and_nothing_else(bundle: Bundle) -> None:
    """input PCM == round(32767 · gain · env · v_source(t)) and output PCM == env x its own
    steady-state fit, sample by sample over the whole clip (including both fades): the fades are
    the same raised cosine on every clip and no limiter/compressor touched the samples."""
    env = fade_env()
    t_abs = SETTLE_S + np.arange(CLIP_N) / FS
    v_src = sum(a * np.sin(TWO_PI * f * t_abs) for f, a in TONES)
    worst = 0.0
    for run in bundle.runs.values():
        gain = run.record["audio"]["playback_gain_fs_per_v"]
        pcm_in = run.wavs["input"].pcm
        dev_in = np.max(np.abs(pcm_in - 32767 * gain * env * v_src))
        out = run.wavs["output"].pcm
        dev_out = np.max(np.abs(out - env * steady_model(out)))
        assert dev_in <= 6.0, (run.name, dev_in)  # cubic resampling error ≈ 1e-4 V ≈ 3.3 LSB
        assert dev_out <= 6.0, (run.name, dev_out)
        worst = max(worst, dev_in, dev_out)
    print(f"\n[C3] worst sample deviation from env x model: {worst:.2f} LSB")


@pytest.mark.ngspice
def test_compare_clips_share_one_gain(bundle: Bundle) -> None:
    """Previous (fc 20 Hz) vs current (fc 20 kHz): both clips' decoded PCM tone amplitudes equal
    their own experiment's measured output amplitude x the same shared gain (0.1 dB)."""
    audio = bundle.comparison["audio"]
    assert audio is not None and audio["gain_adjustment_db"] == 0.0
    gain = audio["playback_gain_fs_per_v"]
    assert gain == policy.PLAYBACK_GAIN_FS_PER_V
    assert set(bundle.comparison_wavs) == {"previous", "output"}
    sources = {"previous": bundle.runs["fc_min"], "output": bundle.runs["fc_max"]}
    for role, wav in bundle.comparison_wavs.items():
        assert (wav.channels, wav.sample_width, wav.rate, wav.frames) == (1, 2, FS, CLIP_N)
        amps = amplitudes(fit_pcm(wav.pcm))
        for k, tone in enumerate(sources[role].record["tones"]):
            assert abs(db(amps[k] / 32767 / (gain * tone["output_amplitude_v"]))) <= PCM_TOL_DB
    # The real level difference survives: 4 kHz is ~46 dB quieter in the previous clip.
    prev = amplitudes(fit_pcm(bundle.comparison_wavs["previous"].pcm))
    cur = amplitudes(fit_pcm(bundle.comparison_wavs["output"].pcm))
    expected = db(h_lowpass(4000.0, *_rc(bundle, "fc_min"))) - db(
        h_lowpass(4000.0, *_rc(bundle, "fc_max"))
    )
    assert abs(db(prev[1] / cur[1]) - expected) <= 0.5
    deltas = {round(t["frequency_hz"]): t for t in bundle.comparison["tones"]}
    assert abs(deltas[4000]["delta_db"] + expected) <= 1e-3


def _rc(bundle: Bundle, name: str) -> tuple[float, float]:
    return bundle.runs[name].r, bundle.runs[name].c


@pytest.mark.ngspice
def test_spectrum_peaks_match_fitted_tone_levels(bundle: Bundle) -> None:
    for run in bundle.runs.values():
        sp = run.record["spectrum"]
        assert math.isclose(sp["resolution_hz"], FS / (46 * PERIOD_N), rel_tol=1e-12)
        assert len(sp["frequency_hz"]) <= 480
        assert all(20.0 - 1e-9 <= f <= 12_000.0 + 1e-9 for f in sp["frequency_hz"])
        assert min(sp["input_dbv"] + sp["output_dbv"]) >= -120.0
        for tone in run.record["tones"]:
            idx = [
                i for i, f in enumerate(sp["frequency_hz"]) if abs(f - tone["frequency_hz"]) < 1e-6
            ]
            assert len(idx) == 1, (run.name, tone["frequency_hz"])
            i = idx[0]
            assert abs(sp["output_dbv"][i] - db(tone["output_amplitude_v"] / math.sqrt(2))) <= 0.01
            assert abs(sp["input_dbv"][i] - db(tone["input_amplitude_v"] / math.sqrt(2))) <= 0.01


@pytest.mark.ngspice
def test_goal_pass_fail_follows_measured_gain(bundle: Bundle) -> None:
    """Default goal (440 Hz ≤ 1 dB loss, 4 kHz ≥ 12 dB loss): each constraint is judged on the
    measured gain at exactly its frequency and cites it; the straddling designs come out as the
    independent formula says."""
    for run in bundle.runs.values():
        goal = run.record["goal"]
        assert goal is not None and goal["goal_id"] == bundle.goal["id"]
        verdicts = []
        for res in goal["results"]:
            c = res["constraint"]
            exact = db(h_lowpass(c["frequency_hz"], run.r, run.c))
            assert abs(res["measured_gain_db"] - exact) <= 1e-4
            attenuation = -res["measured_gain_db"]
            ok = (
                attenuation <= c["limit_db"]
                if c["kind"] == "preserve"
                else attenuation >= c["limit_db"]
            )
            assert res["passed"] is ok
            assert res["evidence"] == {
                "result_id": run.record["id"],
                "quantity": "output_gain_db",
                "frequency_hz": c["frequency_hz"],
            }
            verdicts.append(ok)
        assert goal["passed"] is all(verdicts)
        if run.name in EXPECTED_STRADDLE:
            assert tuple(verdicts) == EXPECTED_STRADDLE[run.name], run.name


def _default_interval() -> tuple[float, float]:
    lo = 440.0 / math.sqrt(10 ** (1.0 / 10) - 1)
    hi = 4000.0 / math.sqrt(10 ** (12.0 / 10) - 1)
    return lo, hi


@pytest.mark.ngspice
def test_default_goal_feasibility_equals_analytical_interval(bundle: Bundle) -> None:
    lo, hi = _default_interval()  # 864.6998 Hz, 1038.0359 Hz
    feas = bundle.workbench["feasibility"]
    assert feas["feasible"] is True and feas["basis"] == "analytical_first_order_rc"
    assert feas["cutoff_range_hz"][0] == pytest.approx(lo, rel=1e-9)
    assert feas["cutoff_range_hz"][1] == pytest.approx(hi, rel=1e-9)
    assert feas["explanation"].startswith("Prediction:")
    for name in STRADDLE_FC:
        run = bundle.runs[name]
        inside = lo <= fc_of(run.r, run.c) <= hi
        assert run.record["goal"]["passed"] is inside


# ======================================================================================
# 4b. Feasibility edge cases (no simulation)
# ======================================================================================


@pytest.fixture(scope="module")
def plain_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(tmp_path_factory.mktemp("verif_c_plain")))) as client:
        client.get("/api/health")
        yield client


def _put_goal(client: TestClient, constraints: list[tuple[str, float, float]]) -> dict[str, Any]:
    resp = client.put(
        "/api/workbench/goal",
        json={
            "title": "verification goal",
            "constraints": [
                {"kind": k, "frequency_hz": f, "limit_db": lim, "label": f"c{i}"}
                for i, (k, f, lim) in enumerate(constraints)
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    return strict_json(resp.text)["feasibility"]


def test_contradictory_goals_are_reported_infeasible(plain_client: TestClient) -> None:
    inverted = _put_goal(plain_client, [("preserve", 4000, 1), ("attenuate", 440, 12)])
    assert inverted["feasible"] is False and inverted["cutoff_range_hz"] is None
    tight = _put_goal(plain_client, [("preserve", 1000, 0.5), ("attenuate", 2000, 6)])
    assert tight["feasible"] is False
    # Analytical: preserve needs fc ≥ 1000/√(10^0.05−1) = 2862 Hz; attenuate needs
    # fc ≤ 2000/√(10^0.6−1) = 1155 Hz.
    assert 1000 / math.sqrt(10**0.05 - 1) > 2000 / math.sqrt(10**0.6 - 1)


def test_feasibility_respects_the_supported_cutoff_range(plain_client: TestClient) -> None:
    """A goal that only a cutoff outside 20 Hz–20 kHz could meet cannot be met by any design this
    lab accepts (revision creation rejects such cutoffs), so it must not be reported feasible."""
    high = _put_goal(plain_client, [("preserve", 19_000, 0.1)])  # needs fc ≥ 124.5 kHz
    low = _put_goal(plain_client, [("attenuate", 20, 60)])  # needs fc ≤ 0.02 Hz
    for feas in (high, low):
        rng = feas["cutoff_range_hz"]
        assert feas["feasible"] is False or (rng[1] >= 20.0 and rng[0] <= 20_000.0), feas


# ======================================================================================
# 5. Units parser and netlist safety through the public API (no simulation)
# ======================================================================================

# (component, text, expected SI value or None = rejected with a field-named 422)
UNIT_CASES: list[tuple[str, str, float | None]] = [
    ("R1", "1k", 1e3),
    ("R1", " 1 k ", 1e3),
    ("R1", "1kΩ", 1e3),  # U+03A9 GREEK CAPITAL OMEGA
    ("R1", "1kΩ", 1e3),  # U+2126 OHM SIGN
    ("R1", "1 kohm", 1e3),
    ("R1", "1KOHMS", 1e3),
    ("R1", "4k7", 4.7e3),
    ("R1", "1K5", 1.5e3),
    ("R1", "100R", 100.0),
    ("R1", "1e3", 1e3),
    ("R1", "+2.2 k", 2.2e3),
    ("R1", "100", 100.0),
    ("R1", "4R7", None),  # 4.7 Ω, below bounds
    ("R1", "1M", None),  # mega, above bounds (not SPICE milli)
    ("R1", "1m", None),  # milli
    ("R1", "1meg", None),
    ("R1", "1f", None),
    ("R1", "1fF", None),  # wrong unit kind
    ("R1", "100 nF", None),
    ("R1", "1kk", None),
    ("R1", "1e3k", None),
    ("R1", "1,5k", None),
    ("R1", "1.5.3k", None),
    ("R1", "-1k", None),
    ("R1", "0", None),
    ("R1", "NaN", None),
    ("R1", "inf", None),
    ("R1", "1e400", None),
    ("R1", "1e-400", None),
    ("R1", "0x10", None),
    ("R1", "１ｋ", None),  # fullwidth digits
    ("R1", "1k​", None),
    ("R1", "1k\n.control", None),
    ("R1", "1k\nV2 in 0 1", None),
    ("R1", "1k .include x", None),
    ("R1", ".include /etc/passwd", None),
    ("R1", "1k;rm -rf /", None),
    ("R1", "$(id)", None),
    ("R1", "{1k}", None),
    ("R1", "1k\x00", None),
    ("R1", "", None),
    ("R1", "1" * 40, None),
    ("R1", "100k", None),  # in bounds but fc = 15.9 Hz with C1 = 100 nF (joint constraint)
    ("C1", "100n", 100e-9),
    ("C1", "100 nF", 100e-9),
    ("C1", "0.1u", 100e-9),
    ("C1", "0.1µF", 100e-9),  # U+00B5 MICRO SIGN
    ("C1", "0.1μF", 100e-9),  # U+03BC GREEK SMALL LETTER MU
    ("C1", "4µ7", 4.7e-6),
    ("C1", "1e-7F", 100e-9),
    ("C1", "47 nF", 47e-9),
    ("C1", "100f", None),  # ambiguous femto/farad
    ("C1", "100nf", None),  # lowercase f = femto
    ("C1", "100", None),  # 100 F
    ("C1", "1M", None),
    ("C1", "1mF", None),
    ("C1", "999p", None),
    ("C1", "2N2", None),  # prefixes are case-sensitive
    ("C1", "4R7", None),
    ("C1", "100nΩ", None),
    ("C1", "100n\n.end", None),
    ("C1", "100n .tran 1 1", None),
]

_NUM = r"-?\d+(\.\d+)?(e[+-]?\d+)?"
_NETLIST_LINE = re.compile(
    "|".join(
        (
            r"\* resonance trusted netlist",
            rf"V1 in 0 DC 0 AC {_NUM}",
            rf"V1[a-d] (in|v1_m\d) (v1_m\d|0) DC 0 SIN\(0 {_NUM} {_NUM} 0 0 0\)",
            rf"R1 in out {_NUM}",
            rf"C1 out 0 {_NUM}",
            r"\.control",
            r"set filetype=binary",
            rf"ac dec \d+ {_NUM} {_NUM}",
            rf"tran {_NUM} {_NUM} 0 {_NUM}",
            r"write result\.raw( (v\(in\)|v\(out\)|i\(V1a?\)))+",
            r"\.endc",
            r"\.end",
        )
    )
)


@pytest.mark.parametrize(("component", "text", "expected"), UNIT_CASES)
def test_units_battery_through_the_public_api(
    plain_client: TestClient, component: str, text: str, expected: float | None
) -> None:
    base = plain_client.post("/api/revisions", json={"template_id": "rc_lowpass"}).json()
    resp = plain_client.post(
        "/api/revisions",
        json={"parent_id": base["id"], "values": [{"component_id": component, "value": text}]},
    )
    body = strict_json(resp.text)
    if expected is None:
        assert resp.status_code == 422, body
        assert body["error"]["code"] == "validation_error"
        assert body["error"]["field"] == "values.0.value"
        assert body["error"]["message"]
        return
    assert resp.status_code == 201, body
    rev = Revision.model_validate(body)
    (comp,) = [c for c in rev.components if c.id == component]
    assert comp.value_si == pytest.approx(expected, rel=1e-12)
    parsed = plain_client.post("/api/units/parse", json={"component_id": component, "text": text})
    assert parsed.status_code == 200 and parsed.json()["ok"] is True
    assert parsed.json()["value_si"] == pytest.approx(expected, rel=1e-12)
    # What reaches ngspice: only fixed tokens and repr(float) numbers.
    circuit = revision_to_circuit(rev)
    for compiled in (
        compile_ac(circuit, AcSweep(start_hz=10.0, stop_hz=1e5, points_per_decade=40)),
        compile_transient(
            circuit,
            TransientRequest(stop_s=1.4, max_step_s=1e-5, probes=("v(in)", "v(out)", "i(V1)")),
        ),
    ):
        lines = compiled.text.splitlines()
        assert all(_NETLIST_LINE.fullmatch(line) for line in lines), compiled.text
        assert (
            f"{component} {'in out' if component == 'R1' else 'out 0'} {comp.value_si!r}" in lines
        )


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e400", "-1e3", "0", "1e-320"])
def test_non_finite_and_non_positive_json_numbers_are_rejected(
    plain_client: TestClient, raw: str
) -> None:
    base = plain_client.post("/api/revisions", json={"template_id": "rc_lowpass"}).json()
    body = '{{"parent_id": "{}", "values": [{{"component_id": "R1", "value": {}}}]}}'.format(
        base["id"],
        raw,
    )
    resp = plain_client.post(
        "/api/revisions", content=body, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 422
    err = strict_json(resp.text)["error"]
    assert err["code"] == "validation_error" and err["field"] == "values.0.value"


def test_json_boolean_is_not_read_as_the_number_one(plain_client: TestClient) -> None:
    base = plain_client.post("/api/revisions", json={"template_id": "rc_lowpass"}).json()
    resp = plain_client.post(
        "/api/revisions",
        json={"parent_id": base["id"], "values": [{"component_id": "R1", "value": True}]},
    )
    assert resp.status_code == 422
    assert "1 Ω" not in resp.json()["error"]["message"], resp.json()["error"]["message"]


# ======================================================================================
# Over-range rendering (only reachable through the renderer)
# ======================================================================================


def test_over_range_comparison_gets_one_shared_disclosed_reduction(tmp_path: Path) -> None:
    """A passive RC driven by the fixed 0.9 V-peak source never exceeds full scale (all corner
    clips above peak ≤ 0.9 FS), so the over-range path is exercised through the renderer: one
    reduction for every clip, disclosed, relative levels preserved in the decoded PCM, no
    saturated samples."""
    store = DocumentStore(tmp_path / "db.sqlite3")
    artifacts = ArtifactStore(tmp_path / "art", store, max_count=50, max_bytes=50 << 20)
    try:
        t = SETTLE_S + np.arange(CLIP_N) / FS
        loud = 1.6 * np.sin(TWO_PI * 440.0 * t)
        quiet = 0.2 * np.sin(TWO_PI * 440.0 * t)
        cmp_ = render_comparison(
            [
                ClipSource("expected", "Loud clip", loud, None),
                ClipSource("observed", "Quiet clip", quiet, None),
            ],
            artifacts,
            "sess_verifC_render",
        )
        expected_adj = 20 * math.log10(0.95 / 1.6)
        assert cmp_.gain_adjustment_db == pytest.approx(expected_adj, abs=1e-6)
        assert "reduced together by 4.5 dB" in cmp_.note and "Loud clip" in cmp_.note
        decoded = []
        for clip in cmp_.clips:
            opened = artifacts.open(clip.artifact_id, "sess_verifC_render")
            assert opened is not None
            wav = decode_wav(opened[0].read_bytes())
            assert np.max(np.abs(wav.pcm)) < 32767  # nothing saturated
            assert clip.clipped is False
            decoded.append(amplitudes(fit_pcm(wav.pcm))[0])
        assert db(decoded[0] / decoded[1]) == pytest.approx(db(1.6 / 0.2), abs=0.01)
        factor = 10 ** (expected_adj / 20)
        assert decoded[0] / 32767 == pytest.approx(1.6 * factor, rel=1e-3)
        assert decoded[1] / 32767 == pytest.approx(0.2 * factor, rel=1e-3)
        within = render_comparison(
            [ClipSource("input", "In", 0.99 * np.sin(TWO_PI * 440.0 * t), None)],
            artifacts,
            "sess_verifC_render",
        )
        assert within.gain_adjustment_db == 0.0
    finally:
        store.close()


# ======================================================================================
# 6. Hypothesis evaluation
# ======================================================================================


def _preds(view: str, r: float, c: float) -> list[tuple[str, float, float]]:
    """(kind, value, declared tolerance) per measured quantity, from the policy text."""
    out: list[tuple[str, float, float]] = []

    def cur_tol(x: float) -> float:
        return (
            policy.EVIDENCE_CURRENT_ABS_TOLERANCE_MA
            + policy.EVIDENCE_CURRENT_TOLERANCE_PCT / 100 * abs(x)
        )

    if view in ("v_sweep", "i_sweep"):
        for f in CASE_GRID:
            if view == "v_sweep":
                z = h_lowpass(f, r, c)
                out += [
                    ("db", db(z), policy.EVIDENCE_GAIN_TOLERANCE_DB),
                    ("deg", deg(z), policy.EVIDENCE_PHASE_TOLERANCE_DEG),
                ]
            else:
                ma = 1e3 * i_delivered(f, r, c)
                out += [
                    ("ma", abs(ma), cur_tol(abs(ma))),
                    ("deg", deg(ma), policy.EVIDENCE_PHASE_TOLERANCE_DEG),
                ]
    else:
        for f, a in TONES:
            if view == "t_volt":
                z = h_lowpass(f, r, c)
                out += [
                    ("db", db(z), policy.EVIDENCE_TONE_GAIN_TOLERANCE_DB),
                    ("deg", deg(z), policy.EVIDENCE_TONE_PHASE_TOLERANCE_DEG),
                ]
            else:
                ma = 1e3 * a * i_delivered(f, r, c)
                out += [
                    ("ma", abs(ma), cur_tol(abs(ma))),
                    ("deg", deg(ma), policy.EVIDENCE_TONE_PHASE_TOLERANCE_DEG),
                ]
    return out


def _dev(kind: str, a: float, b: float) -> float:
    return angle_diff(a, b) if kind == "deg" else abs(a - b)


def _disjoint(pa: list[tuple[str, float, float]], pb: list[tuple[str, float, float]]) -> bool:
    return any(_dev(k, x, y) > ta + tb for (k, x, ta), (_, y, tb) in zip(pa, pb, strict=True))


def _partition(view: str, r0: float, c0: float) -> tuple[tuple[str, ...], ...]:
    preds = {h: _preds(view, r0 * mr, c0 * mc) for h, (mr, mc) in VARIANTS.items()}
    groups: list[list[str]] = []
    for h in VARIANTS:
        for g in groups:
            if not _disjoint(preds[h], preds[g[0]]):
                g.append(h)
                break
        else:
            groups.append([h])
    # Non-transitive overlaps would make the partition order-dependent: require none.
    for g in groups:
        for a, b in combinations(g, 2):
            assert not _disjoint(preds[a], preds[b])
    return tuple(tuple(g) for g in groups)


VIEWS = ("v_sweep", "i_sweep", "t_volt", "t_curr")
SINGLETONS = tuple((h,) for h in VARIANTS)


def test_reference_partition_for_every_drawable_design() -> None:
    """Independent of the service: for every drawable nominal design, voltage instruments group
    the hypotheses exactly as {N}, {R+,C+}, {R−,C−} and both current instruments separate all
    five (non-overlapping consistency bands), as docs/contracts.md documents."""
    designs = faults.nominal_designs()
    assert len(designs) >= 20
    for r0, c0 in designs:
        assert 40.0 <= fc_of(r0, c0) <= 5000.0
        for view in VIEWS:
            expected = VOLTAGE_GROUPS if view in ("v_sweep", "t_volt") else SINGLETONS
            assert _partition(view, r0, c0) == expected, (r0, c0, view)


CASE_ID = new_id("case")


def _synthetic(view: str, r: float, c: float, scale: float) -> Observation:
    """An observation of (r, c) offset by ±scale x tolerance (alternating sign per quantity)."""
    preds = _preds(view, r, c)
    vals = [v + (scale if i % 4 < 2 else -scale) * tol for i, (_, v, tol) in enumerate(preds)]
    common: dict[str, Any] = dict(
        id=new_id("obs"),
        case_id=CASE_ID,
        sequence=1,
        counted=True,
        requested_by="user",
        status="succeeded",
        created_at=datetime.now(UTC),
        simulator=SimulatorInfo(version="synthetic"),
        settings=ObservationSettings(),
        summary="synthetic verification observation",
    )
    if view in ("v_sweep", "i_sweep"):
        sweep = SweepObservation(
            quantity="output_gain" if view == "v_sweep" else "source_current",
            frequency_hz=CASE_GRID,
            magnitude=vals[0::2],
            magnitude_unit="dB" if view == "v_sweep" else "mA",
            phase_deg=vals[1::2],
            expected_magnitude=vals[0::2],
            expected_phase_deg=vals[1::2],
        )
        kind = "output_voltage_sweep" if view == "v_sweep" else "source_current_sweep"
        return Observation(kind=kind, sweep=sweep, **common)
    voltage = view == "t_volt"
    tones = []
    for k, (tone, (_, amp)) in enumerate(zip(policy.DEFAULT_TONES, TONES, strict=True)):
        mag, ph = vals[2 * k], vals[2 * k + 1]
        tones.append(
            ObservedTone(
                frequency_hz=tone.frequency_hz,
                label=tone.label,
                role=tone.role,
                amplitude=amp * 10 ** (mag / 20) if voltage else mag,
                amplitude_unit="V" if voltage else "mA",
                gain_db=mag if voltage else None,
                phase_deg=ph,
                expected_amplitude=1.0,
                expected_gain_db=0.0 if voltage else None,
                expected_phase_deg=0.0,
            )
        )
    transient = TransientObservation(
        probe="output_voltage" if voltage else "source_current",
        tones=tones,
        waveform=ObservedWaveform(
            sample_rate_hz=FS,
            start_s=0.0,
            unit="V" if voltage else "mA",
            input_v=[0.0],
            observed=[0.0],
            expected=[0.0],
        ),
    )
    return Observation(
        kind="transient_capture", probe=transient.probe, transient=transient, **common
    )


def test_service_assessment_agrees_with_reference_on_every_design() -> None:
    """For every drawable design, truth, and instrument: an observation 0.9 tolerance away from
    the truth keeps the truth consistent and leaves exactly the reference group; equivalent
    hypotheses are never split; status is supported only for a singleton."""
    for r0, c0 in faults.nominal_designs():
        nominal = {"R1": r0, "C1": c0}
        for truth, (mr, mc) in VARIANTS.items():
            for view in VIEWS:
                obs = _synthetic(view, r0 * mr, c0 * mc, 0.9)
                state = hypotheses.assess(
                    "rc_lowpass", nominal, [obs], publish_distinguishing=False
                )
                group = (
                    next(g for g in VOLTAGE_GROUPS if truth in g)
                    if view in ("v_sweep", "t_volt")
                    else (truth,)
                )
                assert tuple(state.remaining) == group, (r0, c0, truth, view, state.remaining)
                status = {a.hypothesis_id: a.status for a in state.assessments}
                assert status[truth] == ("supported" if len(group) == 1 else "unresolved")
                assert all(status[h] == "contradicted" for h in VARIANTS if h not in group)


@pytest.mark.parametrize("design", [(3300.0, 470e-9), (470.0, 68e-9), (2200.0, 47e-9)])
def test_deviation_just_beyond_tolerance_contradicts(design: tuple[float, float]) -> None:
    r0, c0 = design
    for view in VIEWS:
        obs = _synthetic(view, r0, c0, 1.02)
        state = hypotheses.assess(
            "rc_lowpass", {"R1": r0, "C1": c0}, [obs], publish_distinguishing=False
        )
        status = {a.hypothesis_id: a.status for a in state.assessments}
        assert status["no_fault"] == "contradicted", (design, view)


# --- Real ngspice cases at the extreme drawable designs --------------------------------

LOW_FC = (3300.0, 470e-9)  # fc 102.6 Hz: lowest drawable cutoff; thinnest transient-current margin
HIGH_FC = (470.0, 68e-9)  # fc 4.98 kHz: highest drawable cutoff (R−/C− variants at ~10 kHz)
CASE_SCENARIOS = [
    # (nominal, truth, experiments, repair component, repair should restore)
    (LOW_FC, "no_fault", [("transient_capture", "source_current")], "R1", True),
    (LOW_FC, "c1_increased", [("transient_capture", "source_current")], "R1", False),
    (
        HIGH_FC,
        "r1_decreased",
        [("output_voltage_sweep", None), ("source_current_sweep", None)],
        "R1",
        True,
    ),
    (HIGH_FC, "c1_decreased", [("transient_capture", "source_current")], "C1", True),
]


def _assert_audio_matches_tones(
    rig: Any, session: str, audio: Any, tones: list[ObservedTone], observed_role: str
) -> None:
    """Case audio honesty: decoded Expected/Observed (or Repaired) PCM levels equal the reported
    amplitudes x one shared gain, and their ratio equals the reported gain difference."""
    assert audio is not None
    gain = audio.playback_gain_fs_per_v
    assert gain == policy.PLAYBACK_GAIN_FS_PER_V and audio.gain_adjustment_db == 0.0
    by_role = {}
    for clip in audio.clips:
        opened = rig.artifacts.open(clip.artifact_id, session)
        assert opened is not None
        wav = decode_wav(opened[0].read_bytes())
        assert (wav.channels, wav.sample_width, wav.rate, wav.frames) == (1, 2, FS, CLIP_N)
        by_role[clip.role] = amplitudes(fit_pcm(wav.pcm))
    assert set(by_role) == {"expected", observed_role}
    for k, tone in enumerate(tones):
        assert abs(db(by_role[observed_role][k] / 32767 / (gain * tone.amplitude))) <= PCM_TOL_DB
        assert (
            abs(db(by_role["expected"][k] / 32767 / (gain * tone.expected_amplitude))) <= PCM_TOL_DB
        )
        assert tone.gain_db is not None and tone.expected_gain_db is not None
        pcm_rel = db(by_role[observed_role][k] / by_role["expected"][k])
        assert abs(pcm_rel - (tone.gain_db - tone.expected_gain_db)) <= PCM_TOL_DB


def _truth_margin(case: PublicCase, truth: str) -> float:
    (assessment,) = [a for a in case.evidence.assessments if a.hypothesis_id == truth]
    assert assessment.status != "contradicted"
    return max((c.deviation / c.tolerance for c in assessment.checks), default=0.0)


@pytest.mark.ngspice
@pytest.mark.parametrize(("nominal", "truth", "plan", "repair", "restores"), CASE_SCENARIOS)
async def test_real_cases_at_extreme_designs(
    tmp_path: Path,
    nominal: tuple[float, float],
    truth: str,
    plan: list[tuple[str, str | None]],
    repair: str,
    restores: bool,
) -> None:
    settings = _settings(tmp_path)
    settings.resonance_data_dir.mkdir(parents=True, exist_ok=True)
    simulator = create_simulator(settings)
    simulator.info()
    rig = build_rig(settings, simulator)
    session = "sess_verifC_" + "c" * 24
    try:
        svc = rig.service
        case = await svc.harness_create_case(session, seed_for(truth, nominal))  # type: ignore[arg-type]
        r0, c0 = nominal
        assert (
            case.intended_revision.components[1].value_si,
            case.intended_revision.components[2].value_si,
        ) == nominal
        group = next(g for g in VOLTAGE_GROUPS if truth in g)
        assert (
            tuple(case.evidence.remaining) == group
        )  # symptom: never split, truth never contradicted
        margins = [_truth_margin(case, truth)]
        symptom = case.observations[0].transient
        assert symptom is not None
        _assert_audio_matches_tones(rig, session, symptom.audio, symptom.tones, "observed")
        await svc.set_mode(session, case.id, "guide")
        last = None
        for j, (instrument, probe) in enumerate(plan):
            req = CaseExperimentRequest.model_validate(
                {
                    "instrument": instrument,
                    "probe": probe,
                    "client_request_id": f"verifC-{j}-{truth}",
                }
            )
            resp = await svc.run_experiment(session, case.id, req, requested_by="user")
            last = resp
            margins.append(_truth_margin(resp.case, truth))
            sweep = resp.observation.sweep
            if sweep is not None:  # reference (intended design) values vs the formulas
                for f, m, p in zip(
                    sweep.frequency_hz,
                    sweep.expected_magnitude,
                    sweep.expected_phase_deg,
                    strict=True,
                ):
                    if sweep.quantity == "output_gain":
                        z = h_lowpass(f, r0, c0)
                        assert abs(m - db(z)) <= 20 * math.log10(1 + GAIN_REL_TOL)
                    else:
                        z = 1e3 * i_delivered(f, r0, c0)
                        assert abs(m - abs(z)) <= GAIN_REL_TOL * abs(z)
                    assert angle_diff(p, deg(z)) <= PHASE_TOL_DEG
            if instrument == "output_voltage_sweep":
                assert tuple(resp.case.evidence.remaining) == group  # voltage never splits R from C
        assert last is not None
        assert last.case.evidence.remaining == [truth]
        assert {a.hypothesis_id: a.status for a in last.case.evidence.assessments}[
            truth
        ] == "supported"
        assert max(margins) <= 0.25, margins  # ngspice + fit error sits well inside tolerance
        quantity = "source_current_ma" if last.observation.sweep is not None else "tone_current_ma"
        submission = DiagnosisSubmission(
            conclusion="no_fault" if truth == "no_fault" else "fault",
            hypothesis_id=None if truth == "no_fault" else truth,  # type: ignore[arg-type]
            citations=[EvidenceCitation(observation_id=last.observation.id, quantity=quantity)],  # type: ignore[arg-type]
            explanation="verification lens C",
        )
        case = await svc.submit_diagnosis(session, case.id, submission, submitted_by="user")
        assert case.diagnosis is not None and case.diagnosis.evidence_check.status == "supported"
        await svc.propose_repair(
            session, case.id, RepairProposalInput(component_id=repair), proposed_by="user"
        )  # type: ignore[arg-type]
        case = await svc.verify_repair(session, case.id)
        rep = case.repair
        assert rep is not None and rep.functional_recovery is restores
        # Independent expectation: only the proposed component returns to nominal.
        mr, mc = VARIANTS[truth]
        r_rep, c_rep = (r0, c0 * mc) if repair == "R1" else (r0 * mr, c0)
        gain_dev = max(
            abs(db(h_lowpass(f, r_rep, c_rep)) - db(h_lowpass(f, r0, c0))) for f in CASE_GRID
        )
        phase_dev = max(
            angle_diff(deg(h_lowpass(f, r_rep, c_rep)), deg(h_lowpass(f, r0, c0)))
            for f in CASE_GRID
        )
        assert rep.max_gain_deviation_db == pytest.approx(gain_dev, abs=0.01)
        assert rep.max_phase_deviation_deg == pytest.approx(phase_dev, abs=0.05)
        assert any(math.isclose(rep.worst_frequency_hz, f, rel_tol=1e-6) for f in CASE_GRID)
        assert ("does not match" in rep.summary) is (not restores)
        _assert_audio_matches_tones(rig, session, rep.audio, rep.tones, "repaired")
        closed = await svc.close_case(session, case.id)
        assert closed.reveal is not None
        assert closed.reveal.outcome.exact_identification == "correct"
        expected_recovery = (
            "not_needed" if truth == "no_fault" else ("restored" if restores else "not_restored")
        )
        assert closed.reveal.outcome.functional_recovery == expected_recovery
        print(
            f"\n[C6] {nominal} {truth}: truth worst dev/tol {max(margins):.3f}; repair {repair} "
            f"recovered={rep.functional_recovery} max dev {rep.max_gain_deviation_db:.4f} dB "
            f"(formula {gain_dev:.4f}), {rep.max_phase_deviation_deg:.3f} deg "
            f"(formula {phase_dev:.3f})"
        )
    finally:
        rig.service.close()
        rig.store.close()
