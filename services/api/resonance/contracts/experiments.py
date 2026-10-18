"""Explore-mode experiments: measured responses, two-tone captures, audio, and goals."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .circuits import ComponentChange, Tone
from .common import (
    ApiError,
    ArtifactId,
    ComparisonId,
    Contract,
    EvidenceRef,
    ExecutionStatus,
    ExperimentId,
    GoalId,
    RevisionId,
    SimulatorInfo,
)

# --------------------------------------------------------------------------------------
# Settings actually used (echoed in every record)
# --------------------------------------------------------------------------------------


class AcSweepSettings(Contract):
    start_hz: float
    stop_hz: float
    points_per_decade: int
    points: int
    source_ac_magnitude_v: float = 1.0


class TransientSettings(Contract):
    tones: list[Tone]
    settle_s: float = Field(description="Simulated time discarded before the clip starts.")
    clip_s: float = Field(description="Duration of the analysed/playable steady-state clip.")
    max_step_s: float = Field(description="ngspice maximum internal timestep.")
    sample_rate_hz: int = Field(description="Uniform audio grid the waveform is resampled to.")
    fade_ms: float = Field(description="Matched raised-cosine fade applied to playback only.")


class ExperimentSettings(Contract):
    ac: AcSweepSettings
    transient: TransientSettings


# --------------------------------------------------------------------------------------
# Measurements
# --------------------------------------------------------------------------------------


class FrequencyResponse(Contract):
    """ngspice AC analysis with a 1 V source. Source current uses the 'delivered' convention:
    positive current flows out of V1's + terminal into the circuit."""

    frequency_hz: list[float]
    gain_db: list[float] = Field(description="20·log10|V(out)/V(in)|.")
    phase_deg: list[float] = Field(description="Phase of V(out)/V(in), wrapped to (−180, 180].")
    source_current_ma: list[float] = Field(description="|I(V1)| in mA for a 1 V source.")
    source_current_phase_deg: list[float]


class ToneLevel(Contract):
    """Steady-state amplitude of one demo tone, fitted from the simulated transient waveform
    and cross-checked against an AC analysis at exactly the same frequency."""

    frequency_hz: float
    label: str
    role: Literal["wanted", "interference"]
    input_amplitude_v: float
    output_amplitude_v: float
    transient_gain_db: float = Field(description="20·log10(output/input) from the transient fit.")
    ac_gain_db: float = Field(description="AC analysis gain at this exact frequency.")
    agreement_db: float = Field(description="transient_gain_db − ac_gain_db.")
    phase_deg: float = Field(description="Output phase relative to input from the transient fit.")


class WaveformWindow(Contract):
    """A short uniform-grid window of the steady-state transient for the oscilloscope view."""

    sample_rate_hz: int
    start_s: float = Field(description="Window start, measured from the start of the clip.")
    input_v: list[float]
    output_v: list[float]


class Spectrum(Contract):
    """Magnitude spectrum of the steady-state clip (derived from simulated samples)."""

    frequency_hz: list[float]
    input_dbv: list[float]
    output_dbv: list[float]
    resolution_hz: float
    method: str = Field(examples=["rectangular window over an integer number of signal periods"])


class ConsistencyCheck(Contract):
    """An automated internal check reported with the result (e.g. transient vs AC agreement)."""

    name: str
    passed: bool
    detail: str


# --------------------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------------------

ClipRole = Literal["input", "output", "expected", "observed", "repaired", "previous"]


class AudioClip(Contract):
    artifact_id: ArtifactId
    url: str = Field(description="Relative URL serving audio/wav for this session only.")
    role: ClipRole
    label: str = Field(examples=["Input", "Output", "Expected (intended design)"])
    source_id: str | None = Field(
        default=None, description="ExperimentId or ObservationId that produced the samples."
    )
    duration_s: float
    sample_rate_hz: int
    peak_fs: float = Field(description="Peak |sample| as a fraction of full scale after gain.")
    clipped: bool


