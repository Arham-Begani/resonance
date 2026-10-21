"""Hypothesis catalog, analytical predictions, and deterministic evidence evaluation (public).

Everything here is a pure function of PUBLIC data: the intended (nominal) component values and
template (circuit family), the case's public two-tone signal, the public hypothesis catalog, the
declared tolerances in ``contracts.policy``, the declared measurement floors in
``challenges.instruments``, and sanitized ``Observation`` records. Nothing here may import from
``resonance.challenges.evaluator``.

Rules (docs/contracts.md "Investigation rules"):
- A hypothesis is *contradicted* if any recorded observation deviates from its analytical
  prediction beyond the declared tolerance; *supported* if it is consistent with every recorded
  observation and is the only such hypothesis; otherwise *unresolved*. Missing data never
  contradicts a hypothesis. There are no likelihood percentages.
- Predictions are the first-order RC equations of ``resonance.simulation.analytic`` for the
  case's family, evaluated for nominal values × the hypothesis multiplier, or with C1 removed
  for the open-capacitor hypothesis (low-pass H = 1, high-pass H = 0, source current 0). They
  are predictions, never substitutes for simulation.
- Tolerances: |Δ| ≤ 0.5 dB for output/tone gain, |wrapped Δ| ≤ 3° for every phase, and
  |Δ| ≤ abs + pct·|predicted| for currents (numpy ``isclose`` convention).
- Floors and zeros: gains are compared after clamping both sides at
  ``instruments.MEASUREMENT_FLOOR_DB`` (two "no measurable output" readings agree); a zero
  current is handled by the absolute current tolerance. A phase is evaluated only where the
  prediction has a defined phase (non-zero magnitude) AND the observation measured a non-zero
  magnitude; a zero phasor's phase is never marked consistent or contradicted.
- Only successful, non-replay observations are evaluated.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from itertools import combinations

import numpy as np
from numpy.typing import NDArray

from ..contracts.circuits import TemplateId, Tone
from ..contracts.common import QuantityName
from ..contracts.investigation import (
    DiagnosisConclusion,
    DiagnosisSubmission,
    EvidenceCheck,
    EvidencePolicy,
    EvidenceState,
    Hypothesis,
    HypothesisAssessment,
    HypothesisCheck,
    HypothesisId,
    HypothesisStatus,
    InstrumentKind,
    Observation,
    TransientProbe,
)
from ..contracts.policy import (
    DB_FLOOR,
    DEFAULT_TONES,
    EVIDENCE_CURRENT_ABS_TOLERANCE_MA,
    EVIDENCE_CURRENT_TOLERANCE_PCT,
    EVIDENCE_GAIN_TOLERANCE_DB,
    EVIDENCE_PHASE_TOLERANCE_DEG,
    EVIDENCE_TONE_GAIN_TOLERANCE_DB,
    EVIDENCE_TONE_PHASE_TOLERANCE_DEG,
    FAULT_MULTIPLIERS,
)
from ..simulation import analytic
from .instruments import (
    CASE_FREQUENCY_GRID_HZ,
    CURRENT_FLOOR_MA,
    INSTRUMENTS,
    MEASUREMENT_FLOOR_DB,
    TRANSIENT_PROBES,
)

FloatArray = NDArray[np.float64]

# --------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------

_INCREASE = max(FAULT_MULTIPLIERS)
_DECREASE = min(FAULT_MULTIPLIERS)


def _multiplier_words(multiplier: float) -> str:
    words = {2.0: "twice", 0.5: "half"}
    return words.get(multiplier, f"{multiplier:g}×")


def _fault_label(component: str, multiplier: float) -> str:
    return f"{component} is about {_multiplier_words(multiplier)} its nominal value"


HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis(id="no_fault", label="No fault: the circuit matches its intended design"),
    Hypothesis(
        id="r1_increased",
        label=_fault_label("R1", _INCREASE),
        component_id="R1",
        change="increased",
        multiplier=_INCREASE,
    ),
    Hypothesis(
        id="r1_decreased",
        label=_fault_label("R1", _DECREASE),
        component_id="R1",
        change="decreased",
        multiplier=_DECREASE,
    ),
    Hypothesis(
        id="c1_increased",
        label=_fault_label("C1", _INCREASE),
        component_id="C1",
        change="increased",
        multiplier=_INCREASE,
    ),
    Hypothesis(
        id="c1_decreased",
        label=_fault_label("C1", _DECREASE),
        component_id="C1",
        change="decreased",
        multiplier=_DECREASE,
    ),
    Hypothesis(
        id="c1_disconnected",
        label="C1 is disconnected (open)",
        component_id="C1",
        change="disconnected",
    ),
)

HYPOTHESES_BY_ID: dict[HypothesisId, Hypothesis] = {h.id: h for h in HYPOTHESES}
HYPOTHESIS_IDS: tuple[HypothesisId, ...] = tuple(h.id for h in HYPOTHESES)

SHORT_LABELS: dict[HypothesisId, str] = {
    "no_fault": "no fault",
    "r1_increased": f"R1 about {_multiplier_words(_INCREASE)} nominal",
    "r1_decreased": f"R1 about {_multiplier_words(_DECREASE)} nominal",
    "c1_increased": f"C1 about {_multiplier_words(_INCREASE)} nominal",
    "c1_decreased": f"C1 about {_multiplier_words(_DECREASE)} nominal",
    "c1_disconnected": "C1 disconnected (open)",
}


def hypothesis_catalog() -> list[Hypothesis]:
    return [h.model_copy() for h in HYPOTHESES]


def evidence_policy() -> EvidencePolicy:
    return EvidencePolicy(
        gain_tolerance_db=EVIDENCE_GAIN_TOLERANCE_DB,
        phase_tolerance_deg=EVIDENCE_PHASE_TOLERANCE_DEG,
        current_tolerance_pct=EVIDENCE_CURRENT_TOLERANCE_PCT,
        current_abs_tolerance_ma=EVIDENCE_CURRENT_ABS_TOLERANCE_MA,
        tone_gain_tolerance_db=EVIDENCE_TONE_GAIN_TOLERANCE_DB,
        tone_phase_tolerance_deg=EVIDENCE_TONE_PHASE_TOLERANCE_DEG,
        description=(
            "Each explanation predicts every measurement with first-order RC equations for this "
            "circuit family (an open capacitor is modeled as removed from the circuit). It is "
            "contradicted when any recorded measurement differs from its prediction by more "
            f"than {EVIDENCE_GAIN_TOLERANCE_DB:g} dB (output gain), "
            f"{EVIDENCE_TONE_GAIN_TOLERANCE_DB:g} dB (tone gain), "
            f"{EVIDENCE_PHASE_TOLERANCE_DEG:g}° (sweep phase), "
            f"{EVIDENCE_TONE_PHASE_TOLERANCE_DEG:g}° (tone phase), or "
            f"{EVIDENCE_CURRENT_TOLERANCE_PCT:g}% of the predicted current plus "
            f"{EVIDENCE_CURRENT_ABS_TOLERANCE_MA:g} mA (currents). Gains at or below "
            f"{MEASUREMENT_FLOOR_DB:g} dB count as no measurable output and agree with each "
            "other. A phase is compared only where both the prediction and the measurement "
            "have a non-zero magnitude (a zero signal has no phase). An explanation is "
            "supported only when it is the single explanation consistent with every "
            "measurement. Missing measurements never rule an explanation out."
        ),
    )


# --------------------------------------------------------------------------------------
# Analytical predictions (first-order RC, ideal source, no load)
# --------------------------------------------------------------------------------------


def hypothesis_values(
    intended: Mapping[str, float], hypothesis_id: HypothesisId
) -> dict[str, float]:
    """Component values a hypothesis implies: nominal × multiplier for its one component.
    The open-capacitor hypothesis keeps the nominal values (the part is present but open);
    see ``hypothesis_disconnected``."""
    values = {"R1": float(intended["R1"]), "C1": float(intended["C1"])}
    hyp = HYPOTHESES_BY_ID[hypothesis_id]
    if hyp.component_id is not None and hyp.multiplier is not None:
        values[hyp.component_id] = values[hyp.component_id] * hyp.multiplier
    return values


def hypothesis_disconnected(hypothesis_id: HypothesisId) -> frozenset[str]:
    """Components a hypothesis removes from the circuit (``CircuitSpec.disconnected``)."""
    hyp = HYPOTHESES_BY_ID[hypothesis_id]
    if hyp.change == "disconnected" and hyp.component_id is not None:
        return frozenset({hyp.component_id})
    return frozenset()


def transfer(
    template_id: TemplateId,
    values: Mapping[str, float],
    freqs: Sequence[float] | FloatArray,
    disconnected: Set[str] = frozenset(),
) -> NDArray[np.complex128]:
    """V(out)/V(in) predicted by the first-order RC equations."""
    return analytic.transfer(
        template_id, np.asarray(freqs, dtype=np.float64), values, disconnected
    )


def source_current(
    template_id: TemplateId,
    values: Mapping[str, float],
    freqs: Sequence[float] | FloatArray,
    disconnected: Set[str] = frozenset(),
) -> NDArray[np.complex128]:
    """Delivered source current in amperes for a 1 V source (series RC input impedance)."""
    return analytic.source_current(
        template_id, np.asarray(freqs, dtype=np.float64), values, 1.0, disconnected
    )


def wrap_deg(d: FloatArray | float) -> FloatArray:
    """Wrap an angle difference into (-180, 180]."""
    return analytic.wrap_phase_deg(d)


def current_tolerance_ma(predicted_ma: float) -> float:
    return EVIDENCE_CURRENT_ABS_TOLERANCE_MA + EVIDENCE_CURRENT_TOLERANCE_PCT / 100.0 * abs(
        predicted_ma
    )


def gain_measurable(gain_db: float) -> bool:
    """True if an output/tone gain is above the declared measurement floor."""
    return gain_db > MEASUREMENT_FLOOR_DB


def current_measurable(current_ma: float) -> bool:
    """True if a current magnitude (mA) is above the declared measurement floor."""
    return abs(current_ma) > CURRENT_FLOOR_MA


@dataclass(frozen=True)
class Prediction:
    """One predicted quantity with its declared consistency tolerance.

    ``floor``: magnitudes are compared after clamping both sides at this floor (dB gains).
    ``defined``: False for the phase of a zero-magnitude prediction; it is never compared."""

    quantity: QuantityName
    unit: str
    frequency_hz: float
    value: float
    tolerance: float
    angle: bool = False
    floor: float | None = None
    defined: bool = True

    def deviation(self, observed: float) -> float:
        if self.angle:
            return abs(float(wrap_deg(observed - self.value)))
        if self.floor is not None:
            return abs(max(observed, self.floor) - max(self.value, self.floor))
        return abs(observed - self.value)

    def disjoint(self, other: Prediction) -> bool:
        """True if no single measurement can be consistent with both predictions."""
        if not (self.defined and other.defined):
            return False
        return other.deviation(self.value) > self.tolerance + other.tolerance


def _gain_pair(
    quantity: QuantityName,
    phase_quantity: QuantityName,
    f: float,
    h: complex,
    gain_tol: float,
    phase_tol: float,
) -> list[Prediction]:
    gain = float(analytic.gain_db(h, floor_db=DB_FLOOR))
    defined = gain_measurable(gain)
    phase = float(analytic.phase_deg(h)) if defined else 0.0
    return [
        Prediction(quantity, "dB", f, gain, gain_tol, floor=MEASUREMENT_FLOOR_DB),
        Prediction(phase_quantity, "deg", f, phase, phase_tol, angle=True, defined=defined),
    ]


def _current_pair(
    quantity: QuantityName,
    phase_quantity: QuantityName,
    f: float,
    current_ma: complex,
    phase_tol: float,
) -> list[Prediction]:
    magnitude = abs(current_ma)
    defined = current_measurable(magnitude)
    phase = float(analytic.phase_deg(current_ma)) if defined else 0.0
    return [
        Prediction(quantity, "mA", f, magnitude, current_tolerance_ma(magnitude)),
        Prediction(phase_quantity, "deg", f, phase, phase_tol, angle=True, defined=defined),
    ]


def predictions(
    template_id: TemplateId,
    values: Mapping[str, float],
    kind: InstrumentKind,
    probe: TransientProbe | None = None,
    *,
    frequencies_hz: Sequence[float] | None = None,
    tones: Sequence[Tone] = DEFAULT_TONES,
    disconnected: Set[str] = frozenset(),
) -> list[Prediction]:
    """Predicted measurements, ordered per frequency (magnitude then phase).

    Sweeps use ``frequencies_hz`` (default: the declared grid); transient captures use the
    case's source tones (amplitudes are public)."""
    out: list[Prediction] = []
    if kind in (InstrumentKind.OUTPUT_VOLTAGE_SWEEP, InstrumentKind.SOURCE_CURRENT_SWEEP):
        freqs = np.asarray(
            frequencies_hz if frequencies_hz is not None else CASE_FREQUENCY_GRID_HZ,
            dtype=np.float64,
        )
        if kind == InstrumentKind.OUTPUT_VOLTAGE_SWEEP:
            h = transfer(template_id, values, freqs, disconnected)
            for f, z in zip(freqs, h, strict=True):
                out += _gain_pair(
                    "output_gain_db",
                    "output_phase_deg",
                    float(f),
                    complex(z),
                    EVIDENCE_GAIN_TOLERANCE_DB,
                    EVIDENCE_PHASE_TOLERANCE_DEG,
                )
        else:
            i = source_current(template_id, values, freqs, disconnected)
            for f, z in zip(freqs, i, strict=True):
                out += _current_pair(
                    "source_current_ma",
                    "source_current_phase_deg",
                    float(f),
                    complex(z) * 1e3,
                    EVIDENCE_PHASE_TOLERANCE_DEG,
                )
        return out
    freqs = np.asarray([t.frequency_hz for t in tones], dtype=np.float64)
    if probe == "output_voltage":
        h = transfer(template_id, values, freqs, disconnected)
        for t, z in zip(tones, h, strict=True):
            out += _gain_pair(
                "tone_gain_db",
                "tone_phase_deg",
                t.frequency_hz,
                complex(z),
                EVIDENCE_TONE_GAIN_TOLERANCE_DB,
                EVIDENCE_TONE_PHASE_TOLERANCE_DEG,
            )
    elif probe == "source_current":
        i = source_current(template_id, values, freqs, disconnected)
        for t, z in zip(tones, i, strict=True):
            out += _current_pair(
                "tone_current_ma",
                "tone_current_phase_deg",
                t.frequency_hz,
                complex(z) * t.amplitude_v * 1e3,
                EVIDENCE_TONE_PHASE_TOLERANCE_DEG,
            )
    else:
        raise ValueError("a transient capture needs a probe")
    return out


