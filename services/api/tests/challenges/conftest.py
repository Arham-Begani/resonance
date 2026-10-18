"""Fixtures for investigation tests. Data directories always live under pytest's tmp_path."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from challenges_testkit import AnalyticTestSimulator, Rig, build_rig
from fastapi.testclient import TestClient

from resonance.config import Settings
from resonance.contracts.internal import Simulator
from resonance.simulation import create_simulator


@pytest.fixture
def double() -> AnalyticTestSimulator:
    return AnalyticTestSimulator()


@pytest.fixture
def rig(settings: Settings, double: AnalyticTestSimulator) -> Iterator[Rig]:
    r = build_rig(settings, double)
    yield r
    r.service.close()
    r.store.close()


@pytest.fixture
def ngspice_rig(settings: Settings) -> Iterator[Rig]:
    simulator = create_simulator(settings)
    simulator.info()  # fail loudly if ngspice is not runnable
    r = build_rig(settings, simulator)
    yield r
    r.service.close()
    r.store.close()


@pytest.fixture
def app_client_factory(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[Simulator], TestClient]]:
    """The real FastAPI app (lifespan, routes, error handlers, session cookie) with an injected
    simulator."""
    import resonance.app as app_module

    clients: list[TestClient] = []

    def make(simulator: Simulator) -> TestClient:
        monkeypatch.setattr(app_module, "create_simulator", lambda _settings: simulator)
        client = TestClient(app_module.create_app(settings))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)
