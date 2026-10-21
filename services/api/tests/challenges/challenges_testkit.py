"""Test-only helpers for the investigation tests (never imported by application code).

``AnalyticTestSimulator`` is a clearly named TEST DOUBLE of the ``Simulator`` protocol: it
evaluates the exact first-order RC solution of either family (AC: transfer function and input
admittance; transient: steady state plus the exact decaying term for zero initial capacitor
voltage; an open capacitor gives the exact constant transfer and zero current) on a uniform
time grid. It exists so lifecycle/budget/isolation tests run fast and deterministically;
the physics and identifiability claims are tested against real ngspice (``@pytest.mark.ngspice``).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from resonance.challenges.evaluator import Evaluator, EvaluatorStore, faults
from resonance.challenges.instruments import CASE_FREQUENCY_GRID_HZ
from resonance.challenges.service import CaseService
from resonance.circuits.units import format_value
from resonance.config import Settings
from resonance.contracts.circuits import TemplateId, ValueUnit
from resonance.contracts.common import SimulatorInfo
from resonance.contracts.internal import (
    AcData,
    AcPoints,
    AcSweep,
    CancelToken,
    CircuitSpec,
    SimulationCancelled,
    SimulationFailed,
    Simulator,
    SimulatorUnavailable,
    TransientData,
    TransientRequest,
)
from resonance.contracts.investigation import HypothesisId
from resonance.contracts.policy import TEMPLATE_TONES
from resonance.simulation import analytic
from resonance.store.artifacts import ArtifactStore
from resonance.store.db import DocumentStore


@dataclass
class Rig:
    service: CaseService
    store: DocumentStore
    artifacts: ArtifactStore
    settings: Settings
    simulator: Simulator


def build_rig(settings: Settings, simulator: Simulator) -> Rig:
    """A CaseService with its own stores under the test's data directory."""
    settings.resonance_data_dir.mkdir(parents=True, exist_ok=True)
    store = DocumentStore(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir, store, max_count=600, max_bytes=300 << 20)
    evaluator = Evaluator(EvaluatorStore(settings.evaluator_db_path), simulator, artifacts)
    return Rig(CaseService(store, evaluator), store, artifacts, settings, simulator)


SESSION_A = "sessionA_" + "a" * 32
SESSION_B = "sessionB_" + "b" * 32


