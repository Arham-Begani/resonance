"""Circuit templates, components, nodes, sources, and immutable revisions (public)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from .common import Contract, ProposalId, RevisionId

TemplateId = Literal["rc_lowpass", "rc_highpass"]
"""Milestone 1 implements ``rc_lowpass`` only. ``rc_highpass`` is reserved for milestone 2 and
must not be offered by ``GET /api/templates`` until it passes its physics gates."""

NodeId = Literal["in", "out", "gnd"]
"""Named, user-visible nodes. Netlists may use additional internal node names (e.g. a
mid-point between series two-tone sources); those never appear in public payloads."""

ComponentId = Literal["V1", "R1", "C1"]
EditableComponentId = Literal["R1", "C1"]


class ComponentKind(StrEnum):
    VOLTAGE_SOURCE = "voltage_source"
    RESISTOR = "resistor"
    CAPACITOR = "capacitor"


class NodeRole(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    GROUND = "ground"


ValueUnit = Literal["ohm", "farad"]


class CircuitNode(Contract):
    id: NodeId
    label: str = Field(examples=["Input", "Output", "Ground"])
    role: NodeRole


class ValueBounds(Contract):
    min_si: float
    max_si: float
    unit: ValueUnit


class Component(Contract):
    id: ComponentId
    kind: ComponentKind
    label: str = Field(description="Short designator shown on the schematic, e.g. 'R1'.")
    name: str = Field(description="Kind name for prose, e.g. 'resistor'.")
    value_si: float | None = Field(
        default=None, description="Canonical SI value (ohms or farads). None for the source."
    )
    unit: ValueUnit | None = None
    display_value: str | None = Field(default=None, examples=["1 kΩ", "100 nF"])
    nodes: tuple[NodeId, NodeId] = Field(
        description="(first, second) terminal nodes. For V1: (positive, negative)."
    )
    editable: bool
    bounds: ValueBounds | None = None
    description: str = Field(
        description="Trusted topology text built from the graph, e.g. "
        "'C1 — capacitor — 100 nF — connected between Output and Ground'."
    )


class Tone(Contract):
    frequency_hz: float = Field(gt=0)
    amplitude_v: float = Field(gt=0, description="Peak amplitude in volts.")
    label: str = Field(examples=["Musical tone", "Whistle"])
    role: Literal["wanted", "interference"]


class SourceSpec(Contract):
    """Ideal voltage source V1 between ``in`` (+) and ``gnd`` (−).

    AC analyses drive it with ``ac_magnitude_v`` (1 V, 0°) so output voltage equals the
    transfer function and source current equals input admittance × 1 V. Transient analyses
    drive it with the sum of ``tones`` (sines starting at t = 0, zero phase)."""

    kind: Literal["ideal_voltage"] = "ideal_voltage"
    ac_magnitude_v: float = 1.0
    tones: list[Tone] = Field(min_length=1, max_length=4)
    description: str


class PredictedResponse(Contract):
    """Analytical prediction for the revision's nominal values. This is NOT a measurement."""

    basis: Literal["analytical_first_order_rc"] = "analytical_first_order_rc"
    cutoff_hz: float
    time_constant_s: float


class ComponentChange(Contract):
    component_id: ComponentId
    from_value_si: float
    to_value_si: float
    from_display: str
    to_display: str


RevisionOrigin = Literal["template_default", "user_edit", "agent_proposal_applied", "case_intended"]


class Revision(Contract):
    """Immutable circuit revision. Editing values always creates a new revision."""

    id: RevisionId
    template_id: TemplateId
    title: str = Field(examples=["RC low-pass filter"])
    parent_id: RevisionId | None = None
    origin: RevisionOrigin
    created_at: datetime
    nodes: list[CircuitNode]
    components: list[Component]
    source: SourceSpec
    prediction: PredictedResponse
    changes: list[ComponentChange] = Field(
        default_factory=list, description="Differences from parent_id (empty for roots)."
    )
    frozen: bool = Field(
        default=False,
        description="True for a case's intended design: it cannot be edited or used as a "
        "parent while the case is open.",
    )


class CircuitTemplate(Contract):
    id: TemplateId
    title: str
    summary: str
    default_revision: Revision = Field(
        description="A ready-to-simulate revision with the template's default values."
    )


# --------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------


class ComponentValueInput(Contract):
    component_id: EditableComponentId
    value: str | float = Field(
        description="A number in canonical SI units, or text with an explicit SI prefix and an "
        "optional unit, e.g. '4.7k', '4.7 kΩ', '100n', '100 nF', '0.1 µF'. Ambiguous or "
        "out-of-range values are rejected with a validation_error naming the field.",
        examples=["1k", "100 nF"],
    )

    @field_validator("value", mode="before")
    @classmethod
    def _reject_booleans(cls, value: object) -> object:
        # JSON true/false would otherwise be coerced to 1.0/0.0 (verification finding C-03).
        if isinstance(value, bool):
            raise ValueError("enter a number or text such as '4.7k', not true/false")
        return value


class RevisionCreateRequest(Contract):
    parent_id: RevisionId | None = Field(
        default=None, description="Parent revision. Omit to start from a template default."
    )
    template_id: TemplateId | None = Field(
        default=None, description="Required when parent_id is omitted."
    )
    values: list[ComponentValueInput] = Field(default_factory=list, max_length=2)
    origin: Literal["user_edit", "agent_proposal_applied"] = "user_edit"
    proposal_id: ProposalId | None = None


class ValueParseRequest(Contract):
    component_id: EditableComponentId
    text: str = Field(max_length=32)
    template_id: TemplateId = "rc_lowpass"


class ValueParseResult(Contract):
    ok: bool
    value_si: float | None = None
    display_value: str | None = None
    message: str | None = Field(
        default=None, description="Why the text was rejected (ambiguous, invalid, out of range)."
    )
