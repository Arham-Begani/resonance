"""Lead-owned app plumbing: session cookies, error shapes, AI status, artifact access."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from resonance.agent.status import ai_status
from resonance.api.deps import SESSION_COOKIE
from resonance.app import create_app
from resonance.config import Settings


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "resonance_data_dir": tmp_path / "data",
        "web_dist_dir": tmp_path / "no-web",
        "nebius_api_key": None,
        "nebius_model": None,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    with TestClient(create_app(_settings(tmp_path))) as c:
        yield c  # type: ignore[misc]


def test_session_cookie_is_issued_once_and_kept(client: TestClient) -> None:
    first = client.get("/api/health")
    assert first.status_code == 200
    sid = first.cookies.get(SESSION_COOKIE)
    assert sid and len(sid) >= 32
    assert "httponly" in first.headers["set-cookie"].lower()
    second = client.get("/api/health")
    assert SESSION_COOKIE not in second.cookies  # not re-issued while valid


def test_invalid_session_cookie_is_replaced(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "short")
    response = client.get("/api/health")
    new_sid = response.cookies.get(SESSION_COOKIE)
    assert new_sid and new_sid != "short"


def test_validation_errors_are_structured(client: TestClient) -> None:
    response = client.post("/api/units/parse", json={"component_id": "R9", "text": "1k"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["field"] == "component_id"


def test_unknown_routes_and_artifacts_return_structured_404(client: TestClient) -> None:
    for path in ("/api/nope", "/api/artifacts/art_doesnotexist0", "/api/artifacts/../x"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"


def test_artifact_from_another_session_is_not_served(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as owner, TestClient(app) as stranger:
        owner.get("/api/health")
        owner_sid = owner.cookies.get(SESSION_COOKIE)
        assert owner_sid
        artifact_id = app.state.artifacts.put(owner_sid, b"RIFF....WAVE")
        assert owner.get(f"/api/artifacts/{artifact_id}").status_code == 200
        stranger.get("/api/health")
        response = stranger.get(f"/api/artifacts/{artifact_id}")
        assert response.status_code == 404
        assert response.json() == owner.get("/api/artifacts/art_doesnotexist0").json()


def test_workbench_starts_with_default_goal(client: TestClient) -> None:
    body = client.get("/api/workbench").json()
    assert body["goal"]["origin"] == "default"
    kinds = [(c["kind"], c["frequency_hz"], c["limit_db"]) for c in body["goal"]["constraints"]]
    assert kinds == [("preserve", 440.0, 1.0), ("attenuate", 4000.0, 12.0)]
    assert body["feasibility"]["basis"] == "analytical_first_order_rc"
    assert body["revisions"] == [] and body["experiments"] == []


def test_ai_status_reports_missing_configuration_without_secrets(tmp_path: Path) -> None:
    missing_key = ai_status(_settings(tmp_path))
    assert missing_key.available is False and missing_key.state == "missing_key"
    assert "NEBIUS_API_KEY" in missing_key.message

    missing_model = ai_status(_settings(tmp_path, nebius_api_key=SecretStr("sk-test-123")))
    assert missing_model.state == "missing_model"

    ready = ai_status(
        _settings(tmp_path, nebius_api_key=SecretStr("sk-test-123"), nebius_model="nvidia/x")
    )
    assert ready.available is True and ready.state == "ready" and ready.model == "nvidia/x"
    for status in (missing_key, missing_model, ready):
        assert "sk-test-123" not in status.model_dump_json()


def test_health_never_leaks_the_api_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path, nebius_api_key=SecretStr("sk-secret-xyz"), nebius_model="m")
    with TestClient(create_app(settings)) as c:
        text = c.get("/api/health").text
    assert "sk-secret-xyz" not in text


def test_single_server_serves_spa_api_and_rejects_traversal(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    with TestClient(create_app(_settings(tmp_path, web_dist_dir=dist))) as c:
        assert "<title>app</title>" in c.get("/").text
        assert "<title>app</title>" in c.get("/investigate").text  # client-side route
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/favicon.ico").status_code == 404  # missing static file, not the shell
        assert c.get("/api/health").headers["content-type"].startswith("application/json")
        assert c.get("/api/nope").json()["error"]["code"] == "not_found"
        for probe in ("/..%2fsecret.txt", "/assets/..%2f..%2fsecret.txt"):
            response = c.get(probe)
            assert "nope" not in response.text, probe