@dataclass
class AnalyticTestSimulator:
    """TEST DOUBLE: exact analytical RC responses with failure/delay/cancel injection."""

    fail_next: int = 0  # raise a retryable SimulationFailed for the next N calls
    unavailable: bool = False  # raise the NON-retryable SimulatorUnavailable on every call
    delay_s: float = 0.0  # simulated run time (cancellation is polled while waiting)
    calls: list[tuple[str, dict[str, float]]] = field(default_factory=list)

    def info(self) -> SimulatorInfo:
        return SimulatorInfo(version="analytic-test-double")

    async def _wait(self, cancel: CancelToken | None) -> None:
        end = asyncio.get_running_loop().time() + self.delay_s
        while True:
            if cancel is not None and cancel.cancelled:
                raise SimulationCancelled("The simulation was cancelled.")
            remaining = end - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.01, remaining))

    def _maybe_fail(self) -> None:
        if self.unavailable:
            raise SimulatorUnavailable("The circuit simulator is not available on this server.")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise SimulationFailed("The simulator could not complete this run.")

    async def ac(
        self, circuit: CircuitSpec, sweep: AcSweep | AcPoints, *, cancel: CancelToken | None = None
    ) -> AcData:
        self.calls.append(("ac", dict(circuit.values)))
        await self._wait(cancel)
        self._maybe_fail()
        if isinstance(sweep, AcSweep):
            n = math.floor(sweep.points_per_decade * math.log10(sweep.stop_hz / sweep.start_hz))
            f = np.geomspace(sweep.start_hz, sweep.stop_hz, n + 1)
        else:
            f = np.asarray(sweep.frequencies_hz, dtype=np.float64)
        h = analytic.transfer(circuit.template_id, f, circuit.values, circuit.disconnected)
        i = analytic.source_current(
            circuit.template_id, f, circuit.values, 1.0, circuit.disconnected
        )
        probes = sweep.probes
        return AcData(
            frequency_hz=f,
            v_in=np.ones_like(h) if "v(in)" in probes else None,
            v_out=h if "v(out)" in probes else None,
            i_source=i if "i(V1)" in probes else None,
        )

    async def transient(
        self, circuit: CircuitSpec, request: TransientRequest, *, cancel: CancelToken | None = None
    ) -> TransientData:
        self.calls.append(("transient", dict(circuit.values)))
        await self._wait(cancel)
        self._maybe_fail()
        r, c = circuit.values["R1"], circuit.values["C1"]
        tau = r * c
        open_c = "C1" in circuit.disconnected
        t = np.arange(0.0, request.stop_s + request.max_step_s / 2, request.max_step_s)
        v_in = np.zeros_like(t)
        v_out = np.zeros_like(t)
        for tone in circuit.source.tones:
            w = 2.0 * math.pi * tone.frequency_hz
            h = complex(
                analytic.transfer(
                    circuit.template_id, tone.frequency_hz, circuit.values, circuit.disconnected
                )
            )
            mag, ph = abs(h), math.atan2(h.imag, h.real)
            v_in += tone.amplitude_v * np.sin(w * t)
            # Steady state + homogeneous term so that v_out(0) = 0 (uncharged capacitor; the same
            # e^(-t/RC) term for both families). An open capacitor has no dynamics.
            decay = 0.0 if open_c else math.sin(ph) * np.exp(-t / tau)
            v_out += tone.amplitude_v * mag * (np.sin(w * t + ph) - decay)
        if open_c:
            current = np.zeros_like(t)  # the series path through C1 is open
        elif circuit.template_id == "rc_lowpass":
            current = (v_in - v_out) / r  # through R1 (in → out)
        else:
            current = v_out / r  # through R1 (out → ground) = through C1 = from the source
        probes = request.probes
        return TransientData(
            time_s=t,
            v_in=v_in if "v(in)" in probes else None,
            v_out=v_out if "v(out)" in probes else None,
            i_source=current if "i(V1)" in probes else None,
        )


def seed_for(
    fault: HypothesisId,
    nominal: tuple[float, float] | None = None,
    template_id: TemplateId = "rc_lowpass",
) -> str:
    """Deterministically find a seed whose generated case (of this family) has this fault (and
    nominal design)."""
    for i in range(400_000):
        seed = f"test-{fault}-{i}"
        design = faults.generate(seed, template_id)
        if design.fault.hypothesis_id != fault:
            continue
        if nominal is None or (design.nominal["R1"], design.nominal["C1"]) == nominal:
            return seed
    raise AssertionError("no seed found")


# --------------------------------------------------------------------------------------
# Leak scanning
# --------------------------------------------------------------------------------------

_PREFIXES = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6}


def value_spellings(value: float, unit: ValueUnit) -> list[str]:
    """Plausible textual spellings of a component value (with and without units)."""
    symbol_words = {"ohm": ["Ω", "ohm", "ohms", "R"], "farad": ["F", "farad", "farads"]}[unit]
    out = {format_value(value, unit), format_value(value, unit).replace(" ", "")}
    numeric = {repr(value), f"{value:g}", f"{value:e}", f"{value:.3e}", f"{value:.2e}"}
    numeric |= {s.replace("e-0", "e-").replace("e+0", "e+") for s in list(numeric)}
    numeric |= {s.replace("e+", "e") for s in list(numeric)}
    if value >= 1:
        numeric |= {str(int(round(value))), f"{value:.1f}"}
    else:
        numeric |= {f"{value:.12f}".rstrip("0")}
    out |= numeric
    for prefix, scale in _PREFIXES.items():
        mant = value / scale
        if 0.001 <= mant < 100_000:
            m = f"{mant:g}"
            for word in symbol_words:
                out |= {f"{m}{prefix}{word}", f"{m} {prefix}{word}"}
            if unit == "ohm" and prefix in "kM":
                out.add(f"{m}{prefix}")  # "20k" (bounded match: not followed by letters)
            if unit == "farad" and prefix in "pnuµ":
                out.add(f"{m}{prefix}")
    for word in symbol_words:
        out |= {f"{numeric_main(value)} {word}", f"{numeric_main(value)}{word}"}
    return sorted(out, key=len, reverse=True)


