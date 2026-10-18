"""Audio test helpers (imported by the audio tests and ``conftest.py``).

``AnalyticRcTestDouble`` is a TEST-ONLY stand-in for the ``Simulator`` protocol. It returns
the exact closed-form RC response (steady state + start-up transient) on an irregular,
ngspice-like time grid so the resampling/fitting pipeline can be tested without ngspice. It is
never used by application code; real-simulator tests are marked ``ngspice``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from resonance.contracts.circuits import SourceSpec, Tone
from resonance.contracts.common import SimulatorInfo
from resonance.contracts.internal import (
    AcData,
    AcPoints,
    AcSweep,
    CancelToken,
    CircuitSpec,
    FloatArray,
    TransientData,
    TransientRequest,
)
from resonance.contracts.policy import DEFAULT_TONES


def make_circuit(
    r_ohm: float = 1e3,
    c_farad: float = 100e-9,
    template_id: str = "rc_lowpass",
    tones: tuple[Tone, ...] = DEFAULT_TONES,
) -> CircuitSpec:
    return CircuitSpec(
        template_id=template_id,  # type: ignore[arg-type]
        values={"R1": r_ohm, "C1": c_farad},
        source=SourceSpec(tones=list(tones), description="Two-tone test source"),
    )


def irregular_grid(stop_s: float, max_step_s: float, seed: int = 7) -> FloatArray:
    """Strictly increasing timepoints with steps in [0.5, 1] × max_step plus rare tiny steps."""
    rng = np.random.default_rng(seed)
    n_est = int(stop_s / (0.5 * max_step_s)) + 64  # enough steps to reach stop_s
    steps = rng.uniform(0.5, 1.0, n_est) * max_step_s
    tiny = rng.random(n_est) < 0.001
    steps[tiny] = 4e-9
    t = np.concatenate([[0.0], np.cumsum(steps)])
    t = t[t < stop_s]
    return np.append(t, stop_s)


def _source(circuit: CircuitSpec, t: FloatArray) -> FloatArray:
    out = np.zeros_like(t)
    for tn in circuit.source.tones:
        out += tn.amplitude_v * np.sin(2 * np.pi * tn.frequency_hz * t)
    return out


def _capacitor_like(circuit: CircuitSpec, t: FloatArray) -> FloatArray:
    """Solution of τ·x' + x = v_in(t), x(0) = 0 (the low-pass capacitor voltage, or the
    series-capacitor voltage of the high-pass)."""
    tau = circuit.values["R1"] * circuit.values["C1"]
    x = np.zeros_like(t)
    for tone in circuit.source.tones:
        w = 2 * np.pi * tone.frequency_hz
        h = 1.0 / (1.0 + 1j * w * tau)
        mag, ph = abs(h), np.angle(h)
        x += tone.amplitude_v * mag * (np.sin(w * t + ph) - np.sin(ph) * np.exp(-t / tau))
    return x


def rc_current(circuit: CircuitSpec, t: FloatArray) -> FloatArray:
    """Exact current delivered by the source (A): (v_in − x)/R for both families."""
    return (_source(circuit, t) - _capacitor_like(circuit, t)) / circuit.values["R1"]


def rc_output(circuit: CircuitSpec, t: FloatArray) -> FloatArray:
    """Exact v(out) for zero-phase sine sources switched on at t = 0 with v_C(0) = 0."""
    x = _capacitor_like(circuit, t)
    if circuit.template_id == "rc_lowpass":
        return x
    return _source(circuit, t) - x


@dataclass
class AnalyticRcTestDouble:
    """TEST DOUBLE implementing the Simulator protocol with closed-form RC results."""

    transient_requests: list[TransientRequest] = field(default_factory=list)
    ac_requests: list[AcSweep | AcPoints] = field(default_factory=list)

    def info(self) -> SimulatorInfo:
        return SimulatorInfo(version="analytic-test-double")

    async def ac(
        self,
        circuit: CircuitSpec,
        sweep: AcSweep | AcPoints,
        *,
        cancel: CancelToken | None = None,
    ) -> AcData:
        self.ac_requests.append(sweep)
        if isinstance(sweep, AcSweep):
            decades = np.log10(sweep.stop_hz / sweep.start_hz)
            n = int(round(decades * sweep.points_per_decade)) + 1
            f = np.geomspace(sweep.start_hz, sweep.stop_hz, n)
        else:
            f = np.asarray(sweep.frequencies_hz, dtype=np.float64)
        r, c = circuit.values["R1"], circuit.values["C1"]
        jwrc = 1j * 2 * np.pi * f * r * c
        h = 1 / (1 + jwrc) if circuit.template_id == "rc_lowpass" else jwrc / (1 + jwrc)
        i = (1j * 2 * np.pi * f * c) / (1 + jwrc)
        ones = np.ones_like(f, dtype=np.complex128)
        return AcData(frequency_hz=f, v_in=ones, v_out=h.astype(np.complex128), i_source=i)

    async def transient(
        self,
        circuit: CircuitSpec,
        request: TransientRequest,
        *,
        cancel: CancelToken | None = None,
    ) -> TransientData:
        self.transient_requests.append(request)
        t = irregular_grid(request.stop_s, request.max_step_s)
        return TransientData(
            time_s=t,
            v_in=_source(circuit, t) if "v(in)" in request.probes else None,
            v_out=rc_output(circuit, t) if "v(out)" in request.probes else None,
            i_source=rc_current(circuit, t) if "i(V1)" in request.probes else None,
        )
