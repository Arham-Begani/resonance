"""POST/GET /api/revisions through the real app (session-scoped, structured errors)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from resonance.app import create_app
from resonance.config import Settings


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


def test_create_and_read_revisions(client: TestClient) -> None:
    created = client.post("/api/revisions", json={"template_id": "rc_lowpass"})
    assert created.status_code == 201
    root = created.json()
    assert root["origin"] == "template_default" and root["id"].startswith("rev_")

    edited = client.post(
        "/api/revisions",
        json={"parent_id": root["id"], "values": [{"component_id": "R1", "value": "1.6 kΩ"}]},
    )
    assert edited.status_code == 201
    child = edited.json()
    assert child["parent_id"] == root["id"]
    assert child["changes"][0]["to_display"] == "1.6 kΩ"

    fetched = client.get(f"/api/revisions/{child['id']}")
    assert fetched.status_code == 200 and fetched.json() == child
    workbench = client.get("/api/workbench").json()
    assert [r["id"] for r in workbench["revisions"]] == [root["id"], child["id"]]


def test_both_templates_offered_and_family_switch_over_http(client: TestClient) -> None:
    offered = client.get("/api/templates").json()
    assert [t["id"] for t in offered] == ["rc_lowpass", "rc_highpass"]
    high_template = offered[1]
    assert high_template["default_goal"]["title"] == "Keep the musical tone. Remove the mains hum."
    assert high_template["cutoff_range_hz"] == [20.0, 20000.0]

    low = client.post("/api/revisions", json={"template_id": "rc_lowpass"}).json()
    switched = client.post(
        "/api/revisions",
        json={
            "parent_id": low["id"],
            "template_id": "rc_highpass",
            "values": [{"component_id": "R1", "value": "7.5 kΩ"}],
        },
    )
    assert switched.status_code == 201
    high = switched.json()
    assert high["template_id"] == "rc_highpass" and high["parent_id"] == low["id"]
    assert high["changes"] == [] and high["origin"] == "user_edit"
    assert {c["id"]: c["display_value"] for c in high["components"]} == {
        "V1": None,
        "C1": "100 nF",
        "R1": "7.5 kΩ",
    }
    assert [t["frequency_hz"] for t in high["source"]["tones"]] == [50.0, 440.0]


def test_validation_error_names_field(client: TestClient) -> None:
    root = client.post("/api/revisions", json={"template_id": "rc_lowpass"}).json()
    response = client.post(
        "/api/revisions",
        json={"parent_id": root["id"], "values": [{"component_id": "C1", "value": "10f"}]},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error" and error["field"] == "values.0.value"


def test_revisions_are_session_scoped(client: TestClient) -> None:
    mine = client.post("/api/revisions", json={"template_id": "rc_lowpass"}).json()
    client.cookies.clear()  # next request gets a fresh anonymous session
    theirs = client.get(f"/api/revisions/{mine['id']}")
    missing = client.get("/api/revisions/rev_doesnotexist0000")
    assert theirs.status_code == 404 and missing.status_code == 404
    assert theirs.json() == missing.json()
    as_parent = client.post(
        "/api/revisions",
        json={"parent_id": mine["id"], "values": [{"component_id": "R1", "value": "2k"}]},
    )
    assert as_parent.status_code == 404


def test_units_parse_endpoint_uses_template_bounds(client: TestClient) -> None:
    ok = client.post("/api/units/parse", json={"component_id": "C1", "text": "0.1 µF"}).json()
    assert ok == {"ok": True, "value_si": 1e-7, "display_value": "100 nF", "message": None}
    bad = client.post("/api/units/parse", json={"component_id": "R1", "text": "1G"}).json()
    assert bad["ok"] is False and "too large" in bad["message"]
