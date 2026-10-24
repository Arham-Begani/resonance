"""Learn mode: predict, measure, compare (owner: audio & signal engineer).

* ``learn_prompts(revision)`` — deterministic questions: each editable component (signal-path
  order) × ``policy.LEARN_MULTIPLIERS``, keeping only changes whose values stay inside the
  component bounds and the template's supported cutoff range. Questions name the revision's
  OWN tones (``revision.source.tones``), interference first.
* ``run_learn_attempt(...)`` — validates the request, measures the base revision (reusing its
  newest succeeded experiment while its audio is still stored) and the changed revision with
  ngspice, compares them with the shared-gain comparison, and grades every per-tone prediction
  from the MEASURED exact-frequency AC gain change: |Δ| < ``LEARN_SAME_THRESHOLD_DB`` is
  "about the same", otherwise "louder" or "quieter". Explanation and tradeoff are
  deterministic text built from the recorded numbers and cite both experiment IDs.

Atomicity: nothing is persisted until every measurement succeeded and the client is still
connected. Then the fresh base experiment (if one was needed), the changed revision, its
experiment, and finally the ``learn_attempt`` are stored, so a failure or cancellation never
leaves a half-recorded attempt (audio artifacts written during a failed attempt are orphaned
and evicted by the bounded artifact store, never referenced).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..api.errors import ApiException, not_found, simulation_error_to_api
from ..circuits.revisions import build_revision, revision_to_circuit, revision_values
from ..circuits.templates import get_template, validate_values
from ..circuits.units import ValueParseError, format_frequency, format_value
from ..contracts.circuits import Revision, Tone
from ..contracts.common import ErrorCode, EvidenceRef, new_id
from ..contracts.experiments import ExperimentRecord, FrequencyResponse, Goal, ToneLevel
from ..contracts.internal import CancelToken, SimulationCancelled, Simulator
from ..contracts.learn import (
    GradedPrediction,
    LearnAttempt,
    LearnAttemptRequest,
    LearnChange,
    LearnPrompt,
    TonePredictionChoice,
    TonePredictionInput,
)
from ..contracts.policy import LEARN_MULTIPLIERS, LEARN_SAME_THRESHOLD_DB
from ..store.artifacts import ArtifactStore
from ..store.db import DocumentStore, utcnow
from .goals import MEASURED_GAIN_QUANTITY
from .service import compare_experiments, measure_explore_experiment

# (infinitive, gerund) for each allowed multiplier.
_VERBS: dict[float, tuple[str, str]] = {
    0.25: ("quarter", "Quartering"),
    0.5: ("halve", "Halving"),
    2.0: ("double", "Doubling"),
    4.0: ("quadruple", "Quadrupling"),
}
_RECENT_EXPERIMENTS = 50
# The -3 dB point of a first-order RC response: 20·log10(1/√2).
_HALF_POWER_DB = -10.0 * math.log10(2.0)
_UNCHANGED_DB = 0.005  # below the 0.01 dB display resolution


def _stale() -> ApiException:
    return ApiException(
        ErrorCode.STALE_REVISION,
        "This design belongs to an open investigation and can't be changed in Learn. Use one "
        "of your own workbench revisions.",
        field="revision_id",
    )


def _fmt(value_si: float, component_id: str) -> str:
    return format_value(value_si, "ohm" if component_id == "R1" else "farad")


def _lower_first(label: str) -> str:
    """'Musical tone' → 'musical tone'; leaves acronyms such as 'AM tone' alone."""
    if len(label) > 1 and label[0].isupper() and label[1].islower():
        return label[0].lower() + label[1:]
    return label


def _ordered_tones(tones: Sequence[Tone]) -> list[Tone]:
    """Interference first, then wanted tones; each group in signal order."""
    return [t for t in tones if t.role == "interference"] + [t for t in tones if t.role == "wanted"]


def _tone_list(tones: Sequence[Tone]) -> str:
    names = [
        f"the {_lower_first(t.label)} ({format_frequency(t.frequency_hz)})"
        for t in _ordered_tones(tones)
    ]
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _changed_value(revision: Revision, change: LearnChange) -> tuple[float, float]:
    before = revision_values(revision)[change.component_id]
    return before, before * change.multiplier


# --------------------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------------------


def learn_prompts(revision: Revision) -> list[LearnPrompt]:
    """Deterministic prediction questions for a public session revision."""
    if revision.frozen:
        raise _stale()
    template = get_template(revision.template_id)
    values = revision_values(revision)
    tones = list(revision.source.tones)
    prompts: list[LearnPrompt] = []
    for component_id in template.editable_ids:
        if component_id not in ("R1", "C1"):
            continue
        for multiplier in LEARN_MULTIPLIERS:
            change = LearnChange(component_id=component_id, multiplier=multiplier)
            before, after = _changed_value(revision, change)
            try:
                validate_values(revision.template_id, {**values, component_id: after})
            except ValueParseError:
                continue  # leaves the component bounds or the supported cutoff range
            verb = _VERBS[multiplier][0]
            question = (
                f"If we {verb} {component_id} ({_fmt(before, component_id)} → "
                f"{_fmt(after, component_id)}), what happens to {_tone_list(tones)}?"
            )
            prompts.append(
                LearnPrompt(
                    id=f"{component_id}x{multiplier:g}",
                    template_id=revision.template_id,
                    question=question,
                    change=change,
                    tones=[t.model_copy() for t in tones],
                )
            )
    return prompts


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedAttempt:
    revision: Revision  # base (stored)
    changed: Revision  # new, NOT stored yet
    predictions: tuple[tuple[Tone, TonePredictionInput], ...]  # in signal order


def _validation(message: str, field: str) -> ApiException:
    return ApiException(ErrorCode.VALIDATION_ERROR, message, field=field)


def _match_predictions(
    tones: Sequence[Tone], predictions: Sequence[TonePredictionInput]
) -> tuple[tuple[Tone, TonePredictionInput], ...]:
    chosen: dict[int, TonePredictionInput] = {}
    expected = " and ".join(format_frequency(t.frequency_hz) for t in tones)
    for i, prediction in enumerate(predictions):
        match = next(
            (
                k
                for k, t in enumerate(tones)
                if math.isclose(t.frequency_hz, prediction.frequency_hz, rel_tol=1e-9)
            ),
            None,
        )
        if match is None:
            raise _validation(
                f"This circuit's test signal has no tone at {prediction.frequency_hz:g} Hz. "
                f"Predict for {expected}.",
                f"predictions.{i}.frequency_hz",
            )
        if match in chosen:
            raise _validation(
                f"{format_frequency(tones[match].frequency_hz)} is predicted more than once.",
                f"predictions.{i}.frequency_hz",
            )
        chosen[match] = prediction
    missing = [t for k, t in enumerate(tones) if k not in chosen]
    if missing:
        names = " and ".join(f"{t.label} ({format_frequency(t.frequency_hz)})" for t in missing)
        raise _validation(f"Add a prediction for {names}.", "predictions")
    return tuple((t, chosen[k]) for k, t in enumerate(tones))


def plan_attempt(revision: Revision, body: LearnAttemptRequest) -> PlannedAttempt:
    """Validate a Learn attempt and build (not store) the changed revision."""
    if revision.frozen:
        raise _stale()
    component_id = body.change.component_id
    _before, after = _changed_value(revision, body.change)
    try:
        changed = build_revision(
            revision.template_id,
            {component_id: after},
            origin="user_edit",
            parent=revision,
            edited=(component_id,),
        )
    except ValueParseError as exc:
        raise _validation(str(exc), "change.multiplier") from None
    predictions = _match_predictions(revision.source.tones, body.predictions)
    return PlannedAttempt(revision=revision, changed=changed, predictions=predictions)


# --------------------------------------------------------------------------------------
# Grading and deterministic text
# --------------------------------------------------------------------------------------


def observed_change(delta_db: float) -> TonePredictionChoice:
    # Grade on the same 0.01 dB resolution the explanation prints, so the grade can never
    # contradict the numbers on screen at the threshold (verification finding M2-01).
    shown = round(delta_db, 2)
    if abs(shown) < LEARN_SAME_THRESHOLD_DB:
        return "about_the_same"
    return "louder" if shown > 0 else "quieter"


def _tone_level(record: ExperimentRecord, frequency_hz: float) -> ToneLevel:
    for level in record.tones:
        if math.isclose(level.frequency_hz, frequency_hz, rel_tol=1e-9):
            return level
    raise KeyError(frequency_hz)


def grade_predictions(
    predictions: Sequence[tuple[Tone, TonePredictionInput]],
    base: ExperimentRecord,
    new: ExperimentRecord,
) -> list[GradedPrediction]:
    """Grade each prediction from the measured exact-frequency AC gain change (new − base)."""
    graded: list[GradedPrediction] = []
    for tone, prediction in predictions:
        before = _tone_level(base, tone.frequency_hz).ac_gain_db
        after = _tone_level(new, tone.frequency_hz).ac_gain_db
        delta = after - before
        observed = observed_change(delta)
        graded.append(
            GradedPrediction(
                frequency_hz=tone.frequency_hz,
                label=tone.label,
                role=tone.role,
                predicted=prediction.expectation,
                observed=observed,
                delta_db=delta,
                correct=prediction.expectation == observed,
                evidence=[
                    EvidenceRef(
                        result_id=record.id,
                        quantity=MEASURED_GAIN_QUANTITY,
                        frequency_hz=tone.frequency_hz,
                    )
                    for record in (base, new)
                ],
            )
        )
    return graded


def measured_cutoff_hz(response: FrequencyResponse | None) -> float | None:
    """Frequency where the measured sweep crosses −3.01 dB, interpolated linearly in log f
    between the two swept points around the crossing (None if it never crosses)."""
    if response is None:
        return None
    f, g = response.frequency_hz, response.gain_db
    for k in range(len(f) - 1):
        a, b = g[k] - _HALF_POWER_DB, g[k + 1] - _HALF_POWER_DB
        if a == 0.0:
            return f[k]
        if (a < 0.0) != (b < 0.0):
            t = a / (a - b)
            return math.exp(math.log(f[k]) + t * (math.log(f[k + 1]) - math.log(f[k])))
    return None


_CHOICE_TEXT: dict[TonePredictionChoice, str] = {
    "louder": "louder",
    "quieter": "quieter",
    "about_the_same": "about the same",
}


def _change_text(delta_db: float) -> str:
    if abs(delta_db) < _UNCHANGED_DB:
        return "unchanged"
    if delta_db < 0:
        return f"{-delta_db:.2f} dB quieter"
    return f"{delta_db:.2f} dB louder"


def _approx_frequency(hz: float) -> str:
    return format_frequency(float(f"{hz:.3g}"))


def attempt_explanation(
    planned: PlannedAttempt,
    change: LearnChange,
    base: ExperimentRecord,
    new: ExperimentRecord,
    graded: Sequence[GradedPrediction],
) -> str:
    component_id = change.component_id
    before, after = _changed_value(planned.revision, change)
    gerund = _VERBS[change.multiplier][1]
    parts = [
        f"{gerund} {component_id} ({_fmt(before, component_id)} → {_fmt(after, component_id)}) "
        "moved the predicted cutoff (fc = 1/(2π·R1·C1)) from "
        f"{format_frequency(planned.revision.prediction.cutoff_hz)} to "
        f"{format_frequency(planned.changed.prediction.cutoff_hz)}."
    ]
    cut_a, cut_b = measured_cutoff_hz(base.response), measured_cutoff_hz(new.response)
    if cut_a is not None and cut_b is not None:
        parts.append(
            f"The measured −3 dB point moved from about {_approx_frequency(cut_a)} to about "
            f"{_approx_frequency(cut_b)} ({base.id} → {new.id}, interpolated between swept "
            "points)."
        )
    tone_parts = []
    for g in graded:
        a = _tone_level(base, g.frequency_hz).ac_gain_db
        b = _tone_level(new, g.frequency_hz).ac_gain_db
        verdict = _change_text(g.delta_db)
        if g.observed == "about_the_same":
            verdict += f", about the same (under {LEARN_SAME_THRESHOLD_DB:g} dB)"
        tone_parts.append(
            f"{g.label} ({format_frequency(g.frequency_hz)}) {a:.2f} dB → {b:.2f} dB, {verdict}"
        )
    parts.append(
        f"Measured gain at each tone (ngspice AC at exactly that frequency, {base.id} → "
        f"{new.id}): " + "; ".join(tone_parts) + "."
    )
    score = sum(g.correct for g in graded)
    misses = [
        f"{g.label}: you predicted {_CHOICE_TEXT[g.predicted]}, measured {_CHOICE_TEXT[g.observed]}"
        for g in graded
        if not g.correct
    ]
    verdict = f"Your predictions: {score} of {len(graded)} matched the measurement"
    parts.append(verdict + (f" ({'; '.join(misses)})." if misses else "."))
    return " ".join(parts)


def attempt_tradeoff(
    graded: Sequence[GradedPrediction], base: ExperimentRecord, new: ExperimentRecord
) -> str:
    wanted = [g for g in graded if g.role == "wanted"]
    interference = [g for g in graded if g.role == "interference"]

    def listed(group: Sequence[GradedPrediction]) -> str:
        return "; ".join(
            f"{g.label} ({format_frequency(g.frequency_hz)}) {_change_text(g.delta_db)}"
            for g in group
        )

    groups = [f"interference {listed(interference)}"] if interference else []
    groups += [f"wanted {listed(wanted)}"] if wanted else []
    parts = [f"Tradeoff (measured, {base.id} → {new.id}): " + "; ".join(groups) + "."]
    if len(wanted) == 1 and len(interference) == 1:
        w, i = wanted[0], interference[0]
        wl, il = _lower_first(w.label), _lower_first(i.label)
        separation = w.delta_db - i.delta_db
        if abs(separation) < _UNCHANGED_DB:
            parts.append(f"The {wl} and the {il} keep the same separation.")
        elif separation > 0:
            cost = (
                f"at a cost of {-w.delta_db:.2f} dB to the {wl}"
                if w.delta_db <= -_UNCHANGED_DB
                else f"without losing any of the {wl}"
            )
            parts.append(
                f"Relative to the {il}, the {wl} gained {separation:.2f} dB: better "
                f"filtering {cost}."
            )
        else:
            gain = (
                f"the {wl} gained {w.delta_db:.2f} dB, but " if w.delta_db >= _UNCHANGED_DB else ""
            )
            parts.append(
                f"Relative to the {il}, the {wl} lost {-separation:.2f} dB: {gain}the "
                f"{il} is filtered less."
            )
    return " ".join(parts)


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def reusable_base_experiment(
    store: DocumentStore, artifacts: ArtifactStore, session_id: str, revision: Revision
) -> ExperimentRecord | None:
    """The revision's newest succeeded experiment whose output audio is still stored."""
    records = store.list_docs(
        "experiment", session_id, ExperimentRecord, limit=_RECENT_EXPERIMENTS, newest_first=True
    )
    for record in records:
        if record.revision_id != revision.id or record.status != "succeeded":
            continue
        if record.audio is None or not record.tones:
            continue
        clips = [c for c in record.audio.clips if c.role == "output"]
        if clips and artifacts.open(clips[0].artifact_id, session_id) is not None:
            return record
    return None


