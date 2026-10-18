"""Hypothesis catalog, analytical predictions, and deterministic evidence evaluation (public).

Everything here is a pure function of PUBLIC data: the intended (nominal) component values,
the public demo tones, the public hypothesis catalog, the declared tolerances in
``contracts.policy``, and sanitized ``Observation`` records. Nothing here may import from
``resonance.challenges.evaluator``.

Rules (docs/contracts.md "Investigation rules"):
- A hypothesis is *contradicted* if any recorded observation deviates from its analytical
  prediction beyond the declared tolerance; *supported* if it is consistent with every recorded
  observation and is the only such hypothesis; otherwise *unresolved*. Missing data never
  contradicts a hypothesis. There are no likelihood percentages.
- Predictions are the first-order RC equations of ``resonance.simulation.analytic`` evaluated
  for nominal values × the hypothesis multiplier. They are predictions, never substitutes for
  simulation.
- Tolerances: |Δ| ≤ 0.5 dB for output/tone gain, |wrapped Δ| ≤ 3° for every phase, and
  |Δ| ≤ abs + pct·|predicted| for currents (numpy ``isclose`` convention).
- Only successful, non-replay observations are evaluated.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
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
from .instruments import CASE_FREQUENCY_GRID_HZ, INSTRUMENTS, TRANSIENT_PROBES

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
)

HYPOTHESES_BY_ID: dict[HypothesisId, Hypothesis] = {h.id: h for h in HYPOTHESES}
HYPOTHESIS_IDS: tuple[HypothesisId, ...] = tuple(h.id for h in HYPOTHESES)

SHORT_LABELS: dict[HypothesisId, str] = {
    "no_fault": "no fault",
    "r1_increased": f"R1 about {_multiplier_words(_INCREASE)} nominal",
    "r1_decreased": f"R1 about {_multiplier_words(_DECREASE)} nominal",
    "c1_increased": f"C1 about {_multiplier_words(_INCREASE)} nominal",
    "c1_decreased": f"C1 about {_multiplier_words(_DECREASE)} nominal",
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
            "Each explanation predicts every measurement with first-order RC equations. It is "
            "contradicted when any recorded measurement differs from its prediction by more "
            f"than {EVIDENCE_GAIN_TOLERANCE_DB:g} dB (output gain), "
            f"{EVIDENCE_TONE_GAIN_TOLERANCE_DB:g} dB (tone gain), "
            f"{EVIDENCE_PHASE_TOLERANCE_DEG:g}° (sweep phase), "
            f"{EVIDENCE_TONE_PHASE_TOLERANCE_DEG:g}° (tone phase), or "
            f"{EVIDENCE_CURRENT_TOLERANCE_PCT:g}% of the predicted current plus "
            f"{EVIDENCE_CURRENT_ABS_TOLERANCE_MA:g} mA (currents). It is supported only when it "
            "is the single explanation consistent with every measurement. Missing measurements "
            "never rule an explanation out."
        ),
    )


# --------------------------------------------------------------------------------------
# Analytical predictions (first-order RC, ideal source, no load)
# --------------------------------------------------------------------------------------


def hypothesis_values(
    intended: Mapping[str, float], hypothesis_id: HypothesisId
) -> dict[str, float]:
    """Component values a hypothesis implies: nominal × multiplier for its one component."""
    values = {"R1": float(intended["R1"]), "C1": float(intended["C1"])}
    hyp = HYPOTHESES_BY_ID[hypothesis_id]
    if hyp.component_id is not None and hyp.multiplier is not None:
        values[hyp.component_id] = values[hyp.component_id] * hyp.multiplier
    return values


def transfer(
    template_id: TemplateId, values: Mapping[str, float], freqs: Sequence[float] | FloatArray
) -> NDArray[np.complex128]:
    """V(out)/V(in) predicted by the first-order RC equations."""
    return analytic.transfer(template_id, np.asarray(freqs, dtype=np.float64), values)


def source_current(
    template_id: TemplateId, values: Mapping[str, float], freqs: Sequence[float] | FloatArray
) -> NDArray[np.complex128]:
    """Delivered source current in amperes for a 1 V source (series RC input impedance)."""
    return analytic.source_current(template_id, np.asarray(freqs, dtype=np.float64), values)


def wrap_deg(d: FloatArray | float) -> FloatArray:
    """Wrap an angle difference into (-180, 180]."""
    return analytic.wrap_phase_deg(d)


def current_tolerance_ma(predicted_ma: float) -> float:
    return EVIDENCE_CURRENT_ABS_TOLERANCE_MA + EVIDENCE_CURRENT_TOLERANCE_PCT / 100.0 * abs(
        predicted_ma
    )


@dataclass(frozen=True)
class Prediction:
    """One predicted quantity with its declared consistency tolerance."""

    quantity: QuantityName
    unit: str
    frequency_hz: float
    value: float
    tolerance: float
    angle: bool = False

    def deviation(self, observed: float) -> float:
        if self.angle:
            return abs(float(wrap_deg(observed - self.value)))
        return abs(observed - self.value)

    def disjoint(self, other: Prediction) -> bool:
        """True if no single measurement can be consistent with both predictions."""
        return other.deviation(self.value) > self.tolerance + other.tolerance


def predictions(
    template_id: TemplateId,
    values: Mapping[str, float],
    kind: InstrumentKind,
    probe: TransientProbe | None = None,
    *,
    frequencies_hz: Sequence[float] | None = None,
    tones: Sequence[Tone] = DEFAULT_TONES,
) -> list[Prediction]:
    """Predicted measurements, ordered per frequency (magnitude then phase).

    Sweeps use ``frequencies_hz`` (default: the declared grid); transient captures use the
    source tones (amplitudes are public)."""
    out: list[Prediction] = []
    if kind in (InstrumentKind.OUTPUT_VOLTAGE_SWEEP, InstrumentKind.SOURCE_CURRENT_SWEEP):
        freqs = np.asarray(
            frequencies_hz if frequencies_hz is not None else CASE_FREQUENCY_GRID_HZ,
            dtype=np.float64,
        )
        if kind == InstrumentKind.OUTPUT_VOLTAGE_SWEEP:
            h = transfer(template_id, values, freqs)
            for f, g, p in zip(freqs, analytic.gain_db(h), analytic.phase_deg(h), strict=True):
                out.append(
                    Prediction(
                        "output_gain_db", "dB", float(f), float(g), EVIDENCE_GAIN_TOLERANCE_DB
                    )
                )
                out.append(
                    Prediction(
                        "output_phase_deg",
                        "deg",
                        float(f),
                        float(p),
                        EVIDENCE_PHASE_TOLERANCE_DEG,
                        angle=True,
                    )
                )
        else:
            i = source_current(template_id, values, freqs)
            for f, m, p in zip(freqs, np.abs(i) * 1e3, analytic.phase_deg(i), strict=True):
                out.append(
                    Prediction(
                        "source_current_ma",
                        "mA",
                        float(f),
                        float(m),
                        current_tolerance_ma(float(m)),
                    )
                )
                out.append(
                    Prediction(
                        "source_current_phase_deg",
                        "deg",
                        float(f),
                        float(p),
                        EVIDENCE_PHASE_TOLERANCE_DEG,
                        angle=True,
                    )
                )
        return out
    freqs = np.asarray([t.frequency_hz for t in tones], dtype=np.float64)
    if probe == "output_voltage":
        h = transfer(template_id, values, freqs)
        for t, g, p in zip(tones, analytic.gain_db(h), analytic.phase_deg(h), strict=True):
            out.append(
                Prediction(
                    "tone_gain_db", "dB", t.frequency_hz, float(g), EVIDENCE_TONE_GAIN_TOLERANCE_DB
                )
            )
            out.append(
                Prediction(
                    "tone_phase_deg",
                    "deg",
                    t.frequency_hz,
                    float(p),
                    EVIDENCE_TONE_PHASE_TOLERANCE_DEG,
                    angle=True,
                )
            )
    elif probe == "source_current":
        i = source_current(template_id, values, freqs)
        for t, y, p in zip(tones, np.abs(i), analytic.phase_deg(i), strict=True):
            ma = float(y) * t.amplitude_v * 1e3
            out.append(
                Prediction("tone_current_ma", "mA", t.frequency_hz, ma, current_tolerance_ma(ma))
            )
            out.append(
                Prediction(
                    "tone_current_phase_deg",
                    "deg",
                    t.frequency_hz,
                    float(p),
                    EVIDENCE_TONE_PHASE_TOLERANCE_DEG,
                    angle=True,
                )
            )
    else:
        raise ValueError("a transient capture needs a probe")
    return out


def separation_ratio(a: Sequence[Prediction], b: Sequence[Prediction]) -> float:
    """Largest |Δ| / tolerance between two hypotheses' predictions of the same measurement."""
    return max(x.deviation(y.value) / x.tolerance for x, y in zip(a, b, strict=True))


