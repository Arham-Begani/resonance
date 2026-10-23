"""Learn attempts end to end through FastAPI with real ngspice, for both circuit families.

Grades are checked against the recorded experiments (fetched separately) and against an
independent closed-form estimate of each tone's gain change. Failure and cancellation tests
inject faults into an otherwise real simulator and require that nothing is persisted.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient

from resonance.api.deps import get_simulator
from resonance.api.errors import ApiException
from resonance.app import create_app
from resonance.circuits.revisions import build_revision
from resonance.config import Settings
from resonance.contracts.circuits import Revision
from resonance.contracts.common import ErrorCode, SimulatorInfo
from resonance.contracts.experiments import ExperimentRecord
from resonance.contracts.internal import (
    AcData,
    AcPoints,
    AcSweep,
    CancelToken,
    CircuitSpec,
    SimulationFailed,
    Simulator,
    TransientData,
    TransientRequest,
)
from resonance.contracts.learn import LearnAttempt, LearnAttemptRequest
from resonance.contracts.policy import LEARN_SAME_THRESHOLD_DB
from resonance.explore.learn import run_learn_attempt
from resonance.simulation import create_simulator
from resonance.store.db import DocumentStore

pytestmark = pytest.mark.ngspice


@dataclass
class Lab:
    client: TestClient
    session: str

    @property
    def store(self) -> DocumentStore:
        store: DocumentStore = self.client.app.state.store  # type: ignore[attr-defined]
        return store

    def counts(self) -> tuple[int, int, int]:
        return (
            len(self.store.list_docs("revision", self.session, Revision, limit=1000)),
            len(self.store.list_docs("experiment", self.session, ExperimentRecord, limit=1000)),
            len(self.store.list_docs("learn_attempt", self.session, LearnAttempt, limit=1000)),
        )


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Lab]:
    root: Path = tmp_path_factory.mktemp("learn_api")
    settings = Settings(
        _env_file=None,
        resonance_data_dir=root / "data",
        web_dist_dir=root / "no-web",
        nebius_api_key=None,
        nebius_model=None,
        resonance_dev_agent_fixture=False,
    )
    with TestClient(create_app(settings)) as client:
        client.get("/api/health")  # receive the session cookie before anything else
        yield Lab(client, client.cookies["rs_session"])


def _attenuation(template: str, f: float, r: float, c: float) -> float:
    fc = 1 / (2 * math.pi * r * c)
    ratio = f / fc if template == "rc_lowpass" else fc / f
    return 10 * math.log10(1 + ratio * ratio)


def _attempt_body(
    revision_id: str, component: str, multiplier: float, expectations: dict[float, str]
) -> dict[str, Any]:
    return {
        "revision_id": revision_id,
        "change": {"component_id": component, "multiplier": multiplier},
        "predictions": [{"frequency_hz": f, "expectation": e} for f, e in expectations.items()],
    }


def _check_graded_against_records(
    lab: Lab, attempt: dict[str, Any], r: tuple[float, float], c: tuple[float, float]
) -> None:
    """Every grade comes from the two recorded experiments' exact-frequency AC gains."""
    client = lab.client
    base = client.get(f"/api/experiments/{attempt['base_experiment_id']}").json()
    new = client.get(f"/api/experiments/{attempt['new_experiment_id']}").json()
    assert base["revision_id"] == attempt["base_revision_id"]
    assert new["revision_id"] == attempt["new_revision_id"]
    assert base["status"] == new["status"] == "succeeded"
    before = {t["frequency_hz"]: t["ac_gain_db"] for t in base["tones"]}
    after = {t["frequency_hz"]: t["ac_gain_db"] for t in new["tones"]}
    template = attempt["template_id"]
    for g in attempt["graded"]:
        f = g["frequency_hz"]
        assert g["delta_db"] == pytest.approx(after[f] - before[f], abs=1e-12)
        predicted_change = _attenuation(template, f, r[0], c[0]) - _attenuation(
            template, f, r[1], c[1]
        )
        assert g["delta_db"] == pytest.approx(predicted_change, abs=0.01)  # physics check
        if abs(g["delta_db"]) < LEARN_SAME_THRESHOLD_DB:
            assert g["observed"] == "about_the_same"
        else:
            assert g["observed"] == ("louder" if g["delta_db"] > 0 else "quieter")
        assert g["correct"] is (g["predicted"] == g["observed"])
        assert g["evidence"] == [
            {"result_id": base["id"], "quantity": "output_gain_db", "frequency_hz": f},
            {"result_id": new["id"], "quantity": "output_gain_db", "frequency_hz": f},
        ]
    assert attempt["score"] == sum(g["correct"] for g in attempt["graded"])
    assert attempt["total"] == len(attempt["graded"]) == len(base["tones"])
    for text in (attempt["explanation"], attempt["tradeoff"]):
        assert base["id"] in text and new["id"] in text
    cmp = attempt["comparison"]
    assert (cmp["a_id"], cmp["b_id"]) == (base["id"], new["id"])
    clips = cmp["audio"]["clips"]
    assert [c["role"] for c in clips] == ["previous", "output"]
    assert clips[0]["artifact_id"] == base["audio"]["clips"][1]["artifact_id"]
    assert clips[1]["artifact_id"] == new["audio"]["clips"][1]["artifact_id"]
    assert cmp["audio"]["gain_adjustment_db"] == 0.0
    print(
        f"\n{template} {attempt['change']['component_id']}x{attempt['change']['multiplier']:g}: "
        + "; ".join(
            f"{g['label']} delta {g['delta_db']:+.3f} dB predicted {g['predicted']} observed "
            f"{g['observed']} -> {'correct' if g['correct'] else 'incorrect'}"
            for g in attempt["graded"]
        )
        + f" (score {attempt['score']}/{attempt['total']})\n  {attempt['explanation']}\n  "
        f"{attempt['tradeoff']}"
    )


