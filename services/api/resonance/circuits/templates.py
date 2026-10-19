"""Circuit templates: typed graphs with tested layouts and validated value ranges.

Owner: circuit & simulation engineer.

Public API (stable):

- ``list_templates() -> list[CircuitTemplate]`` — offered templates (milestone 1: ``rc_lowpass``
  only), each with a preview ``default_revision`` (built once per process; it is NOT stored in
  any session — create a session revision with ``POST /api/revisions {template_id}``).
- ``get_template(template_id) -> TemplateDefinition`` — topology, defaults, bounds. Raises
  ``TemplateUnavailableError`` for templates that are not offered.
- ``available_template_ids() -> tuple[TemplateId, ...]``
- ``parse_component_value(template_id, component_id, value) -> (value_si, unit)`` — explicit SI
  parse + per-component bounds; raises ``ValueParseError`` naming the component.
- ``check_component_bounds(template_id, component_id, value_si)`` — bounds only.
- ``validate_values(template_id, values, *, edited=()) -> None`` — every editable component
  present, finite, within bounds, and the joint cutoff constraint. On a joint violation the
  error names the last component in ``edited`` (if any). Messages repeat the values passed in:
  call it with PUBLIC values only (for a repair, validate the proposed value against the
  intended design, never against hidden values).
- ``build_components(template_id, values) -> list[Component]`` and ``default_source(...)`` —
  trusted descriptions built from graph data.

Validated ranges (rationale in the docstring of ``_RC_LOWPASS``):
R1 100 Ω – 100 kΩ, C1 1 nF – 10 µF, and the joint constraint 20 Hz ≤ fc = 1/(2π·R1·C1) ≤ 20 kHz.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from ..contracts.circuits import (
    CircuitNode,
    CircuitTemplate,
    Component,
    ComponentId,
    ComponentKind,
    NodeId,
    NodeRole,
    SourceSpec,
    TemplateId,
    Tone,
    ValueBounds,
    ValueUnit,
)
from ..contracts.goals import GoalConstraint, GoalInput
from ..contracts.policy import DEFAULT_TONES, TEMPLATE_DEFAULT_GOALS
from .units import ValueParseError, format_frequency, format_value, parse_value_detailed


class TemplateUnavailableError(ValueError):
    """The template ID is unknown or not offered yet. ``str()`` is learner-safe."""


@dataclass(frozen=True)
class ComponentDef:
    id: ComponentId
    kind: ComponentKind
    name: str
    nodes: tuple[NodeId, NodeId]  # (first, second); for V1: (positive, negative)
    editable: bool
    unit: ValueUnit | None = None
    default_si: float | None = None
    bounds: ValueBounds | None = None


@dataclass(frozen=True)
class TemplateDefinition:
    id: TemplateId
    title: str
    summary: str
    nodes: tuple[CircuitNode, ...]
    components: tuple[ComponentDef, ...]
    cutoff_min_hz: float
    cutoff_max_hz: float

    def component(self, component_id: str) -> ComponentDef:
        for comp in self.components:
            if comp.id == component_id:
                return comp
        raise ValueParseError(f"This circuit has no component named {component_id}.")

    @property
    def editable_ids(self) -> tuple[ComponentId, ...]:
        return tuple(c.id for c in self.components if c.editable)

    def default_values(self) -> dict[str, float]:
        return {c.id: float(c.default_si) for c in self.components if c.default_si is not None}

    def node_label(self, node_id: NodeId) -> str:
        for node in self.nodes:
            if node.id == node_id:
                return node.label
        raise KeyError(node_id)


_NODES = (
    CircuitNode(id="in", label="Input", role=NodeRole.INPUT),
    CircuitNode(id="out", label="Output", role=NodeRole.OUTPUT),
    CircuitNode(id="gnd", label="Ground", role=NodeRole.GROUND),
)

_R_BOUNDS = ValueBounds(min_si=100.0, max_si=100e3, unit="ohm")
_C_BOUNDS = ValueBounds(min_si=1e-9, max_si=10e-6, unit="farad")

_RC_LOWPASS = TemplateDefinition(
    id="rc_lowpass",
    title="RC low-pass filter",
    summary=(
        "A resistor from Input to Output and a capacitor from Output to Ground. Low "
        "frequencies pass; frequencies above the cutoff fc = 1/(2π·R1·C1) are reduced, by "
        "about 6 dB per octave well above it."
    ),
    nodes=_NODES,
    components=(
        ComponentDef(
            id="V1",
            kind=ComponentKind.VOLTAGE_SOURCE,
            name="ideal voltage source",
            nodes=("in", "gnd"),
            editable=False,
        ),
        ComponentDef(
            id="R1",
            kind=ComponentKind.RESISTOR,
            name="resistor",
            nodes=("in", "out"),
            editable=True,
            unit="ohm",
            default_si=1000.0,
            bounds=_R_BOUNDS,
        ),
        ComponentDef(
            id="C1",
            kind=ComponentKind.CAPACITOR,
            name="capacitor",
            nodes=("out", "gnd"),
            editable=True,
            unit="farad",
            default_si=100e-9,
            bounds=_C_BOUNDS,
        ),
    ),
    # Rationale for the joint range 20 Hz – 20 kHz (the audible band, also the case sweep):
    # - fc ≥ 20 Hz → τ ≤ 7.96 ms. TRANSIENT_SETTLE_S = 0.2 s then covers ≥ 25 τ for any
    #   allowed design and ≥ 12.5 τ for its 2× fault variant (policy requires ≥ 7 τ).
    # - fc ≤ 20 kHz → τ ≥ 7.96 µs, comparable to TRANSIENT_MAX_STEP_S (10 µs); ngspice's
    #   truncation-error control shortens steps where needed (tests check tone gains at the
    #   fc = 20 kHz corner). The cutoff stays inside the 20 Hz–20 kHz case grid and the
    #   10 Hz–100 kHz explore sweep, and above 20 kHz a change would be inaudible anyway.
    # Per-component bounds are common small-signal values; with an ideal source R1 ≥ 100 Ω
    # keeps the source current ≤ 10 mA per volt. Defaults (1 kΩ, 100 nF) give fc ≈ 1.59 kHz.
    cutoff_min_hz=20.0,
    cutoff_max_hz=20_000.0,
)

_DEFINITIONS: dict[str, TemplateDefinition] = {"rc_lowpass": _RC_LOWPASS}
_AVAILABLE: tuple[TemplateId, ...] = ("rc_lowpass",)


def available_template_ids() -> tuple[TemplateId, ...]:
    return _AVAILABLE


def get_template(template_id: str) -> TemplateDefinition:
    if template_id not in _AVAILABLE:
        raise TemplateUnavailableError("That circuit template is not available yet.")
    return _DEFINITIONS[template_id]


# --------------------------------------------------------------------------------------
# Values: parsing, bounds, joint constraint
# --------------------------------------------------------------------------------------

_MEGA_MILLI_NOTE = (
    " Note: “M” means mega (×10⁶) and “m” means milli (×10⁻³) here, unlike SPICE where M is milli."
)


def _editable(template: TemplateDefinition, component_id: str) -> ComponentDef:
    comp = template.component(component_id)
    if not comp.editable or comp.bounds is None or comp.unit is None:
        raise ValueParseError(f"{component_id} can't be edited.", component_id=component_id)
    return comp


def _bounds_message(comp: ComponentDef, value_si: float, prefix: str = "") -> str:
    assert comp.bounds is not None and comp.unit is not None
    lo = format_value(comp.bounds.min_si, comp.unit)
    hi = format_value(comp.bounds.max_si, comp.unit)
    direction = "too small" if value_si < comp.bounds.min_si else "too large"
    msg = (
        f"{comp.id} = {format_value(value_si, comp.unit)} is {direction}. This lab supports "
        f"{comp.name} values from {lo} to {hi}."
    )
    if prefix in ("M", "m", "meg", "Meg", "MEG"):
        msg += _MEGA_MILLI_NOTE
    return msg


def check_component_bounds(
    template_id: TemplateId, component_id: str, value_si: float, *, prefix: str = ""
) -> None:
    """Raise ``ValueParseError`` (naming the component) unless the value is finite and within
    the component's bounds."""
    comp = _editable(get_template(template_id), component_id)
    assert comp.bounds is not None
    if isinstance(value_si, bool) or not isinstance(value_si, int | float):
        raise ValueParseError(f"{component_id} needs a numeric value.", component_id=component_id)
    if not math.isfinite(value_si) or value_si <= 0:
        raise ValueParseError(
            f"{component_id} must be a positive, finite value.", component_id=component_id
        )
    # A relative tolerance keeps exact boundary values (e.g. "100k") accepted.
    lo, hi = comp.bounds.min_si, comp.bounds.max_si
    if value_si < lo * (1 - 1e-12) or value_si > hi * (1 + 1e-12):
        raise ValueParseError(_bounds_message(comp, value_si, prefix), component_id=component_id)


