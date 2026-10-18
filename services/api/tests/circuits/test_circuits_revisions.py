"""Templates and immutable revisions: graph-derived data, bounds, persistence rules."""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from resonance.api.errors import ApiException
from resonance.circuits import templates
from resonance.circuits.revisions import (
    build_revision,
    create_revision,
    get_revision,
    revision_to_circuit,
    revision_values,
)
from resonance.circuits.units import ValueParseError
from resonance.contracts.agent import EditProposal, PredictedToneGain, ProposedChange
from resonance.contracts.circuits import ComponentValueInput, RevisionCreateRequest
from resonance.contracts.common import ErrorCode, new_id
from resonance.contracts.policy import TRANSIENT_SETTLE_S
from resonance.store.db import DocumentStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DocumentStore]:
    db = DocumentStore(tmp_path / "data" / "test.sqlite3")
    yield db
    db.close()


def _req(**kwargs: object) -> RevisionCreateRequest:
    return RevisionCreateRequest.model_validate(kwargs)


def _value(cid: str, value: str | float) -> ComponentValueInput:
    return ComponentValueInput.model_validate({"component_id": cid, "value": value})


# --------------------------------------------------------------------------- templates


def test_only_lowpass_is_offered_with_graph_derived_descriptions() -> None:
    offered = templates.list_templates()
    assert [t.id for t in offered] == ["rc_lowpass"]
    rev = offered[0].default_revision
    by_id = {c.id: c for c in rev.components}
    assert set(by_id) == {"V1", "R1", "C1"}
    assert by_id["R1"].nodes == ("in", "out") and by_id["C1"].nodes == ("out", "gnd")
    assert by_id["V1"].nodes == ("in", "gnd") and not by_id["V1"].editable
    assert (
        by_id["C1"].description == "C1 — capacitor — 100 nF — connected between Output and Ground"
    )
    assert by_id["R1"].description == "R1 — resistor — 1 kΩ — connected between Input and Output"
    assert [n.label for n in rev.nodes] == ["Input", "Output", "Ground"]
    assert [t.frequency_hz for t in rev.source.tones] == [440.0, 4000.0]
    assert rev.prediction.basis == "analytical_first_order_rc"
    assert math.isclose(rev.prediction.cutoff_hz, 1 / (2 * math.pi * 1e3 * 100e-9))
    with pytest.raises(templates.TemplateUnavailableError):
        templates.get_template("rc_highpass")


def test_cutoff_range_keeps_settling_honest_for_fault_variants() -> None:
    """The slowest allowed design's 2× fault variant still settles within the public settle
    period (≥ 7 time constants), as the policy requires."""
    template = templates.get_template("rc_lowpass")
    tau_slowest = 1 / (2 * math.pi * template.cutoff_min_hz)
    assert TRANSIENT_SETTLE_S >= 7 * (2 * tau_slowest)


@pytest.mark.parametrize(
    ("values", "blamed", "fragment"),
    [
        ({"R1": 50.0, "C1": 100e-9}, "R1", "too small"),
        ({"R1": 1e3, "C1": 20e-6}, "C1", "too large"),
        ({"R1": 100e3, "C1": 10e-6}, "C1", "cutoff"),  # fc ≈ 0.16 Hz
        ({"R1": 100.0, "C1": 1e-9}, "C1", "cutoff"),  # fc ≈ 1.6 MHz
    ],
)
def test_validate_values_names_the_component(
    values: dict[str, float], blamed: str, fragment: str
) -> None:
    with pytest.raises(ValueParseError) as excinfo:
        templates.validate_values("rc_lowpass", values, edited=("R1", "C1"))
    assert excinfo.value.component_id == blamed
    assert fragment in str(excinfo.value)


def test_boundary_values_are_accepted() -> None:
    templates.validate_values("rc_lowpass", {"R1": 100e3, "C1": 1e-9})  # fc ≈ 1.59 kHz
    templates.validate_values("rc_lowpass", {"R1": 100.0, "C1": 10e-6})  # fc ≈ 159 Hz


