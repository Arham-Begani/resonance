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
from resonance.contracts.circuits import ComponentValueInput, RevisionCreateRequest, TemplateId
from resonance.contracts.common import ErrorCode, new_id
from resonance.contracts.policy import TEMPLATE_DEFAULT_GOALS, TEMPLATE_TONES, TRANSIENT_SETTLE_S
from resonance.simulation import analytic
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


def test_both_families_are_offered_with_graph_derived_descriptions() -> None:
    offered = {t.id: t for t in templates.list_templates()}
    assert list(offered) == ["rc_lowpass", "rc_highpass"]
    assert templates.available_template_ids() == ("rc_lowpass", "rc_highpass")

    low = offered["rc_lowpass"].default_revision
    by_id = {c.id: c for c in low.components}
    assert set(by_id) == {"V1", "R1", "C1"}
    assert by_id["R1"].nodes == ("in", "out") and by_id["C1"].nodes == ("out", "gnd")
    assert by_id["V1"].nodes == ("in", "gnd") and not by_id["V1"].editable
    assert (
        by_id["C1"].description == "C1 — capacitor — 100 nF — connected between Output and Ground"
    )
    assert by_id["R1"].description == "R1 — resistor — 1 kΩ — connected between Input and Output"
    assert [n.label for n in low.nodes] == ["Input", "Output", "Ground"]
    assert [t.frequency_hz for t in low.source.tones] == [440.0, 4000.0]
    assert low.prediction.basis == "analytical_first_order_rc"
    assert math.isclose(low.prediction.cutoff_hz, 1 / (2 * math.pi * 1e3 * 100e-9))
    assert offered["rc_lowpass"].summary.startswith(
        "A resistor (R1) from Input to Output and a capacitor (C1) from Output to Ground."
    )

    high_template = offered["rc_highpass"]
    high = high_template.default_revision
    assert high.title == high_template.title == "RC high-pass filter"
    by_id = {c.id: c for c in high.components}
    assert [c.id for c in high.components] == ["V1", "C1", "R1"]  # signal-path order
    assert by_id["C1"].nodes == ("in", "out") and by_id["R1"].nodes == ("out", "gnd")
    assert by_id["V1"].nodes == ("in", "gnd") and not by_id["V1"].editable
    assert by_id["C1"].editable and by_id["R1"].editable
    low_bounds = {c.id: c.bounds for c in low.components}
    assert all(by_id[cid].bounds == low_bounds[cid] for cid in ("R1", "C1"))  # same bounds
    assert by_id["C1"].description == "C1 — capacitor — 100 nF — connected between Input and Output"
    assert by_id["R1"].description == "R1 — resistor — 10 kΩ — connected between Output and Ground"
    assert "50 Hz + 440 Hz" in by_id["V1"].description
    assert high_template.summary.startswith(
        "A capacitor (C1) from Input to Output and a resistor (R1) from Output to Ground."
    )
    assert high_template.cutoff_range_hz == (20.0, 20_000.0)
    assert high.source.tones == list(TEMPLATE_TONES["rc_highpass"])
    assert "50 Hz Mains hum at 0.3 V peak + 440 Hz Musical tone at 0.6 V peak" in (
        high.source.description
    )
    assert math.isclose(high.prediction.cutoff_hz, 1 / (2 * math.pi * 10e3 * 100e-9))
    goal_title, goal_constraints = TEMPLATE_DEFAULT_GOALS["rc_highpass"]
    assert high_template.default_goal.title == goal_title
    assert [(c.kind, c.frequency_hz) for c in high_template.default_goal.constraints] == [
        (kind, f) for kind, f, _limit, _label in goal_constraints
    ]
    with pytest.raises(templates.TemplateUnavailableError):
        templates.get_template("rc_bandpass")


def test_highpass_default_is_a_teaching_start_not_a_solution() -> None:
    """Analytical prediction for the default high-pass (fc ≈ 159 Hz): it keeps the 440 Hz tone
    within 1 dB but reduces the 50 Hz hum by less than the default goal's 12 dB, and the
    goal's feasible cutoffs (192.7–223.9 Hz) lie inside the supported range."""
    values = templates.get_template("rc_highpass").default_values()
    gains = analytic.gain_db(analytic.transfer("rc_highpass", [50.0, 440.0], values))
    assert -1.0 < gains[1] < 0.0
    assert -12.0 < gains[0] < -10.0
    lo = 50.0 * math.sqrt(10 ** (12 / 10) - 1)  # attenuate 50 Hz by ≥ 12 dB
    hi = 440.0 * math.sqrt(10 ** (1 / 10) - 1)  # keep 440 Hz within 1 dB
    assert (round(lo, 1), round(hi, 1)) == (192.7, 223.9)
    assert analytic.cutoff_hz(values["R1"], values["C1"]) < lo
    templates.validate_values("rc_highpass", {"R1": 7.5e3, "C1": 100e-9})  # fc ≈ 212 Hz


