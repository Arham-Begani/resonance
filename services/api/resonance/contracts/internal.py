"""Internal (non-HTTP) interfaces shared by simulation, audio, explore, and the evaluator.

These types never appear in OpenAPI. ``CircuitSpec`` objects for hidden cases are created and
held only inside ``resonance.challenges.evaluator``; everything else receives them only as
opaque arguments to simulation/audio functions and must not log, persist, or return them.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from .circuits import SourceSpec, TemplateId
from .common import SimulatorInfo

FloatArray = NDArray[np.float64]
ComplexArray = NDArray[np.complex128]

Probe = Literal["v(in)", "v(out)", "i(V1)"]
"""Quantities a simulation may return. Only requested probes are written by ngspice."""


@dataclass(frozen=True)
class CircuitSpec:
    """What to simulate: template + canonical SI values (+ future connection state).

    Public revisions convert to a CircuitSpec; hidden cases build their own inside the
    evaluator. Netlists are compiled from this by trusted code only."""

    template_id: TemplateId
    values: Mapping[str, float]  # {"R1": ohms, "C1": farads}
    source: SourceSpec
    # Only frozenset({"C1"}) is supported: C1 is omitted from the netlist (an open capacitor).
    disconnected: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class AcSweep:
    """Logarithmic sweep: ngspice ``ac dec points_per_decade start stop``."""

    start_hz: float
    stop_hz: float
    points_per_decade: int
    probes: tuple[Probe, ...] = ("v(in)", "v(out)", "i(V1)")


@dataclass(frozen=True)
class AcPoints:
    """Exact frequencies (e.g. the demo tones). At most 16 points."""

    frequencies_hz: tuple[float, ...]
    probes: tuple[Probe, ...] = ("v(in)", "v(out)", "i(V1)")


@dataclass(frozen=True)
class AcData:
    frequency_hz: FloatArray
    v_in: ComplexArray | None
    v_out: ComplexArray | None
    i_source: ComplexArray | None  # delivered convention: out of V1's + terminal


@dataclass(frozen=True)
class TransientRequest:
    """Two-tone transient driven by ``CircuitSpec.source.tones``."""

    stop_s: float
    max_step_s: float
    probes: tuple[Probe, ...] = ("v(in)", "v(out)")


@dataclass(frozen=True)
class TransientData:
    time_s: FloatArray  # adaptive ngspice timepoints, strictly increasing
    v_in: FloatArray | None
    v_out: FloatArray | None
    i_source: FloatArray | None  # delivered convention


class CancelToken:
    """Cooperative cancellation shared by agent runs, HTTP requests, and simulator jobs."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


class SimulationError(Exception):
    """Base for simulator failures. ``str(exc)`` must be learner-safe: no netlist text,
    component values, file paths, or raw simulator output."""

    retryable: bool = False


class SimulatorUnavailable(SimulationError):
    pass


class SimulationFailed(SimulationError):
    retryable = True


class SimulationTimeout(SimulationError):
    retryable = True


class SimulationCancelled(SimulationError):
    pass


class Simulator(Protocol):
    """Implemented by ``resonance.simulation`` (ngspice). Tests may provide doubles."""

    def info(self) -> SimulatorInfo: ...

    async def ac(
        self,
        circuit: CircuitSpec,
        sweep: AcSweep | AcPoints,
        *,
        cancel: CancelToken | None = None,
    ) -> AcData: ...

    async def transient(
        self,
        circuit: CircuitSpec,
        request: TransientRequest,
        *,
        cancel: CancelToken | None = None,
    ) -> TransientData: ...
