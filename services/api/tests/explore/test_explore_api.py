"""Explore bundle end to end through FastAPI with real ngspice.

One module-scoped app runs two experiments (1 kΩ/100 nF and 1.6 kΩ/100 nF, both against the
default goal) that every test below inspects.
"""

from __future__ import annotations

import io
import math
import time
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from resonance.app import create_app
from resonance.circuits.revisions import build_revision
from resonance.config import Settings
from resonance.contracts.policy import (
    AUDIO_CLIP_S,
    AUDIO_FADE_MS,
    AUDIO_SAMPLE_RATE_HZ,
    EXPLORE_SWEEP_START_HZ,
    EXPLORE_SWEEP_STOP_HZ,
    PLAYBACK_GAIN_FS_PER_V,
    TRANSIENT_MAX_STEP_S,
    TRANSIENT_SETTLE_S,
    WAVEFORM_WINDOW_MAX_SAMPLES,
)

pytestmark = pytest.mark.ngspice


@dataclass
class Bundle:
    client: TestClient
    goal: dict[str, Any]
    rev_a: dict[str, Any]
    rev_b: dict[str, Any]
    exp_a: dict[str, Any]
    exp_b: dict[str, Any]
    wall_s: tuple[float, float]


def _settings(root: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        resonance_data_dir=root / "data",
        web_dist_dir=root / "no-web",
        nebius_api_key=None,
        nebius_model=None,
        resonance_dev_agent_fixture=False,
    )


def _run(client: TestClient, revision_id: str, goal_id: str | None) -> tuple[dict[str, Any], float]:
    body: dict[str, Any] = {"revision_id": revision_id}
    if goal_id:
        body["goal_id"] = goal_id
    start = time.perf_counter()
    response = client.post("/api/experiments", json=body)
    wall = time.perf_counter() - start
    assert response.status_code == 201, response.text
    return response.json(), wall


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Bundle]:
    with TestClient(create_app(_settings(tmp_path_factory.mktemp("explore_api")))) as client:
        goal = client.get("/api/workbench").json()["goal"]
        rev_a = client.post("/api/revisions", json={"template_id": "rc_lowpass"}).json()
        rev_b = client.post(
            "/api/revisions",
            json={"parent_id": rev_a["id"], "values": [{"component_id": "R1", "value": "1.6k"}]},
        ).json()
        exp_a, wall_a = _run(client, rev_a["id"], goal["id"])
        exp_b, wall_b = _run(client, rev_b["id"], goal["id"])
        print(
            f"\nexplore bundle wall time: {wall_a:.2f} s and {wall_b:.2f} s "
            f"(duration_ms {exp_a['duration_ms']} / {exp_b['duration_ms']})"
        )
        yield Bundle(client, goal, rev_a, rev_b, exp_a, exp_b, (wall_a, wall_b))


def _wav(client: TestClient, url: str) -> tuple[wave._wave_params, np.ndarray]:
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(response.content), "rb") as w:
        params = w.getparams()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    return params, data


def test_record_is_complete_and_internally_consistent(bundle: Bundle) -> None:
    rec = bundle.exp_a
    assert rec["status"] == "succeeded" and rec["revision_id"] == bundle.rev_a["id"]
    assert rec["simulator"]["name"] == "ngspice"
    tr = rec["settings"]["transient"]
    assert (tr["settle_s"], tr["clip_s"], tr["max_step_s"]) == (
        TRANSIENT_SETTLE_S,
        AUDIO_CLIP_S,
        TRANSIENT_MAX_STEP_S,
    )
    assert (tr["sample_rate_hz"], tr["fade_ms"]) == (AUDIO_SAMPLE_RATE_HZ, AUDIO_FADE_MS)
    resp = rec["response"]
    assert resp["frequency_hz"][0] == pytest.approx(EXPLORE_SWEEP_START_HZ)
    assert resp["frequency_hz"][-1] == pytest.approx(EXPLORE_SWEEP_STOP_HZ)
    assert len(resp["frequency_hz"]) == rec["settings"]["ac"]["points"] == 161
    assert all(-180 < p <= 180 for p in resp["phase_deg"])
    # 1 kΩ: |I| → 1 mA per volt well above the cutoff; phase → 0.
    assert resp["source_current_ma"][-1] == pytest.approx(1.0, rel=0.01)
    assert [t["frequency_hz"] for t in rec["tones"]] == [440.0, 4000.0]
    for tone in rec["tones"]:
        assert abs(tone["agreement_db"]) <= 0.5
    checks = {c["name"]: c for c in rec["checks"]}
    assert set(checks) == {
        "tone_gain_agreement",
        "tone_phase_agreement",
        "input_matches_source",
        "steady_state",
        "no_clipping",
    }
    assert all(c["passed"] for c in checks.values()), checks
    wf = rec["waveform"]
    assert len(wf["input_v"]) == len(wf["output_v"]) <= WAVEFORM_WINDOW_MAX_SAMPLES
    spec = rec["spectrum"]
    for tone in rec["tones"]:
        k = spec["frequency_hz"].index(tone["frequency_hz"])
        rms_dbv = 20 * math.log10(tone["output_amplitude_v"] / math.sqrt(2))
        assert spec["output_dbv"][k] == pytest.approx(rms_dbv, abs=0.01)


