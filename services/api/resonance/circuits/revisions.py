"""Immutable circuit revisions: pure builders and session-scoped persistence.

Owner: circuit & simulation engineer.

Public API (stable):

- ``build_revision(template_id, values, *, origin, parent=None, frozen=False, edited=())
  -> Revision`` — pure. ``values`` may be partial; missing editable values come from ``parent``
  (if given) or the template defaults. Validates bounds and the joint cutoff constraint
  (``ValueParseError``), assigns a new opaque ID, the analytical ``prediction`` (labeled, not a
  measurement), ``changes`` versus ``parent``, and trusted descriptions. The investigation
  engineer builds a case's intended design with
  ``build_revision(..., origin="case_intended", frozen=True)`` and stores it themselves.
- ``revision_values(rev) -> dict[str, float]`` — editable component values (SI).
- ``revision_to_circuit(rev) -> CircuitSpec`` — what to simulate for a PUBLIC revision.
- ``create_revision(store, session_id, RevisionCreateRequest) -> Revision`` — parse, validate,
  persist (kind ``"revision"``). Raises ``ApiException``: validation_error naming
  ``values.<i>.value`` / ``template_id`` / ``origin`` / ``proposal_id``; not_found for an unknown
  or other-session parent or proposal; stale_revision for a frozen parent or a proposal made for
  a different revision; conflict for a proposal that is no longer pending.
  A request with a ``parent_id`` and no ``values`` creates an unchanged copy whose parent is
  that revision (``changes == []``) — e.g. to restore an earlier revision as the newest (undo).
- ``get_revision(store, session_id, revision_id) -> Revision`` — raises not_found (404).
- ``find_revision(store, session_id, revision_id) -> Revision | None``.

Applying an explore proposal: ``POST /api/revisions`` with ``parent_id`` = the proposal's
``base_revision_id``, ``origin="agent_proposal_applied"``, ``proposal_id``, and either no
``values`` (the proposal's values are used) or exactly the proposal's values. The proposal is
then marked ``applied``. To change values further, create a normal ``user_edit`` afterwards.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from ..api.errors import ApiException, not_found
from ..contracts.agent import EditProposal
from ..contracts.circuits import (
    ComponentChange,
    PredictedResponse,
    Revision,
    RevisionCreateRequest,
    RevisionOrigin,
    TemplateId,
)
from ..contracts.common import ErrorCode, new_id
from ..contracts.internal import CircuitSpec
from ..simulation import analytic
from ..store.db import DocKind, DocumentStore
from .templates import (
    TemplateUnavailableError,
    build_components,
    default_source,
    get_template,
    parse_component_value,
    validate_values,
)
from .units import ValueParseError, format_value

_REVISION_KIND: DocKind = "revision"


def revision_values(rev: Revision) -> dict[str, float]:
    """Editable component values of a revision in canonical SI units."""
    return {
        c.id: float(c.value_si) for c in rev.components if c.editable and c.value_si is not None
    }


def revision_to_circuit(rev: Revision) -> CircuitSpec:
    """Convert a PUBLIC revision to the simulator's input."""
    return CircuitSpec(
        template_id=rev.template_id,
        values=revision_values(rev),
        source=rev.source.model_copy(deep=True),
    )


def build_revision(
    template_id: TemplateId,
    values: Mapping[str, float],
    *,
    origin: RevisionOrigin,
    parent: Revision | None = None,
    frozen: bool = False,
    edited: Sequence[str] = (),
) -> Revision:
    """Build (but do not store) a new immutable revision. See the module docstring."""
    template = get_template(template_id)
    if parent is not None and parent.template_id != template_id:
        raise ValueParseError("A revision must use the same circuit template as its parent.")
    base = revision_values(parent) if parent is not None else template.default_values()
    merged: dict[str, float] = dict(base)
    for cid, value in values.items():
        if cid not in template.editable_ids:
            raise ValueParseError(f"{cid} is not an editable component of this circuit.")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueParseError(f"{cid} needs a numeric value.", component_id=cid)
        merged[cid] = float(value)
    validate_values(template_id, merged, edited=edited or tuple(values))

    source = parent.source.model_copy(deep=True) if parent else default_source(template_id)
    changes: list[ComponentChange] = []
    if parent is not None:
        for comp in template.components:
            if not comp.editable or comp.unit is None:
                continue
            old, new = base[comp.id], merged[comp.id]
            if old != new:
                changes.append(
                    ComponentChange(
                        component_id=comp.id,
                        from_value_si=old,
                        to_value_si=new,
                        from_display=format_value(old, comp.unit),
                        to_display=format_value(new, comp.unit),
                    )
                )
    r, c = merged["R1"], merged["C1"]
    return Revision(
        id=new_id("rev"),
        template_id=template_id,
        title=template.title,
        parent_id=parent.id if parent else None,
        origin=origin,
        created_at=datetime.now(UTC),
        nodes=[n.model_copy() for n in template.nodes],
        components=build_components(template_id, merged, source),
        source=source,
        prediction=PredictedResponse(
            cutoff_hz=analytic.cutoff_hz(r, c), time_constant_s=analytic.time_constant_s(r, c)
        ),
        changes=changes,
        frozen=frozen,
    )


