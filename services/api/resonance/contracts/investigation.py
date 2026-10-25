"""Hidden-fault investigation: PUBLIC case state only.

Boundary rule: nothing in this module may carry the actual (hidden) component values, the
fault identity, the seed, netlists, or any quantity derived from them except the allowed,
sanitized measurements in ``Observation`` and, after closing, ``CaseReveal``. Before close,
every field (including ``status``, ``available_actions``, summaries, and transitions) must be
computed from public data only: the intended design, recorded observations, submissions, the
repair proposal, and measured verification results.
The hidden state lives in ``resonance.challenges.evaluator`` and never uses these models.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from .circuits import ComponentId, EditableComponentId, Revision
from .common import (
    AgentRunId,
    ApiError,
    CaseId,
    Contract,
    DiagnosisId,
    ExecutionStatus,
    ObservationId,
    QuantityName,
    RepairId,
    SimulatorInfo,
)
from .experiments import AcSweepSettings, AudioComparison, TransientSettings

# --------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------


class CaseStatus(StrEnum):
    """ready → observing → diagnosis_submitted | unresolved → repair_verification → closed.
    ``failed`` = infrastructure failure after the permitted retries; it can only be closed.
    The full transition table is in docs/contracts.md."""

    READY = "ready"
    OBSERVING = "observing"
    DIAGNOSIS_SUBMITTED = "diagnosis_submitted"
    UNRESOLVED = "unresolved"
    REPAIR_VERIFICATION = "repair_verification"
    CLOSED = "closed"
    FAILED = "failed"


InvestigationMode = Literal["guide", "agent"]

CaseAction = Literal[
    "choose_mode",
    "run_experiment",
    "start_agent",
    "request_hint",
    "cancel_agent",
    "submit_diagnosis",
    "propose_repair",
    "verify_repair",
    "close",
]


# --------------------------------------------------------------------------------------
# Instruments (the only ways to observe the hidden circuit)
# --------------------------------------------------------------------------------------


class InstrumentKind(StrEnum):
    OUTPUT_VOLTAGE_SWEEP = "output_voltage_sweep"
    SOURCE_CURRENT_SWEEP = "source_current_sweep"
    TRANSIENT_CAPTURE = "transient_capture"


TransientProbe = Literal["output_voltage", "source_current"]
"""What a transient capture records: V(out), or the current delivered by V1."""


class InstrumentSpec(Contract):
    """Declares exactly what an instrument returns, so learners and the agent can choose."""

    kind: InstrumentKind
    title: str
    description: str = Field(description="Plain-language statement of what is measured.")
    returns: list[QuantityName] = Field(description="Quantity names returned.")
    frequency_hz: list[float] | None = Field(
        default=None, description="The fixed, declared frequency grid (sweeps only)."
    )
    probes: list[TransientProbe] | None = Field(
        default=None, description="Quantities a transient capture may record."
    )
    cost: int = Field(default=1, description="Experiments consumed on success.")


class CaseExperimentRequest(Contract):
    instrument: InstrumentKind
    probe: TransientProbe | None = Field(
        default=None, description="Required for transient_capture; forbidden for sweeps."
    )
    client_request_id: str = Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Required idempotency key scoped to the case. Same key + same body returns "
        "the stored result without consuming budget; same key + different body → 409; a key "
        "still in flight → 409 (retryable).",
    )

    @model_validator(mode="after")
    def _probe_matches_instrument(self) -> Self:
        if self.instrument == InstrumentKind.TRANSIENT_CAPTURE and self.probe is None:
            raise ValueError("probe is required for transient_capture")
        if self.instrument != InstrumentKind.TRANSIENT_CAPTURE and self.probe is not None:
            raise ValueError("probe is only allowed for transient_capture")
        return self


# --------------------------------------------------------------------------------------
# Observations (sanitized)
# --------------------------------------------------------------------------------------


class ObservationSettings(Contract):
    """Echo of the public settings used (all declared constants; never hidden-dependent)."""

    sweep: AcSweepSettings | None = None
    transient: TransientSettings | None = None


class SweepObservation(Contract):
    quantity: Literal["output_gain", "source_current"]
    frequency_hz: list[float]
    magnitude: list[float] = Field(description="dB for output_gain; mA (1 V source) for current.")
    magnitude_unit: Literal["dB", "mA"]
    phase_deg: list[float]
    expected_magnitude: list[float] = Field(
        description="Same quantity simulated on the public intended design (reference)."
    )
    expected_phase_deg: list[float]


class ObservedTone(Contract):
    """One demo tone in a transient capture. For ``output_voltage`` the amplitude is in V and
    ``gain_db`` is relative to the source tone; for ``source_current`` the amplitude is in mA,
    ``gain_db`` is null, and the phase is relative to the source voltage tone."""

    frequency_hz: float
    label: str
    role: Literal["wanted", "interference"]
    amplitude: float
    amplitude_unit: Literal["V", "mA"]
    gain_db: float | None = None
    phase_deg: float
    expected_amplitude: float
    expected_gain_db: float | None = None
    expected_phase_deg: float


class ObservedWaveform(Contract):
    """Uniform-grid window (≤ policy.WAVEFORM_WINDOW_MAX_SAMPLES samples per series)."""

    sample_rate_hz: int
    start_s: float
    unit: Literal["V", "mA"] = Field(description="Unit of observed/expected series.")
    input_v: list[float] = Field(description="Source voltage over the same window.")
    observed: list[float]
    expected: list[float]


class TransientObservation(Contract):
    probe: TransientProbe
    tones: list[ObservedTone]
    waveform: ObservedWaveform
    audio: AudioComparison | None = Field(
        default=None,
        description="Expected (intended design) vs Observed output clips at the shared gain. "
        "Only for the output_voltage probe (current is not played as audio).",
    )


ObservationKind = Literal[
    "symptom", "output_voltage_sweep", "source_current_sweep", "transient_capture"
]


class Observation(Contract):
    id: ObservationId
    case_id: CaseId
    sequence: int = Field(description="0 is the standardized symptom; experiments follow.")
    kind: ObservationKind
    probe: TransientProbe | None = None
    counted: bool = Field(description="True if this observation consumed experiment budget.")
    requested_by: Literal["system", "user", "agent"]
    agent_run_id: AgentRunId | None = None
    status: ExecutionStatus
    replay: bool = Field(
        default=False,
        description="Reserved: milestone 1 refuses experiments on closed cases, so always false.",
    )
    created_at: datetime
    simulator: SimulatorInfo
    settings: ObservationSettings
    sweep: SweepObservation | None = None
    transient: TransientObservation | None = None
    summary: str = Field(
        description="Deterministic text built from the measured numbers. While the case is "
        "observing it must not name instruments to try next."
    )
    error: ApiError | None = None


# --------------------------------------------------------------------------------------
# Hypotheses and evidence (deterministic evaluation over public data)
# --------------------------------------------------------------------------------------

HypothesisId = Literal[
    "no_fault", "r1_increased", "r1_decreased", "c1_increased", "c1_decreased", "c1_disconnected"
]
"""Hypothesis catalog. ``c1_disconnected`` (an open capacitor) is drawn by the case generator
only once it is proven simulatable and identifiable with the allowed instruments for both
template families (tests in tests/challenges and tests/simulation)."""

FaultHypothesisId = Literal[
    "r1_increased", "r1_decreased", "c1_increased", "c1_decreased", "c1_disconnected"
]


class Hypothesis(Contract):
    id: HypothesisId
    label: str = Field(examples=["R1 is about twice its nominal value"])
    component_id: EditableComponentId | None = None
    change: Literal["increased", "decreased", "disconnected"] | None = None
    multiplier: float | None = Field(
        default=None, description="2.0 or 0.5 for value drift; null for no fault or disconnected."
    )


HypothesisStatus = Literal["supported", "contradicted", "unresolved"]
"""supported: consistent with every recorded observation AND the only such hypothesis.
contradicted: at least one recorded observation differs beyond the declared tolerance.
unresolved: consistent with every recorded observation, but another hypothesis is too
(or nothing has been observed yet). Missing data never contradicts a hypothesis."""


class HypothesisCheck(Contract):
    observation_id: ObservationId
    consistent: bool
    quantity: QuantityName
    frequency_hz: float | None = Field(default=None, description="Where the worst deviation was.")
    observed: float
    predicted: float = Field(description="Analytical prediction for this hypothesis.")
    deviation: float
    tolerance: float
    unit: str


class HypothesisAssessment(Contract):
    hypothesis_id: HypothesisId
    status: HypothesisStatus
    checks: list[HypothesisCheck] = Field(description="One entry per evaluated observation.")


class EvidencePolicy(Contract):
    gain_tolerance_db: float
    phase_tolerance_deg: float
    current_tolerance_pct: float
    current_abs_tolerance_ma: float
    tone_gain_tolerance_db: float
    tone_phase_tolerance_deg: float
    description: str
    measurement_floor_db: float = Field(
        default=-120.0,
        description="Measurement floor for output/tone gains. Published case gains are clamped "
        "here: a value at or below it means no measurable output, and its phase (published as "
        "0°) is undefined. Equals challenges.instruments.MEASUREMENT_FLOOR_DB.",
    )
    current_floor_ma: float = Field(
        default=1e-6,
        description="Measurement floor for currents (mA). A current at or below it is published "
        "as exactly 0 (no measurable current), and its phase (published as 0°) is undefined. "
        "Equals challenges.instruments.CURRENT_FLOOR_MA.",
    )


class EvidenceState(Contract):
    assessments: list[HypothesisAssessment]
    remaining: list[HypothesisId] = Field(description="Hypotheses not contradicted.")
    statement: str = Field(
        description="Deterministic summary, e.g. 'Two explanations fit…'. While "
        "distinguishing_instruments is withheld it must not name instruments."
    )
    distinguishing_instruments: list[InstrumentKind] | None = Field(
        default=None,
        description="Instruments whose predictions differ among the remaining hypotheses. "
        "Null while status is ready/observing AND budget remains (it would steer the "
        "investigation); published once the budget is spent or the status is past observing.",
    )


# --------------------------------------------------------------------------------------
# Diagnosis, repair, reveal
# --------------------------------------------------------------------------------------


class DiagnosisConclusion(StrEnum):
    FAULT = "fault"
    NO_FAULT = "no_fault"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EvidenceCitation(Contract):
    """Validated server-side: the observation belongs to this case and succeeded, the quantity
    exists for its kind, and frequency_hz (if given) matches a measured frequency (±0.1 %)."""

    observation_id: ObservationId
    quantity: QuantityName
    frequency_hz: float | None = None
    note: str = Field(default="", max_length=240)


class DiagnosisSubmission(Contract):
    conclusion: DiagnosisConclusion
    hypothesis_id: FaultHypothesisId | None = Field(
        default=None, description="Required when conclusion = fault; forbidden otherwise."
    )
    candidate_hypothesis_ids: list[HypothesisId] = Field(
        default_factory=list,
        max_length=5,
        description="Only for insufficient_evidence: explanations that still fit.",
    )
    citations: list[EvidenceCitation] = Field(
        default_factory=list,
        max_length=8,
        description="At least one is required for fault and no_fault conclusions.",
    )
    explanation: str = Field(max_length=1200)
    suggested_instrument: InstrumentKind | None = Field(
        default=None, description="Only for insufficient_evidence: what would help."
    )

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Self:
        c = self.conclusion
        if c == DiagnosisConclusion.FAULT and self.hypothesis_id is None:
            raise ValueError("hypothesis_id is required for a fault conclusion")
        if c != DiagnosisConclusion.FAULT and self.hypothesis_id is not None:
            raise ValueError("hypothesis_id is only allowed for a fault conclusion")
        if c != DiagnosisConclusion.INSUFFICIENT_EVIDENCE and (
            self.candidate_hypothesis_ids or self.suggested_instrument is not None
        ):
            raise ValueError(
                "candidate_hypothesis_ids and suggested_instrument are only allowed for "
                "insufficient_evidence"
            )
        if c in (DiagnosisConclusion.FAULT, DiagnosisConclusion.NO_FAULT) and not self.citations:
            raise ValueError("cite at least one observation for this conclusion")
        return self


class EvidenceCheck(Contract):
    """Trusted code's assessment of the submission against recorded evidence. Recorded and
    displayed; it does not silently change the submission."""

    status: Literal[
        "supported",  # named hypothesis is the only remaining one
        "ambiguous",  # named hypothesis remains but so do others (unsupported certainty)
        "contradicted",  # named hypothesis is contradicted by recorded evidence
        "consistent_abstention",  # insufficient_evidence while ≥2 hypotheses remain
        "premature_abstention",  # insufficient_evidence while exactly one remains
        "no_supported_hypothesis",  # every hypothesis is contradicted
    ]
    remaining: list[HypothesisId]
    message: str


class DiagnosisRecord(Contract):
    id: DiagnosisId
    submission: DiagnosisSubmission
    submitted_by: Literal["user", "agent"]
    agent_run_id: AgentRunId | None = None
    evidence_check: EvidenceCheck
    created_at: datetime


class RepairProposalInput(Contract):
    """A repair replaces ONE component with a part of its intended nominal value (taken from
    the frozen intended revision). Free values are not accepted, so a compensating change to the
    wrong component cannot pass as a repair. May be replaced until verification."""

    component_id: EditableComponentId
    rationale: str = Field(default="", max_length=600)


class RepairProposal(Contract):
    id: RepairId
    component_id: EditableComponentId
    value_si: float = Field(description="The intended nominal value of the component.")
    display_value: str
    rationale: str
    proposed_by: Literal["user", "agent"]
    created_at: datetime


class VerificationCriteria(Contract):
    frequency_hz: list[float]
    gain_tolerance_db: float
    phase_tolerance_deg: float
    description: str


class VerificationPoint(Contract):
    frequency_hz: float
    repaired_gain_db: float
    expected_gain_db: float
    repaired_phase_deg: float
    expected_phase_deg: float
    within_tolerance: bool


class RepairVerification(Contract):
    """Only the proposed component is changed in the hidden circuit; the result is compared
    with the intended design. Functional recovery is reported separately from whether the
    fault was correctly identified (that is only known at reveal)."""

    id: RepairId
    proposal: RepairProposal
    status: ExecutionStatus
    criteria: VerificationCriteria
    functional_recovery: bool | None = None
    max_gain_deviation_db: float | None = None
    max_phase_deviation_deg: float | None = None
    worst_frequency_hz: float | None = None
    points: list[VerificationPoint] = Field(default_factory=list)
    tones: list[ObservedTone] = Field(default_factory=list)
    audio: AudioComparison | None = Field(
        default=None, description="Expected vs Repaired output clips at one shared gain."
    )
    summary: str
    error: ApiError | None = None


class FaultReveal(Contract):
    hypothesis_id: HypothesisId
    component_id: ComponentId | None = None
    multiplier: float | None = None
    nominal_value_si: float | None = None
    actual_value_si: float | None = None
    description: str


class CaseOutcome(Contract):
    """Definitions:
    - exact_identification: correct if a fault/no_fault conclusion names the true cause;
      abstained for insufficient_evidence; not_submitted if closed without a diagnosis.
    - unsupported_certainty: a fault/no_fault conclusion whose EvidenceCheck was ambiguous or
      contradicted.
    - functional_recovery: not_needed when the truth is no_fault (regardless of action);
      otherwise restored / not_restored from the measured verification, or not_attempted.
    """

    terminal_status: Literal["closed", "failed"]
    exact_identification: Literal["correct", "incorrect", "abstained", "not_submitted"]
    true_cause_among_candidates: bool | None = None
    functional_recovery: Literal["restored", "not_restored", "not_attempted", "not_needed"]
    unsupported_certainty: bool
    experiments_used: int
    experiments_by_user: int
    experiments_by_agent: int
    hints_used: int


class CaseReveal(Contract):
    fault: FaultReveal
    seed: str = Field(description="Published only after closing, for reproducibility.")
    generator_version: str
    outcome: CaseOutcome
    revealed_at: datetime


class Budget(Contract):
    total: int
    used: int
    remaining: int
    infrastructure_retries_per_experiment: int
    infrastructure_failures: int


class PublicCase(Contract):
    id: CaseId
    status: CaseStatus
    mode: InvestigationMode | None = None
    created_at: datetime
    updated_at: datetime
    intended_revision: Revision = Field(description="Frozen intended design (nominal labels).")
    instruments: list[InstrumentSpec]
    hypotheses: list[Hypothesis]
    policy: EvidencePolicy
    budget: Budget
    hints_used: int = 0
    observations: list[Observation]
    evidence: EvidenceState
    diagnosis: DiagnosisRecord | None = None
    repair_proposal: RepairProposal | None = None
    repair: RepairVerification | None = None
    reveal: CaseReveal | None = Field(default=None, description="Only present once closed.")
    active_run_id: AgentRunId | None = None
    available_actions: list[CaseAction]


class CaseSummary(Contract):
    id: CaseId
    status: CaseStatus
    created_at: datetime


# --------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------


class CaseCreateRequest(Contract):
    """No seed, fault, or value fields: clients cannot choose or learn the hidden state.
    ``template_id`` picks the circuit family (public); null draws it at random with the published
    ``policy.CASE_FAMILY_PRIOR``. Returns 409 if the session already has an open case."""

    template_id: Literal["rc_lowpass", "rc_highpass"] | None = None


class CaseModeRequest(Contract):
    mode: InvestigationMode


class ObservationResponse(Contract):
    observation: Observation
    case: PublicCase
