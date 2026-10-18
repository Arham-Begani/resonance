"""Hidden-fault case lifecycle: the public side of the investigation boundary.

STABLE INTERFACE (the agent runtime codes against exactly this; ``startup(app)`` attaches the
singleton as ``app.state.case_service``)::

    class CaseService:
        async def create_case(self, session_id, template_id="rc_lowpass") -> PublicCase
        def get_case(self, session_id, case_id) -> PublicCase
        def get_observation(self, session_id, case_id, observation_id) -> Observation
        def is_closing_or_closed(self, session_id, case_id) -> bool
        async def set_mode(self, session_id, case_id, mode) -> PublicCase
        async def run_experiment(self, session_id, case_id, request, *, requested_by,
                                 agent_run_id=None, cancel=None) -> ObservationResponse
        async def submit_diagnosis(self, session_id, case_id, submission, *, submitted_by,
                                   agent_run_id=None) -> PublicCase
        async def propose_repair(self, session_id, case_id, body, *, proposed_by,
                                 agent_run_id=None) -> PublicCase
        async def verify_repair(self, session_id, case_id, *, requested_by="user",
                                agent_run_id=None) -> PublicCase
        async def close_case(self, session_id, case_id) -> PublicCase
        def claim_for_agent(self, session_id, case_id, run_id, kind=None) -> None
        def release_agent(self, session_id, case_id, run_id) -> None
        async def record_hint(self, session_id, case_id) -> PublicCase
        def register_run_canceller(self, fn: Callable[[str], Awaitable[None]]) -> None
        def register_run_liveness(self, fn: Callable[[str], bool]) -> None
        def available_actions_for_run(self, session_id, case_id, run_id) -> list[CaseAction]

Errors are ``resonance.api.errors.ApiException``: 404 not_found (unknown case/observation OR
another session's — identical message); 409 conflict (not allowed in this status; another
mutation of this case is running → ``retryable=true``; same idempotency key with a different
body), budget_exhausted, run_in_progress; 422 validation_error (never consumes budget, the
diagnosis, or the verification); 409 cancelled; 502/503/504 simulator errors after the
automatic retry (no budget consumed).

Transition table (docs/contracts.md V-11):
- (no open case) → ``create_case`` → ready (409 if the session already has an unclosed case).
  Observation 0 is the standardized symptom (output-voltage two-tone capture, uncounted,
  ``requested_by="system"``). If it fails the hidden state is discarded and the mapped simulator
  error is raised (no case). Sets the session's ``active_case_id`` (it keeps pointing at the
  latest case after close; clients check ``status``).
- ready: ``set_mode`` → observing; diagnosis → diagnosis_submitted | unresolved; close.
- observing: experiments (budget permitting); agent run (``mode == "agent"``) or hint run
  (``mode == "guide"``, then ``record_hint``) via ``claim_for_agent``; ``set_mode`` while no
  run is active;
  diagnosis → diagnosis_submitted (fault/no_fault) | unresolved (insufficient_evidence); close.
- diagnosis_submitted | unresolved: ``propose_repair`` (replaceable) then ``verify_repair``
  (once) → repair_verification; close.
- repair_verification, failed: close. closed: read-only (everything else → 409; no replay,
  ``Observation.replay`` is always false).

Concurrency (V-02): exactly one mutating operation per case at a time; a second concurrent one
gets 409 conflict (retryable). Experiments and verifications persist an ``in_flight`` marker
(the experiment's budget is reserved while it runs, released on failure/cancel, and committed
with the observation in one write). ``client_request_id`` is required: same key + same body →
the stored response, no budget; same key + different body → 409; key still in flight → 409
retryable. While an agent run is active every mutation except close (and the agent's own calls
with the owning ``agent_run_id``) → 409 run_in_progress. ``set_mode`` is refused while a run is
active, so the agent runtime calls ``set_mode(..., "agent")`` BEFORE ``claim_for_agent``.

Infrastructure failures: one automatic retry without consuming budget; if it fails again a
failed uncounted observation is recorded, ``infrastructure_failures`` grows and a structured
error is raised (an agent run must stop as failed). After ``CASE_MAX_INFRA_FAILURES`` → failed.
Cancellation (``cancel`` token, a client disconnect, or close) records a cancelled uncounted
observation and raises ``cancelled``.

Close (V-03): mark the case closing (every mutation, and every agent-facing read through
``is_closing_or_closed``, refuses), await the registered run canceller for at most
``CASE_CLOSE_AGENT_WAIT_S``, cancel and await any in-flight experiment/verification, THEN mark
the evaluator closed and attach ``CaseReveal``. Idempotent. ``fn(run_id)`` should return only
once that run can make no further case calls.

Public-only computation (V-06): before close, status, available actions, budget, evidence, and
summaries depend only on public data. Only the reveal reads hidden state.

``harness_create_case`` creates a case from a given seed for the private evaluation harness
(in-process only; there is no HTTP route for it).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..api.errors import ApiException, not_found, simulation_error_to_api
from ..circuits.revisions import build_revision
from ..circuits.units import format_value
from ..contracts.circuits import Revision, TemplateId
from ..contracts.common import ErrorCode, QuantityName, new_id
from ..contracts.internal import CancelToken, SimulationCancelled, SimulationError
from ..contracts.investigation import (
    Budget,
    CaseAction,
    CaseExperimentRequest,
    CaseOutcome,
    CaseReveal,
    CaseStatus,
    DiagnosisConclusion,
    DiagnosisRecord,
    DiagnosisSubmission,
    EvidenceState,
    FaultReveal,
    InstrumentKind,
    InvestigationMode,
    Observation,
    ObservationKind,
    ObservationResponse,
    ObservationSettings,
    PublicCase,
    RepairProposal,
    RepairProposalInput,
    RepairVerification,
    SweepObservation,
    TransientObservation,
    TransientProbe,
    VerificationCriteria,
    VerificationPoint,
)
from ..contracts.policy import (
    CASE_CLOSE_AGENT_WAIT_S,
    CASE_EXPERIMENT_BUDGET,
    CASE_INFRA_RETRIES_PER_EXPERIMENT,
    CASE_MAX_INFRA_FAILURES,
    REPAIR_GAIN_TOLERANCE_DB,
    REPAIR_PHASE_TOLERANCE_DEG,
)
from ..store.db import DocumentStore, utcnow
from ..store.session_state import load_session_state, save_session_state
from . import hypotheses, summaries
from .evaluator import (
    Evaluator,
    EvaluatorFailure,
    EvaluatorStore,
    RepairMeasurement,
    RevealData,
    UnknownCase,
)
from .instruments import (
    CASE_FREQUENCY_GRID_HZ,
    case_transient_settings,
    instrument_specs,
    sweep_settings,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger("resonance.challenges")

Requester = Literal["user", "agent"]
RunCanceller = Callable[[str], Awaitable[None]]
AgentRunKind = Literal["investigate", "hint"]

_AFTER_DIAGNOSIS = (CaseStatus.DIAGNOSIS_SUBMITTED, CaseStatus.UNRESOLVED)

_CITABLE: dict[tuple[str, str | None], frozenset[QuantityName]] = {
    ("output_voltage_sweep", None): frozenset({"output_gain_db", "output_phase_deg"}),
    ("source_current_sweep", None): frozenset({"source_current_ma", "source_current_phase_deg"}),
    ("transient", "output_voltage"): frozenset(
        {"tone_gain_db", "tone_phase_deg", "tone_amplitude_v"}
    ),
    ("transient", "source_current"): frozenset({"tone_current_ma", "tone_current_phase_deg"}),
}

_BUSY_MESSAGE = "Another action on this case is still running. Try again in a moment."


class CommittedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    observation_id: str


class InFlight(BaseModel):
    """Persisted marker of a running experiment/verification (budget reservation)."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["experiment", "repair"]
    client_request_id: str | None = None
    fingerprint: str | None = None
    started_at: datetime


