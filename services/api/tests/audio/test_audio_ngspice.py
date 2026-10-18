"""Real-ngspice audio checks: transient tone levels agree with exact-frequency AC.

Designs are derived from the template's validated bounds (``resonance.circuits.templates``):
the component-bound corners that satisfy the joint cutoff constraint, the slowest and fastest
allowed cutoffs reached with each component at its limit, and the 2× / 0.5× fault variants of
the slowest and fastest designs (hidden cases pass the same capture pipeline).
"""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from resonance.audio.capture import run_two_tone_capture
from resonance.circuits.templates import get_template, validate_values
from resonance.config import Settings
from resonance.contracts.circuits import SourceSpec
from resonance.contracts.internal import AcPoints, CircuitSpec
from resonance.contracts.policy import DEFAULT_TONES
from resonance.explore.experiments import (
    INPUT_AMPLITUDE_TOLERANCE_DB,
    STEADY_STATE_TOLERANCE_DB,
    TONE_AGREEMENT_TOLERANCE_DB,
    TONE_PHASE_TOLERANCE_DEG,
)
from resonance.simulation import create_simulator

pytestmark = pytest.mark.ngspice


def _designs() -> list[tuple[str, float, float]]:
    t = get_template("rc_lowpass")
    r_b = t.component("R1").bounds
    c_b = t.component("C1").bounds
    assert r_b is not None and c_b is not None
    fc_lo, fc_hi = t.cutoff_min_hz * 1.001, t.cutoff_max_hz * 0.999

    def r_for(c: float, fc: float) -> float:
        return 1 / (2 * math.pi * c * fc)

    nominal = [
        ("default", 1e3, 100e-9),
        ("goal design", 1.6e3, 100e-9),
        ("R min, C max", r_b.min_si, c_b.max_si),
        ("R max, C min", r_b.max_si, c_b.min_si),
        ("slowest, R max", r_b.max_si, r_for(r_b.max_si, fc_lo)),
        ("slowest, C max", r_for(c_b.max_si, fc_lo), c_b.max_si),
        ("fastest, R min", r_b.min_si, r_for(r_b.min_si, fc_hi)),
        ("fastest, C min", r_for(c_b.min_si, fc_hi), c_b.min_si),
    ]
    valid = []
    for name, r, c in nominal:
        try:
            validate_values("rc_lowpass", {"R1": r, "C1": c})
        except ValueError:
            continue  # a corner excluded by the joint cutoff constraint
        valid.append((name, r, c))
    slow = [d for d in valid if d[0].startswith("slowest")]
    fast = [d for d in valid if d[0].startswith("fastest")]
    assert len(slow) == 2 and len(fast) == 2
    faults = [(f"{n} x2 R (fault)", r * 2, c) for n, r, c in slow]
    faults += [(f"{n} x2 C (fault)", r, c * 2) for n, r, c in slow]
    faults += [(f"{n} x0.5 R (fault)", r * 0.5, c) for n, r, c in fast]
    faults += [(f"{n} x0.5 C (fault)", r, c * 0.5) for n, r, c in fast]
    return valid + faults


async def _measure(simulator: object, name: str, r: float, c: float) -> dict[str, object]:
    circuit = CircuitSpec(
        template_id="rc_lowpass",
        values={"R1": r, "C1": c},
        source=SourceSpec(tones=list(DEFAULT_TONES), description="two-tone test source"),
    )
    freqs = tuple(t.frequency_hz for t in DEFAULT_TONES)
    cap, ac = await asyncio.gather(
        run_two_tone_capture(simulator, circuit, nodes=("out", "i_source")),  # type: ignore[arg-type]
        simulator.ac(circuit, AcPoints(freqs)),  # type: ignore[attr-defined]
    )
    rows = []
    for k, f in enumerate(freqs):
        h = ac.v_out[k] / ac.v_in[k]
        y = ac.i_source[k] / ac.v_in[k]  # delivered A per volt
        tr_gain = cap.tone_gain_db("out", k)
        ac_gain = 20 * math.log10(abs(h))
        phase_err = (cap.tone_phase_deg("out", k) - math.degrees(float(np.angle(h))) + 180) % 360
        in_err = 20 * math.log10(cap.reference.amplitude_v(k) / DEFAULT_TONES[k].amplitude_v)
        ac_ma = abs(y) * DEFAULT_TONES[k].amplitude_v * 1000
        i_err = 20 * math.log10(cap.node("i_source").amplitude(k) / ac_ma)
        i_phase = cap.tone_phase_deg("i_source", k) - math.degrees(float(np.angle(y)))
        rows.append(
            (
                f,
                tr_gain,
                ac_gain,
                tr_gain - ac_gain,
                phase_err - 180,
                in_err,
                i_err,
                (i_phase + 180) % 360 - 180,
            )
        )
    return {
        "name": name,
        "r": r,
        "c": c,
        "rows": rows,
        "drift": cap.steady_state_drift("out"),
        "i_drift": cap.steady_state_drift("i_source"),
    }


async def test_tone_levels_match_ac_across_template_corners(settings: Settings) -> None:
    simulator = create_simulator(settings)
    designs = _designs()
    # One design at a time (its transient and AC run together): the simulator's queue has a
    # bounded wait, so flooding it with every design at once would time out by design.
    results = [await _measure(simulator, n, r, c) for n, r, c in designs]
    worst = worst_i = 0.0
    print()
    for res in results:
        fc = 1 / (2 * math.pi * res["r"] * res["c"])  # type: ignore[operator]
        rows = res["rows"]
        worst = max([worst] + [abs(row[3]) for row in rows])  # type: ignore[attr-defined]
        worst_i = max([worst_i] + [abs(row[6]) for row in rows])  # type: ignore[attr-defined]
        detail = "  ".join(
            f"{f:g} Hz: V tr {tr:+.3f} / ac {ac:+.3f} dB (d {d:+.4f} dB, {p:+.3f} deg), "
            f"I d {di:+.4f} dB {dp:+.3f} deg"
            for f, tr, ac, d, p, _, di, dp in rows  # type: ignore[attr-defined]
        )
        print(f"{res['name']:<28} R={res['r']:.4g} C={res['c']:.4g} fc={fc:8.1f} Hz  {detail}")
        for f, _tr, _ac, d, p, in_err, di, dp in rows:  # type: ignore[attr-defined]
            assert abs(d) <= TONE_AGREEMENT_TOLERANCE_DB, (res["name"], f, d)
            assert abs(p) <= TONE_PHASE_TOLERANCE_DEG, (res["name"], f, p)
            assert abs(in_err) <= INPUT_AMPLITUDE_TOLERANCE_DB, (res["name"], f, in_err)
            assert abs(di) <= TONE_AGREEMENT_TOLERANCE_DB, (res["name"], f, di)
            assert abs(dp) <= TONE_PHASE_TOLERANCE_DEG, (res["name"], f, dp)
        # Settling (fixed public TRANSIENT_SETTLE_S) is sufficient even for the slowest faults.
        for key in ("drift", "i_drift"):
            drift = res[key]
            assert drift.max_tone_change_db <= STEADY_STATE_TOLERANCE_DB, (res["name"], key)  # type: ignore[attr-defined]
    print(f"worst tone agreement: voltage {worst:.4f} dB, source current {worst_i:.4f} dB")
