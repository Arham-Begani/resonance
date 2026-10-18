"""Case generator properties (V-05/V-07), the hidden store, and deterministic evidence logic."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pytest
from challenges_testkit import AnalyticTestSimulator

from resonance.challenges import hypotheses
from resonance.challenges.evaluator import Evaluator, EvaluatorStore, faults
from resonance.challenges.instruments import CASE_FREQUENCY_GRID_HZ, INSTRUMENTS
from resonance.circuits.templates import validate_values
from resonance.contracts.investigation import (
    DiagnosisSubmission,
    EvidenceCitation,
    EvidenceState,
    HypothesisAssessment,
    InstrumentKind,
)
from resonance.contracts.policy import CASE_NOMINAL_CUTOFF_RANGE_HZ, FAULT_PRIOR
from resonance.store.artifacts import ArtifactStore
from resonance.store.db import DocumentStore


def test_every_drawable_design_is_valid_and_unambiguous_per_instrument() -> None:
    designs = faults.nominal_designs()
    assert len(designs) >= 40
    lo, hi = CASE_NOMINAL_CUTOFF_RANGE_HZ
    for r, c in designs:
        nominal = {"R1": r, "C1": c}
        assert lo <= faults.cutoff_hz(nominal) <= hi
        for h in hypotheses.HYPOTHESIS_IDS:  # every variant must be valid (no conditioning)
            validate_values("rc_lowpass", hypotheses.hypothesis_values(nominal, h))
        for kind, probe, a, b, ratio in faults.pair_ratios("rc_lowpass", nominal):
            assert ratio < 0.5 or ratio > 2.0, (r, c, kind, probe, a, b, ratio)
            if kind == InstrumentKind.TRANSIENT_CAPTURE and probe == "output_voltage":
                if hypotheses.voltage_equivalent(a, b):
                    assert ratio < 0.5
                else:
                    assert ratio >= 3.0, (r, c, a, b, ratio)  # the symptom separates them


def test_voltage_equivalent_pairs_are_exactly_indistinguishable_by_voltage() -> None:
    for r, c in faults.nominal_designs():
        nominal = {"R1": r, "C1": c}
        for a, b in (("r1_increased", "c1_increased"), ("r1_decreased", "c1_decreased")):
            for kind, probe in faults.INSTRUMENT_VIEWS:
                pa = hypotheses.predictions(
                    "rc_lowpass", hypotheses.hypothesis_values(nominal, a), kind, probe
                )
                pb = hypotheses.predictions(
                    "rc_lowpass", hypotheses.hypothesis_values(nominal, b), kind, probe
                )
                ratio = hypotheses.separation_ratio(pa, pb)
                voltage_only = kind == InstrumentKind.OUTPUT_VOLTAGE_SWEEP or probe == (
                    "output_voltage"
                )
                assert (ratio < 1e-9) if voltage_only else (ratio > 2.0)


def test_fault_prior_and_independence_from_nominal_design() -> None:
    designs = set(faults.nominal_designs())
    n = 20_000
    fault_counts: Counter[str] = Counter()
    nominal_by_fault: dict[str, set[tuple[float, float]]] = defaultdict(set)
    for i in range(n):
        d = faults.generate(f"prior-{i}")
        fault_counts[d.fault.hypothesis_id] += 1
        nominal_by_fault[d.fault.hypothesis_id].add((d.nominal["R1"], d.nominal["C1"]))
    for fault, prior in FAULT_PRIOR.items():
        assert abs(fault_counts[fault] / n - prior) < 0.015, fault_counts
        # Every fault can come with every nominal design: public values carry no fault info.
        assert nominal_by_fault[fault] == designs, fault


def test_seeds_are_opaque_and_generation_is_reproducible() -> None:
    seeds = {faults.new_seed() for _ in range(200)}
    assert len(seeds) == 200
    assert all(re.fullmatch(r"[0-9a-f]{32}", s) for s in seeds)
    a, b = faults.generate("reproduce-me"), faults.generate("reproduce-me")
    assert a == b
    assert a.actual == hypotheses.hypothesis_values(a.nominal, a.fault.hypothesis_id)


def test_evaluator_store_reveals_only_after_close(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path / "public.sqlite3")
    artifacts = ArtifactStore(tmp_path / "art", store, max_count=10, max_bytes=1 << 20)
    hidden = EvaluatorStore(tmp_path / "evaluator.sqlite3")
    evaluator = Evaluator(hidden, AnalyticTestSimulator(), artifacts)
    design = evaluator.create_case_from_seed("case_storetest01", "store-seed")
    expected = faults.generate("store-seed")
    assert design.values == dict(expected.nominal)
    with pytest.raises(PermissionError):
        evaluator.reveal("case_storetest01")
    evaluator.mark_closed("case_storetest01")
    data = evaluator.reveal("case_storetest01")
    assert (data.seed, data.hypothesis_id) == ("store-seed", expected.fault.hypothesis_id)
    evaluator.discard("case_storetest01")
    assert hidden.get("case_storetest01") is None
    evaluator.close()
    store.close()


def test_declared_grid_matches_ngspice_decade_sweep() -> None:
    grid = np.asarray(CASE_FREQUENCY_GRID_HZ)
    assert len(grid) == 31 and grid[0] == 20.0
    assert np.allclose(grid, np.geomspace(20.0, 20_000.0, 31), rtol=1e-12)
    kinds = [spec.kind for spec in INSTRUMENTS]
    assert kinds == list(InstrumentKind)
    transient = next(s for s in INSTRUMENTS if s.kind == InstrumentKind.TRANSIENT_CAPTURE)
    assert transient.probes == ["output_voltage", "source_current"]


def _state(remaining: list[str]) -> EvidenceState:
    return EvidenceState(
        assessments=[
            HypothesisAssessment(
                hypothesis_id=h,
                status="unresolved" if h in remaining else "contradicted",
                checks=[],
            )
            for h in hypotheses.HYPOTHESIS_IDS
        ],
        remaining=remaining,  # type: ignore[arg-type]
        statement="",
    )


@pytest.mark.parametrize(
    ("conclusion", "hypothesis", "remaining", "status", "unsupported"),
    [
        ("fault", "r1_increased", ["r1_increased"], "supported", False),
        ("fault", "r1_increased", ["r1_increased", "c1_increased"], "ambiguous", True),
        ("fault", "r1_decreased", ["r1_increased", "c1_increased"], "contradicted", True),
        ("no_fault", None, ["no_fault"], "supported", False),
        ("no_fault", None, ["r1_increased"], "contradicted", True),
        (
            "insufficient_evidence",
            None,
            ["r1_increased", "c1_increased"],
            "consistent_abstention",
            False,
        ),
        ("insufficient_evidence", None, ["r1_increased"], "premature_abstention", False),
        ("fault", "c1_increased", [], "no_supported_hypothesis", False),
    ],
)
def test_evidence_check_statuses(
    conclusion: str, hypothesis: str | None, remaining: list[str], status: str, unsupported: bool
) -> None:
    payload: dict[str, object] = {"conclusion": conclusion, "explanation": "x"}
    if conclusion != "insufficient_evidence":
        payload["citations"] = [
            EvidenceCitation(observation_id="obs_xxxxxxxxxx", quantity="tone_gain_db")
        ]
    if hypothesis:
        payload["hypothesis_id"] = hypothesis
    submission = DiagnosisSubmission.model_validate(payload)
    check = hypotheses.evidence_check(submission, _state(remaining))
    assert check.status == status
    assert hypotheses.unsupported_certainty(submission, check) is unsupported
    assert "%" not in check.message  # no likelihood percentages


def test_assessment_without_observations_and_withheld_instruments() -> None:
    state = hypotheses.assess(
        "rc_lowpass", {"R1": 1000.0, "C1": 1e-7}, [], publish_distinguishing=False
    )
    assert state.remaining == list(hypotheses.HYPOTHESIS_IDS)
    assert {a.status for a in state.assessments} == {"unresolved"}
    assert state.distinguishing_instruments is None
    pair = hypotheses.distinguishing_instruments(
        "rc_lowpass", {"R1": 1000.0, "C1": 1e-7}, ["r1_increased", "c1_increased"]
    )
    assert pair == [InstrumentKind.SOURCE_CURRENT_SWEEP, InstrumentKind.TRANSIENT_CAPTURE]
    assert (
        hypotheses.distinguishing_instruments(
            "rc_lowpass", {"R1": 1000.0, "C1": 1e-7}, ["no_fault"]
        )
        == []
    )


def test_statements_never_name_instruments() -> None:
    words = ("sweep", "capture", "current", "probe", "instrument", "measure the")
    for k in range(len(hypotheses.HYPOTHESIS_IDS) + 1):
        remaining = list(hypotheses.HYPOTHESIS_IDS[:k])
        text = hypotheses._statement(remaining, True).lower()  # noqa: SLF001
        assert not any(w in text for w in words), text


def test_current_tolerance_has_absolute_floor() -> None:
    assert math.isclose(hypotheses.current_tolerance_ma(0.0), 1e-6)
    assert math.isclose(hypotheses.current_tolerance_ma(1.0), 0.03 + 1e-6)
