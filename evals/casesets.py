"""Frozen evaluation case sets (private evaluation harness; never served over HTTP).

A case set is a list of (circuit family, opaque seed) pairs. The seed reproduces the hidden
case through the private in-process entry point ``CaseService.harness_create_case`` with the
generator version recorded in the file; the file also records the expected condition and
nominal design so drift is detected (``load`` refuses a set whose seeds no longer generate the
recorded cases). The expected condition is harness ground truth: it is never shown to the agent
or the baseline, which only see the public case through the CaseService API.

Sets
- ``heldout_v1``: 2 families × 6 conditions × 2 = 24 balanced cases. Nominal designs come from
  ``HELDOUT_DESIGNS``, six drawable designs per family spread over the cutoff range and DISJOINT
  from every design used in ``services/api/tests/{challenges,verification,agent}`` (checked by
  ``tests/challenges/test_evals_harness.py``) and from ``DEV_DESIGNS``. Within a family,
  condition i uses designs i and (i + 3) mod 6, so every condition is seen with two different
  designs and every design with two different conditions.
- ``dev_v1``: 2 families × 6 conditions × 1 = 12 cases on the development designs (which do
  appear in tests). Use it while changing prompts or the harness; report held-out numbers only.

Seeds are ``sha256("resonance-eval|<set>|<family>|<index>|<attempt>")[:32]``: opaque 128-bit
hex strings whose preimage carries no condition; the first attempt that generates the target
case is kept.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from resonance.challenges.evaluator import faults
from resonance.challenges.hypotheses import HYPOTHESIS_IDS
from resonance.contracts.circuits import TemplateId
from resonance.contracts.investigation import HypothesisId

EVALS_DIR = Path(__file__).resolve().parent
CASES_DIR = EVALS_DIR / "cases"
FAMILIES: tuple[TemplateId, ...] = ("rc_lowpass", "rc_highpass")
CONDITIONS: tuple[HypothesisId, ...] = HYPOTHESIS_IDS

Design = tuple[float, float]  # (R1 ohms, C1 farads)

HELDOUT_DESIGNS: dict[TemplateId, tuple[Design, ...]] = {
    "rc_lowpass": (
        (6800.0, 220e-9),  # fc ≈ 106 Hz
        (4700.0, 150e-9),  # ≈ 226 Hz
        (680.0, 680e-9),  # ≈ 344 Hz
        (1500.0, 150e-9),  # ≈ 707 Hz
        (4700.0, 22e-9),  # ≈ 1.54 kHz
        (3300.0, 15e-9),  # ≈ 3.22 kHz
    ),
    "rc_highpass": (
        (4700.0, 680e-9),  # ≈ 49.8 Hz
        (10_000.0, 220e-9),  # ≈ 72 Hz
        (2200.0, 330e-9),  # ≈ 219 Hz
        (10_000.0, 33e-9),  # ≈ 482 Hz
        (680.0, 330e-9),  # ≈ 709 Hz
        (6800.0, 15e-9),  # ≈ 1.56 kHz
    ),
}

DEV_DESIGNS: dict[TemplateId, tuple[Design, ...]] = {
    "rc_lowpass": ((2200.0, 47e-9), (1000.0, 100e-9)),
    "rc_highpass": ((2200.0, 47e-9), (1000.0, 100e-9)),
}

SET_NAMES = ("heldout_v1", "dev_v1")
MAX_SEED_ATTEMPTS = 1_000_000


@dataclass(frozen=True)
class CaseSpec:
    key: str
    template_id: TemplateId
    seed: str
    expected_condition: HypothesisId
    expected_nominal: Design


@dataclass(frozen=True)
class CaseSet:
    name: str
    generator_version: str
    cases: tuple[CaseSpec, ...]
    sha256: str  # of the file content, for result provenance


def _plan(name: str) -> list[tuple[TemplateId, int, HypothesisId, Design]]:
    """(family, index, condition, design) for every case of a set."""
    out: list[tuple[TemplateId, int, HypothesisId, Design]] = []
    for family in FAMILIES:
        if name == "heldout_v1":
            designs = HELDOUT_DESIGNS[family]
            n = len(CONDITIONS)
            for j in range(2 * n):
                condition = CONDITIONS[j % n]
                design = designs[j % n] if j < n else designs[(j % n + 3) % n]
                out.append((family, j, condition, design))
        elif name == "dev_v1":
            designs = DEV_DESIGNS[family]
            for j, condition in enumerate(CONDITIONS):
                out.append((family, j, condition, designs[j % len(designs)]))
        else:
            raise ValueError(f"unknown case set {name!r}")
    return out


def find_seed(name: str, family: TemplateId, index: int, condition: str, design: Design) -> str:
    """The first opaque seed (deterministic search) that generates the target case."""
    for attempt in range(MAX_SEED_ATTEMPTS):
        text = f"resonance-eval|{name}|{family}|{index}|{attempt}"
        seed = hashlib.sha256(text.encode()).hexdigest()[:32]
        generated = faults.generate(seed, family)
        if generated.fault.hypothesis_id == condition and (
            generated.nominal["R1"],
            generated.nominal["C1"],
        ) == design:
            return seed
    raise RuntimeError("no seed found for a planned case")


def build(name: str) -> dict[str, Any]:
    """The JSON content of a case set (deterministic for a given generator version)."""
    cases = []
    for family, index, condition, design in _plan(name):
        short = "lp" if family == "rc_lowpass" else "hp"
        cases.append(
            {
                "key": f"{name}-{short}-{index + 1:02d}",
                "template_id": family,
                "seed": find_seed(name, family, index, condition, design),
                "expected": {
                    "hypothesis_id": condition,
                    "nominal": {"R1": design[0], "C1": design[1]},
                },
            }
        )
    designs = HELDOUT_DESIGNS if name == "heldout_v1" else DEV_DESIGNS
    return {
        "name": name,
        "generator_version": faults.GENERATOR_VERSION,
        "built_by": "evals/build_cases.py",
        "description": (
            "Private evaluation cases (harness ground truth). Each seed reproduces its case "
            "through CaseService.harness_create_case(session, seed, template_id) with this "
            "generator version."
        ),
        "designs": {f: [list(d) for d in designs[f]] for f in FAMILIES},
        "cases": cases,
    }


def dumps(content: Mapping[str, Any]) -> str:
    return json.dumps(content, indent=2, sort_keys=False) + "\n"


def path_for(name: str) -> Path:
    return CASES_DIR / f"{name}.json"


def load(name_or_path: str | Path) -> CaseSet:
    """Load a frozen set and verify that every seed still generates its recorded case."""
    path = Path(name_or_path)
    if not path.suffix:
        path = path_for(str(name_or_path))
    text = path.read_text(encoding="utf-8")
    content = json.loads(text)
    if content["generator_version"] != faults.GENERATOR_VERSION:
        raise RuntimeError(
            f"case set {content['name']} was built for generator {content['generator_version']}, "
            f"but this code is {faults.GENERATOR_VERSION}; build a new frozen set (do not edit "
            "the old one)."
        )
    specs = []
    for raw in content["cases"]:
        family = cast(TemplateId, raw["template_id"])
        nominal = raw["expected"]["nominal"]
        spec = CaseSpec(
            key=raw["key"],
            template_id=family,
            seed=raw["seed"],
            expected_condition=raw["expected"]["hypothesis_id"],
            expected_nominal=(float(nominal["R1"]), float(nominal["C1"])),
        )
        generated = faults.generate(spec.seed, family)
        actual = (generated.nominal["R1"], generated.nominal["C1"])
        if generated.fault.hypothesis_id != spec.expected_condition or actual != (
            spec.expected_nominal
        ):
            raise RuntimeError(f"case {spec.key} no longer generates its recorded case")
        specs.append(spec)
    return CaseSet(
        name=content["name"],
        generator_version=content["generator_version"],
        cases=tuple(specs),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def select(case_set: CaseSet, keys: Sequence[str] | None, limit: int | None) -> list[CaseSpec]:
    cases = list(case_set.cases)
    if keys:
        wanted = set(keys)
        cases = [c for c in cases if c.key in wanted]
        missing = wanted - {c.key for c in cases}
        if missing:
            raise ValueError(f"unknown case keys: {sorted(missing)}")
    return cases[:limit] if limit is not None else cases
