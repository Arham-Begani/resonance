"""Trusted netlist compilation: ``CircuitSpec`` + approved analysis → ngspice input text.

Owner: circuit & simulation engineer.

Safety contract:
- Only fixed tokens (element names, node names, keywords, a fixed title, a fixed output file
  name) and validated finite numbers formatted with ``repr(float)`` ever appear in the text.
  No user, model, or template prose is copied into a netlist; there are no includes, shell
  commands, paths, or model-authored ``.control`` lines.
- Topology comes from the template graph (``circuits.templates``); ``gnd`` maps to node 0.
- The ``.control`` block runs exactly one approved analysis (or one exact-frequency AC run per
  requested point) and ``write``s ONLY the requested probes to ``OUTPUT_FILE`` in the job
  directory. ``write`` is never issued without explicit vectors (that would dump everything).
- Anything unexpected raises ``NetlistError`` (an internal error; its message is generic).

Public API (stable): ``compile_ac(circuit, analysis)``, ``compile_transient(circuit, request)``
→ ``CompiledNetlist``; ``NetlistError``; the ``*_LIMIT*`` constants below.

Analyses:
- ``AcSweep`` → ``ac dec <ppd> <start> <stop>``. ngspice returns
  ``geomspace(start, stop, floor(ppd·log10(stop/start)) + 1)`` (endpoints exact).
- ``AcPoints`` → one ``ac lin 1 f f`` run per frequency, appended to the same raw file.
- ``TransientRequest`` → series SIN sources (one per tone, zero phase, starting at t = 0) and
  ``tran <max_step> <stop> 0 <max_step>``. Internal mid-node names (``v1_m1``…) exist only here.
Source current: AC writes ``i(V1)``; transient writes ``i(V1a)`` (the element adjacent to
``in``). ngspice reports current INTO the + terminal; the simulator negates it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..contracts.circuits import ComponentKind, NodeId
from ..contracts.internal import AcPoints, AcSweep, CircuitSpec, Probe, TransientRequest
from .templates import TemplateDefinition, TemplateUnavailableError, get_template

TITLE = "* resonance trusted netlist"
OUTPUT_FILE = "result.raw"

SIMULATABLE_TEMPLATES = frozenset({"rc_lowpass"})

# Defense-in-depth limits for anything that reaches ngspice (wider than the editing bounds so
# hidden 0.5×/2× fault variants of any allowed design remain simulatable).
VALUE_LIMITS: dict[ComponentKind, tuple[float, float]] = {
    ComponentKind.RESISTOR: (1.0, 1e8),
    ComponentKind.CAPACITOR: (1e-13, 1e-1),
}
AC_MIN_HZ = 0.1
AC_MAX_HZ = 1e7
AC_MAX_POINTS_PER_DECADE = 200
AC_MAX_POINTS = 2001
AC_MAX_EXACT_POINTS = 16
TRAN_MAX_STOP_S = 2.0
TRAN_MIN_MAX_STEP_S = 1e-7
TRAN_MAX_STEPS = 600_000
TONE_MIN_HZ = 1.0
TONE_MAX_HZ = 100_000.0
TONE_MAX_AMPLITUDE_V = 10.0
MAX_TONES = 4
SOURCE_MAX_AC_V = 10.0

_PROBE_ORDER: tuple[Probe, ...] = ("v(in)", "v(out)", "i(V1)")
_NUMBER_TOKEN = re.compile(r"^-?\d+(\.\d+)?(e[+-]?\d+)?$")
_NODE_NAMES: dict[NodeId, str] = {"in": "in", "out": "out", "gnd": "0"}


class NetlistError(ValueError):
    """The circuit or analysis is outside what trusted compilation accepts (internal error)."""


@dataclass(frozen=True)
class CompiledNetlist:
    text: str
    output_file: str
    kind: str  # "ac" | "transient"
    probes: tuple[Probe, ...]  # canonical order, deduplicated
    raw_variables: tuple[str, ...]  # expected raw-file variable names, scale first
    plot_count: int
    complex_data: bool


def _num(value: object) -> str:
    """Format a validated finite float unambiguously (``repr`` round-trips exactly)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise NetlistError("non-numeric value")
    x = float(value)
    if not math.isfinite(x):
        raise NetlistError("non-finite value")
    token = repr(x)
    if not _NUMBER_TOKEN.fullmatch(token):
        raise NetlistError("unexpected number format")
    return token