def hypothesis_predictions(
    template_id: TemplateId,
    intended: Mapping[str, float],
    hypothesis_id: HypothesisId,
    kind: InstrumentKind,
    probe: TransientProbe | None = None,
    *,
    frequencies_hz: Sequence[float] | None = None,
    tones: Sequence[Tone] = DEFAULT_TONES,
) -> list[Prediction]:
    """``predictions`` for the circuit a catalog hypothesis implies."""
    return predictions(
        template_id,
        hypothesis_values(intended, hypothesis_id),
        kind,
        probe,
        frequencies_hz=frequencies_hz,
        tones=tones,
        disconnected=hypothesis_disconnected(hypothesis_id),
    )


def separation_ratio(a: Sequence[Prediction], b: Sequence[Prediction]) -> float:
    """Largest |Δ| / tolerance between two hypotheses' predictions of the same measurement
    (undefined phases are skipped)."""
    return max(
        (
            x.deviation(y.value) / x.tolerance
            for x, y in zip(a, b, strict=True)
            if x.defined and y.defined
        ),
        default=0.0,
    )


def voltage_equivalent(a: HypothesisId, b: HypothesisId) -> bool:
    """True if two hypotheses imply the same output-voltage transfer in both families: the same
    R1·C1 product with C1 connected, or both with C1 open."""
    da, db = hypothesis_disconnected(a), hypothesis_disconnected(b)
    if da or db:
        return da == db
    unit = {"R1": 1.0, "C1": 1.0}
    va, vb = hypothesis_values(unit, a), hypothesis_values(unit, b)
    return math.isclose(va["R1"] * va["C1"], vb["R1"] * vb["C1"], rel_tol=1e-12)


