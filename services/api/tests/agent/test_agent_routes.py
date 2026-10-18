"""HTTP surface of agent runs: SSE framing and replay, JSON log, session scoping, cancel,
AI-unavailable behaviour, and proposal dismissal."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from agent_testkit import (
    SESSION,
    TEST_API_KEY,
    FakeCaseService,
    Hang,
    ScriptedProvider,
    completion,
    make_service,
    make_settings,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from resonance.api import routes_agent
from resonance.api.deps import SESSION_COOKIE, install_session_middleware
from resonance.api.errors import install_error_handlers
from resonance.config import Settings
from resonance.contracts.agent import (
    AgentEvent,
    AgentRun,
    EditProposal,
    PredictedToneGain,
    ProposedChange,
)
from resonance.contracts.common import new_id
from resonance.store.db import DocumentStore, utcnow

EVENT = TypeAdapter(AgentEvent)
OTHER_SESSION = "o" * 40


def build_app(
    settings: Settings, store: DocumentStore, provider: ScriptedProvider, cases: FakeCaseService
) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    install_session_middleware(app, settings)
    app.include_router(routes_agent.router)
    app.state.settings = settings
    app.state.store = store
    app.state.case_service = cases
    app.state.agent_service = make_service(settings, store, provider, cases)
    return app


@pytest.fixture
def cases() -> FakeCaseService:
    return FakeCaseService(SESSION, mode="agent")


def client_for(app: FastAPI, session: str = SESSION) -> TestClient:
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, session)
    return client


@pytest.fixture
def make_client(settings, store, cases) -> Iterator[Any]:
    clients: list[TestClient] = []

    def make(provider: ScriptedProvider, *, app_settings: Settings | None = None) -> TestClient:
        app = build_app(app_settings or settings, store, provider, cases)
        client = client_for(app)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def parse_sse(text: str) -> list[dict[str, str]]:
    frames: list[dict[str, str]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, str] = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                frame.setdefault("comment", line[1:].strip())
                continue
            key, _, value = line.partition(": ")
            frame[key] = frame[key] + "\n" + value if key in frame else value
        frames.append(frame)
    return frames


def start_run(client: TestClient, cases: FakeCaseService) -> dict[str, Any]:
    response = client.post(f"/api/cases/{cases.case_id}/agent-runs", json={"kind": "investigate"})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def stream(client: TestClient, run_id: str, headers: dict[str, str] | None = None) -> str:
    with client.stream("GET", f"/api/agent-runs/{run_id}/events", headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return "".join(response.iter_text())


def test_sse_streams_genuine_events_and_replays_from_last_event_id(make_client, cases) -> None:
    provider = ScriptedProvider(
        [completion(None, [("get_case", {})]), completion("The case has one observation.")]
    )
    client = make_client(provider)
    run = start_run(client, cases)
    assert run["status"] == "running" and run["model"]

    frames = [f for f in parse_sse(stream(client, run["id"])) if "event" in f]
    events = [EVENT.validate_json(f["data"]) for f in frames]
    assert [f["event"] for f in frames] == [e.type for e in events]
    assert [int(f["id"]) for f in frames] == [e.seq for e in events]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].type == "run_started" and events[-1].type == "run_finished"
    assert events[-1].run.status == "completed"

    replay = [f for f in parse_sse(stream(client, run["id"], {"Last-Event-ID": "3"})) if "id" in f]
    assert [int(f["id"]) for f in replay] == [e.seq for e in events[3:]]

    log = client.get(f"/api/agent-runs/{run['id']}/events/log").json()
    assert [e["seq"] for e in log["events"]] == [e.seq for e in events]
    assert log["run"]["status"] == "completed"
    assert client.get(f"/api/agent-runs/{run['id']}").json()["usage"]["model_calls"] == 2


def test_sse_sends_keepalive_comments_while_idle(make_client, cases, monkeypatch) -> None:
    monkeypatch.setattr("fastapi.routing._PING_INTERVAL", 0.05)
    provider = ScriptedProvider([Hang(0.4)])  # replies "late reply" after 0.4 s
    client = make_client(provider)
    run = start_run(client, cases)
    frames = parse_sse(stream(client, run["id"]))
    assert any(f.get("comment") == "ping" for f in frames)
    assert frames[-1]["event"] == "run_finished"


def test_unknown_or_foreign_runs_are_404(make_client, cases, store) -> None:
    provider = ScriptedProvider([completion("Done.")])
    client = make_client(provider)
    run = start_run(client, cases)
    stream(client, run["id"])  # let it finish

    other = client_for(client.app, OTHER_SESSION)  # type: ignore[arg-type]
    missing = client.get(f"/api/agent-runs/{new_id('run')}")
    assert missing.status_code == 404
    for path in ("", "/events", "/events/log"):
        foreign = other.get(f"/api/agent-runs/{run['id']}{path}")
        assert foreign.status_code == 404
        assert foreign.json() == missing.json(), "foreign IDs look exactly like missing IDs"
    assert other.post(f"/api/agent-runs/{run['id']}/cancel").status_code == 404


def test_cancel_endpoint_stops_a_running_run(make_client, cases) -> None:
    provider = ScriptedProvider([Hang(30)])
    client = make_client(provider)
    run = start_run(client, cases)
    response = client.post(f"/api/agent-runs/{run['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    frames = [f for f in parse_sse(stream(client, run["id"])) if "event" in f]
    assert frames[-1]["event"] == "run_finished"
    assert json.loads(frames[-1]["data"])["run"]["status"] == "cancelled"
    assert cases.active_run_id is None


@pytest.mark.parametrize("path", ["case", "explore"])
def test_ai_unavailable_returns_503_with_setup_hint_and_creates_no_run(
    make_client, cases, store, tmp_path: Path, path: str
) -> None:
    provider = ScriptedProvider([completion("never")])
    client = make_client(provider, app_settings=make_settings(tmp_path, key=None))
    if path == "case":
        response = client.post(
            f"/api/cases/{cases.case_id}/agent-runs", json={"kind": "investigate"}
        )
    else:
        response = client.post(
            "/api/explore/agent-runs",
            json={"revision_id": new_id("rev"), "message": "Make the whistle quieter."},
        )
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "ai_unavailable"
    assert "NEBIUS_API_KEY" in error["message"] and ".env" in error["message"]
    assert store.list_docs("agent_run", SESSION, AgentRun) == []
    assert "claim" not in cases.calls and provider.requests == []


def test_no_secret_in_http_payloads(make_client, cases) -> None:
    provider = ScriptedProvider([completion("Hello.")])
    client = make_client(provider)
    run = start_run(client, cases)
    payloads = [
        stream(client, run["id"]),
        client.get(f"/api/agent-runs/{run['id']}/events/log").text,
        client.get(f"/api/agent-runs/{run['id']}").text,
    ]
    for payload in payloads:
        assert TEST_API_KEY not in payload


def _proposal(store: DocumentStore, status: str = "pending") -> EditProposal:
    proposal = EditProposal(
        id=new_id("prop"),
        base_revision_id=new_id("rev"),
        changes=[
            ProposedChange(
                component_id="C1",
                from_value_si=1e-7,
                to_value_si=2.2e-7,
                from_display="100 nF",
                to_display="220 nF",
            )
        ],
        rationale="Lower the cutoff.",
        predicted=[PredictedToneGain(frequency_hz=4000.0, label="Whistle", predicted_gain_db=-15)],
        status=status,  # type: ignore[arg-type]
        created_at=utcnow(),
    )
    store.put("proposal", proposal.id, SESSION, proposal)
    return proposal


def test_dismiss_proposal(make_client, store) -> None:
    client = make_client(ScriptedProvider())
    pending = _proposal(store)
    response = client.post(f"/api/proposals/{pending.id}/dismiss")
    assert response.status_code == 200 and response.json()["status"] == "dismissed"
    assert client.post(f"/api/proposals/{pending.id}/dismiss").status_code == 200  # idempotent
    applied = _proposal(store, "applied")
    assert client.post(f"/api/proposals/{applied.id}/dismiss").status_code == 409
    other = client_for(client.app, OTHER_SESSION)  # type: ignore[arg-type]
    assert other.post(f"/api/proposals/{pending.id}/dismiss").status_code == 404


@pytest.mark.parametrize(
    ("key", "model", "state"),
    [
        (None, "m", "missing_key"),
        ("your_nebius_api_key_here", "m", "missing_key"),  # the .env.example placeholder
        ("sk-real", None, "missing_model"),
        ("sk-real", "m", "ready"),
    ],
)
def test_ai_status_states(tmp_path: Path, key: str | None, model: str | None, state: str) -> None:
    from resonance.agent.status import ai_status

    settings = make_settings(tmp_path, key=key).model_copy(update={"nebius_model": model})
    status = ai_status(settings)
    assert status.state == state and status.available == (state == "ready")
    assert "sk-real" not in status.model_dump_json()