class CaseRecord(BaseModel):
    """Persisted public case state (DocumentStore kind "case"). Contains NO hidden state."""

    model_config = ConfigDict(extra="forbid")

    id: str
    template_id: TemplateId
    status: CaseStatus
    mode: InvestigationMode | None = None
    created_at: datetime
    updated_at: datetime
    intended_revision: Revision
    observations: list[Observation] = Field(default_factory=list)
    diagnosis: DiagnosisRecord | None = None
    repair_proposal: RepairProposal | None = None
    repair: RepairVerification | None = None
    reveal: CaseReveal | None = None
    active_run_id: str | None = None
    budget_used: int = 0
    infrastructure_failures: int = 0
    hints_used: int = 0
    in_flight: InFlight | None = None
    request_keys: dict[str, CommittedRequest] = Field(default_factory=dict)


@dataclass(frozen=True)
class _ObsMeta:
    obs_id: str
    case_id: str
    kind: ObservationKind
    probe: TransientProbe | None
    requested_by: Literal["system", "user", "agent"]
    agent_run_id: str | None


@dataclass
class _Attempts:
    """Whether the automatic retry actually ran (failure messages must not claim it if not)."""

    retried: bool = False


def _retry_text(retried: bool) -> str:
    return ", even after one automatic retry" if retried else ""


class _LinkedCancel(CancelToken):
    """Cancelled when either this token or the caller's token is cancelled."""

    def __init__(self, parent: CancelToken | None) -> None:
        super().__init__()
        self._parent = parent

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or (self._parent is not None and self._parent.cancelled)


def _conflict(
    message: str,
    code: ErrorCode = ErrorCode.CONFLICT,
    *,
    retryable: bool = False,
    field: str | None = None,
) -> ApiException:
    return ApiException(code, message, retryable=retryable, field=field)


def _invalid(message: str, field: str | None = None) -> ApiException:
    return ApiException(ErrorCode.VALIDATION_ERROR, message, field=field)


def _intended_values(revision: Revision) -> dict[str, float]:
    return {
        c.id: float(c.value_si)
        for c in revision.components
        if c.id in ("R1", "C1") and c.value_si is not None
    }


def _fingerprint(request: CaseExperimentRequest) -> str:
    return f"{request.instrument.value}|{request.probe or ''}"


def _settings_for(instrument: InstrumentKind) -> ObservationSettings:
    if instrument == InstrumentKind.TRANSIENT_CAPTURE:
        return ObservationSettings(transient=case_transient_settings())
    return ObservationSettings(sweep=sweep_settings())


