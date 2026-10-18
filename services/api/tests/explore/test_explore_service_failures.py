"""Explore bundle failure handling with TEST DOUBLES (no ngspice): structured errors, sibling
jobs cancelled, nothing persisted, and no learner-facing leak of internals."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from resonance.api.errors import ApiException
from resonance.circuits.revisions import build_revision, revision_to_circuit
from resonance.contracts.common import ErrorCode, SimulatorInfo
from resonance.contracts.experiments import ExperimentRecord
from resonance.contracts.internal import (
    AcData,
    AcPoints,
    AcSweep,
    CancelToken,
    CircuitSpec,
    SimulationCancelled,
    SimulationFailed,
    SimulatorUnavailable,
    TransientData,
    TransientRequest,
)
from resonance.explore.service import run_explore_experiment
from resonance.store.artifacts import ArtifactStore
from resonance.store.db import DocumentStore

SESSION = "s" * 40


class ScriptedSimulatorDouble:
    """TEST DOUBLE: each job waits for cancellation unless scripted to fail or return."""

    def __init__(self, *, ac_error: Exception | None = None, garbage: bool = False) -> None:
        self.ac_error = ac_error
        self.garbage = garbage
        self.cancelled_jobs = 0

    def info(self) -> SimulatorInfo:
        return SimulatorInfo(version="test-double")

    async def _wait_for_cancel(self, cancel: CancelToken | None) -> None:
        for _ in range(500):
            if cancel is not None and cancel.cancelled:
                self.cancelled_jobs += 1
                raise SimulationCancelled("The simulation was cancelled.")
            await asyncio.sleep(0.01)
        raise AssertionError("job was never cancelled")

    async def ac(
        self, circuit: CircuitSpec, sweep: AcSweep | AcPoints, *, cancel: CancelToken | None = None
    ) -> AcData:
        if self.ac_error is not None:
            await asyncio.sleep(0.02)
            raise self.ac_error
        if self.garbage:
            f = np.array([440.0, 4000.0])
            ones = np.ones(2, dtype=np.complex128)
            return AcData(frequency_hz=f, v_in=ones, v_out=ones, i_source=ones)
        await self._wait_for_cancel(cancel)
        raise AssertionError("unreachable")

    async def transient(
        self, circuit: CircuitSpec, request: TransientRequest, *, cancel: CancelToken | None = None
    ) -> TransientData:
        if self.garbage:  # covers only 10 ms of the requested 1.4 s
            t = np.linspace(0.0, 0.01, 50)
            return TransientData(time_s=t, v_in=np.sin(t), v_out=np.sin(t), i_source=None)
        await self._wait_for_cancel(cancel)
        raise AssertionError("unreachable")


class UnavailableDouble(ScriptedSimulatorDouble):
    def info(self) -> SimulatorInfo:
        raise SimulatorUnavailable("The circuit simulator is not available on this server.")


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[tuple[DocumentStore, ArtifactStore]]:
    store = DocumentStore(tmp_path / "db.sqlite3")
    yield store, ArtifactStore(tmp_path / "art", store, max_count=20, max_bytes=10 * 1024 * 1024)
    store.close()


async def _run(
    sim: ScriptedSimulatorDouble,
    stores: tuple[DocumentStore, ArtifactStore],
    cancel: CancelToken | None = None,
) -> ApiException:
    store, artifacts = stores
    revision = build_revision("rc_lowpass", {}, origin="template_default")
    with pytest.raises(ApiException) as info:
        await run_explore_experiment(
            simulator=sim,
            store=store,
            artifacts=artifacts,
            session_id=SESSION,
            revision=revision,
            circuit=revision_to_circuit(revision),
            cancel=cancel,
        )
    assert store.list_docs("experiment", SESSION, ExperimentRecord) == []
    assert store.artifact_usage() == (0, 0)
    return info.value


async def test_simulator_failure_cancels_sibling_jobs_and_persists_nothing(
    stores: tuple[DocumentStore, ArtifactStore],
) -> None:
    sim = ScriptedSimulatorDouble(ac_error=SimulationFailed("internal detail"))
    exc = await _run(sim, stores)
    assert exc.error.code == ErrorCode.SIMULATION_FAILED and exc.status == 502
    assert exc.error.retryable
    assert "internal detail" not in exc.error.message
    assert sim.cancelled_jobs == 1  # the transient was stopped, not left running


async def test_unavailable_simulator_is_503(stores: tuple[DocumentStore, ArtifactStore]) -> None:
    exc = await _run(UnavailableDouble(), stores)
    assert exc.error.code == ErrorCode.SIMULATOR_UNAVAILABLE and exc.status == 503


async def test_client_cancellation_stops_every_job(
    stores: tuple[DocumentStore, ArtifactStore],
) -> None:
    sim = ScriptedSimulatorDouble()
    token = CancelToken()
    asyncio.get_running_loop().call_later(0.05, token.cancel)
    exc = await _run(sim, stores, cancel=token)
    assert exc.error.code == ErrorCode.CANCELLED
    assert sim.cancelled_jobs == 3


async def test_unusable_simulator_output_is_a_generic_failure(
    stores: tuple[DocumentStore, ArtifactStore],
) -> None:
    exc = await _run(ScriptedSimulatorDouble(garbage=True), stores)
    assert exc.error.code == ErrorCode.SIMULATION_FAILED
    assert exc.error.message == "The simulation finished but its result could not be analysed."


class _RequestDouble:
    """TEST DOUBLE for ``Request.is_disconnected``: reports a disconnect on the third poll."""

    def __init__(self) -> None:
        self.polls = 0

    async def is_disconnected(self) -> bool:
        self.polls += 1
        return self.polls >= 3


async def test_client_disconnect_trips_the_cancel_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from resonance.api import routes_experiments

    monkeypatch.setattr(routes_experiments, "DISCONNECT_POLL_S", 0.01)
    token = CancelToken()
    request = _RequestDouble()
    await asyncio.wait_for(
        routes_experiments._cancel_on_disconnect(request, token),  # type: ignore[arg-type]
        timeout=2.0,
    )
    assert token.cancelled and request.polls == 3


async def test_frozen_case_design_is_refused_for_every_caller(
    stores: tuple[DocumentStore, ArtifactStore],
) -> None:
    store, artifacts = stores
    frozen = build_revision("rc_lowpass", {}, origin="case_intended", frozen=True)
    sim = ScriptedSimulatorDouble()
    with pytest.raises(ApiException) as info:
        await run_explore_experiment(
            simulator=sim,
            store=store,
            artifacts=artifacts,
            session_id=SESSION,
            revision=frozen,
            circuit=revision_to_circuit(frozen),
        )
    assert info.value.error.code == ErrorCode.STALE_REVISION
    assert sim.cancelled_jobs == 0  # nothing was simulated