def parse_component_value(
    template_id: TemplateId, component_id: str, value: str | float | int
) -> tuple[float, ValueUnit]:
    """Parse a learner value for one component (explicit SI units) and check its bounds."""
    comp = _editable(get_template(template_id), component_id)
    assert comp.unit is not None
    try:
        parsed = parse_value_detailed(value, comp.unit)
    except ValueParseError as exc:
        raise ValueParseError(f"{component_id}: {exc.message}", component_id=component_id) from None
    check_component_bounds(template_id, component_id, parsed.value_si, prefix=parsed.prefix)
    return parsed.value_si, comp.unit


def validate_values(
    template_id: TemplateId, values: Mapping[str, float], *, edited: Sequence[str] = ()
) -> None:
    """Validate a complete set of editable values (bounds + joint cutoff constraint)."""
    template = get_template(template_id)
    expected = set(template.editable_ids)
    unknown = sorted(set(values) - expected)
    if unknown:
        raise ValueParseError(
            f"{unknown[0]} is not an editable component of this circuit.", component_id=None
        )
    for cid in template.editable_ids:
        if cid not in values:
            raise ValueParseError(f"A value for {cid} is required.", component_id=cid)
        check_component_bounds(template_id, cid, values[cid])
    r, c = float(values["R1"]), float(values["C1"])
    fc = 1.0 / (2.0 * math.pi * r * c)
    lo, hi = template.cutoff_min_hz, template.cutoff_max_hz
    if lo * (1 - 1e-9) <= fc <= hi * (1 + 1e-9):
        return
    blame = next((cid for cid in reversed(edited) if cid in expected), None)
    change = "smaller" if fc < lo else "larger"
    raise ValueParseError(
        f"With R1 = {format_value(r, 'ohm')} and C1 = {format_value(c, 'farad')} the cutoff "
        f"would be {format_frequency(fc)}. This lab supports cutoffs from "
        f"{format_frequency(lo)} to {format_frequency(hi)} (fc = 1/(2π·R1·C1)); choose a "
        f"{change} R1 or C1.",
        component_id=blame,
    )


