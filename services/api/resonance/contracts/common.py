"""Shared primitives for every API contract.

Conventions (apply to every contract module):
- Every physical quantity is a float in canonical SI units unless the field name carries an
  explicit unit suffix: ``_hz``, ``_s``, ``_ms``, ``_v``, ``_ma`` (milliamps), ``_db``,
  ``_dbv``, ``_deg``, ``_fs`` (fraction of digital full scale), ``_si`` (canonical SI for the
  component's unit given alongside it).
- Lists that describe one series (e.g. ``frequency_hz`` and ``gain_db``) have equal length.
- IDs are opaque, random, prefixed strings. They never encode content, seeds, or values.
- Request models forbid unknown fields. Responses never carry hidden case state.
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    """Base for all API contracts: strict about unknown fields."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------
# Opaque identifiers
# --------------------------------------------------------------------------------------

_ID_BODY = r"[A-Za-z0-9_-]{8,64}"

RevisionId = Annotated[str, Field(pattern=rf"^rev_{_ID_BODY}$", examples=["rev_x7Kq2LmP9aBc"])]
ExperimentId = Annotated[str, Field(pattern=rf"^exp_{_ID_BODY}$")]
ArtifactId = Annotated[str, Field(pattern=rf"^art_{_ID_BODY}$")]
ComparisonId = Annotated[str, Field(pattern=rf"^cmp_{_ID_BODY}$")]
GoalId = Annotated[str, Field(pattern=rf"^goal_{_ID_BODY}$")]
CaseId = Annotated[str, Field(pattern=rf"^case_{_ID_BODY}$")]
ObservationId = Annotated[str, Field(pattern=rf"^obs_{_ID_BODY}$")]
DiagnosisId = Annotated[str, Field(pattern=rf"^dx_{_ID_BODY}$")]
RepairId = Annotated[str, Field(pattern=rf"^rep_{_ID_BODY}$")]
ProposalId = Annotated[str, Field(pattern=rf"^prop_{_ID_BODY}$")]
AgentRunId = Annotated[str, Field(pattern=rf"^run_{_ID_BODY}$")]
LearnAttemptId = Annotated[str, Field(pattern=rf"^learn_{_ID_BODY}$")]

IdPrefix = Literal[
    "rev", "exp", "art", "cmp", "goal", "case", "obs", "dx", "rep", "prop", "run", "learn"
]


def new_id(prefix: IdPrefix) -> str:
    """Create an opaque random identifier. Never derive IDs from content or seeds."""
    return f"{prefix}_{secrets.token_urlsafe(12)}"


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"  # 422: invalid input; never consumes budget
    NOT_FOUND = "not_found"  # 404: unknown ID or not owned by this session
    CONFLICT = "conflict"  # 409: action not allowed in current state
    STALE_REVISION = "stale_revision"  # 409: revision is frozen or not the expected parent
    BUDGET_EXHAUSTED = "budget_exhausted"  # 409: no experiments remain
    RUN_IN_PROGRESS = "run_in_progress"  # 409: an agent run already owns this case
    SIMULATION_FAILED = "simulation_failed"  # 502: simulator returned an error
    SIMULATION_TIMEOUT = "simulation_timeout"  # 504: simulator exceeded its time limit
    SIMULATOR_UNAVAILABLE = "simulator_unavailable"  # 503: ngspice not found / not runnable
    AI_UNAVAILABLE = "ai_unavailable"  # 503: Nebius credentials/model not configured
    AI_ERROR = "ai_error"  # 502: model request failed or returned unusable output
    LIMIT_REACHED = "limit_reached"  # 429: a declared cap (tokens/tool calls/wall time) hit
    CANCELLED = "cancelled"  # 409: the user cancelled the operation
    INTERNAL_ERROR = "internal_error"  # 500


class ApiError(Contract):
    """Structured error. ``message`` is learner-facing and must never contain hidden values,
    netlists, file paths, seeds, stack traces, or secrets."""

    code: ErrorCode
    message: str
    field: str | None = Field(default=None, description="Offending request field, if any.")
    retryable: bool = False


class ErrorResponse(Contract):
    error: ApiError


# --------------------------------------------------------------------------------------
# Shared small types
# --------------------------------------------------------------------------------------

ExecutionStatus = Literal["succeeded", "failed", "cancelled"]


class SimulatorInfo(Contract):
    name: Literal["ngspice"] = "ngspice"
    version: str = Field(description="Version string reported by the ngspice binary, e.g. '47'.")


QuantityName = Literal[
    "output_gain_db",
    "output_phase_deg",
    "source_current_ma",
    "source_current_phase_deg",
    "tone_gain_db",
    "tone_phase_deg",
    "tone_amplitude_v",
    "tone_current_ma",
    "tone_current_phase_deg",
]
"""Closed vocabulary for every evidence reference, citation, and hypothesis check."""


class EvidenceRef(Contract):
    """Points a numerical claim at the recorded result that measured it."""

    result_id: str = Field(
        pattern=rf"^(exp|obs)_{_ID_BODY}$", description="An ExperimentId or ObservationId."
    )
    quantity: QuantityName
    frequency_hz: float | None = None