def _in_range(value: float, lo: float, hi: float) -> bool:
    return math.isfinite(value) and lo <= value <= hi


def _template(circuit: CircuitSpec) -> TemplateDefinition:
    if circuit.template_id not in SIMULATABLE_TEMPLATES:
        raise NetlistError("template not simulatable")
    try:
        return get_template(circuit.template_id)
    except TemplateUnavailableError:
        raise NetlistError("template not available") from None


def _validated_values(circuit: CircuitSpec, template: TemplateDefinition) -> dict[str, float]:
    if circuit.disconnected:
        raise NetlistError("disconnected components are not supported yet")
    valued = [c for c in template.components if c.kind != ComponentKind.VOLTAGE_SOURCE]
    expected = {c.id for c in valued}
    if set(circuit.values) != expected:
        raise NetlistError("circuit values do not match the template")
    out: dict[str, float] = {}
    for comp in valued:
        raw = circuit.values[comp.id]
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise NetlistError("non-numeric component value")
        value = float(raw)
        lo, hi = VALUE_LIMITS[comp.kind]
        if not _in_range(value, lo, hi):
            raise NetlistError("component value outside simulation limits")
        out[comp.id] = value
    return out


def _passive_lines(template: TemplateDefinition, values: dict[str, float]) -> list[str]:
    lines = []
    for comp in template.components:
        if comp.kind == ComponentKind.VOLTAGE_SOURCE:
            continue
        if comp.kind not in (ComponentKind.RESISTOR, ComponentKind.CAPACITOR):
            raise NetlistError("unsupported component kind")
        a, b = (_NODE_NAMES[n] for n in comp.nodes)
        lines.append(f"{comp.id} {a} {b} {_num(values[comp.id])}")
    return lines


def _source_nodes(template: TemplateDefinition) -> tuple[str, str]:
    v1 = template.component("V1")
    if v1.kind != ComponentKind.VOLTAGE_SOURCE:
        raise NetlistError("template has no voltage source V1")
    pos, neg = (_NODE_NAMES[n] for n in v1.nodes)
    return pos, neg


def _probes(requested: Sequence[str]) -> tuple[Probe, ...]:
    if not requested:
        raise NetlistError("at least one probe is required")
    unknown = set(requested) - set(_PROBE_ORDER)
    if unknown:
        raise NetlistError("unknown probe")
    return tuple(p for p in _PROBE_ORDER if p in requested)


def _assemble(elements: list[str], control: list[str]) -> str:
    lines = [TITLE, *elements, ".control", "set filetype=binary", *control, ".endc", ".end"]
    text = "\n".join(lines) + "\n"
    if not text.isascii():
        raise NetlistError("non-ASCII netlist")
    return text