# --------------------------------------------------------------------------- pure builder


def test_build_revision_is_immutable_and_records_changes() -> None:
    root = build_revision("rc_lowpass", {}, origin="template_default")
    snapshot = root.model_dump()
    child = build_revision("rc_lowpass", {"R1": 1600.0}, origin="user_edit", parent=root)
    assert child.id != root.id and child.parent_id == root.id
    assert root.model_dump() == snapshot  # parent untouched
    assert revision_values(child) == {"R1": 1600.0, "C1": 100e-9}
    assert [c.model_dump() for c in child.changes] == [
        {
            "component_id": "R1",
            "from_value_si": 1000.0,
            "to_value_si": 1600.0,
            "from_display": "1 kΩ",
            "to_display": "1.6 kΩ",
        }
    ]
    circuit = revision_to_circuit(child)
    assert circuit.template_id == "rc_lowpass" and dict(circuit.values) == {
        "R1": 1600.0,
        "C1": 100e-9,
    }


def test_case_intended_revision_is_frozen() -> None:
    rev = build_revision(
        "rc_lowpass", {"R1": 2200.0, "C1": 47e-9}, origin="case_intended", frozen=True
    )
    assert rev.frozen and rev.origin == "case_intended" and rev.changes == []


# --------------------------------------------------------------------------- persistence


def test_create_from_template_then_edit(store: DocumentStore) -> None:
    root = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    assert root.origin == "template_default" and root.parent_id is None
    child = create_revision(
        store, "s1", _req(parent_id=root.id, values=[_value("C1", "47n"), _value("R1", "3k3")])
    )
    assert child.origin == "user_edit"
    assert revision_values(child) == {"R1": 3300.0, "C1": 47e-9}
    assert {c.component_id for c in child.changes} == {"R1", "C1"}
    assert get_revision(store, "s1", root.id) == root  # stored parent unchanged


def test_invalid_value_names_the_request_field(store: DocumentStore) -> None:
    root = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    with pytest.raises(ApiException) as excinfo:
        create_revision(
            store, "s1", _req(parent_id=root.id, values=[_value("R1", "2k"), _value("C1", "10f")])
        )
    err = excinfo.value.error
    assert err.code == ErrorCode.VALIDATION_ERROR and err.field == "values.1.value"
    assert "ambiguous" in err.message


def test_bounds_and_joint_violations_name_the_field(store: DocumentStore) -> None:
    root = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    with pytest.raises(ApiException) as excinfo:
        create_revision(store, "s1", _req(parent_id=root.id, values=[_value("R1", "1M")]))
    assert excinfo.value.error.field == "values.0.value"
    assert "too large" in excinfo.value.error.message and "mega" in excinfo.value.error.message

    # Each value is within its own bounds, but the cutoff (≈ 0.16 Hz) is not supported.
    with pytest.raises(ApiException) as excinfo:
        create_revision(
            store,
            "s1",
            _req(parent_id=root.id, values=[_value("R1", "100k"), _value("C1", "10u")]),
        )
    assert excinfo.value.error.field == "values.1.value"
    assert "cutoff" in excinfo.value.error.message


def test_frozen_parent_is_stale(store: DocumentStore) -> None:
    case_rev = build_revision("rc_lowpass", {}, origin="case_intended", frozen=True)
    store.put("revision", case_rev.id, "s1", case_rev)
    with pytest.raises(ApiException) as excinfo:
        create_revision(store, "s1", _req(parent_id=case_rev.id, values=[_value("R1", "2k")]))
    assert excinfo.value.error.code == ErrorCode.STALE_REVISION
    assert excinfo.value.status == 409