@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
def test_cutoff_range_keeps_settling_honest_for_fault_variants(template_id: TemplateId) -> None:
    """The slowest allowed design's 2× fault variant still settles within the public settle
    period (≥ 7 time constants), as the policy requires. Both families decay with τ = R1·C1."""
    template = templates.get_template(template_id)
    tau_slowest = 1 / (2 * math.pi * template.cutoff_min_hz)
    assert TRANSIENT_SETTLE_S >= 7 * (2 * tau_slowest)


@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
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
    template_id: TemplateId, values: dict[str, float], blamed: str, fragment: str
) -> None:
    with pytest.raises(ValueParseError) as excinfo:
        templates.validate_values(template_id, values, edited=("R1", "C1"))
    assert excinfo.value.component_id == blamed
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
def test_boundary_values_are_accepted(template_id: TemplateId) -> None:
    templates.validate_values(template_id, {"R1": 100e3, "C1": 1e-9})  # fc ≈ 1.59 kHz
    templates.validate_values(template_id, {"R1": 100.0, "C1": 10e-6})  # fc ≈ 159 Hz


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


def test_template_or_parent_is_required(store: DocumentStore) -> None:
    with pytest.raises(ApiException) as excinfo:
        create_revision(store, "s1", _req())
    assert excinfo.value.error.field == "template_id"
    assert excinfo.value.error.code == ErrorCode.VALIDATION_ERROR


def test_highpass_root_and_same_family_edit(store: DocumentStore) -> None:
    root = create_revision(store, "s1", _req(template_id="rc_highpass"))
    assert root.template_id == "rc_highpass" and root.origin == "template_default"
    assert revision_values(root) == {"C1": 100e-9, "R1": 10e3}
    child = create_revision(store, "s1", _req(parent_id=root.id, values=[_value("R1", "7.5k")]))
    assert child.template_id == "rc_highpass" and child.source == root.source
    assert [(c.component_id, c.to_display) for c in child.changes] == [("R1", "7.5 kΩ")]
    circuit = revision_to_circuit(child)
    assert circuit.template_id == "rc_highpass" and circuit.disconnected == frozenset()
    assert [t.frequency_hz for t in circuit.source.tones] == [50.0, 440.0]


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


# --------------------------------------------------------------------------- family switch


def test_switching_family_creates_a_linked_revision_of_the_target_template(
    store: DocumentStore,
) -> None:
    low = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    low_edit = create_revision(store, "s1", _req(parent_id=low.id, values=[_value("R1", "2k2")]))

    high = create_revision(store, "s1", _req(parent_id=low_edit.id, template_id="rc_highpass"))
    assert high.template_id == "rc_highpass" and high.title == "RC high-pass filter"
    assert high.parent_id == low_edit.id  # linked for history/undo
    assert high.origin == "user_edit" and high.changes == [] and not high.frozen
    assert revision_values(high) == {"C1": 100e-9, "R1": 10e3}  # target defaults, not 2.2 kΩ
    assert high.source.tones == list(TEMPLATE_TONES["rc_highpass"])
    assert {c.id: c.nodes for c in high.components}["C1"] == ("in", "out")
    assert math.isclose(high.prediction.cutoff_hz, 1 / (2 * math.pi * 10e3 * 100e-9))

    # Back to low-pass with an explicit value: the template default fills the other one.
    back = create_revision(
        store,
        "s1",
        _req(parent_id=high.id, template_id="rc_lowpass", values=[_value("R1", "4.7k")]),
    )
    assert back.template_id == "rc_lowpass" and back.parent_id == high.id
    assert back.changes == [] and back.origin == "user_edit"
    assert revision_values(back) == {"R1": 4700.0, "C1": 100e-9}
    assert back.source.tones == list(TEMPLATE_TONES["rc_lowpass"])
    assert get_revision(store, "s1", low_edit.id) == low_edit  # parent untouched