def test_clips_are_playable_and_share_one_gain(bundle: Bundle) -> None:
    audio = bundle.exp_a["audio"]
    assert audio["playback_gain_fs_per_v"] == PLAYBACK_GAIN_FS_PER_V
    assert audio["gain_adjustment_db"] == 0.0
    assert [c["role"] for c in audio["clips"]] == ["input", "output"]
    params_in, pcm_in = _wav(bundle.client, audio["clips"][0]["url"])
    params_out, pcm_out = _wav(bundle.client, audio["clips"][1]["url"])
    n = round(AUDIO_CLIP_S * AUDIO_SAMPLE_RATE_HZ)
    for params in (params_in, params_out):
        assert (params.nchannels, params.sampwidth, params.framerate, params.nframes) == (
            1,
            2,
            AUDIO_SAMPLE_RATE_HZ,
            n,
        )
    # Honest levels: the PCM loudness ratio equals the measured tone attenuation.
    tones = bundle.exp_a["tones"]
    expected_ratio = math.sqrt(sum(t["output_amplitude_v"] ** 2 for t in tones)) / math.sqrt(
        sum(t["input_amplitude_v"] ** 2 for t in tones)
    )
    fade = round(AUDIO_FADE_MS * AUDIO_SAMPLE_RATE_HZ / 1000)
    body = slice(fade, fade + 46 * 1200)  # whole 25 ms periods between the fades
    ratio = np.sqrt(np.mean(pcm_out[body] ** 2)) / np.sqrt(np.mean(pcm_in[body] ** 2))
    assert 20 * math.log10(ratio) == pytest.approx(20 * math.log10(expected_ratio), abs=0.01)


def test_goal_is_judged_on_measured_gain(bundle: Bundle) -> None:
    for rec, expect_pass in ((bundle.exp_a, (True, False)), (bundle.exp_b, (True, True))):
        goal = rec["goal"]
        assert goal["goal_id"] == bundle.goal["id"] and goal["experiment_id"] == rec["id"]
        measured = {t["frequency_hz"]: t["ac_gain_db"] for t in rec["tones"]}
        for result, passed in zip(goal["results"], expect_pass, strict=True):
            f = result["constraint"]["frequency_hz"]
            assert result["measured_gain_db"] == pytest.approx(measured[f])
            assert result["evidence"] == {
                "result_id": rec["id"],
                "quantity": "output_gain_db",
                "frequency_hz": f,
            }
            assert result["passed"] is passed
        assert goal["passed"] is all(expect_pass)
        print(
            f"\n{rec['revision_id'][:8]}...: "
            + "; ".join(
                f"{r['constraint']['frequency_hz']:g} Hz attenuation "
                f"{r['measured_attenuation_db']:.3f} dB (limit {r['constraint']['kind']} "
                f"{r['constraint']['limit_db']:g} dB, margin {r['margin_db']:+.3f})"
                for r in goal["results"]
            )
        )


def test_get_compare_and_session_scoping(bundle: Bundle) -> None:
    client = bundle.client
    a, b = bundle.exp_a, bundle.exp_b
    assert client.get(f"/api/experiments/{a['id']}").json() == a
    cmp = client.get("/api/experiments/compare", params={"a": a["id"], "b": b["id"]})
    assert cmp.status_code == 200, cmp.text
    data = cmp.json()
    assert data["changes"] == [
        {
            "component_id": "R1",
            "from_value_si": 1000.0,
            "to_value_si": 1600.0,
            "from_display": "1 kΩ",
            "to_display": "1.6 kΩ",
        }
    ]
    whistle = data["tones"][1]
    assert whistle["delta_db"] == pytest.approx(
        b["tones"][1]["ac_gain_db"] - a["tones"][1]["ac_gain_db"]
    )
    assert "Goal: not met → met." in data["summary"]
    clips = data["audio"]["clips"]
    assert [c["role"] for c in clips] == ["previous", "output"]
    assert clips[0]["artifact_id"] == a["audio"]["clips"][1]["artifact_id"]
    assert clips[1]["artifact_id"] == b["audio"]["clips"][1]["artifact_id"]
    assert "reused unchanged" in data["audio"]["note"]

    session = client.cookies.get("rs_session")
    assert session
    client.cookies.clear()
    try:  # a different anonymous session sees nothing
        assert client.get(f"/api/experiments/{a['id']}").status_code == 404
        assert client.get(a["audio"]["clips"][0]["url"]).status_code == 404
        other = client.get("/api/experiments/compare", params={"a": a["id"], "b": b["id"]})
        assert other.status_code == 404
        run = client.post("/api/experiments", json={"revision_id": bundle.rev_a["id"]})
        assert run.status_code == 404
    finally:
        client.cookies.clear()
        client.cookies.set("rs_session", session)


def test_request_errors(bundle: Bundle) -> None:
    client = bundle.client
    stale_goal = client.post(
        "/api/experiments",
        json={"revision_id": bundle.rev_a["id"], "goal_id": "goal_notthecurrentone"},
    )
    assert stale_goal.status_code == 409
    assert stale_goal.json()["error"] == {
        "code": "conflict",
        "message": "That goal is no longer the current goal. Reload the workbench and try again.",
        "field": "goal_id",
        "retryable": False,
    }
    missing = client.post("/api/experiments", json={"revision_id": "rev_doesnotexist1"})
    assert missing.status_code == 404
    assert client.get("/api/experiments/exp_doesnotexist1").status_code == 404
    # A frozen case design is never measured through explore.
    frozen = build_revision("rc_lowpass", {}, origin="case_intended", frozen=True)
    store = client.app.state.store  # type: ignore[attr-defined]
    store.put("revision", frozen.id, client.cookies.get("rs_session"), frozen)
    refused = client.post("/api/experiments", json={"revision_id": frozen.id})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "stale_revision"