def test_lowpass_attempt_reuses_the_base_experiment_and_grades_measured_change(
    lab: Lab,
) -> None:
    client = lab.client
    rev = client.post("/api/revisions", json={"template_id": "rc_lowpass"}).json()
    goal_id = client.get("/api/workbench").json()["goal"]["id"]
    base = client.post("/api/experiments", json={"revision_id": rev["id"], "goal_id": goal_id})
    assert base.status_code == 201, base.text
    prompts = client.get("/api/learn/prompts", params={"revision_id": rev["id"]}).json()
    assert "C1x2" in [p["id"] for p in prompts]
    body = _attempt_body(rev["id"], "C1", 2.0, {4000.0: "quieter", 440.0: "quieter"})
    body["reasoning"] = "A bigger capacitor lowers the cutoff."
    response = client.post("/api/learn/attempts", json=body)
    assert response.status_code == 201, response.text
    attempt = response.json()
    assert attempt["id"].startswith("learn_")
    assert attempt["base_experiment_id"] == base.json()["id"]  # reused, not re-simulated
    assert attempt["predictions"] == body["predictions"]
    assert attempt["reasoning"] == body["reasoning"]
    graded = {g["frequency_hz"]: g for g in attempt["graded"]}
    # 4 kHz whistle: ≈ −5.55 dB (quieter, predicted correctly); 440 Hz tone: ≈ −0.84 dB, which
    # is under the 1 dB threshold, so "quieter" is graded incorrect.
    assert (graded[4000.0]["observed"], graded[4000.0]["correct"]) == ("quieter", True)
    assert (graded[440.0]["observed"], graded[440.0]["correct"]) == ("about_the_same", False)
    assert (attempt["score"], attempt["total"]) == (1, 2)
    assert "Your predictions: 1 of 2 matched the measurement" in attempt["explanation"]
    assert "you predicted quieter, measured about the same" in attempt["explanation"]
    _check_graded_against_records(lab, attempt, (1e3, 1e3), (100e-9, 200e-9))

    new_rev = client.get(f"/api/revisions/{attempt['new_revision_id']}").json()
    assert new_rev["parent_id"] == rev["id"] and new_rev["origin"] == "user_edit"
    assert [(c["component_id"], c["to_display"]) for c in new_rev["changes"]] == [("C1", "200 nF")]
    assert client.get(f"/api/learn/attempts/{attempt['id']}").json() == attempt

    # Another anonymous session sees none of it.
    client.cookies.clear()
    try:
        assert client.get(f"/api/learn/attempts/{attempt['id']}").status_code == 404
        other = client.get("/api/learn/prompts", params={"revision_id": rev["id"]})
        assert other.status_code == 404
        assert client.post("/api/learn/attempts", json=body).status_code == 404
    finally:
        client.cookies.clear()
        client.cookies.set("rs_session", lab.session)


def test_highpass_attempts_measure_a_fresh_base_then_reuse_it(lab: Lab) -> None:
    client = lab.client
    rev = client.post("/api/revisions", json={"template_id": "rc_highpass"}).json()
    before = lab.counts()
    first = client.post(
        "/api/learn/attempts",
        json=_attempt_body(rev["id"], "R1", 0.5, {50.0: "quieter", 440.0: "about_the_same"}),
    )
    assert first.status_code == 201, first.text
    a1 = first.json()
    # Base + changed experiment and the changed revision were stored with the attempt.
    assert lab.counts() == (before[0] + 1, before[1] + 2, before[2] + 1)
    graded = {g["frequency_hz"]: g for g in a1["graded"]}
    # fc 159 Hz → 318 Hz: hum ≈ −5.7 dB (quieter, correct); tone ≈ −1.29 dB, beyond the 1 dB
    # threshold, so "about the same" is graded incorrect.
    assert (graded[50.0]["observed"], graded[50.0]["correct"]) == ("quieter", True)
    assert (graded[440.0]["observed"], graded[440.0]["correct"]) == ("quieter", False)
    _check_graded_against_records(lab, a1, (10e3, 5e3), (100e-9, 100e-9))
    assert "Mains hum (50 Hz)" in a1["comparison"]["audio"]["note"]

    second = client.post(
        "/api/learn/attempts",
        json=_attempt_body(rev["id"], "C1", 2.0, {50.0: "louder", 440.0: "about_the_same"}),
    )
    assert second.status_code == 201, second.text
    a2 = second.json()
    assert a2["base_experiment_id"] == a1["base_experiment_id"]  # reused
    assert (a2["score"], a2["total"]) == (2, 2)
    _check_graded_against_records(lab, a2, (10e3, 10e3), (100e-9, 200e-9))