def _cancelled() -> ApiException:
    return simulation_error_to_api(SimulationCancelled("The simulation was cancelled."))


async def run_learn_attempt(
    *,
    simulator: Simulator,
    store: DocumentStore,
    artifacts: ArtifactStore,
    session_id: str,
    revision: Revision,
    body: LearnAttemptRequest,
    goal: Goal | None,
    cancel: CancelToken | None = None,
) -> LearnAttempt:
    """Validate, measure both revisions, grade, and persist one Learn attempt atomically."""
    token = cancel or CancelToken()
    planned = plan_attempt(revision, body)
    base = reusable_base_experiment(store, artifacts, session_id, revision)
    fresh_base: ExperimentRecord | None = None
    if base is None:
        base = fresh_base = await measure_explore_experiment(
            simulator=simulator,
            artifacts=artifacts,
            session_id=session_id,
            revision=revision,
            circuit=revision_to_circuit(revision),
            goal=goal,
            cancel=token,
        )
    if token.cancelled:
        raise _cancelled()
    new = await measure_explore_experiment(
        simulator=simulator,
        artifacts=artifacts,
        session_id=session_id,
        revision=planned.changed,
        circuit=revision_to_circuit(planned.changed),
        goal=goal,
        cancel=token,
    )
    comparison = await asyncio.to_thread(
        compare_experiments,
        artifacts=artifacts,
        session_id=session_id,
        a=base,
        b=new,
        rev_a=revision,
        rev_b=planned.changed,
    )
    graded = grade_predictions(planned.predictions, base, new)
    attempt = LearnAttempt(
        id=new_id("learn"),
        template_id=revision.template_id,
        base_revision_id=revision.id,
        new_revision_id=planned.changed.id,
        base_experiment_id=base.id,
        new_experiment_id=new.id,
        change=body.change,
        predictions=list(body.predictions),
        reasoning=body.reasoning,
        graded=graded,
        score=sum(g.correct for g in graded),
        total=len(graded),
        comparison=comparison,
        explanation=attempt_explanation(planned, body.change, base, new, graded),
        tradeoff=attempt_tradeoff(graded, base, new),
        created_at=utcnow(),
    )
    if token.cancelled:
        raise _cancelled()  # the learner left: keep nothing half-recorded
    if fresh_base is not None:
        store.put("experiment", fresh_base.id, session_id, fresh_base)
    store.put("revision", planned.changed.id, session_id, planned.changed)
    store.put("experiment", new.id, session_id, new)
    store.put("learn_attempt", attempt.id, session_id, attempt)
    return attempt


def get_learn_attempt(store: DocumentStore, session_id: str, attempt_id: str) -> LearnAttempt:
    attempt = store.get("learn_attempt", attempt_id, session_id, LearnAttempt)
    if attempt is None:
        raise not_found("Learn attempt")
    return attempt