def numeric_main(value: float) -> str:
    return f"{value:g}"


def _bounded(token: str) -> re.Pattern[str]:
    # Not preceded by a digit/dot; not followed by a digit, dot, or a letter (so "20k" does not
    # match "20 kHz" or "20kHz", but "20 kΩ" is matched by its own spelling).
    return re.compile(r"(?<![0-9.])" + re.escape(token) + r"(?![0-9.A-Za-zΩ])")


def iter_leaves(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from iter_leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from iter_leaves(v, f"{path}[{i}]")
    else:
        yield path, node


# Paths that carry declared measured quantities (or values derived only from them), where a
# numeric coincidence with a hidden value is a measurement, not a leak.
MEASURED_PATH = re.compile(
    r"(^|\.)("
    r"sweep\.(magnitude|phase_deg)\[\d+\]"
    r"|transient\.tones\[\d+\]\.(amplitude|gain_db|phase_deg)"
    r"|transient\.waveform\.observed\[\d+\]"
    r"|audio\.gain_adjustment_db"
    r"|evidence\.assessments\[\d+\]\.checks\[\d+\]\.(observed|deviation|predicted|tolerance)"
    r"|repair\.(max_gain_deviation_db|max_phase_deviation_deg)"
    r"|repair\.points\[\d+\]\.(repaired_gain_db|repaired_phase_deg)"
    r"|repair\.tones\[\d+\]\.(amplitude|gain_db|phase_deg)"
    r")$"
)
FREQUENCY_KEY = re.compile(r"(frequency_hz(\[\d+\])?|_hz)$")
PUBLIC_FREQUENCIES = (
    list(CASE_FREQUENCY_GRID_HZ)
    + [t.frequency_hz for tones in TEMPLATE_TONES.values() for t in tones]
    + [20.0, 20_000.0]
)


@dataclass(frozen=True)
class Secrets:
    numbers: tuple[float, ...]  # actual values that differ from nominal
    strings: tuple[str, ...]  # seed, value spellings
    label_strings: tuple[str, ...]  # the true hypothesis id/label (allowed only on catalog paths)


def secrets_for(
    seed: str,
    fault: HypothesisId,
    nominal: dict[str, float],
    template_id: TemplateId = "rc_lowpass",
) -> Secrets:
    design = faults.generate(seed, template_id)
    assert design.fault.hypothesis_id == fault
    numbers: list[float] = []
    strings: list[str] = [seed]
    for comp, value in design.actual.items():
        if math.isclose(value, nominal[comp], rel_tol=1e-12):
            continue  # equal to the public nominal value
        numbers.append(value)
        strings += value_spellings(value, "ohm" if comp == "R1" else "farad")
    from resonance.challenges.hypotheses import HYPOTHESES_BY_ID, SHORT_LABELS

    labels: tuple[str, ...] = ()
    if fault != "no_fault":
        labels = (fault, HYPOTHESES_BY_ID[fault].label, SHORT_LABELS[fault])
    return Secrets(tuple(numbers), tuple(strings), labels)


LABEL_ALLOWED_PATH = re.compile(
    r"(^|\.)(hypotheses\[\d+\]\.(id|label)|evidence\..*|diagnosis\.(submission|evidence_check)\..*)$"
)


OPAQUE_ID = re.compile(
    r"(?<![A-Za-z0-9_-])(?:case|obs|rev|art|cmp|dx|rep|run|goal|exp|prop)_[A-Za-z0-9_-]{16}"
    r"(?![A-Za-z0-9_-])"
)
"""IDs minted by ``contracts.common.new_id`` (prefix + ``secrets.token_urlsafe(12)``)."""


def mask_opaque_ids(text: str, seen: set[str] | None = None) -> str:
    """Replace opaque random IDs by a placeholder before scanning prose for value spellings.

    Random IDs are 16 base64url characters, so short spellings ("5k", "5n", "2k", "20n") occur
    inside them by chance (measured ≈ 2e-4 per ID for "5k"). IDs get their own, stronger check
    instead (exact ``new_id`` format and global uniqueness across flows, see ``seen``)."""
    if seen is not None:
        seen.update(m.group(0) for m in OPAQUE_ID.finditer(text))
    return OPAQUE_ID.sub("<opaque-id>", text)


@dataclass(frozen=True)
class Finding:
    path: str
    value: Any  # the offending leaf (compared across fault variants)
    detail: str

    def __str__(self) -> str:
        return f"{self.detail} at {self.path}"


def find_leaks(payload: Any, secrets: Secrets, ids: set[str] | None = None) -> list[Finding]:
    """Structural scan of one parsed JSON payload. Opaque IDs are collected into ``ids`` and
    masked; every other string is scanned for value spellings, the seed, and the true label."""
    findings: list[Finding] = []
    patterns = [(s, _bounded(s)) for s in secrets.strings]
    for path, leaf in iter_leaves(payload):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, int | float):
            for secret in secrets.numbers:
                if math.isclose(float(leaf), secret, rel_tol=1e-9):
                    if MEASURED_PATH.search(path):
                        continue
                    if FREQUENCY_KEY.search(path) and any(
                        math.isclose(float(leaf), f, rel_tol=1e-9) for f in PUBLIC_FREQUENCIES
                    ):
                        continue
                    findings.append(Finding(path, leaf, f"number {leaf!r}"))
        elif isinstance(leaf, str):
            text = mask_opaque_ids(leaf, ids)
            for spelled, pattern in patterns:
                if pattern.search(text):
                    findings.append(Finding(path, leaf, f"string {spelled!r} in {leaf[:120]!r}"))
            for label in secrets.label_strings:
                if label in text and not LABEL_ALLOWED_PATH.search(path):
                    findings.append(Finding(path, leaf, f"true-cause label {label!r}"))
    return findings