def compile_ac(circuit: CircuitSpec, analysis: AcSweep | AcPoints) -> CompiledNetlist:
    template = _template(circuit)
    values = _validated_values(circuit, template)
    ac_mag = circuit.source.ac_magnitude_v
    if not _in_range(float(ac_mag), 1e-12, SOURCE_MAX_AC_V):
        raise NetlistError("source AC magnitude outside limits")
    probes = _probes(analysis.probes)
    vectors = [p for p in probes]  # netlist vector names: v(in), v(out), i(V1)
    raw_vars = ("frequency", *(p.lower() for p in probes))
    pos, neg = _source_nodes(template)
    elements = [f"V1 {pos} {neg} DC 0 AC {_num(ac_mag)}", *_passive_lines(template, values)]
    write = f"write {OUTPUT_FILE} {' '.join(vectors)}"

    if isinstance(analysis, AcSweep):
        start, stop, ppd = analysis.start_hz, analysis.stop_hz, analysis.points_per_decade
        if isinstance(ppd, bool) or not isinstance(ppd, int):
            raise NetlistError("points per decade must be an integer")
        if not (_in_range(start, AC_MIN_HZ, AC_MAX_HZ) and _in_range(stop, AC_MIN_HZ, AC_MAX_HZ)):
            raise NetlistError("sweep frequencies outside limits")
        if not stop > start:
            raise NetlistError("sweep stop must exceed start")
        if not 1 <= ppd <= AC_MAX_POINTS_PER_DECADE:
            raise NetlistError("points per decade outside limits")
        if expected_sweep_points(start, stop, ppd) > AC_MAX_POINTS:
            raise NetlistError("too many sweep points")
        control = [f"ac dec {ppd} {_num(start)} {_num(stop)}", write]
        plots = 1
    elif isinstance(analysis, AcPoints):
        freqs = tuple(analysis.frequencies_hz)
        if not 1 <= len(freqs) <= AC_MAX_EXACT_POINTS:
            raise NetlistError("number of exact frequencies outside limits")
        control = ["set appendwrite"]
        for f in freqs:
            if not _in_range(float(f), AC_MIN_HZ, AC_MAX_HZ):
                raise NetlistError("frequency outside limits")
            control += [f"ac lin 1 {_num(f)} {_num(f)}", write]
        plots = len(freqs)
    else:
        raise NetlistError("unsupported analysis")

    return CompiledNetlist(
        text=_assemble(elements, control),
        output_file=OUTPUT_FILE,
        kind="ac",
        probes=probes,
        raw_variables=raw_vars,
        plot_count=plots,
        complex_data=True,
    )


def expected_sweep_points(start_hz: float, stop_hz: float, points_per_decade: int) -> int:
    """Number of points ngspice generates for ``ac dec`` (±1 at exact-decade float edges)."""
    return math.floor(points_per_decade * math.log10(stop_hz / start_hz) + 1e-9) + 1


def transient_step_estimate(request: TransientRequest) -> int:
    """Upper-bound estimate of output timepoints (ngspice adds a few at breakpoints)."""
    return math.ceil(request.stop_s / request.max_step_s) + 64


def compile_transient(circuit: CircuitSpec, request: TransientRequest) -> CompiledNetlist:
    template = _template(circuit)
    values = _validated_values(circuit, template)
    stop, step = float(request.stop_s), float(request.max_step_s)
    if not _in_range(stop, 1e-6, TRAN_MAX_STOP_S):
        raise NetlistError("transient duration outside limits")
    if not _in_range(step, TRAN_MIN_MAX_STEP_S, stop / 10.0):
        raise NetlistError("transient step outside limits")
    if stop / step > TRAN_MAX_STEPS:
        raise NetlistError("too many transient steps")
    tones = list(circuit.source.tones)
    if not 1 <= len(tones) <= MAX_TONES:
        raise NetlistError("number of tones outside limits")
    probes = _probes(request.probes)

    pos, neg = _source_nodes(template)
    chain = [pos, *(f"v1_m{i}" for i in range(1, len(tones))), neg]
    elements = []
    for i, tone in enumerate(tones):
        f, a = float(tone.frequency_hz), float(tone.amplitude_v)
        if not _in_range(f, TONE_MIN_HZ, TONE_MAX_HZ) or not _in_range(
            a, 1e-9, TONE_MAX_AMPLITUDE_V
        ):
            raise NetlistError("tone outside limits")
        name = f"V1{'abcd'[i]}"
        elements.append(f"{name} {chain[i]} {chain[i + 1]} DC 0 SIN(0 {_num(a)} {_num(f)} 0 0 0)")
    elements += _passive_lines(template, values)

    vectors = [("i(V1a)" if p == "i(V1)" else p) for p in probes]
    raw_vars = ("time", *(v.lower() for v in vectors))
    control = [
        f"tran {_num(step)} {_num(stop)} 0 {_num(step)}",
        f"write {OUTPUT_FILE} {' '.join(vectors)}",
    ]
    return CompiledNetlist(
        text=_assemble(elements, control),
        output_file=OUTPUT_FILE,
        kind="transient",
        probes=probes,
        raw_variables=raw_vars,
        plot_count=1,
        complex_data=False,
    )