class AudioComparison(Contract):
    """A set of clips rendered with ONE shared voltage-to-playback gain.

    Clips are never independently normalized. If headroom forced a reduction, the same
    ``gain_adjustment_db`` applies to every clip and ``note`` discloses it."""

    id: ComparisonId
    clips: list[AudioClip] = Field(min_length=1, max_length=4)
    playback_gain_fs_per_v: float = Field(description="Shared gain: full-scale units per volt.")
    gain_adjustment_db: float = Field(
        default=0.0, le=0.0, description="Shared reduction applied to all clips (0 = none)."
    )
    fade_ms: float
    note: str = Field(description="Plain-language disclosure of gain and processing.")


# --------------------------------------------------------------------------------------
# Goals
# --------------------------------------------------------------------------------------


class GoalConstraint(Contract):
    kind: Literal["preserve", "attenuate"] = Field(
        description="preserve: attenuation ≤ limit_db. attenuate: attenuation ≥ limit_db."
    )
    frequency_hz: float = Field(ge=20, le=20_000)
    limit_db: float = Field(ge=0, le=60)
    label: str = Field(max_length=60, examples=["Musical tone", "Whistle"])


class GoalInput(Contract):
    title: str = Field(max_length=120)
    constraints: list[GoalConstraint] = Field(min_length=1, max_length=4)


class Goal(Contract):
    id: GoalId
    title: str = Field(examples=["Keep the musical tone. Reduce the whistle."])
    constraints: list[GoalConstraint]
    origin: Literal["default", "user", "agent"]


class FeasibilityAnalysis(Contract):
    """Analytical feasibility for the template family (a prediction, not a measurement)."""

    basis: Literal["analytical_first_order_rc"] = "analytical_first_order_rc"
    template_id: str
    feasible: bool
    cutoff_range_hz: tuple[float, float] | None = Field(
        default=None, description="Cutoff frequencies that satisfy every constraint, if any."
    )
    explanation: str


class ConstraintResult(Contract):
    constraint: GoalConstraint
    measured_gain_db: float
    measured_attenuation_db: float
    passed: bool
    margin_db: float = Field(description="Positive = satisfied with this much room.")
    evidence: EvidenceRef


class GoalEvaluation(Contract):
    goal_id: GoalId
    experiment_id: ExperimentId
    passed: bool
    results: list[ConstraintResult]
    feasibility: FeasibilityAnalysis


# --------------------------------------------------------------------------------------
# Experiment records
# --------------------------------------------------------------------------------------


class ExperimentRunRequest(Contract):
    revision_id: RevisionId
    goal_id: GoalId | None = Field(
        default=None, description="Evaluate this goal against the measured result."
    )


class ExperimentRecord(Contract):
    """One explore experiment: AC sweep + two-tone transient + audio on one revision."""

    id: ExperimentId
    revision_id: RevisionId
    status: ExecutionStatus
    created_at: datetime
    duration_ms: int
    simulator: SimulatorInfo
    settings: ExperimentSettings
    response: FrequencyResponse | None = None
    tones: list[ToneLevel] = Field(default_factory=list)
    waveform: WaveformWindow | None = None
    spectrum: Spectrum | None = None
    audio: AudioComparison | None = Field(
        default=None, description="Input and Output clips with one shared gain."
    )
    goal: GoalEvaluation | None = None
    checks: list[ConsistencyCheck] = Field(default_factory=list)
    error: ApiError | None = None


class ExperimentSummary(Contract):
    id: ExperimentId
    revision_id: RevisionId
    status: ExecutionStatus
    created_at: datetime
    goal_passed: bool | None = None


class ToneDelta(Contract):
    frequency_hz: float
    label: str
    a_gain_db: float
    b_gain_db: float
    delta_db: float = Field(description="b − a.")


class ExperimentComparison(Contract):
    """Deterministic comparison of two recorded experiments (A = before, B = after)."""

    a_id: ExperimentId
    b_id: ExperimentId
    a_revision_id: RevisionId
    b_revision_id: RevisionId
    changes: list[ComponentChange]
    tones: list[ToneDelta]
    audio: AudioComparison | None = Field(
        default=None, description="Previous vs current output clips at the same shared gain."
    )
    summary: str = Field(description="Deterministic text built from the numbers above.")