def raw_text_leaks(text: str, secrets: Secrets) -> list[str]:
    """Scan raw text (logs, headers) for the seed and value spellings, IDs masked."""
    masked = mask_opaque_ids(text)
    return [secret for secret in secrets.strings if _bounded(secret).search(masked)]


def leaf_map(payload: Any) -> dict[str, Any]:
    return dict(iter_leaves(payload))


# --------------------------------------------------------------------------------------
# Differential skeletons
# --------------------------------------------------------------------------------------

_ID = re.compile(r"^(case|obs|rev|art|cmp|dx|rep|run|goal|exp|prop)_[A-Za-z0-9_-]{8,64}$")
_TS_KEYS = {"created_at", "updated_at", "revealed_at", "started_at"}
_MASK_SUBTREES = re.compile(
    r"(^|\.)(evidence|diagnosis\.evidence_check|repair\.points|repair\.tones|"
    r"repair\.functional_recovery|repair\.worst_frequency_hz)$"
)
_MASK_KEYS = {"summary", "note"}


def skeleton(node: Any, path: str = "") -> Any:
    """Replace IDs, timestamps, and declared measured quantities by placeholders so payloads
    of the same nominal design under different hidden faults can be compared."""
    if _MASK_SUBTREES.search(path):
        return "<derived-from-measurements>"
    if isinstance(node, dict):
        role = node.get("role")
        out = {}
        for k, v in node.items():
            sub = f"{path}.{k}" if path else k
            if k in _TS_KEYS:
                out[k] = "<ts>"
            elif k in _MASK_KEYS:
                out[k] = "<text-from-measurements>"
            elif role in ("observed", "repaired") and k in ("peak_fs", "clipped"):
                out[k] = "<measured>"
            else:
                out[k] = skeleton(v, sub)
        return out
    if isinstance(node, list):
        return [skeleton(v, f"{path}[{i}]") for i, v in enumerate(node)]
    if isinstance(node, str):
        if _ID.match(node):
            return "<id>"
        if node.startswith("/api/artifacts/"):
            return "<artifact-url>"
        return node
    if MEASURED_PATH.search(path):
        return "<measured>"
    return node


def dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def first(items: Sequence[Any]) -> Any:
    return items[0]
