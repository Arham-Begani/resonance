"""ngspice execution and result parsing (owner: circuit & simulation engineer).

Public entry point: ``create_simulator(settings) -> Simulator`` (see ``contracts.internal``).

- ``info()`` → ``SimulatorInfo(version="47")`` (detected once with ``ngspice -v``).
- ``await ac(circuit, AcSweep | AcPoints, cancel=...)`` → ``AcData``: frequency (Hz), complex
  ``v_in``/``v_out``/``i_source`` for the REQUESTED probes only (others ``None``). With the 1 V
  AC source, ``v_out`` is the transfer function and ``i_source`` (delivered convention: out of
  V1's + terminal) is the input admittance × 1 V. ``AcSweep`` frequencies are ngspice's
  ``geomspace(start, stop, floor(ppd·log10(stop/start)) + 1)`` (endpoints exact). These differ
  from a hand-computed ``start·10^(k/ppd)`` grid by float rounding (~1e-9 relative), so
  consumers must match points BY INDEX or with a relative tolerance, never by float equality.
  ``AcPoints`` returns exactly the requested frequencies, in order (≤ 16).
- ``await transient(circuit, TransientRequest, cancel=...)`` → ``TransientData``: adaptive,
  strictly increasing ngspice timepoints from 0 to ``stop_s`` and the requested probes. The
  source is the sum of ``circuit.source.tones`` (zero-phase sines starting at t = 0).
- Failures raise ``SimulatorUnavailable | SimulationFailed | SimulationTimeout |
  SimulationCancelled`` (``SimulationRequestError`` is a non-retryable ``SimulationFailed`` for
  requests outside the declared limits in ``circuits.netlist``). ``str(exc)`` is learner-safe.

Use the single app-wide instance (``app.state.simulator``): the concurrency limit is per
instance. There is deliberately NO result cache keyed by circuit content: warm-cache timing
could reveal hidden case circuits (verification note V-04). Do not add one at this layer.
Analytical RC equations live in ``simulation.analytic`` and are checks/predictions only.
"""

from __future__ import annotations

from ..config import Settings, resolve_ngspice_bin
from ..contracts.internal import Simulator
from .ngspice import NgspiceRunner, NgspiceSimulator, SimulationRequestError, UnavailableSimulator

__all__ = [
    "NgspiceRunner",
    "NgspiceSimulator",
    "SimulationRequestError",
    "UnavailableSimulator",
    "create_runner",
    "create_simulator",
]


def create_runner(settings: Settings) -> NgspiceRunner | None:
    """The bounded subprocess runner for this configuration, or None if ngspice is missing."""
    binary = resolve_ngspice_bin(settings)
    if binary is None:
        return None
    return NgspiceRunner(
        binary,
        jobs_dir=settings.jobs_dir,
        timeout_s=settings.ngspice_timeout_s,
        max_output_bytes=settings.ngspice_max_output_mb * 1024 * 1024,
        max_concurrency=settings.ngspice_max_concurrency,
    )


def create_simulator(settings: Settings) -> Simulator:
    runner = create_runner(settings)
    if runner is None:
        return UnavailableSimulator()
    return NgspiceSimulator(runner)