def test_other_sessions_revisions_behave_as_missing(store: DocumentStore) -> None:
    theirs = create_revision(store, "other", _req(template_id="rc_lowpass"))
    missing = new_id("rev")
    messages = []
    for rid in (theirs.id, missing):
        with pytest.raises(ApiException) as excinfo:
            create_revision(store, "mine", _req(parent_id=rid, values=[_value("R1", "2k")]))
        assert excinfo.value.status == 404
        messages.append(excinfo.value.error.message)
        with pytest.raises(ApiException) as excinfo:
            get_revision(store, "mine", rid)
        assert excinfo.value.status == 404
    assert messages[0] == messages[1]


def test_template_required_and_unavailable_template_rejected(store: DocumentStore) -> None:
    with pytest.raises(ApiException) as excinfo:
        create_revision(store, "s1", _req())
    assert excinfo.value.error.field == "template_id"
    with pytest.raises(ApiException) as excinfo:
        create_revision(store, "s1", _req(template_id="rc_highpass"))
    assert excinfo.value.error.field == "template_id"


def test_duplicate_component_values_rejected(store: DocumentStore) -> None:
    with pytest.raises(ApiException) as excinfo:
        create_revision(
            store,
            "s1",
            _req(template_id="rc_lowpass", values=[_value("R1", "2k"), _value("R1", "3k")]),
        )
    assert excinfo.value.error.field == "values.1"


def _proposal(store: DocumentStore, session: str, base_id: str, r1: float) -> EditProposal:
    proposal = EditProposal(
        id=new_id("prop"),
        base_revision_id=base_id,
        changes=[
            ProposedChange(
                component_id="R1",
                from_value_si=1000.0,
                to_value_si=r1,
                from_display="1 kΩ",
                to_display="1.6 kΩ",
            )
        ],
        rationale="test",
        predicted=[
            PredictedToneGain(frequency_hz=440.0, label="Musical tone", predicted_gain_db=0)
        ],
        status="pending",
        created_at=datetime.now(UTC),
    )
    store.put("proposal", proposal.id, session, proposal)
    return proposal


def test_applying_a_proposal_marks_it_applied(store: DocumentStore) -> None:
    root = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    proposal = _proposal(store, "s1", root.id, 1600.0)
    rev = create_revision(
        store,
        "s1",
        _req(parent_id=root.id, origin="agent_proposal_applied", proposal_id=proposal.id),
    )
    assert rev.origin == "agent_proposal_applied" and revision_values(rev)["R1"] == 1600.0
    stored = store.get("proposal", proposal.id, "s1", EditProposal)
    assert stored is not None and stored.status == "applied"
    with pytest.raises(ApiException) as excinfo:  # cannot apply twice
        create_revision(
            store,
            "s1",
            _req(parent_id=root.id, origin="agent_proposal_applied", proposal_id=proposal.id),
        )
    assert excinfo.value.error.code == ErrorCode.CONFLICT


def test_proposal_must_match_parent_and_values(store: DocumentStore) -> None:
    root = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    other = create_revision(store, "s1", _req(parent_id=root.id, values=[_value("C1", "47n")]))
    proposal = _proposal(store, "s1", root.id, 1600.0)
    with pytest.raises(ApiException) as excinfo:
        create_revision(
            store,
            "s1",
            _req(parent_id=other.id, origin="agent_proposal_applied", proposal_id=proposal.id),
        )
    assert excinfo.value.error.code == ErrorCode.STALE_REVISION
    with pytest.raises(ApiException) as excinfo:
        create_revision(
            store,
            "s1",
            _req(
                parent_id=root.id,
                origin="agent_proposal_applied",
                proposal_id=proposal.id,
                values=[_value("R1", "2k")],
            ),
        )
    assert excinfo.value.error.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(ApiException) as excinfo:  # another session cannot apply it
        create_revision(
            store,
            "other",
            _req(parent_id=root.id, origin="agent_proposal_applied", proposal_id=proposal.id),
        )
    assert excinfo.value.status == 404
