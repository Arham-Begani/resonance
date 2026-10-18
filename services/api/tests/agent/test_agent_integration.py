"""Agent runs against the REAL app, CaseService, evaluator and ngspice (provider scripted).

Checks the contract between the agent runtime and the investigation service: tools go through
the public CaseService only, nothing hidden reaches the model, and close stops the run before
the reveal. The model is a TEST-ONLY scripted provider; the case is real.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from agent_testkit import Hang, ScriptedProvider, completion, make_settings
from fastapi.testclient import TestClient

from resonance.agent.explore_tools import AppExplorePort
from resonance.agent.service import AgentService
from resonance.app import create_app
from resonance.config import resolve_ngspice_bin

pytestmark = pytest.mark.ngspice


@pytest.fixture
def real_app(tmp_path: Path) -> Iterator[tuple[TestClient, Any]]:
    settings = make_settings(tmp_path)
    if resolve_ngspice_bin(settings) is None and not os.environ.get("RESONANCE_REQUIRE_NGSPICE"):
        pytest.skip("ngspice is not available")
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app


def _use_provider(app: Any, provider: ScriptedProvider) -> AgentService:
    state = app.state
    service = AgentService(
        settings=state.settings,
        store=state.store,
        cases=state.case_service,
        explore=AppExplorePort(state),
        client_factory=provider.client_factory(),
    )
    state.case_service.register_run_canceller(service.cancel_and_wait)
    state.agent_service = service
    return service


def _wait(client: TestClient, run_id: str, limit_s: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        run: dict[str, Any] = client.get(f"/api/agent-runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_agent_investigates_a_real_case_without_seeing_hidden_state(real_app) -> None:
    client, app = real_app
    provider = ScriptedProvider(
        [
            completion(None, [("get_case", {})]),
            completion(None, [("assess_evidence", {})]),
            completion(None, [("run_experiment", {"instrument": "source_current_sweep"})]),
            completion(None, [("run_experiment", {"instrument": "transient_capture"})]),  # invalid
            completion("I measured the source current."),
        ]
    )
    _use_provider(app, provider)
    case = client.post("/api/cases", json={}).json()
    response = client.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": "investigate"})
    assert response.status_code == 201, response.text
    run = _wait(client, response.json()["id"])
    assert run["status"] == "completed", run

    case = client.get(f"/api/cases/{case['id']}").json()
    assert case["mode"] == "agent" and case["active_run_id"] is None
    agent_obs = [o for o in case["observations"] if o["requested_by"] == "agent"]
    assert len(agent_obs) == 1 and agent_obs[0]["agent_run_id"] == run["id"]
    assert case["budget"]["used"] == 1, "the invalid request did not consume budget"

    closed = client.post(f"/api/cases/{case['id']}/close").json()
    reveal = closed["reveal"]
    sent = json.dumps(provider.requests)
    assert reveal["seed"] not in sent
    assert reveal["fault"]["description"] not in sent
    # Field names of hidden-adjacent or bulky data that must never be serialized for the model
    # (waveform samples, audio comparisons, evaluator hints).
    for marker in ("distinguishing_instruments", "/api/artifacts/", "input_v", "playback_gain"):
        assert marker not in sent, marker
    assert not re.search(r"art_[A-Za-z0-9_-]{8,}", sent), "no artifact IDs"


def test_close_cancels_the_run_before_revealing(real_app) -> None:
    client, app = real_app
    provider = ScriptedProvider([Hang(30)])
    _use_provider(app, provider)
    case = client.post("/api/cases", json={}).json()
    run = client.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": "investigate"}).json()

    closed = client.post(f"/api/cases/{case['id']}/close")
    assert closed.status_code == 200 and closed.json()["reveal"] is not None
    log = client.get(f"/api/agent-runs/{run['id']}/events/log").json()
    assert log["run"]["status"] == "cancelled"
    assert log["events"][-1]["type"] == "run_finished"
    revealed_at = datetime.fromisoformat(closed.json()["reveal"]["revealed_at"])
    assert all(datetime.fromisoformat(e["at"]) <= revealed_at for e in log["events"])

    again = client.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": "investigate"})
    assert again.status_code == 409