def voltage_equivalent(a: HypothesisId, b: HypothesisId) -> bool:
    """True if two hypotheses imply the same R1·C1 product (identical voltage transfer)."""
    va, vb = (
        hypothesis_values({"R1": 1.0, "C1": 1.0}, a),
        hypothesis_values({"R1": 1.0, "C1": 1.0}, b),
    )
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


def _observed_and_predicted(
    template_id: TemplateId,
    values: Mapping[str, float],
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
        preds = predictions(template_id, values, kind, frequencies_hz=sweep.frequency_hz)
        observed: list[float] = []
        for m, p in zip(sweep.magnitude, sweep.phase_deg, strict=True):
            observed += [m, p]
        return list(zip(observed, preds, strict=True))
    assert obs.transient is not None
    transient = obs.transient
    by_freq = {t.frequency_hz: t for t in tones}
    used = [by_freq[t.frequency_hz] for t in transient.tones]
    preds = predictions(
        template_id, values, InstrumentKind.TRANSIENT_CAPTURE, transient.probe, tones=used
    )
    observed = []
    for tone in transient.tones:
        magnitude = tone.gain_db if transient.probe == "output_voltage" else tone.amplitude
        observed += [float(magnitude if magnitude is not None else math.nan), tone.phase_deg]
    return list(zip(observed, preds, strict=True))


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
        values = hypothesis_values(intended_values, hyp.id)
        checks = [
            c
            for c in (
                _check(o, _observed_and_predicted(template_id, values, o, tones)) for o in usable
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
                h: predictions(
                    template_id,
                    hypothesis_values(intended_values, h),
                    spec.kind,
                    probe,
                    tones=tones,
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