class CaseService:
    def __init__(self, store: DocumentStore, evaluator: Evaluator) -> None:
        self._store = store
        self._evaluator = evaluator
        self._locks: dict[str, asyncio.Lock] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._closing: set[str] = set()
        self._tokens: dict[str, _LinkedCancel] = {}
        self._canceller: RunCanceller | None = None
        self._liveness: Callable[[str], bool] | None = None
        self._claimed_here: set[str] = set()  # runs claimed by THIS process, not yet released

    def close(self) -> None:
        self._evaluator.close()

    # ------------------------------------------------------------------ persistence helpers

    def _lock(self, case_id: str) -> asyncio.Lock:
        lock = self._locks.get(case_id)
        if lock is None:
            lock = self._locks[case_id] = asyncio.Lock()
        return lock

    def _busy(self, case_id: str) -> bool:
        return self._lock(case_id).locked()

    def _load(self, session_id: str, case_id: str) -> CaseRecord:
        record = self._store.get("case", case_id, session_id, CaseRecord)
        if record is None:
            raise not_found("Case")
        if record.in_flight is not None and not self._busy(case_id):
            record.in_flight = None  # stale marker (e.g. after a restart): nothing is running
        if self._owner_is_stale(record):
            # The owning run is gone (crash/restart): release it and record nothing else.
            log.info("case %s: releasing ownership held by a run that is no longer live", case_id)
            record.active_run_id = None
            self._store.put("case", record.id, session_id, record)
        return record

    def _owner_is_stale(self, record: CaseRecord) -> bool:
        owner = record.active_run_id
        if owner is None or self._liveness is None or owner in self._claimed_here:
            return False
        try:
            return not self._liveness(owner)
        except Exception as exc:  # a broken predicate must not unlock a case
            log.error("run liveness check failed (%s)", type(exc).__name__)
            return False

    def _save(self, session_id: str, record: CaseRecord) -> None:
        record.updated_at = utcnow()
        self._store.put("case", record.id, session_id, record)

    def _refuse_if_closing(self, record: CaseRecord) -> None:
        if record.status == CaseStatus.CLOSED or record.id in self._closing:
            raise _conflict("This case is closed.")

    @asynccontextmanager
    async def _mutation(self, case_id: str) -> AsyncIterator[None]:
        """Exactly one mutating operation per case; a concurrent one gets a retryable 409."""
        if case_id in self._closing:
            raise _conflict("This case is closed.")
        if self._busy(case_id):
            raise _conflict(_BUSY_MESSAGE, retryable=True)
        async with self._lock(case_id):
            yield

    # ------------------------------------------------------------------ public views

    def _budget(self, record: CaseRecord) -> Budget:
        reserved = (
            1
            if record.in_flight is not None and record.in_flight.operation == ("experiment")
            else 0
        )
        return Budget(
            total=CASE_EXPERIMENT_BUDGET,
            used=record.budget_used,
            remaining=max(0, CASE_EXPERIMENT_BUDGET - record.budget_used - reserved),
            infrastructure_retries_per_experiment=CASE_INFRA_RETRIES_PER_EXPERIMENT,
            infrastructure_failures=record.infrastructure_failures,
        )

    def _evidence(self, record: CaseRecord) -> EvidenceState:
        remaining = CASE_EXPERIMENT_BUDGET - record.budget_used
        publish = record.status not in (CaseStatus.READY, CaseStatus.OBSERVING) or remaining <= 0
        return hypotheses.assess(
            record.template_id,
            _intended_values(record.intended_revision),
            record.observations,
            publish_distinguishing=publish,
            tones=record.intended_revision.source.tones,
        )

    def _available_actions(self, record: CaseRecord) -> list[CaseAction]:
        status = record.status
        if status == CaseStatus.CLOSED or record.id in self._closing:
            return []
        active = record.active_run_id is not None
        remaining = CASE_EXPERIMENT_BUDGET - record.budget_used
        actions: list[CaseAction] = []
        if not active:
            if status == CaseStatus.READY:
                actions += ["choose_mode", "submit_diagnosis"]
            elif status == CaseStatus.OBSERVING:
                actions.append("choose_mode")
                if remaining > 0:
                    actions.append("run_experiment")
                if record.mode == "agent":
                    actions.append("start_agent")
                elif record.mode == "guide":
                    actions.append("request_hint")
                actions.append("submit_diagnosis")
            elif status in _AFTER_DIAGNOSIS:
                actions.append("propose_repair")
                if record.repair_proposal is not None:
                    actions.append("verify_repair")
        else:
            actions.append("cancel_agent")
        actions.append("close")
        return actions

    def _public(self, record: CaseRecord) -> PublicCase:
        return PublicCase(
            id=record.id,
            status=record.status,
            mode=record.mode,
            created_at=record.created_at,
            updated_at=record.updated_at,
            intended_revision=record.intended_revision,
            instruments=instrument_specs(),
            hypotheses=hypotheses.hypothesis_catalog(),
            policy=hypotheses.evidence_policy(),
            budget=self._budget(record),
            hints_used=record.hints_used,
            observations=record.observations,
            evidence=self._evidence(record),
            diagnosis=record.diagnosis,
            repair_proposal=record.repair_proposal,
            repair=record.repair,
            reveal=record.reveal if record.status == CaseStatus.CLOSED else None,
            active_run_id=record.active_run_id,
            available_actions=self._available_actions(record),
        )

    # ------------------------------------------------------------------ creation

    async def create_case(self, session_id: str, template_id: str = "rc_lowpass") -> PublicCase:
        return await self._create(session_id, template_id, seed=None)

    async def harness_create_case(
        self, session_id: str, seed: str, template_id: str = "rc_lowpass"
    ) -> PublicCase:
        """PRIVATE EVALUATION HARNESS ONLY (in-process; never routed over HTTP): create a case
        whose hidden state is generated from ``seed``."""
        return await self._create(session_id, template_id, seed=seed)

    async def _create(self, session_id: str, template_id: str, seed: str | None) -> PublicCase:
        if template_id != "rc_lowpass":
            raise _invalid(
                "Mysteries are available for the RC low-pass filter only.", "template_id"
            )
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise _conflict("A mystery is already being prepared.", retryable=True)
        async with lock:
            state = load_session_state(self._store, session_id)
            if state.active_case_id is not None:
                current = self._store.get("case", state.active_case_id, session_id, CaseRecord)
                if current is not None and current.status != CaseStatus.CLOSED:
                    raise _conflict("Close the current mystery before starting a new one.")
            return await self._create_locked(session_id, seed)

    async def _create_locked(self, session_id: str, seed: str | None) -> PublicCase:
        case_id = new_id("case")
        # Hidden state first (V-19); public writes happen only after the symptom succeeded.
        design = (
            self._evaluator.create_case(case_id, "rc_lowpass")
            if seed is None
            else self._evaluator.create_case_from_seed(case_id, seed, "rc_lowpass")
        )
        meta = _ObsMeta(
            obs_id=new_id("obs"),
            case_id=case_id,
            kind="symptom",
            probe="output_voltage",
            requested_by="system",
            agent_run_id=None,
        )
        try:
            revision = build_revision(
                "rc_lowpass", design.values, origin="case_intended", frozen=True
            )
            measurement = await self._measure_with_retry(
                case_id,
                session_id,
                revision,
                InstrumentKind.TRANSIENT_CAPTURE,
                "output_voltage",
                meta.obs_id,
                None,
            )
        except BaseException as exc:
            self._evaluator.discard(case_id)
            if isinstance(exc, SimulationError):
                raise simulation_error_to_api(exc) from None
            if isinstance(exc, EvaluatorFailure):
                raise ApiException(
                    ErrorCode.INTERNAL_ERROR, "The mystery could not be prepared."
                ) from None
            raise
        now = utcnow()
        symptom = self._observation(meta, sequence=0, counted=False, measurement=measurement)
        record = CaseRecord(
            id=case_id,
            template_id="rc_lowpass",
            status=CaseStatus.READY,
            created_at=now,
            updated_at=now,
            intended_revision=revision,
            observations=[symptom],
        )
        self._store.put("revision", revision.id, session_id, revision)
        self._save(session_id, record)
        state = load_session_state(self._store, session_id)
        state.active_case_id = case_id
        save_session_state(self._store, session_id, state)
        log.info("case %s created", case_id)
        return self._public(record)

    # ------------------------------------------------------------------ reads

    def get_case(self, session_id: str, case_id: str) -> PublicCase:
        return self._public(self._load(session_id, case_id))

    def get_observation(self, session_id: str, case_id: str, observation_id: str) -> Observation:
        record = self._load(session_id, case_id)
        for obs in record.observations:
            if obs.id == observation_id:
                return obs
        raise not_found("Observation")

    def is_closing_or_closed(self, session_id: str, case_id: str) -> bool:
        """Agent tools must check this before every execution and refuse when True."""
        record = self._store.get("case", case_id, session_id, CaseRecord)
        if record is None:
            return True
        return case_id in self._closing or record.status == CaseStatus.CLOSED

    # ------------------------------------------------------------------ mode, agent, hints

    async def set_mode(self, session_id: str, case_id: str, mode: InvestigationMode) -> PublicCase:
        self._load(session_id, case_id)
        async with self._mutation(case_id):
            record = self._load(session_id, case_id)
            self._refuse_if_closing(record)
            if record.active_run_id is not None:
                raise _conflict(
                    "The AI lab partner is working on this case. Cancel it first.",
                    ErrorCode.RUN_IN_PROGRESS,
                )
            if record.status not in (CaseStatus.READY, CaseStatus.OBSERVING):
                raise _conflict("The investigation mode can only be chosen before a diagnosis.")
            record.mode = mode
            record.status = CaseStatus.OBSERVING
            self._save(session_id, record)
            return self._public(record)

    def claim_for_agent(
        self, session_id: str, case_id: str, run_id: str, kind: AgentRunKind | None = None
    ) -> None:
        """Give an agent run ownership of the case (status observing, mode chosen). The mode
        implies the run kind: "agent" → investigate, "guide" → hint; a given ``kind`` must
        match it."""
        record = self._load(session_id, case_id)
        self._refuse_if_closing(record)
        if record.active_run_id == run_id:
            return
        if record.active_run_id is not None:
            raise _conflict(
                "Another AI run is already working on this case.", ErrorCode.RUN_IN_PROGRESS
            )
        if self._busy(case_id):
            raise _conflict(_BUSY_MESSAGE, retryable=True)
        if record.status != CaseStatus.OBSERVING:
            raise _conflict("AI runs are available while the investigation is observing.")
        if record.mode is None:
            raise _conflict("Choose Guide me or Investigate for me first.")
        if kind == "investigate" and record.mode != "agent":
            raise _conflict("Choose Investigate for me before starting the AI lab partner.")
        if kind == "hint" and record.mode != "guide":
            raise _conflict("Hints are available in Guide me mode.")
        record.active_run_id = run_id
        self._claimed_here.add(run_id)
        self._save(session_id, record)

    def release_agent(self, session_id: str, case_id: str, run_id: str) -> None:
        self._claimed_here.discard(run_id)
        record = self._store.get("case", case_id, session_id, CaseRecord)
        if record is not None and record.active_run_id == run_id:
            record.active_run_id = None
            self._save(session_id, record)

    def register_run_liveness(self, fn: Callable[[str], bool]) -> None:
        """``fn(run_id)`` is True only for runs alive in this process. An owner claimed by an
        earlier process (crash/restart) whose run is not live is released on the next load.
        Runs claimed through this instance stay owners until ``release_agent``."""
        self._liveness = fn

    def available_actions_for_run(
        self, session_id: str, case_id: str, run_id: str
    ) -> list[CaseAction]:
        """The actions the OWNING run may take now (same state machine, with that run as the
        actor); empty if ``run_id`` does not own the case or the case is closing/closed.
        Run-only actions are offered; user actions (mode, close, start/cancel) are not."""
        record = self._load(session_id, case_id)
        if (
            record.active_run_id != run_id
            or record.status == CaseStatus.CLOSED
            or case_id in self._closing
        ):
            return []
        actions: list[CaseAction] = []
        if record.status in (CaseStatus.READY, CaseStatus.OBSERVING):
            budget_left = record.budget_used < CASE_EXPERIMENT_BUDGET
            if record.status == CaseStatus.OBSERVING and budget_left:
                actions.append("run_experiment")
            actions.append("submit_diagnosis")
        elif record.status in _AFTER_DIAGNOSIS and record.repair is None:
            actions.append("propose_repair")
            if record.repair_proposal is not None:
                actions.append("verify_repair")
        return actions

    async def record_hint(self, session_id: str, case_id: str) -> PublicCase:
        """Count one hint (guide mode, observing). The agent runtime calls it once per hint
        run, after ``claim_for_agent`` (so it is not refused by the run's own ownership)."""
        self._load(session_id, case_id)
        async with self._mutation(case_id):
            record = self._load(session_id, case_id)
            self._refuse_if_closing(record)
            if record.status != CaseStatus.OBSERVING or record.mode != "guide":
                raise _conflict("Hints are available in Guide me mode while observing.")
            record.hints_used += 1
            self._save(session_id, record)
            return self._public(record)

    def register_run_canceller(self, fn: RunCanceller) -> None:
        self._canceller = fn

    def _check_owner(self, record: CaseRecord, requester: Requester, run_id: str | None) -> None:
        if requester == "user":
            if record.active_run_id is not None:
                raise _conflict(
                    "The AI lab partner is working on this case. Cancel it to take over.",
                    ErrorCode.RUN_IN_PROGRESS,
                )
        elif record.active_run_id is None or (
            run_id is not None and record.active_run_id != run_id
        ):
            raise _conflict("This AI run does not own the case.", ErrorCode.RUN_IN_PROGRESS)

    # ------------------------------------------------------------------ experiments

    def _stored_response(
        self, record: CaseRecord, request: CaseExperimentRequest
    ) -> ObservationResponse | None:
        committed = record.request_keys.get(request.client_request_id)
        if committed is None:
            return None
        if committed.fingerprint != _fingerprint(request):
            raise _conflict(
                "This client_request_id was already used for a different experiment.",
                field="client_request_id",
            )
        obs = next(o for o in record.observations if o.id == committed.observation_id)
        return ObservationResponse(observation=obs, case=self._public(record))

    def _check_can_experiment(
        self, record: CaseRecord, requester: Requester, run_id: str | None
    ) -> None:
        self._refuse_if_closing(record)
        self._check_owner(record, requester, run_id)
        if record.status == CaseStatus.READY:
            raise _conflict("Choose Guide me or Investigate for me before running experiments.")
        if record.status == CaseStatus.FAILED:
            raise _conflict(
                "This case stopped after repeated simulator failures. Close it to see the answer."
            )
        if record.status != CaseStatus.OBSERVING:
            raise _conflict("Experiments end once a diagnosis has been submitted.")
        if record.budget_used >= CASE_EXPERIMENT_BUDGET:
            raise _conflict(
                f"All {CASE_EXPERIMENT_BUDGET} experiments have been used. Submit a diagnosis, "
                "or say that the evidence is insufficient.",
                ErrorCode.BUDGET_EXHAUSTED,
            )

    async def run_experiment(
        self,
        session_id: str,
        case_id: str,
        request: CaseExperimentRequest,
        *,
        requested_by: Requester,
        agent_run_id: str | None = None,
        cancel: CancelToken | None = None,
    ) -> ObservationResponse:
        record = self._load(session_id, case_id)
        stored = self._stored_response(record, request)
        if stored is not None:
            return stored
        async with self._mutation(case_id):
            record = self._load(session_id, case_id)
            stored = self._stored_response(record, request)
            if stored is not None:
                return stored
            self._check_can_experiment(record, requested_by, agent_run_id)
            if cancel is not None and cancel.cancelled:
                raise ApiException(ErrorCode.CANCELLED, "The experiment was cancelled.")
            record.in_flight = InFlight(
                operation="experiment",
                client_request_id=request.client_request_id,
                fingerprint=_fingerprint(request),
                started_at=utcnow(),
            )
            self._save(session_id, record)  # budget reserved
            meta = _ObsMeta(
                obs_id=new_id("obs"),
                case_id=case_id,
                kind=request.instrument.value,
                probe=request.probe,
                requested_by=requested_by,
                agent_run_id=agent_run_id,
            )
            token = self._tokens[case_id] = _LinkedCancel(cancel)
            attempts = _Attempts()
            try:
                measurement = await self._measure_with_retry(
                    case_id,
                    session_id,
                    record.intended_revision,
                    request.instrument,
                    request.probe,
                    meta.obs_id,
                    token,
                    attempts,
                )
            except (SimulationError, EvaluatorFailure, UnknownCase) as exc:
                raise self._record_failure(
                    session_id, meta, request.instrument, exc, retried=attempts.retried
                ) from None
            finally:
                self._tokens.pop(case_id, None)
            # Commit the observation, the budget, and the idempotency key in one write.
            record = self._load(session_id, case_id)
            obs = self._observation(
                meta, sequence=len(record.observations), counted=True, measurement=measurement
            )
            record.observations.append(obs)
            record.budget_used += 1
            record.in_flight = None
            record.request_keys[request.client_request_id] = CommittedRequest(
                fingerprint=_fingerprint(request), observation_id=obs.id
            )
            self._save(session_id, record)
            return ObservationResponse(observation=obs, case=self._public(record))

    def _record_failure(
        self,
        session_id: str,
        meta: _ObsMeta,
        instrument: InstrumentKind,
        exc: BaseException,
        *,
        retried: bool,
    ) -> ApiException:
        case_id = meta.case_id
        record = self._load(session_id, case_id)
        record.in_flight = None  # budget released
        cancelled = isinstance(exc, SimulationCancelled)
        if cancelled:
            error = ApiException(
                ErrorCode.CANCELLED, "The experiment was cancelled. No experiment budget was used."
            )
        elif isinstance(exc, SimulationError):
            error = ApiException(
                simulation_error_to_api(exc).error.code,
                f"The simulator could not complete this measurement{_retry_text(retried)}. "
                "No experiment budget was used.",
            )
        else:
            error = ApiException(
                ErrorCode.INTERNAL_ERROR,
                "The measurement could not be completed. No experiment budget was used.",
            )
        if not cancelled:
            record.infrastructure_failures += 1
            if record.infrastructure_failures >= CASE_MAX_INFRA_FAILURES:
                record.status = CaseStatus.FAILED
                error = ApiException(
                    error.error.code,
                    error.error.message + " The case has stopped after repeated simulator "
                    "failures; close it to see the answer.",
                )
            else:
                error.error.retryable = True
        obs = Observation(
            id=meta.obs_id,
            case_id=case_id,
            sequence=len(record.observations),
            kind=meta.kind,
            probe=meta.probe,
            counted=False,
            requested_by=meta.requested_by,
            agent_run_id=meta.agent_run_id,
            status="cancelled" if cancelled else "failed",
            created_at=utcnow(),
            simulator=self._evaluator.simulator_info(),
            settings=_settings_for(instrument),
            summary=(
                summaries.CANCELLED_SUMMARY if cancelled else summaries.failed_summary(retried)
            ),
            error=error.error,
        )
        record.observations.append(obs)
        self._save(session_id, record)
        log.info("case %s experiment %s", case_id, obs.status)
        return error

    async def _measure_with_retry(
        self,
        case_id: str,
        session_id: str,
        revision: Revision,
        instrument: InstrumentKind,
        probe: TransientProbe | None,
        obs_id: str,
        cancel: CancelToken | None,
        tracker: _Attempts | None = None,
    ) -> SweepObservation | TransientObservation:
        attempts = 1 + CASE_INFRA_RETRIES_PER_EXPERIMENT
        for attempt in range(attempts):
            if tracker is not None:
                tracker.retried = attempt > 0
            try:
                return await self._evaluator.observe(
                    case_id,
                    session_id,
                    source=revision.source,
                    instrument=instrument,
                    probe=probe,
                    observation_id=obs_id,
                    cancel=cancel,
                )
            except SimulationError as exc:
                if not exc.retryable or attempt + 1 >= attempts:
                    raise
                if cancel is not None and cancel.cancelled:
                    raise SimulationCancelled("The simulation was cancelled.") from None
                log.info("case %s %s retrying once", case_id, instrument.value)
        raise AssertionError("unreachable")

    def _observation(
        self,
        meta: _ObsMeta,
        *,
        sequence: int,
        counted: bool,
        measurement: SweepObservation | TransientObservation,
    ) -> Observation:
        sweep = measurement if isinstance(measurement, SweepObservation) else None
        transient = measurement if isinstance(measurement, TransientObservation) else None
        if sweep is not None:
            summary = summaries.sweep_summary(sweep)
            settings = ObservationSettings(sweep=sweep_settings())
        else:
            assert transient is not None
            summary = summaries.transient_summary(transient, symptom=meta.kind == "symptom")
            settings = ObservationSettings(transient=case_transient_settings())
        return Observation(
            id=meta.obs_id,
            case_id=meta.case_id,
            sequence=sequence,
            kind=meta.kind,
            probe=transient.probe if transient is not None else None,
            counted=counted,
            requested_by=meta.requested_by,
            agent_run_id=meta.agent_run_id,
            status="succeeded",
            created_at=utcnow(),
            simulator=self._evaluator.simulator_info(),
            settings=settings,
            sweep=sweep,
            transient=transient,
            summary=summary,
        )

    # ------------------------------------------------------------------ diagnosis

    @staticmethod
    def _validate_citations(record: CaseRecord, submission: DiagnosisSubmission) -> None:
        by_id = {o.id: o for o in record.observations if o.status == "succeeded"}
        for i, citation in enumerate(submission.citations):
            obs = by_id.get(citation.observation_id)
            if obs is None:
                raise _invalid(
                    "Cite a successful observation recorded in this case.",
                    f"citations.{i}.observation_id",
                )
            if obs.sweep is not None:
                key: tuple[str, str | None] = (obs.kind, None)
                freqs = obs.sweep.frequency_hz
            else:
                assert obs.transient is not None
                key = ("transient", obs.transient.probe)
                freqs = [t.frequency_hz for t in obs.transient.tones]
            if citation.quantity not in _CITABLE[key]:
                allowed = ", ".join(sorted(_CITABLE[key]))
                raise _invalid(
                    f"That observation does not measure {citation.quantity}; it measures "
                    f"{allowed}.",
                    f"citations.{i}.quantity",
                )
            f = citation.frequency_hz
            if f is not None and not any(math.isclose(f, m, rel_tol=1e-3) for m in freqs):
                raise _invalid(
                    "frequency_hz must match a frequency measured by that observation.",
                    f"citations.{i}.frequency_hz",
                )

    async def submit_diagnosis(
        self,
        session_id: str,
        case_id: str,
        submission: DiagnosisSubmission,
        *,
        submitted_by: Requester,
        agent_run_id: str | None = None,
    ) -> PublicCase:
        self._load(session_id, case_id)
        async with self._mutation(case_id):
            record = self._load(session_id, case_id)
            self._refuse_if_closing(record)
            self._check_owner(record, submitted_by, agent_run_id)
            if record.diagnosis is not None:
                raise _conflict("A diagnosis has already been submitted for this case.")
            if record.status not in (CaseStatus.READY, CaseStatus.OBSERVING):
                raise _conflict("A diagnosis cannot be submitted in the current state.")
            self._validate_citations(record, submission)
            state = hypotheses.assess(
                record.template_id,
                _intended_values(record.intended_revision),
                record.observations,
                publish_distinguishing=False,
                tones=record.intended_revision.source.tones,
            )
            record.diagnosis = DiagnosisRecord(
                id=new_id("dx"),
                submission=submission,
                submitted_by=submitted_by,
                agent_run_id=agent_run_id if submitted_by == "agent" else None,
                evidence_check=hypotheses.evidence_check(submission, state),
                created_at=utcnow(),
            )
            record.status = (
                CaseStatus.UNRESOLVED
                if submission.conclusion == DiagnosisConclusion.INSUFFICIENT_EVIDENCE
                else CaseStatus.DIAGNOSIS_SUBMITTED
            )
            self._save(session_id, record)
            log.info("case %s diagnosis submitted", case_id)
            return self._public(record)

    # ------------------------------------------------------------------ repair

    async def propose_repair(
        self,
        session_id: str,
        case_id: str,
        body: RepairProposalInput,
        *,
        proposed_by: Requester,
        agent_run_id: str | None = None,
    ) -> PublicCase:
        """Record (or replace) the repair: ONE component back to its intended nominal value."""
        self._load(session_id, case_id)
        async with self._mutation(case_id):
            record = self._load(session_id, case_id)
            self._refuse_if_closing(record)
            self._check_owner(record, proposed_by, agent_run_id)
            if record.status not in _AFTER_DIAGNOSIS or record.repair is not None:
                raise _conflict("A repair can be proposed after a diagnosis, before verification.")
            component = next(
                (c for c in record.intended_revision.components if c.id == body.component_id),
                None,
            )
            if component is None or component.value_si is None or component.display_value is None:
                raise _invalid("Only R1 or C1 can be repaired.", "component_id")
            record.repair_proposal = RepairProposal(
                id=new_id("rep"),
                component_id=body.component_id,
                value_si=float(component.value_si),
                display_value=component.display_value,
                rationale=body.rationale,
                proposed_by=proposed_by,
                created_at=utcnow(),
            )
            self._save(session_id, record)
            return self._public(record)

    async def verify_repair(
        self,
        session_id: str,
        case_id: str,
        *,
        requested_by: Requester = "user",
        agent_run_id: str | None = None,
    ) -> PublicCase:
        """Verify the RECORDED proposal once: only that component changes in the hidden
        circuit; the result is compared with the intended design on the declared grid."""
        self._load(session_id, case_id)
        async with self._mutation(case_id):
            record = self._load(session_id, case_id)
            self._refuse_if_closing(record)
            self._check_owner(record, requested_by, agent_run_id)
            if record.repair is not None or record.status not in _AFTER_DIAGNOSIS:
                raise _conflict("A repair can be verified once, after a diagnosis.")
            if record.repair_proposal is None:
                raise _conflict("Propose a repair before verifying it.")
            proposal = record.repair_proposal
            record.in_flight = InFlight(operation="repair", started_at=utcnow())
            self._save(session_id, record)
            token = self._tokens[case_id] = _LinkedCancel(None)
            attempts = _Attempts()
            try:
                measurement = await self._repair_with_retry(
                    record, session_id, proposal, token, attempts
                )
            except (SimulationError, EvaluatorFailure, UnknownCase) as exc:
                raise self._record_repair_failure(
                    session_id, case_id, exc, retried=attempts.retried
                ) from None
            finally:
                self._tokens.pop(case_id, None)
            record = self._load(session_id, case_id)
            record.in_flight = None
            record.repair = self._verification(proposal, measurement.sweep, measurement.transient)
            record.status = CaseStatus.REPAIR_VERIFICATION
            self._save(session_id, record)
            log.info("case %s repair verified", case_id)
            return self._public(record)

    async def _repair_with_retry(
        self,
        record: CaseRecord,
        session_id: str,
        proposal: RepairProposal,
        cancel: CancelToken,
        tracker: _Attempts,
    ) -> RepairMeasurement:
        attempts = 1 + CASE_INFRA_RETRIES_PER_EXPERIMENT
        for attempt in range(attempts):
            tracker.retried = attempt > 0
            try:
                return await self._evaluator.measure_repair(
                    record.id,
                    session_id,
                    source=record.intended_revision.source,
                    component_id=proposal.component_id,
                    value_si=proposal.value_si,
                    source_id=proposal.id,
                    cancel=cancel,
                )
            except SimulationError as exc:
                if not exc.retryable or attempt + 1 >= attempts or cancel.cancelled:
                    raise
                log.info("case %s repair retrying once", record.id)
        raise AssertionError("unreachable")

    def _record_repair_failure(
        self, session_id: str, case_id: str, exc: BaseException, *, retried: bool
    ) -> ApiException:
        record = self._load(session_id, case_id)
        record.in_flight = None
        if isinstance(exc, SimulationCancelled):
            self._save(session_id, record)
            return ApiException(
                ErrorCode.CANCELLED, "The repair check was cancelled; it can be run again."
            )
        if isinstance(exc, SimulationError):
            code = simulation_error_to_api(exc).error.code
            message = f"The simulator could not verify this repair{_retry_text(retried)}."
        else:
            code = ErrorCode.INTERNAL_ERROR
            message = "The repair could not be verified."
        record.infrastructure_failures += 1
        retryable = True
        if record.infrastructure_failures >= CASE_MAX_INFRA_FAILURES:
            record.status = CaseStatus.FAILED
            retryable = False
            message += " The case has stopped after repeated simulator failures; close it."
        self._save(session_id, record)
        return ApiException(code, message, retryable=retryable)

    @staticmethod
    def _verification(
        proposal: RepairProposal, sweep: SweepObservation, transient: TransientObservation
    ) -> RepairVerification:
        points: list[VerificationPoint] = []
        worst_ratio, worst_f = -1.0, sweep.frequency_hz[0]
        max_gain = max_phase = 0.0
        for f, g, eg, p, ep in zip(
            sweep.frequency_hz,
            sweep.magnitude,
            sweep.expected_magnitude,
            sweep.phase_deg,
            sweep.expected_phase_deg,
            strict=True,
        ):
            # Differences of values quantized at the evaluator boundary, same resolutions.
            dg = round(abs(g - eg), 3)
            dp = round(abs(float(hypotheses.wrap_deg(p - ep))), 2)
            max_gain, max_phase = max(max_gain, dg), max(max_phase, dp)
            ratio = max(dg / REPAIR_GAIN_TOLERANCE_DB, dp / REPAIR_PHASE_TOLERANCE_DEG)
            if ratio > worst_ratio:
                worst_ratio, worst_f = ratio, f
            points.append(
                VerificationPoint(
                    frequency_hz=f,
                    repaired_gain_db=g,
                    expected_gain_db=eg,
                    repaired_phase_deg=p,
                    expected_phase_deg=ep,
                    within_tolerance=dg <= REPAIR_GAIN_TOLERANCE_DB
                    and dp <= REPAIR_PHASE_TOLERANCE_DEG,
                )
            )
        recovered = all(pt.within_tolerance for pt in points)
        return RepairVerification(
            id=proposal.id,
            proposal=proposal,
            status="succeeded",
            criteria=VerificationCriteria(
                frequency_hz=list(CASE_FREQUENCY_GRID_HZ),
                gain_tolerance_db=REPAIR_GAIN_TOLERANCE_DB,
                phase_tolerance_deg=REPAIR_PHASE_TOLERANCE_DEG,
                description=(
                    "Only the proposed component is replaced (with its intended nominal value) "
                    "in the hidden circuit. Functional recovery means the repaired output gain "
                    f"and phase match the intended design within ±{REPAIR_GAIN_TOLERANCE_DB:g} "
                    f"dB and ±{REPAIR_PHASE_TOLERANCE_DEG:g}° at all {len(points)} frequencies "
                    "from 20 Hz to 20 kHz. Whether the fault was correctly identified is "
                    "reported separately when the case closes."
                ),
            ),
            functional_recovery=recovered,
            max_gain_deviation_db=max_gain,
            max_phase_deviation_deg=max_phase,
            worst_frequency_hz=worst_f,
            points=points,
            tones=transient.tones,
            audio=transient.audio,
            summary=summaries.repair_summary(
                proposal.component_id,
                proposal.display_value,
                points,
                recovered,
                REPAIR_GAIN_TOLERANCE_DB,
                REPAIR_PHASE_TOLERANCE_DEG,
            ),
        )

    # ------------------------------------------------------------------ close and reveal

    async def close_case(self, session_id: str, case_id: str) -> PublicCase:
        record = self._load(session_id, case_id)
        if record.status == CaseStatus.CLOSED:
            return self._public(record)
        if case_id in self._closing:
            raise _conflict("This case is already closing.", retryable=True)
        self._closing.add(case_id)
        try:
            if record.active_run_id is not None:
                await self._stop_run(case_id, record.active_run_id)
            token = self._tokens.get(case_id)
            if token is not None:
                token.cancel()
            async with self._lock(case_id):  # waits for any in-flight work to finish
                record = self._load(session_id, case_id)
                if record.status == CaseStatus.CLOSED:
                    return self._public(record)
                terminal: Literal["closed", "failed"] = (
                    "failed" if record.status == CaseStatus.FAILED else "closed"
                )
                record.active_run_id = None
                record.in_flight = None
                self._evaluator.mark_closed(case_id)
                data = self._evaluator.reveal(case_id)
                record.reveal = self._reveal(record, data, terminal)
                record.status = CaseStatus.CLOSED
                self._save(session_id, record)
                log.info("case %s revealed", case_id)
                return self._public(record)
        finally:
            self._closing.discard(case_id)

    async def _stop_run(self, case_id: str, run_id: str) -> None:
        if self._canceller is None:
            log.warning("case %s: no run canceller registered", case_id)
            return
        try:
            await asyncio.wait_for(self._canceller(run_id), timeout=CASE_CLOSE_AGENT_WAIT_S)
        except TimeoutError:
            log.warning("case %s: AI run did not stop within the close wait", case_id)
        except Exception as exc:
            log.error("case %s: stopping the AI run failed (%s)", case_id, type(exc).__name__)

    @staticmethod
    def _reveal(
        record: CaseRecord, data: RevealData, terminal: Literal["closed", "failed"]
    ) -> CaseReveal:
        true_id = data.hypothesis_id
        dx = record.diagnosis
        among: bool | None = None
        unsupported = False
        exact: Literal["correct", "incorrect", "abstained", "not_submitted"]
        if dx is None:
            exact = "not_submitted"
        elif dx.submission.conclusion == DiagnosisConclusion.INSUFFICIENT_EVIDENCE:
            exact = "abstained"
            among = true_id in dx.submission.candidate_hypothesis_ids
        else:
            named = hypotheses.named_hypothesis(dx.submission)
            exact = "correct" if named == true_id else "incorrect"
            unsupported = hypotheses.unsupported_certainty(dx.submission, dx.evidence_check)
        recovery: Literal["restored", "not_restored", "not_attempted", "not_needed"]
        if true_id == "no_fault":
            recovery = "not_needed"
        elif record.repair is not None and record.repair.functional_recovery is not None:
            recovery = "restored" if record.repair.functional_recovery else "not_restored"
        else:
            recovery = "not_attempted"
        counted = [o for o in record.observations if o.counted]
        hyp = hypotheses.HYPOTHESES_BY_ID[true_id]
        component = next(
            (c for c in record.intended_revision.components if c.id == data.component_id), None
        )
        if component is not None and component.unit is not None and data.actual_value_si:
            actual = format_value(data.actual_value_si, component.unit)
            description = (
                f"{hyp.label}: {component.id} was {actual} instead of the intended "
                f"{component.display_value}."
            )
        else:
            description = "No fault: the circuit matched its intended design."
        return CaseReveal(
            fault=FaultReveal(
                hypothesis_id=true_id,
                component_id=component.id if component is not None else None,
                multiplier=data.multiplier,
                nominal_value_si=data.nominal_value_si,
                actual_value_si=data.actual_value_si,
                description=description,
            ),
            seed=data.seed,
            generator_version=data.generator_version,
            outcome=CaseOutcome(
                terminal_status=terminal,
                exact_identification=exact,
                true_cause_among_candidates=among,
                functional_recovery=recovery,
                unsupported_certainty=unsupported,
                experiments_used=record.budget_used,
                experiments_by_user=sum(1 for o in counted if o.requested_by == "user"),
                experiments_by_agent=sum(1 for o in counted if o.requested_by == "agent"),
                hints_used=record.hints_used,
            ),
            revealed_at=utcnow(),
        )


# ---------------------------------------------------------------------- app wiring


async def startup(app: FastAPI) -> None:
    """Attach ``app.state.case_service``. The evaluator stays private to the service."""
    settings = app.state.settings
    evaluator = Evaluator(
        EvaluatorStore(settings.evaluator_db_path), app.state.simulator, app.state.artifacts
    )
    app.state.case_service = CaseService(app.state.store, evaluator)


async def shutdown(app: FastAPI) -> None:
    service = getattr(app.state, "case_service", None)
    if isinstance(service, CaseService):
        service.close()
