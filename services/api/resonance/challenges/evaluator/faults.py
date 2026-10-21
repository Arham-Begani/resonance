"""Fault catalog and deterministic case generator (evaluator boundary).

The catalog itself is public (it mirrors the public hypothesis catalog), and so are the nominal
design sets, the circuit families and the priors; what is private is which condition a given
case uses, its actual circuit, and the seed that produced it.

Generation is ``generate(seed, template_id) -> HiddenDesign`` using ``random.Random`` seeded
from the generator version, the (public) circuit family, and the seed string, so a published
seed reproduces its case together with the family shown in the intended design.

Design rules (CLAUDE.md, docs/contracts.md V-05/V-07):
- Hosted seeds are ``secrets.token_hex(policy.CASE_SEED_BYTES)`` and unrelated to case IDs.
- The circuit family is public. When the learner does not choose one, ``families.draw_family``
  draws it with ``policy.CASE_FAMILY_PRIOR`` from its own ``secrets`` draw, independent of the
  seed.
- The condition is drawn with ``policy.FAULT_PRIOR`` (1/6 each: no-fault control, four value
  drifts, open capacitor) and the nominal design is drawn uniformly from
  ``nominal_designs(family)``, independently of the condition (i.i.d.; no rejection sampling
  conditioned on the drawn condition). Every drawable nominal design is valid for EVERY
  condition, so the public intended values carry no information about the hidden condition.
- A nominal design is drawable only if: its cutoff is inside
  ``policy.CASE_NOMINAL_CUTOFF_RANGE_HZ``; every variant passes the template's bounds and joint
  cutoff check (``circuits.templates.validate_values``); for every instrument and every pair of
  hypotheses the worst deviation/tolerance ratio is < ``EQUIVALENT_MAX_RATIO`` (indistinguishable)
  or > ``SEPARABLE_MIN_RATIO`` (separable), never in between; and on the standardized symptom
  (with the family's own tones) every non-equivalent pair is at least ``SYMPTOM_MIN_RATIO``
  tolerances apart.
"""

from __future__ import annotations

import math
import random
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from itertools import combinations

from ...contracts.circuits import TemplateId
from ...contracts.investigation import HypothesisId, InstrumentKind, TransientProbe
from ...contracts.policy import CASE_NOMINAL_CUTOFF_RANGE_HZ, CASE_SEED_BYTES, FAULT_PRIOR
from ..hypotheses import (
    HYPOTHESES,
    Prediction,
    hypothesis_disconnected,
    hypothesis_predictions,
    hypothesis_values,
    separation_ratio,
    voltage_equivalent,
)
from ..instruments import family_tones

GENERATOR_VERSION = "rc-m2-v1"
"""Bumped when the catalog, priors, or design filter change: a published seed reproduces its
case only with the generator version recorded in the reveal."""

CASE_TEMPLATES: tuple[TemplateId, ...] = ("rc_lowpass", "rc_highpass")

# Public candidate sets (E6 preferred values). Filtered by ``nominal_designs``.
NOMINAL_R1_OHMS: tuple[float, ...] = (
    470.0,
    680.0,
    1000.0,
    1500.0,
    2200.0,
    3300.0,
    4700.0,
    6800.0,
    10_000.0,
)
NOMINAL_C1_F: tuple[float, ...] = (
    10e-9,
    15e-9,
    22e-9,
    33e-9,
    47e-9,
    68e-9,
    100e-9,
    150e-9,
    220e-9,
    330e-9,
    470e-9,
    680e-9,
)
EQUIVALENT_MAX_RATIO = 0.5
SEPARABLE_MIN_RATIO = 2.0
SYMPTOM_MIN_RATIO = 3.0

INSTRUMENT_VIEWS: tuple[tuple[InstrumentKind, TransientProbe | None], ...] = (
    (InstrumentKind.OUTPUT_VOLTAGE_SWEEP, None),
    (InstrumentKind.SOURCE_CURRENT_SWEEP, None),
    (InstrumentKind.TRANSIENT_CAPTURE, "output_voltage"),  # also the standardized symptom
    (InstrumentKind.TRANSIENT_CAPTURE, "source_current"),
)


@dataclass(frozen=True)
class FaultSpec:
    hypothesis_id: HypothesisId
    component_id: str | None
    multiplier: float | None
    disconnected: bool = False  # the component is open (removed from the netlist)


FAULT_CATALOG: dict[HypothesisId, FaultSpec] = {
    h.id: FaultSpec(
        hypothesis_id=h.id,
        component_id=h.component_id,
        multiplier=h.multiplier,
        disconnected=bool(hypothesis_disconnected(h.id)),
    )
    for h in HYPOTHESES
}
FAULT_ORDER: tuple[HypothesisId, ...] = tuple(FAULT_CATALOG)