def test_invalid_requests_simulate_and_store_nothing(lab: Lab) -> None:
    client = lab.client
    rev = client.post("/api/revisions", json={"template_id": "rc_lowpass"}).json()
    before = lab.counts()
    wrong_tone = _attempt_body(rev["id"], "C1", 2.0, {440.0: "quieter", 50.0: "louder"})
    r = client.post("/api/learn/attempts", json=wrong_tone)
    assert r.status_code == 422 and r.json()["error"]["field"] == "predictions.1.frequency_hz"
    missing = _attempt_body(rev["id"], "C1", 2.0, {440.0: "quieter"})
    r = client.post("/api/learn/attempts", json=missing)
    assert r.status_code == 422 and r.json()["error"]["field"] == "predictions"
    bad_multiplier = _attempt_body(rev["id"], "C1", 3.0, {440.0: "quieter", 4000.0: "louder"})
    assert client.post("/api/learn/attempts", json=bad_multiplier).status_code == 422
    frozen = build_revision("rc_lowpass", {}, origin="case_intended", frozen=True)
    lab.store.put("revision", frozen.id, lab.session, frozen)
    refused = client.post(
        "/api/learn/attempts",
        json=_attempt_body(frozen.id, "C1", 2.0, {440.0: "quieter", 4000.0: "quieter"}),
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "stale_revision"
    prompts = client.get("/api/learn/prompts", params={"revision_id": frozen.id})
    assert prompts.status_code == 409
    assert client.get("/api/learn/attempts/learn_doesnotexist1").status_code == 404
    assert lab.counts() == (before[0] + 1, before[1], before[2])  # only the frozen fixture


class _InjectedFaultSimulator:
    """TEST DOUBLE around the REAL simulator: the second transient (the changed revision's
    capture) fails, or trips the request's cancel token, as a disconnect would."""

    def __init__(self, inner: Simulator, mode: Literal["fail", "cancel"]) -> None:
        self.inner, self.mode, self.transients = inner, mode, 0

    def info(self) -> SimulatorInfo:
        return self.inner.info()

    async def ac(
        self, circuit: CircuitSpec, sweep: AcSweep | AcPoints, *, cancel: CancelToken | None = None
    ) -> AcData:
        return await self.inner.ac(circuit, sweep, cancel=cancel)

    async def transient(
        self, circuit: CircuitSpec, request: TransientRequest, *, cancel: CancelToken | None = None
    ) -> TransientData:
        self.transients += 1
        if self.transients == 2:
            if self.mode == "fail":
                raise SimulationFailed("injected test failure")
            assert cancel is not None
            cancel.cancel()
        return await self.inner.transient(circuit, request, cancel=cancel)


def test_failed_attempt_leaves_nothing_half_recorded(lab: Lab) -> None:
    client = lab.client
    rev = client.post("/api/revisions", json={"template_id": "rc_highpass"}).json()
    before = lab.counts()
    faulty = _InjectedFaultSimulator(client.app.state.simulator, "fail")  # type: ignore[attr-defined]
    client.app.dependency_overrides[get_simulator] = lambda: faulty  # type: ignore[attr-defined]
    try:
        r = client.post(
            "/api/learn/attempts",
            json=_attempt_body(rev["id"], "C1", 2.0, {50.0: "louder", 440.0: "louder"}),
        )
    finally:
        client.app.dependency_overrides.clear()  # type: ignore[attr-defined]
    assert r.status_code == 502 and r.json()["error"]["code"] == "simulation_failed"
    assert "injected" not in r.text
    assert faulty.transients == 2  # the fresh base WAS measured, but is not kept either
    assert lab.counts() == before


async def test_cancelled_attempt_leaves_nothing_half_recorded(lab: Lab) -> None:
    app = lab.client.app
    revision = build_revision("rc_lowpass", {"R1": 2.2e3}, origin="user_edit")
    lab.store.put("revision", revision.id, lab.session, revision)
    before = lab.counts()
    body = LearnAttemptRequest.model_validate(
        _attempt_body(revision.id, "R1", 2.0, {440.0: "quieter", 4000.0: "quieter"})
    )
    token = CancelToken()
    with pytest.raises(ApiException) as info:
        await run_learn_attempt(
            # A simulator of its own: this test runs on pytest's event loop, not the client's.
            simulator=_InjectedFaultSimulator(create_simulator(app.state.settings), "cancel"),  # type: ignore[attr-defined]
            store=lab.store,
            artifacts=app.state.artifacts,  # type: ignore[attr-defined]
            session_id=lab.session,
            revision=revision,
            body=body,
            goal=None,
            cancel=token,
        )
    assert info.value.error.code == ErrorCode.CANCELLED
    assert lab.counts() == before
