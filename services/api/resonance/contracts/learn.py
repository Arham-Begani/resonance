"""Learn mode: predict the effect of a component change, measure it, and compare.

Every graded outcome comes from MEASURED tone gains (ngspice explore experiments), cited by
experiment ID. Explanations are deterministic text built from those numbers; an optional AI
explanation is a separate, clearly labeled agent run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from .circuits import EditableComponentId, RevisionId, TemplateId, Tone
from .common import Contract, EvidenceRef, ExperimentId, LearnAttemptId
from .experiments import ExperimentComparison

TonePredictionChoice = Literal["louder", "quieter", "about_the_same"]


LEARN_ALLOWED_MULTIPLIERS = (0.25, 0.5, 2.0, 4.0)


class LearnChange(Contract):
    component_id: EditableComponentId
    multiplier: float = Field(description="One of 0.25, 0.5, 2 or 4.")

    @field_validator("multiplier")
    @classmethod
    def _allowed(cls, value: float) -> float:
        if value not in LEARN_ALLOWED_MULTIPLIERS:
            raise ValueError("multiplier must be one of 0.25, 0.5, 2 or 4")
        return value


class LearnPrompt(Contract):
    """A deterministic question for the current revision, e.g. 'If we double C1, what happens
    to the whistle and the musical tone?'. Prompts whose change would leave the template's
    supported range are not offered."""

    id: str = Field(examples=["C1x2"], description="Stable key: component + multiplier.")
    template_id: TemplateId
    question: str
    change: LearnChange
    tones: list[Tone] = Field(description="The tones the learner predicts for.")


class TonePredictionInput(Contract):
    frequency_hz: float
    expectation: TonePredictionChoice


class LearnAttemptRequest(Contract):
    revision_id: RevisionId
    change: LearnChange
    predictions: list[TonePredictionInput] = Field(min_length=1, max_length=4)
    reasoning: str | None = Field(
        default=None, max_length=500, description="Optional free-text prediction in own words."
    )


class GradedPrediction(Contract):
    frequency_hz: float
    label: str
    role: Literal["wanted", "interference"]
    predicted: TonePredictionChoice
    observed: TonePredictionChoice = Field(
        description="From the measured change: |delta| < LEARN_SAME_THRESHOLD_DB is about the same."
    )
    delta_db: float = Field(description="Measured tone gain after − before (exact-frequency AC).")
    correct: bool
    evidence: list[EvidenceRef]


class LearnAttempt(Contract):
    id: LearnAttemptId
    template_id: TemplateId
    base_revision_id: RevisionId
    new_revision_id: RevisionId
    base_experiment_id: ExperimentId
    new_experiment_id: ExperimentId
    change: LearnChange
    predictions: list[TonePredictionInput]
    reasoning: str | None = None
    graded: list[GradedPrediction]
    score: int
    total: int
    comparison: ExperimentComparison
    explanation: str = Field(description="Deterministic text built from the measured numbers.")
    tradeoff: str = Field(
        description="Deterministic statement of what was gained and lost (wanted vs interference)."
    )
    created_at: datetime