# --------------------------------------------------------------------------------------
# Persistence (session-scoped)
# --------------------------------------------------------------------------------------


def find_revision(store: DocumentStore, session_id: str, revision_id: str) -> Revision | None:
    return store.get(_REVISION_KIND, revision_id, session_id, Revision)


def get_revision(store: DocumentStore, session_id: str, revision_id: str) -> Revision:
    rev = find_revision(store, session_id, revision_id)
    if rev is None:
        raise not_found("Revision")
    return rev


def _validation(message: str, field: str | None) -> ApiException:
    return ApiException(ErrorCode.VALIDATION_ERROR, message, field=field)


def _load_proposal(
    store: DocumentStore, session_id: str, req: RevisionCreateRequest
) -> EditProposal | None:
    if req.origin != "agent_proposal_applied":
        if req.proposal_id is not None:
            raise _validation(
                "proposal_id is only used when applying a proposal "
                "(origin “agent_proposal_applied”).",
                "proposal_id",
            )
        return None
    if req.proposal_id is None:
        raise _validation("Applying a proposal requires its proposal_id.", "proposal_id")
    proposal = store.get("proposal", req.proposal_id, session_id, EditProposal)
    if proposal is None:
        raise not_found("Proposal")
    if proposal.status != "pending":
        raise ApiException(
            ErrorCode.CONFLICT,
            "This proposal was already applied or dismissed.",
            field="proposal_id",
        )
    if req.parent_id != proposal.base_revision_id:
        raise ApiException(
            ErrorCode.STALE_REVISION,
            "This proposal was made for a different revision.",
            field="parent_id",
        )
    return proposal


def create_revision(store: DocumentStore, session_id: str, req: RevisionCreateRequest) -> Revision:
    """Create, validate, and persist a revision for this session. See the module docstring."""
    parent: Revision | None = None
    if req.parent_id is not None:
        parent = find_revision(store, session_id, req.parent_id)
        if parent is None:
            raise not_found("Revision")
        if parent.frozen:
            raise ApiException(
                ErrorCode.STALE_REVISION,
                "This revision is the intended design of an open investigation and can't be "
                "edited. Start from one of your own revisions.",
                field="parent_id",
            )
        if req.template_id is not None and req.template_id != parent.template_id:
            raise _validation(
                "template_id must match the parent revision's template.", "template_id"
            )
        template_id: TemplateId = parent.template_id
    else:
        if req.template_id is None:
            raise _validation("Choose a circuit template or a parent revision.", "template_id")
        template_id = req.template_id
    try:
        get_template(template_id)
    except TemplateUnavailableError as exc:
        raise _validation(str(exc), "template_id") from None

    proposal = _load_proposal(store, session_id, req)

    values: dict[str, float] = {}
    field_for: dict[str, str] = {}
    for i, item in enumerate(req.values):
        field = f"values.{i}.value"
        if item.component_id in values:
            raise _validation(f"{item.component_id} is listed more than once.", f"values.{i}")
        try:
            value_si, _unit = parse_component_value(template_id, item.component_id, item.value)
        except ValueParseError as exc:
            raise _validation(str(exc), field) from None
        values[item.component_id] = value_si
        field_for[item.component_id] = field

    if proposal is not None:
        proposed: dict[str, float] = {ch.component_id: ch.to_value_si for ch in proposal.changes}
        if not values:
            values = dict(proposed)
        elif set(values) != set(proposed) or any(
            not math.isclose(values[k], proposed[k], rel_tol=1e-9) for k in proposed
        ):
            raise _validation(
                "Applied values must match the proposal. Apply it as proposed, then make "
                "further edits as a normal change.",
                "values",
            )

    origin: RevisionOrigin
    if parent is None and not req.values and proposal is None:
        origin = "template_default"
    else:
        origin = req.origin
    try:
        rev = build_revision(
            template_id, values, origin=origin, parent=parent, edited=tuple(values)
        )
    except ValueParseError as exc:
        err_field = field_for.get(exc.component_id or "", "values" if values else None)
        raise _validation(str(exc), err_field) from None

    store.put(_REVISION_KIND, rev.id, session_id, rev)
    if proposal is not None:
        store.put(
            "proposal",
            proposal.id,
            session_id,
            proposal.model_copy(update={"status": "applied"}),
        )
    return rev