def test_switching_family_validates_values_and_respects_frozen_parents(
    store: DocumentStore,
) -> None:
    low = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    with pytest.raises(ApiException) as excinfo:
        create_revision(
            store,
            "s1",
            _req(parent_id=low.id, template_id="rc_highpass", values=[_value("C1", "10f")]),
        )
    assert excinfo.value.error.field == "values.0.value"
    with pytest.raises(ApiException) as excinfo:  # joint cutoff: 100 kΩ · 10 µF ≈ 0.16 Hz
        create_revision(
            store,
            "s1",
            _req(
                parent_id=low.id,
                template_id="rc_highpass",
                values=[_value("R1", "100k"), _value("C1", "10u")],
            ),
        )
    assert excinfo.value.error.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.error.field == "values.1.value" and "cutoff" in excinfo.value.error.message

    case_rev = build_revision("rc_lowpass", {}, origin="case_intended", frozen=True)
    store.put("revision", case_rev.id, "s1", case_rev)
    with pytest.raises(ApiException) as excinfo:
        create_revision(store, "s1", _req(parent_id=case_rev.id, template_id="rc_highpass"))
    assert excinfo.value.error.code == ErrorCode.STALE_REVISION and excinfo.value.status == 409


def test_build_revision_family_switch_is_pure() -> None:
    low = build_revision("rc_lowpass", {"R1": 3300.0}, origin="user_edit")
    snapshot = low.model_dump()
    high = build_revision("rc_highpass", {"C1": 47e-9}, origin="user_edit", parent=low)
    assert high.parent_id == low.id and high.changes == []
    assert revision_values(high) == {"C1": 47e-9, "R1": 10e3}
    assert low.model_dump() == snapshot


def _switch_proposal(
    store: DocumentStore,
    session: str,
    base_id: str,
    template_id: TemplateId | None,
    changes: dict[str, float],
) -> EditProposal:
    proposal = EditProposal(
        id=new_id("prop"),
        base_revision_id=base_id,
        template_id=template_id,
        changes=[
            ProposedChange(
                component_id=cid,
                from_value_si=1.0,
                to_value_si=value,
                from_display="-",
                to_display="-",
            )
            for cid, value in changes.items()
        ],
        rationale="test",
        predicted=[PredictedToneGain(frequency_hz=50.0, label="Mains hum", predicted_gain_db=-12)],
        status="pending",
        created_at=datetime.now(UTC),
    )
    store.put("proposal", proposal.id, session, proposal)
    return proposal


def test_applying_a_family_switch_proposal(store: DocumentStore) -> None:
    low = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    proposal = _switch_proposal(store, "s1", low.id, "rc_highpass", {"C1": 100e-9, "R1": 7500.0})
    rev = create_revision(
        store,
        "s1",
        _req(parent_id=low.id, origin="agent_proposal_applied", proposal_id=proposal.id),
    )
    assert rev.template_id == "rc_highpass" and rev.parent_id == low.id
    assert rev.origin == "agent_proposal_applied" and rev.changes == []
    assert revision_values(rev) == {"C1": 100e-9, "R1": 7500.0}
    assert rev.source.tones == list(TEMPLATE_TONES["rc_highpass"])
    stored = store.get("proposal", proposal.id, "s1", EditProposal)
    assert stored is not None and stored.status == "applied"


def test_family_switch_proposal_with_matching_request_template_and_values(
    store: DocumentStore,
) -> None:
    low = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    proposal = _switch_proposal(store, "s1", low.id, "rc_highpass", {"C1": 100e-9, "R1": 7500.0})
    rev = create_revision(
        store,
        "s1",
        _req(
            parent_id=low.id,
            template_id="rc_highpass",
            origin="agent_proposal_applied",
            proposal_id=proposal.id,
            values=[_value("R1", "7.5k"), _value("C1", "100n")],
        ),
    )
    assert rev.template_id == "rc_highpass" and revision_values(rev)["R1"] == 7500.0


@pytest.mark.parametrize(
    ("proposal_template", "proposal_changes", "request_template", "field"),
    [
        # The request names a different family than the proposal targets.
        ("rc_highpass", {"C1": 100e-9, "R1": 7500.0}, "rc_lowpass", "template_id"),
        # A same-family proposal cannot be applied as a family switch.
        (None, {"R1": 1600.0}, "rc_highpass", "template_id"),
        # A family switch must list both component values.
        ("rc_highpass", {"R1": 7500.0}, None, "proposal_id"),
    ],
)
def test_inconsistent_family_switch_proposals_are_rejected(
    store: DocumentStore,
    proposal_template: TemplateId | None,
    proposal_changes: dict[str, float],
    request_template: TemplateId | None,
    field: str,
) -> None:
    low = create_revision(store, "s1", _req(template_id="rc_lowpass"))
    proposal = _switch_proposal(store, "s1", low.id, proposal_template, proposal_changes)
    with pytest.raises(ApiException) as excinfo:
        create_revision(
            store,
            "s1",
            _req(
                parent_id=low.id,
                template_id=request_template,
                origin="agent_proposal_applied",
                proposal_id=proposal.id,
            ),
        )
    assert excinfo.value.error.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.error.field == field
    stored = store.get("proposal", proposal.id, "s1", EditProposal)
    assert stored is not None and stored.status == "pending"  # still applicable after a fix
