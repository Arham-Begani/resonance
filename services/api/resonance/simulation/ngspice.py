"""Isolated ngspice subprocess execution and the ngspice-backed ``Simulator``.

Owner: circuit & simulation engineer.

``NgspiceRunner.run`` (blocking; the async simulator calls it via ``asyncio.to_thread``):
- waits for a concurrency slot (``ngspice_max_concurrency``), polling cancellation;
- creates a private job directory with ``tempfile.mkdtemp`` under ``settings.jobs_dir``;
- writes the trusted netlist and runs ``[bin, "-b", "-n", "-o", <log>, <netlist>]`` with
  ``cwd`` = job dir, stdin closed, stdout/stderr to a file, and a minimal environment
  (``config.subprocess_env()``; HOME/USERPROFILE point at the job dir so no user
  ``.spiceinit`` is read; ``-n`` skips it as well). No shell, no API keys;
- polls every 50 ms: cancellation → kill → ``SimulationCancelled``; wall time over
  ``ngspice_timeout_s`` → kill → ``SimulationTimeout``; job-directory size over
  ``ngspice_max_output_mb`` (or log/stdout over ``_MAX_LOG_BYTES``) → kill →
  ``SimulationRequestError``;
- detects ngspice errors even when the exit code is 0 (error lines in the log/stdout, missing
  or malformed output) → ``SimulationFailed``;
- ALWAYS deletes the job directory (success, failure, timeout, cancel).

Nothing here logs netlist text, component values, or simulator output; logs carry the analysis
kind, status, and duration only. Every exception's ``str()`` is a fixed learner-safe sentence.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from ..circuits.netlist import (
    CompiledNetlist,
    NetlistError,
    compile_ac,
    compile_transient,
    expected_sweep_points,
    transient_step_estimate,
)
from ..config import subprocess_env
from ..contracts.common import SimulatorInfo
from ..contracts.internal import (
    AcData,
    AcPoints,
    AcSweep,
    CancelToken,
    CircuitSpec,
    ComplexArray,
    FloatArray,
    SimulationCancelled,
    SimulationFailed,
    SimulationTimeout,
    SimulatorUnavailable,
    TransientData,
    TransientRequest,
)
from .raw import RawFormatError, RawPlot, parse_raw

log = logging.getLogger("resonance.simulation")

MSG_UNAVAILABLE = "The circuit simulator is not available on this server."
MSG_TIMEOUT = "The simulation took too long and was stopped."
MSG_BUSY = "The simulator is busy right now. Please try again in a moment."
MSG_CANCELLED = "The simulation was cancelled."
MSG_FAILED = "The simulator reported an error for this circuit."
MSG_BAD_OUTPUT = "The simulator returned an incomplete result."
MSG_TOO_MUCH = "The simulation produced more data than this server allows."
MSG_REQUEST = "This simulation request is outside the supported limits."

NETLIST_NAME = "circuit.cir"
LOG_NAME = "ngspice.log"
STDOUT_NAME = "stdout.txt"
_POLL_S = 0.05
_MAX_LOG_BYTES = 4 * 1024 * 1024
_STALE_JOB_AGE_S = 3600.0
_VERSION_TIMEOUT_S = 15.0
_ERROR_PATTERNS = re.compile(
    r"(\berror\b|simulation interrupted|no such command|singular matrix|timestep too small|"
    r"gmin stepping failed|source stepping failed|can't find model|not available or has zero "
    r"length|\bfatal\b|\baborted\b|unknown parameter)",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"ngspice-(\d+(?:\.\d+)*)")


class SimulationRequestError(SimulationFailed):
    """The request exceeds declared limits (not retryable)."""

    retryable = False


def _any_cancelled(tokens: Sequence[CancelToken | None]) -> bool:
    return any(t is not None and t.cancelled for t in tokens)


def _dir_size(path: Path) -> tuple[int, int]:
    """(total bytes in the job dir, largest log/stdout size)."""
    total, largest_log = 0, 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    # os.stat (not DirEntry.stat): on Windows the directory entry's cached size
                    # lags behind a file that another process still has open.
                    size = os.stat(entry.path).st_size
                except OSError:
                    continue
                total += size
                if entry.name in (LOG_NAME, STDOUT_NAME):
                    largest_log = max(largest_log, size)
    except OSError:
        pass
    return total, largest_log


def _remove_tree(path: Path) -> None:
    """Delete a job directory; retry briefly because Windows may hold handles after a kill."""
    for _ in range(40):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05)
    log.warning("ngspice job directory could not be removed")


class NgspiceRunner:
    """Runs trusted netlists in isolated, bounded ngspice subprocesses."""

    def __init__(
        self,
        binary: str,
        *,
        jobs_dir: Path,
        timeout_s: float,
        max_output_bytes: int,
        max_concurrency: int,
    ) -> None:
        self.binary = binary
        self.jobs_dir = Path(jobs_dir)
        self.timeout_s = float(timeout_s)
        self.max_output_bytes = int(max_output_bytes)
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrency)))
        self._version: str | None = None
        self._version_lock = threading.Lock()
        self._sweep_stale_jobs()

    # ---------------------------------------------------------------- version

    def version(self) -> str:
        """ngspice version (e.g. ``"47"``), detected once with ``-v`` and cached."""
        with self._version_lock:
            if self._version is not None:
                return self._version
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argument array, no shell
                    [self.binary, "-v"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=_VERSION_TIMEOUT_S,
                    env=subprocess_env(),
                    creationflags=_creationflags(),
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                raise SimulatorUnavailable(MSG_UNAVAILABLE) from None
            out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
            match = _VERSION_RE.search(out)
            if proc.returncode != 0 or not match:
                raise SimulatorUnavailable(MSG_UNAVAILABLE)
            self._version = match.group(1)
            return self._version

    # ---------------------------------------------------------------- jobs

    def _sweep_stale_jobs(self) -> None:
        """Remove job directories left behind by a crashed process (older than an hour)."""
        try:
            entries = list(self.jobs_dir.glob("job_*"))
        except OSError:
            return
        now = time.time()
        for entry in entries:
            try:
                if entry.is_dir() and now - entry.stat().st_mtime > _STALE_JOB_AGE_S:
                    _remove_tree(entry)
            except OSError:
                continue

    def _acquire_slot(self, cancel: Sequence[CancelToken | None]) -> None:
        deadline = time.monotonic() + self.timeout_s
        while not self._slots.acquire(timeout=_POLL_S):
            if _any_cancelled(cancel):
                raise SimulationCancelled(MSG_CANCELLED)
            if time.monotonic() > deadline:
                raise SimulationTimeout(MSG_BUSY)

    def run(
        self,
        netlist: str,
        outputs: Sequence[str],
        *,
        cancel: Sequence[CancelToken | None] = (),
        kind: str = "job",
    ) -> dict[str, bytes]:
        """Run one netlist and return the named output files' bytes. Blocking.

        Internal API (also the test hook for injecting deliberately broken netlists); never
        reachable from HTTP or agent tools with untrusted text."""
        if _any_cancelled(cancel):
            raise SimulationCancelled(MSG_CANCELLED)
        self._acquire_slot(cancel)
        started = time.monotonic()
        status = "failed"
        try:
            result = self._run_in_job_dir(netlist, outputs, cancel)
            status = "succeeded"
            return result
        except SimulationCancelled:
            status = "cancelled"
            raise
        except SimulationTimeout:
            status = "timeout"
            raise
        finally:
            self._slots.release()
            log.debug("ngspice %s %s in %.2f s", kind, status, time.monotonic() - started)

    def _run_in_job_dir(
        self, netlist: str, outputs: Sequence[str], cancel: Sequence[CancelToken | None]
    ) -> dict[str, bytes]:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        job = Path(tempfile.mkdtemp(prefix="job_", dir=self.jobs_dir))
        proc: subprocess.Popen[bytes] | None = None
        try:
            (job / NETLIST_NAME).write_text(netlist, encoding="ascii")
            env = subprocess_env()
            env["HOME"] = str(job)
            env["USERPROFILE"] = str(job)
            with open(job / STDOUT_NAME, "wb") as stdout_fh:
                try:
                    proc = subprocess.Popen(  # noqa: S603 - fixed argument array, no shell
                        [self.binary, "-b", "-n", "-o", LOG_NAME, NETLIST_NAME],
                        cwd=job,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_fh,
                        stderr=subprocess.STDOUT,
                        creationflags=_creationflags(),
                    )
                except OSError:
                    raise SimulatorUnavailable(MSG_UNAVAILABLE) from None
                self._supervise(proc, job, cancel)
            return self._collect(proc, job, outputs)
        finally:
            if proc is not None and proc.poll() is None:
                _kill(proc)
            _remove_tree(job)

    def _supervise(
        self, proc: subprocess.Popen[bytes], job: Path, cancel: Sequence[CancelToken | None]
    ) -> None:
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                proc.wait(timeout=_POLL_S)
            except subprocess.TimeoutExpired:
                pass
            else:
                # A cancellation that arrived while the job was finishing still wins: a
                # cancelled request never produces a result.
                if _any_cancelled(cancel):
                    raise SimulationCancelled(MSG_CANCELLED)
                break
            if _any_cancelled(cancel):
                _kill(proc)
                raise SimulationCancelled(MSG_CANCELLED)
            if time.monotonic() > deadline:
                _kill(proc)
                raise SimulationTimeout(MSG_TIMEOUT)
            total, largest_log = _dir_size(job)
            if total > self.max_output_bytes or largest_log > _MAX_LOG_BYTES:
                _kill(proc)
                raise SimulationRequestError(MSG_TOO_MUCH)

    def _collect(
        self, proc: subprocess.Popen[bytes], job: Path, outputs: Sequence[str]
    ) -> dict[str, bytes]:
        total, largest_log = _dir_size(job)
        if total > self.max_output_bytes or largest_log > _MAX_LOG_BYTES:
            raise SimulationRequestError(MSG_TOO_MUCH)
        text = ""
        for name in (LOG_NAME, STDOUT_NAME):
            path = job / name
            if path.is_file():
                text += path.read_bytes().decode("utf-8", "replace") + "\n"
        if proc.returncode != 0:
            log.warning("ngspice job failed: exit status")
            raise SimulationFailed(MSG_FAILED)
        if _ERROR_PATTERNS.search(text):
            log.warning("ngspice job failed: error output")
            raise SimulationFailed(MSG_FAILED)
        files: dict[str, bytes] = {}
        for name in outputs:
            path = job / name
            if not path.is_file() or path.stat().st_size == 0:
                log.warning("ngspice job failed: missing output")
                raise SimulationFailed(MSG_BAD_OUTPUT)
            files[name] = path.read_bytes()
        return files


def _creationflags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning("ngspice process did not exit after kill")


# ====================================================================================
# Simulator
# ====================================================================================


class NgspiceSimulator:
    """``contracts.internal.Simulator`` backed by ngspice. Create one per process
    (``app.state.simulator``); the concurrency limit is per instance."""

    def __init__(self, runner: NgspiceRunner) -> None:
        self.runner = runner

    def info(self) -> SimulatorInfo:
        return SimulatorInfo(version=self.runner.version())

    async def _execute(
        self, compiled: CompiledNetlist, cancel: CancelToken | None
    ) -> list[RawPlot]:
        local = CancelToken()
        try:
            files = await asyncio.to_thread(
                self.runner.run,
                compiled.text,
                (compiled.output_file,),
                cancel=(cancel, local),
                kind=compiled.kind,
            )
        except asyncio.CancelledError:
            local.cancel()  # the worker thread kills ngspice and cleans up
            raise
        try:
            plots = parse_raw(files[compiled.output_file])
        except RawFormatError:
            log.warning("ngspice job failed: malformed raw output")
            raise SimulationFailed(MSG_BAD_OUTPUT) from None
        if len(plots) != compiled.plot_count:
            raise SimulationFailed(MSG_BAD_OUTPUT)
        for plot in plots:
            if plot.variables != compiled.raw_variables or plot.is_complex != compiled.complex_data:
                raise SimulationFailed(MSG_BAD_OUTPUT)
            if not np.all(np.isfinite(plot.data)):
                raise SimulationFailed(MSG_BAD_OUTPUT)
        return plots

    def _check_output_budget(self, points: int, columns: int, complex_data: bool) -> None:
        width = 16 if complex_data else 8
        if points * columns * width + 64 * 1024 > self.runner.max_output_bytes:
            raise SimulationRequestError(MSG_TOO_MUCH)

    async def ac(
        self,
        circuit: CircuitSpec,
        sweep: AcSweep | AcPoints,
        *,
        cancel: CancelToken | None = None,
    ) -> AcData:
        try:
            compiled = compile_ac(circuit, sweep)
        except NetlistError:
            raise SimulationRequestError(MSG_REQUEST) from None
        if isinstance(sweep, AcSweep):
            est = expected_sweep_points(sweep.start_hz, sweep.stop_hz, sweep.points_per_decade)
        else:
            est = len(sweep.frequencies_hz)
        self._check_output_budget(est + 1, len(compiled.raw_variables), True)
        plots = await self._execute(compiled, cancel)

        if isinstance(sweep, AcSweep):
            data = np.asarray(plots[0].data, dtype=np.complex128)
            freq = data[:, 0].real.astype(np.float64)
            _validate_sweep_grid(freq, sweep)
        else:
            if any(p.data.shape[0] != 1 for p in plots):
                raise SimulationFailed(MSG_BAD_OUTPUT)
            data = np.asarray(np.vstack([p.data for p in plots]), dtype=np.complex128)
            freq = data[:, 0].real.astype(np.float64)
            requested = np.asarray(sweep.frequencies_hz, dtype=np.float64)
            if not np.allclose(freq, requested, rtol=1e-9, atol=0.0):
                raise SimulationFailed(MSG_BAD_OUTPUT)
            freq = requested.copy()

        columns = {p: data[:, i + 1].astype(np.complex128) for i, p in enumerate(compiled.probes)}
        i_src = columns.get("i(V1)")
        return AcData(
            frequency_hz=freq,
            v_in=columns.get("v(in)"),
            v_out=columns.get("v(out)"),
            i_source=None if i_src is None else _delivered(i_src),
        )

    async def transient(
        self,
        circuit: CircuitSpec,
        request: TransientRequest,
        *,
        cancel: CancelToken | None = None,
    ) -> TransientData:
        try:
            compiled = compile_transient(circuit, request)
        except NetlistError:
            raise SimulationRequestError(MSG_REQUEST) from None
        self._check_output_budget(
            transient_step_estimate(request), len(compiled.raw_variables), False
        )
        plots = await self._execute(compiled, cancel)
        data = plots[0].data.real.astype(np.float64)
        t = data[:, 0]
        if t.size < 2 or t[0] != 0.0 or not math.isclose(t[-1], request.stop_s, rel_tol=1e-6):
            raise SimulationFailed(MSG_BAD_OUTPUT)
        dt = np.diff(t)
        if np.any(dt < 0):
            raise SimulationFailed(MSG_BAD_OUTPUT)
        keep = np.concatenate(([True], dt > 0))  # drop exact duplicate timepoints, if any
        data = data[keep]
        columns = {p: data[:, i + 1].copy() for i, p in enumerate(compiled.probes)}
        i_src = columns.get("i(V1)")
        return TransientData(
            time_s=data[:, 0].copy(),
            v_in=columns.get("v(in)"),
            v_out=columns.get("v(out)"),
            i_source=None if i_src is None else _delivered(i_src),
        )


def _delivered[A: (FloatArray, ComplexArray)](i_into_plus: A) -> A:
    """ngspice reports current INTO V1's + terminal; the contract uses the delivered direction.

    ``0.0 - x`` equals ``-x`` exactly for every nonzero value but maps a zero to +0.0 instead
    of −0.0. This matters for an open capacitor (I = 0 exactly): ``angle(-0-0j)`` is −180°,
    which would report a spurious phase, while ``angle(0j)`` is 0°."""
    return np.subtract(0.0, i_into_plus)


def _validate_sweep_grid(freq: FloatArray, sweep: AcSweep) -> None:
    n = freq.size
    expected = expected_sweep_points(sweep.start_hz, sweep.stop_hz, sweep.points_per_decade)
    if n < 2 or abs(n - expected) > 1:
        raise SimulationFailed(MSG_BAD_OUTPUT)
    grid = np.geomspace(sweep.start_hz, sweep.stop_hz, n)
    if not np.allclose(freq, grid, rtol=1e-9, atol=0.0):
        raise SimulationFailed(MSG_BAD_OUTPUT)


class UnavailableSimulator:
    """Used when no ngspice binary is found: every call raises ``SimulatorUnavailable``."""

    def info(self) -> SimulatorInfo:
        raise SimulatorUnavailable(MSG_UNAVAILABLE)

    async def ac(
        self, circuit: CircuitSpec, sweep: AcSweep | AcPoints, *, cancel: CancelToken | None = None
    ) -> AcData:
        raise SimulatorUnavailable(MSG_UNAVAILABLE)

    async def transient(
        self, circuit: CircuitSpec, request: TransientRequest, *, cancel: CancelToken | None = None
    ) -> TransientData:
        raise SimulatorUnavailable(MSG_UNAVAILABLE)