# --------------------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------------------


def _usable(observations: Sequence[Observation]) -> list[Observation]:
    return [
        o
        for o in observations
        if o.status == "succeeded" and not o.replay and (o.sweep is not None or o.transient)
    ]


def _evaluable(
    observed: Sequence[float], preds: Sequence[Prediction]
) -> list[tuple[float, Prediction]]:
    """(observed, prediction) pairs in (magnitude, phase) order per frequency, keeping a phase
    only where both the prediction and the measurement have a non-zero magnitude."""
    pairs: list[tuple[float, Prediction]] = []
    for k in range(0, len(preds), 2):
        magnitude, phase = preds[k], preds[k + 1]
        pairs.append((observed[k], magnitude))
        if magnitude.unit == "dB":
            measured = gain_measurable(observed[k])
        else:
            measured = current_measurable(observed[k])
        if phase.defined and measured:
            pairs.append((observed[k + 1], phase))
    return pairs


def _observed_and_predicted(
    template_id: TemplateId,
    intended: Mapping[str, float],
    hypothesis_id: HypothesisId,
    obs: Observation,
    tones: Sequence[Tone],
) -> list[tuple[float, Prediction]]:
    if obs.sweep is not None:
        sweep = obs.sweep
        kind = (
            InstrumentKind.OUTPUT_VOLTAGE_SWEEP
            if sweep.quantity == "output_gain"
            else InstrumentKind.SOURCE_CURRENT_SWEEP
        )
        preds = hypothesis_predictions(
            template_id, intended, hypothesis_id, kind, frequencies_hz=sweep.frequency_hz
        )
        observed: list[float] = []
        for m, p in zip(sweep.magnitude, sweep.phase_deg, strict=True):
            observed += [m, p]
        return _evaluable(observed, preds)
    assert obs.transient is not None
    transient = obs.transient
    by_freq = {t.frequency_hz: t for t in tones}
    used = [by_freq[t.frequency_hz] for t in transient.tones]
    preds = hypothesis_predictions(
        template_id,
        intended,
        hypothesis_id,
        InstrumentKind.TRANSIENT_CAPTURE,
        transient.probe,
        tones=used,
    )
    observed = []
    for tone in transient.tones:
        magnitude = tone.gain_db if transient.probe == "output_voltage" else tone.amplitude
        observed += [float(magnitude if magnitude is not None else math.nan), tone.phase_deg]
    return _evaluable(observed, preds)