@dataclass(frozen=True)
class HiddenDesign:
    template_id: TemplateId
    nominal: Mapping[str, float]
    fault: FaultSpec

    @property
    def actual(self) -> dict[str, float]:
        """Component values of the hidden circuit (an open part keeps its nominal value)."""
        return hypothesis_values(self.nominal, self.fault.hypothesis_id)

    @property
    def disconnected(self) -> frozenset[str]:
        return hypothesis_disconnected(self.fault.hypothesis_id)


def cutoff_hz(values: Mapping[str, float]) -> float:
    return 1.0 / (2.0 * math.pi * values["R1"] * values["C1"])


def _valid_for_template(template_id: TemplateId, values: Mapping[str, float]) -> bool:
    """Template bounds and the joint cutoff limits (the same check revisions use)."""
    from ...circuits.templates import validate_values
    from ...circuits.units import ValueParseError

    try:
        validate_values(template_id, values)
    except ValueParseError:
        return False
    return True


def view_predictions(
    template_id: TemplateId,
    nominal: Mapping[str, float],
    kind: InstrumentKind,
    probe: TransientProbe | None,
) -> dict[HypothesisId, list[Prediction]]:
    """Every hypothesis's predictions for one instrument view, with the family's tones."""
    tones = family_tones(template_id)
    return {
        h: hypothesis_predictions(template_id, nominal, h, kind, probe, tones=tones)
        for h in FAULT_ORDER
    }


def pair_ratios(
    template_id: TemplateId, nominal: Mapping[str, float]
) -> list[tuple[InstrumentKind, TransientProbe | None, HypothesisId, HypothesisId, float]]:
    """Worst deviation/tolerance ratio for every instrument view and hypothesis pair (public)."""
    out = []
    for kind, probe in INSTRUMENT_VIEWS:
        preds = view_predictions(template_id, nominal, kind, probe)
        for a, b in combinations(FAULT_ORDER, 2):
            out.append((kind, probe, a, b, separation_ratio(preds[a], preds[b])))
    return out


def _design_ok(template_id: TemplateId, nominal: Mapping[str, float]) -> bool:
    lo, hi = CASE_NOMINAL_CUTOFF_RANGE_HZ
    if not lo <= cutoff_hz(nominal) <= hi:
        return False
    variants = [hypothesis_values(nominal, h) for h in FAULT_ORDER]
    if not all(_valid_for_template(template_id, v) for v in variants):
        return False
    for kind, probe, a, b, ratio in pair_ratios(template_id, nominal):
        if EQUIVALENT_MAX_RATIO <= ratio <= SEPARABLE_MIN_RATIO:
            return False
        symptom = kind == InstrumentKind.TRANSIENT_CAPTURE and probe == "output_voltage"
        if symptom and not voltage_equivalent(a, b) and ratio < SYMPTOM_MIN_RATIO:
            return False
    return True


@cache
def nominal_designs(template_id: TemplateId = "rc_lowpass") -> tuple[tuple[float, float], ...]:
    """All (R1, C1) nominal pairs a case of this family may use. Public."""
    if template_id not in CASE_TEMPLATES:
        raise ValueError("unsupported template")
    designs = tuple(
        (r, c)
        for r in NOMINAL_R1_OHMS
        for c in NOMINAL_C1_F
        if _design_ok(template_id, {"R1": r, "C1": c})
    )
    if not designs:
        raise RuntimeError("no valid nominal designs for this template")
    return designs


def new_seed() -> str:
    """An opaque random seed (private until the case is closed)."""
    return secrets.token_hex(CASE_SEED_BYTES)


def generate(seed: str, template_id: TemplateId = "rc_lowpass") -> HiddenDesign:
    """Deterministic: the same (generator version, family, seed) gives the same case. The
    condition and the nominal design are drawn independently."""
    if template_id not in CASE_TEMPLATES:
        raise ValueError("unsupported template")
    rng = random.Random(f"{GENERATOR_VERSION}|{template_id}|{seed}")  # noqa: S311 - not crypto
    weights = [FAULT_PRIOR.get(h, 0.0) for h in FAULT_ORDER]
    fault = FAULT_CATALOG[rng.choices(FAULT_ORDER, weights=weights, k=1)[0]]
    r1, c1 = rng.choice(nominal_designs(template_id))
    return HiddenDesign(template_id=template_id, nominal={"R1": r1, "C1": c1}, fault=fault)
