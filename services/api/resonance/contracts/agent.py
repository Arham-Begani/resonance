"""Agent runs, streamed events, proposals, health/AI status, and workbench state."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from .circuits import EditableComponentId, Revision
from .common import (
    AgentRunId,
    ApiError,
    CaseId,
    Contract,
    DiagnosisId,
    EvidenceRef,
    ExperimentId,
    GoalId,
    ObservationId,
    ProposalId,
    RepairId,
    RevisionId,
)
from .experiments import ExperimentSummary, FeasibilityAnalysis, Goal

# --------------------------------------------------------------------------------------
# Health / configuration status
# --------------------------------------------------------------------------------------


class SimulatorStatus(Contract):
    available: bool
    version: str | None = None
    message: str


AiState = Literal["ready", "missing_key", "missing_model", "dev_fixture", "error"]


class AiStatus(Contract):
    """Configuration status only. 'ready' means configured, not that a call has succeeded."""

    available: bool
    state: AiState
    provider: Literal["nebius_token_factory"] = "nebius_token_factory"
    model: str | None = None
    base_url_host: str | None = None
    message: str = Field(description="Learner-safe explanation or setup instructions.")


class HealthResponse(Contract):
    status: Literal["ok", "degraded"]
    version: str
    simulator: SimulatorStatus
    ai: AiStatus


# --------------------------------------------------------------------------------------
# Explore proposals (agent proposes; the user applies)
# --------------------------------------------------------------------------------------


class ProposedChange(Contract):
    component_id: EditableComponentId
    from_value_si: float
    to_value_si: float
    from_display: str
    to_display: str


class PredictedToneGain(Contract):
    frequency_hz: float
    label: str
    predicted_gain_db: float
    basis: Literal["analytical_first_order_rc"] = "analytical_first_order_rc"


class EditProposal(Contract):
    """A previewed edit. Predictions are analytical and labeled; nothing is measured until the
    user applies the proposal (creating a revision) and runs an experiment."""

    id: ProposalId
    base_revision_id: RevisionId
    changes: list[ProposedChange] = Field(min_length=1, max_length=2)
    rationale: str
    predicted: list[PredictedToneGain]
    predicted_goal_pass: bool | None = None
    status: Literal["pending", "applied", "dismissed"]
    created_at: datetime


# --------------------------------------------------------------------------------------
# Workbench (anonymous session) state
# --------------------------------------------------------------------------------------


class GoalResponse(Contract):
    goal: Goal
    feasibility: FeasibilityAnalysis


class CurrentRevisionRequest(Contract):
    revision_id: RevisionId


class WorkbenchState(Contract):
    revisions: list[Revision] = Field(description="This session's revisions, oldest first.")
    current_revision_id: RevisionId | None = Field(
        default=None,
        description="The revision shown on the workbench (set after edits; undo moves it to a "
        "parent). Persisted per session.",
    )
    experiments: list[ExperimentSummary]
    goal: Goal
    feasibility: FeasibilityAnalysis
    proposals: list[EditProposal] = Field(description="Pending proposals only.")
    active_case_id: CaseId | None = None


# --------------------------------------------------------------------------------------
# Agent runs
# --------------------------------------------------------------------------------------

AgentRunKind = Literal["investigate", "hint", "explore"]
AgentRunStatus = Literal["running", "completed", "failed", "cancelled", "limit_reached"]


class AgentLimits(Contract):
    max_model_calls: int
    max_tool_calls: int
    max_experiments: int
    max_total_tokens: int
    wall_time_s: float
    max_retries: int


class AgentUsage(Contract):
    """Measured from provider responses. Never estimated."""

    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0


class AgentRun(Contract):
    id: AgentRunId
    kind: AgentRunKind
    source: Literal["nebius", "dev_fixture"] = Field(
        default="nebius",
        description="nebius = real Nemotron inference; dev_fixture = labeled scripted test data.",
    )
    case_id: CaseId | None = None
    revision_id: RevisionId | None = None
    status: AgentRunStatus
    model: str
    created_at: datetime
    finished_at: datetime | None = None
    limits: AgentLimits
    usage: AgentUsage
    final_message: str | None = None
    error: ApiError | None = None


class CaseAgentRunRequest(Contract):
    kind: Literal["investigate", "hint"]
    message: str | None = Field(
        default=None,
        max_length=500,
        description="Optional learner question (hint runs) or instruction (investigate runs).",
    )


class ExploreRunRequest(Contract):
    revision_id: RevisionId
    message: str = Field(min_length=1, max_length=500)
    goal_id: GoalId | None = None


# --- Streamed events (SSE: event = type, id = seq, data = JSON of the event) ---------------


class _EventBase(Contract):
    seq: int
    run_id: AgentRunId
    at: datetime


class RunStartedEvent(_EventBase):
    type: Literal["run_started"] = "run_started"
    kind: AgentRunKind
    model: str


class StatusEvent(_EventBase):
    """A genuine stage transition (e.g. 'Waiting for Nemotron', 'Running current sweep')."""

    type: Literal["status"] = "status"
    text: str


class ToolCallEvent(_EventBase):
    """Validated tool arguments as executed (serialized size capped at 2 KB; longer string
    values are truncated with an ellipsis)."""

    type: Literal["tool_call"] = "tool_call"
    call_id: str
    tool: str
    arguments: dict[str, Any]


class ToolResultEvent(_EventBase):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    tool: str
    ok: bool
    summary: str
    observation_id: ObservationId | None = None
    experiment_id: ExperimentId | None = None
    proposal_id: ProposalId | None = None
    error: ApiError | None = None


class MessageEvent(_EventBase):
    """Visible assistant text (never hidden reasoning). Numerical claims cite results."""

    type: Literal["message"] = "message"
    text: str
    citations: list[EvidenceRef] = Field(default_factory=list)


class ProposalEvent(_EventBase):
    type: Literal["proposal"] = "proposal"
    proposal: EditProposal


class DiagnosisEvent(_EventBase):
    type: Literal["diagnosis"] = "diagnosis"
    diagnosis_id: DiagnosisId


class RepairProposalEvent(_EventBase):
    type: Literal["repair_proposal"] = "repair_proposal"
    repair_id: RepairId


class UsageEvent(_EventBase):
    type: Literal["usage"] = "usage"
    usage: AgentUsage


class ErrorEvent(_EventBase):
    type: Literal["error"] = "error"
    error: ApiError


class RunFinishedEvent(_EventBase):
    type: Literal["run_finished"] = "run_finished"
    run: AgentRun


AgentEvent = Annotated[
    RunStartedEvent
    | StatusEvent
    | ToolCallEvent
    | ToolResultEvent
    | MessageEvent
    | ProposalEvent
    | DiagnosisEvent
    | RepairProposalEvent
    | UsageEvent
    | ErrorEvent
    | RunFinishedEvent,
    Field(discriminator="type"),
]


class AgentEventLog(Contract):
    run: AgentRun
    events: list[AgentEvent]