def _check(obs: Observation, pairs: list[tuple[float, Prediction]]) -> HypothesisCheck | None:
    if not pairs:
        return None

    def ratio(item: tuple[float, Prediction]) -> float:
        dev = item[1].deviation(item[0])
        return math.inf if not math.isfinite(dev) else dev / item[1].tolerance

    observed, worst = max(pairs, key=ratio)
    return HypothesisCheck(
        observation_id=obs.id,
        consistent=all(p.deviation(o) <= p.tolerance for o, p in pairs),
        quantity=worst.quantity,
        frequency_hz=worst.frequency_hz,
        observed=observed,
        predicted=worst.value,
        deviation=worst.deviation(observed),
        tolerance=worst.tolerance,
        unit=worst.unit,
    )


def _join(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _statement(remaining: Sequence[HypothesisId], observed_any: bool) -> str:
    total = len(HYPOTHESES)
    if not observed_any:
        return f"Nothing has been measured yet, so all {total} explanations remain possible."
    if not remaining:
        return "No explanation in the catalog fits every recorded observation."
    if len(remaining) == 1:
        return (
            f"Only one explanation fits every recorded observation: {SHORT_LABELS[remaining[0]]}."
        )
    return (
        f"{len(remaining)} explanations fit every recorded observation: "
        f"{_join([SHORT_LABELS[h] for h in remaining])}. The evidence cannot yet tell them "
        "apart."
    )


def assess(
    template_id: TemplateId,
    intended_values: Mapping[str, float],
    observations: Sequence[Observation],
    *,
    publish_distinguishing: bool,
    tones: Sequence[Tone] = DEFAULT_TONES,
) -> EvidenceState:
    """Evaluate every catalog hypothesis against the recorded (sanitized) observations."""
    usable = _usable(observations)
    checks_by: dict[HypothesisId, list[HypothesisCheck]] = {}
    remaining: list[HypothesisId] = []
    for hyp in HYPOTHESES:
        checks = [
            c
            for c in (
                _check(
                    o, _observed_and_predicted(template_id, intended_values, hyp.id, o, tones)
                )
                for o in usable
            )
            if c is not None
        ]
        checks_by[hyp.id] = checks
        if all(c.consistent for c in checks):
            remaining.append(hyp.id)
    assessments: list[HypothesisAssessment] = []
    for hyp in HYPOTHESES:
        status: HypothesisStatus
        if hyp.id not in remaining:
            status = "contradicted"
        elif remaining == [hyp.id] and checks_by[hyp.id]:
            status = "supported"
        else:
            status = "unresolved"
        assessments.append(
            HypothesisAssessment(hypothesis_id=hyp.id, status=status, checks=checks_by[hyp.id])
        )
    distinguishing = (
        distinguishing_instruments(template_id, intended_values, remaining, tones=tones)
        if publish_distinguishing
        else None
    )
    return EvidenceState(
        assessments=assessments,
        remaining=remaining,
        statement=_statement(remaining, bool(usable)),
        distinguishing_instruments=distinguishing,
    )


def distinguishing_instruments(
    template_id: TemplateId,
    intended_values: Mapping[str, float],
    remaining: Sequence[HypothesisId],
    *,
    tones: Sequence[Tone] = DEFAULT_TONES,
) -> list[InstrumentKind]:
    """Instruments for which at least one pair of remaining hypotheses has non-overlapping
    consistency bands somewhere, so one measurement cannot be consistent with both."""
    if len(remaining) < 2:
        return []
    found: list[InstrumentKind] = []
    for spec in INSTRUMENTS:
        probes: Sequence[TransientProbe | None] = (
            TRANSIENT_PROBES if spec.kind == InstrumentKind.TRANSIENT_CAPTURE else (None,)
        )
        separates = False
        for probe in probes:
            preds = {
                h: hypothesis_predictions(
                    template_id, intended_values, h, spec.kind, probe, tones=tones
                )
                for h in remaining
            }
            if any(
                any(x.disjoint(y) for x, y in zip(preds[a], preds[b], strict=True))
                for a, b in combinations(remaining, 2)
            ):
                separates = True
                break
        if separates:
            found.append(spec.kind)
    return found


# --------------------------------------------------------------------------------------
# Diagnosis evidence check
# --------------------------------------------------------------------------------------


def named_hypothesis(submission: DiagnosisSubmission) -> HypothesisId | None:
    if submission.conclusion == DiagnosisConclusion.FAULT:
        return submission.hypothesis_id
    if submission.conclusion == DiagnosisConclusion.NO_FAULT:
        return "no_fault"
    return None


def _first_contradiction(state: EvidenceState, hypothesis_id: HypothesisId) -> str:
    for a in state.assessments:
        if a.hypothesis_id != hypothesis_id:
            continue
        for c in a.checks:
            if not c.consistent:
                where = f" at {c.frequency_hz:g} Hz" if c.frequency_hz is not None else ""
                return (
                    f" Observation {c.observation_id} ({c.quantity}{where}) differs from its "
                    "prediction by more than the declared tolerance."
                )
    return ""


def evidence_check(submission: DiagnosisSubmission, state: EvidenceState) -> EvidenceCheck:
    """Trusted assessment of a submission against the recorded evidence. It never rewrites
    the submission."""
    remaining = list(state.remaining)
    named = named_hypothesis(submission)
    labels = _join([SHORT_LABELS[h] for h in remaining])
    if not remaining:
        return EvidenceCheck(
            status="no_supported_hypothesis",
            remaining=remaining,
            message="No explanation in the catalog fits every recorded observation.",
        )
    if named is not None:
        if named not in remaining:
            return EvidenceCheck(
                status="contradicted",
                remaining=remaining,
                message=(
                    f"The recorded evidence contradicts '{SHORT_LABELS[named]}'."
                    + _first_contradiction(state, named)
                ),
            )
        if remaining == [named]:
            return EvidenceCheck(
                status="supported",
                remaining=remaining,
                message=(
                    f"Supported: '{SHORT_LABELS[named]}' is the only explanation consistent with "
                    "every recorded observation."
                ),
            )
        others = _join([SHORT_LABELS[h] for h in remaining if h != named])
        return EvidenceCheck(
            status="ambiguous",
            remaining=remaining,
            message=(
                f"'{SHORT_LABELS[named]}' fits the evidence, but so does {others}. The recorded "
                "observations cannot single it out, so this conclusion is more certain than "
                "the evidence allows."
            ),
        )
    listed = list(dict.fromkeys(submission.candidate_hypothesis_ids))
    notes = ""
    contradicted_listed = [h for h in listed if h not in remaining]
    missing = [h for h in remaining if h not in listed]
    if contradicted_listed:
        notes += (
            " Listed but contradicted by the evidence: "
            f"{_join([SHORT_LABELS[h] for h in contradicted_listed])}."
        )
    if missing:
        notes += f" Still consistent but not listed: {_join([SHORT_LABELS[h] for h in missing])}."
    if len(remaining) >= 2:
        return EvidenceCheck(
            status="consistent_abstention",
            remaining=remaining,
            message=(
                f"Appropriate caution: {len(remaining)} explanations still fit every "
                f"observation ({labels})." + notes
            ),
        )
    return EvidenceCheck(
        status="premature_abstention",
        remaining=remaining,
        message=(
            f"Only one explanation fits every recorded observation ({labels}); the evidence was "
            "sufficient for a diagnosis." + notes
        ),
    )


def unsupported_certainty(submission: DiagnosisSubmission, check: EvidenceCheck) -> bool:
    """A definite conclusion (fault or no_fault) whose check was ambiguous or contradicted."""
    return named_hypothesis(submission) is not None and check.status in (
        "ambiguous",
        "contradicted",
    )