# --------------------------------------------------------------------------------------
# Trusted graph-derived descriptions
# --------------------------------------------------------------------------------------


def _tones_text(tones: Sequence[Tone]) -> str:
    return " + ".join(
        f"{format_frequency(t.frequency_hz)} {t.label} at {t.amplitude_v:g} V peak" for t in tones
    )


def default_source(template_id: TemplateId) -> SourceSpec:
    template = get_template(template_id)
    v1 = template.component("V1")
    pos, neg = (template.node_label(n) for n in v1.nodes)
    tones = [t.model_copy() for t in DEFAULT_TONES]
    return SourceSpec(
        ac_magnitude_v=1.0,
        tones=tones,
        description=(
            f"Ideal voltage source V1, positive terminal at {pos}, negative terminal at {neg}. "
            f"Test signal: {_tones_text(tones)}. Frequency-response sweeps drive it with 1 V."
        ),
    )


def build_components(
    template_id: TemplateId, values: Mapping[str, float], source: SourceSpec | None = None
) -> list[Component]:
    """Component list for the schematic and the semantic list, from the template graph."""
    template = get_template(template_id)
    src = source or default_source(template_id)
    out: list[Component] = []
    for comp in template.components:
        first, second = (template.node_label(n) for n in comp.nodes)
        if comp.kind == ComponentKind.VOLTAGE_SOURCE:
            freqs = " + ".join(format_frequency(t.frequency_hz) for t in src.tones)
            out.append(
                Component(
                    id=comp.id,
                    kind=comp.kind,
                    label=comp.id,
                    name=comp.name,
                    nodes=comp.nodes,
                    editable=False,
                    description=(
                        f"{comp.id} — {comp.name} — test signal {freqs} — positive terminal at "
                        f"{first}, negative terminal at {second}"
                    ),
                )
            )
            continue
        assert comp.unit is not None
        value = float(values[comp.id])
        display = format_value(value, comp.unit)
        out.append(
            Component(
                id=comp.id,
                kind=comp.kind,
                label=comp.id,
                name=comp.name,
                value_si=value,
                unit=comp.unit,
                display_value=display,
                nodes=comp.nodes,
                editable=comp.editable,
                bounds=comp.bounds,
                description=(
                    f"{comp.id} — {comp.name} — {display} — connected between {first} and {second}"
                ),
            )
        )
    return out


@lru_cache(maxsize=4)
def _template_contract(template_id: TemplateId) -> CircuitTemplate:
    from .revisions import build_revision  # local import: revisions depends on templates

    template = get_template(template_id)
    preview = build_revision(template_id, template.default_values(), origin="template_default")
    title, constraints = TEMPLATE_DEFAULT_GOALS[template_id]
    default_goal = GoalInput(
        title=title,
        constraints=[
            GoalConstraint(kind=kind, frequency_hz=f, limit_db=limit, label=label)
            for kind, f, limit, label in constraints
        ],
    )
    return CircuitTemplate(
        id=template.id,
        title=template.title,
        summary=template.summary,
        default_revision=preview,
        default_goal=default_goal,
        cutoff_range_hz=(template.cutoff_min_hz, template.cutoff_max_hz),
    )


def list_templates() -> list[CircuitTemplate]:
    """Offered templates, each with a preview default revision (not stored in a session)."""
    return [_template_contract(tid).model_copy(deep=True) for tid in _AVAILABLE]
