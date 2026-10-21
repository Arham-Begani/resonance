"""The private evaluation harness (``evals/``): frozen held-out set integrity, its disjointness
from development fixtures, a fast baseline smoke run, and the Nemotron runner's refusal without
credentials.

The smoke run uses the analytic TEST DOUBLE (``AnalyticTestSimulator``); the committed baseline
results in ``evals/results`` come from real ngspice (``evals/run_baseline.py``).
"""

from __future__ import annotations

import ast
import json
import math
import sys
from collections import Counter
from pathlib import Path

import pytest
from challenges_testkit import AnalyticTestSimulator

from resonance.challenges.evaluator import faults
from resonance.challenges.hypotheses import HYPOTHESIS_IDS
from resonance.config import Settings

REPO = Path(__file__).resolve().parents[4]
TESTS = REPO / "services" / "api" / "tests"
sys.path.insert(0, str(REPO / "evals"))

import casesets  # noqa: E402
import harness  # noqa: E402
import report  # noqa: E402
import run_nemotron  # noqa: E402

FIXTURE_DIRS = ("challenges", "verification", "agent")


def _numbers(path: Path) -> set[float]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        float(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    }


def _near(values: set[float], candidates: tuple[float, ...]) -> set[float]:
    return {c for c in candidates if any(math.isclose(v, c, rel_tol=1e-9) for v in values)}


def development_designs() -> set[tuple[float, float]]:
    """Every (R1, C1) design a test could use as a fixture, conservatively: any candidate R
    and any candidate C appearing as literals in the same test file, plus the computed
    lowest/highest-cutoff designs of each family (used by the ngspice family tests)."""
    used: set[tuple[float, float]] = set()
    for sub in FIXTURE_DIRS:
        for path in (TESTS / sub).rglob("*.py"):
            nums = _numbers(path)
            rs = _near(nums, faults.NOMINAL_R1_OHMS)
            cs = _near(nums, faults.NOMINAL_C1_F)
            used |= {(r, c) for r in rs for c in cs}
    for family in casesets.FAMILIES:
        ordered = sorted(
            faults.nominal_designs(family), key=lambda d: faults.cutoff_hz({"R1": d[0], "C1": d[1]})
        )
        used |= {ordered[0], ordered[-1]}
    return used


def test_heldout_designs_are_disjoint_from_development_fixtures() -> None:
    used = development_designs()
    assert len(used) >= 10  # the scan really found the fixtures
    for family in casesets.FAMILIES:
        heldout = set(casesets.HELDOUT_DESIGNS[family])
        assert len(heldout) == 6
        assert heldout <= set(faults.nominal_designs(family)), family  # drawable
        assert not heldout & used, (family, sorted(heldout & used))
        assert not heldout & set(casesets.DEV_DESIGNS[family])
    # This file must not itself become a fixture of held-out values.
    assert not _near(_numbers(Path(__file__)), faults.NOMINAL_R1_OHMS)


def test_frozen_heldout_set_is_balanced_and_reproducible() -> None:
    loaded = casesets.load("heldout_v1")  # verifies every seed against the generator
    assert loaded.generator_version == faults.GENERATOR_VERSION
    assert len(loaded.cases) >= 24
    cells = Counter((c.template_id, c.expected_condition) for c in loaded.cases)
    assert set(cells) == {(f, h) for f in casesets.FAMILIES for h in HYPOTHESIS_IDS}
    assert set(cells.values()) == {2}
    for family in casesets.FAMILIES:
        for condition in HYPOTHESIS_IDS:
            designs = {
                c.expected_nominal
                for c in loaded.cases
                if c.template_id == family and c.expected_condition == condition
            }
            assert len(designs) == 2  # each condition is seen with two different designs
            assert designs <= set(casesets.HELDOUT_DESIGNS[family])
    seeds = [c.seed for c in loaded.cases]
    assert len(set(seeds)) == len(seeds)
    assert all(len(s) == 32 and int(s, 16) >= 0 for s in seeds)
    text = casesets.path_for("heldout_v1").read_text(encoding="utf-8")
    assert text == casesets.dumps(casesets.build("heldout_v1"))  # frozen == deterministic build
    dev = casesets.load("dev_v1")
    assert len(dev.cases) == 12 and not {c.seed for c in dev.cases} & set(seeds)


async def test_baseline_smoke_run_and_report(settings: Settings, tmp_path: Path) -> None:
    dev = casesets.load("dev_v1")
    wanted = {("rc_lowpass", "r1_increased"), ("rc_highpass", "c1_disconnected"),
              ("rc_lowpass", "no_fault")}
    specs = [c for c in dev.cases if (c.template_id, c.expected_condition) in wanted]
    assert len(specs) == 3
    lines: list[str] = []
    run = await harness.run_set(
        "baseline", dev, specs, settings, simulator=AnalyticTestSimulator(), progress=lines.append
    )
    assert len(lines) == 3
    by_truth = {r["true_condition"]: r for r in run["results"]}
    assert set(by_truth) == {"r1_increased", "c1_disconnected", "no_fault"}
    for r in run["results"]:
        assert r["completed"] and r["error"] is None
        assert r["exact_identification"] == "correct"
        assert r["unsupported_certainty"] is False
        assert r["experiments_used"] == 2
        assert r["experiment_sequence"] == ["output_voltage_sweep", "source_current_sweep"]
        assert r["latency_s"]["total"] > 0
    assert by_truth["r1_increased"]["repair_component"] == "R1"
    assert by_truth["r1_increased"]["functional_recovery"] == "restored"
    assert by_truth["c1_disconnected"]["repair_component"] == "C1"
    assert by_truth["c1_disconnected"]["functional_recovery"] == "restored"
    assert by_truth["no_fault"]["functional_recovery"] == "not_needed"
    run["summary"] = report.summarize(run["results"])
    run["notes"] = list(report.HONEST_NOTES)
    json_path, md_path = report.write_run(run, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["totals"]["correct"] == 3
    markdown = md_path.read_text(encoding="utf-8")
    assert "RC high-pass / c1_disconnected" in markdown
    assert "solvable with the standardized symptom plus ONE current" in markdown
    assert "superiority" in markdown


def test_baseline_abstains_when_two_explanations_remain() -> None:
    from resonance.contracts.investigation import EvidenceState

    class _Case:
        evidence = EvidenceState(
            assessments=[], remaining=["r1_increased", "c1_increased"], statement=""
        )

    submission = harness.baseline_submission(_Case(), ["obs_aaaaaaaaaaaa", "obs_bbbbbbbbbbbb"])  # type: ignore[arg-type]
    assert submission.conclusion.value == "insufficient_evidence"
    assert submission.candidate_hypothesis_ids == ["r1_increased", "c1_increased"]


def test_nemotron_runner_refuses_without_credentials(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert settings.nebius_api_key is None
    code = run_nemotron.main(["--set", "dev_v1", "--results-dir", str(tmp_path)], settings)
    assert code == run_nemotron.EXIT_AI_UNAVAILABLE
    err = capsys.readouterr().err
    assert "NOT run" in err and "No results were written" in err
    assert list(tmp_path.iterdir()) == []
    keyed = settings.model_copy(update={"resonance_dev_agent_fixture": True})
    assert run_nemotron.refusal(keyed) is not None  # a scripted fixture is never evaluated
