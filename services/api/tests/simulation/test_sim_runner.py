"""ngspice subprocess isolation: limits, cancellation, failure detection, cleanup.

Broken or long-running netlists are injected through the internal ``NgspiceRunner.run`` test
hook, never through public APIs (which only compile trusted netlists).
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from resonance.circuits.revisions import build_revision, revision_to_circuit
from resonance.config import Settings
from resonance.contracts.internal import (
    AcSweep,
    CancelToken,
    SimulationCancelled,
    SimulationFailed,
    SimulationTimeout,
    SimulatorUnavailable,
    TransientRequest,
)
from resonance.simulation import (
    NgspiceRunner,
    NgspiceSimulator,
    SimulationRequestError,
    UnavailableSimulator,
    create_runner,
    create_simulator,
)
from resonance.simulation import ngspice as ngspice_module

BUSY_LOOP = """* busy
V1 in 0 DC 0 AC 1
R1 in 0 1000
.control
let i = 0
while i < 100000000
let i = i + 1
end
.endc
.end
"""

FLOOD = """* flood
V1 in 0 DC 0 AC 1
R1 in 0 1000
.control
let i = 0
while i < 10000000
echo xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
let i = i + 1
end
.endc
.end
"""

VALID_AC = """* ok
V1 in 0 DC 0 AC 1
R1 in out 1000.0
C1 out 0 1e-07
.control
set filetype=binary
ac dec 10 10.0 1000.0
write result.raw v(out)
.endc
.end
"""


def _no_jobs_left(settings: Settings) -> bool:
    return not list(settings.jobs_dir.glob("job_*")) if settings.jobs_dir.exists() else True


def _runner(settings: Settings) -> NgspiceRunner:
    runner = create_runner(settings)
    assert runner is not None
    return runner


def _assert_learner_safe(exc: BaseException, settings: Settings) -> None:
    text = str(exc)
    for forbidden in ("job_", str(settings.resonance_data_dir), ".cir", "line", "R1", "1000", "\n"):
        assert forbidden not in text


def _circuit(**values: float) -> Any:
    return revision_to_circuit(build_revision("rc_lowpass", values, origin="user_edit"))


@pytest.mark.ngspice
def test_valid_job_returns_output_and_cleans_up(settings: Settings) -> None:
    files = _runner(settings).run(VALID_AC, ("result.raw",))
    assert files["result.raw"].startswith(b"Title:")
    assert _no_jobs_left(settings)


@pytest.mark.ngspice
def test_timeout_kills_process_and_cleans_up(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(ngspice_timeout_s=0.75)
    started = time.monotonic()
    with pytest.raises(SimulationTimeout) as excinfo:
        _runner(settings).run(BUSY_LOOP, ("result.raw",))
    assert time.monotonic() - started < 6.0
    _assert_learner_safe(excinfo.value, settings)
    assert _no_jobs_left(settings)


@pytest.mark.ngspice
def test_cancellation_kills_process_and_cleans_up(settings: Settings) -> None:
    token = CancelToken()
    threading.Timer(0.5, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(SimulationCancelled):
        _runner(settings).run(BUSY_LOOP, ("result.raw",), cancel=(token,))
    assert time.monotonic() - started < 5.0
    assert _no_jobs_left(settings)


@pytest.mark.ngspice
def test_public_transient_honours_cancel_token(settings: Settings) -> None:
    token = CancelToken()
    threading.Timer(0.5, token.cancel).start()
    started = time.monotonic()
    # ~590k timepoints: several seconds if it were allowed to finish.
    request = TransientRequest(2.0, 3.4e-6)
    with pytest.raises(SimulationCancelled):
        asyncio.run(create_simulator(settings).transient(_circuit(), request, cancel=token))
    assert time.monotonic() - started < 4.0
    assert _no_jobs_left(settings)


@pytest.mark.ngspice
def test_cancelling_the_asyncio_task_stops_ngspice(settings: Settings) -> None:
    simulator = create_simulator(settings)

    async def scenario() -> None:
        task = asyncio.create_task(simulator.transient(_circuit(), TransientRequest(2.0, 3.4e-6)))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    deadline = time.monotonic() + 5.0
    while not _no_jobs_left(settings) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _no_jobs_left(settings)


@pytest.mark.ngspice
def test_output_size_cap_is_enforced_while_running(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(ngspice_max_output_mb=1)
    with pytest.raises(SimulationRequestError) as excinfo:
        _runner(settings).run(FLOOD, ("result.raw",))
    assert not excinfo.value.retryable
    assert _no_jobs_left(settings)


@pytest.mark.ngspice
def test_output_size_cap_is_checked_before_running(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_factory(ngspice_max_output_mb=1)

    def no_popen(*args: object, **kwargs: object) -> None:
        raise AssertionError("ngspice must not start for an over-budget request")

    monkeypatch.setattr(ngspice_module.subprocess, "Popen", no_popen)
    with pytest.raises(SimulationRequestError):
        asyncio.run(
            create_simulator(settings).transient(
                _circuit(), TransientRequest(1.4, 1e-5, ("v(in)", "v(out)", "i(V1)"))
            )
        )


@pytest.mark.parametrize(
    "netlist",
    [
        # parse error (exit status 1)
        "* bad\nV1 in 0 DC 0 AC 1\nQ9 foo\n.control\nac dec 10 10 1000\n.endc\n.end\n",
        # singular matrix: ngspice falls back and exits 0 with output, but warns
        "* floating\nV1 in 0 DC 0 AC 1\nR1 in out 1000\nC1 out2 0 1e-7\n.control\n"
        "set filetype=binary\nac dec 10 10 1000\nwrite result.raw v(out)\n.endc\n.end\n",
        # unknown vector: no output file, exit status 0
        "* novec\nV1 in 0 DC 0 AC 1\nR1 in out 1000\nC1 out 0 1e-7\n.control\n"
        "set filetype=binary\nac dec 10 10 1000\nwrite result.raw v(nope)\n.endc\n.end\n",
    ],
)
@pytest.mark.ngspice
def test_ngspice_errors_are_detected_even_with_exit_code_zero(
    settings: Settings, netlist: str
) -> None:
    with pytest.raises(SimulationFailed) as excinfo:
        _runner(settings).run(netlist, ("result.raw",))
    _assert_learner_safe(excinfo.value, settings)
    assert _no_jobs_left(settings)


@pytest.mark.ngspice
def test_concurrency_limit(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(ngspice_max_concurrency=1)
    runner = _runner(settings)
    first_cancel = CancelToken()
    errors: list[BaseException] = []

    def first() -> None:
        try:
            runner.run(BUSY_LOOP, ("result.raw",), cancel=(first_cancel,))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    results: list[dict[str, bytes]] = []
    t1 = threading.Thread(target=first)
    t1.start()
    time.sleep(0.4)
    t2 = threading.Thread(target=lambda: results.append(runner.run(VALID_AC, ("result.raw",))))
    t2.start()
    time.sleep(0.6)
    assert not results  # the second job is still waiting for the only slot
    assert len(list(settings.jobs_dir.glob("job_*"))) == 1
    first_cancel.cancel()
    t1.join(10)
    t2.join(10)
    assert isinstance(errors[0], SimulationCancelled)
    assert results and results[0]["result.raw"].startswith(b"Title:")
    assert _no_jobs_left(settings)


@pytest.mark.ngspice
def test_child_environment_carries_no_secrets(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "sk-test-not-a-real-key")
    seen: list[dict[str, str]] = []
    real_popen = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(dict(kwargs["env"]))
        assert isinstance(args[0], list) and "shell" not in kwargs
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(ngspice_module.subprocess, "Popen", spy)
    _runner(settings).run(VALID_AC, ("result.raw",))
    assert seen and all("sk-test-not-a-real-key" not in v for v in seen[0].values())
    assert "NEBIUS_API_KEY" not in seen[0]


def test_missing_binary_reports_unavailable(
    settings_factory: Callable[..., Settings], tmp_path: Path
) -> None:
    settings = settings_factory(ngspice_bin=str(tmp_path / "no-such-ngspice.exe"))
    simulator = create_simulator(settings)
    assert isinstance(simulator, UnavailableSimulator)
    with pytest.raises(SimulatorUnavailable):
        simulator.info()
    with pytest.raises(SimulatorUnavailable):
        asyncio.run(simulator.ac(_circuit(), AcSweep(10.0, 1e3, 10)))


def test_unrunnable_binary_reports_unavailable_and_cleans_up(
    settings: Settings, tmp_path: Path
) -> None:
    runner = NgspiceRunner(
        str(tmp_path / "missing.exe"),
        jobs_dir=settings.jobs_dir,
        timeout_s=5,
        max_output_bytes=1 << 20,
        max_concurrency=1,
    )
    with pytest.raises(SimulatorUnavailable):
        runner.run(VALID_AC, ("result.raw",))
    with pytest.raises(SimulatorUnavailable):
        NgspiceSimulator(runner).info()
    assert _no_jobs_left(settings)


@pytest.mark.ngspice
def test_version_is_reported(settings: Settings) -> None:
    info = create_simulator(settings).info()
    assert info.name == "ngspice" and info.version.split(".")[0].isdigit()
