"""Agent run lifecycle: start, stream, cancel, persist.

``startup(app)`` attaches ``app.state.agent_service`` and registers the run canceller with the
CaseService, so that closing a case stops its agent run before anything is revealed.

Runs are background asyncio tasks. Each run keeps an ordered in-memory event list (``seq``
starts at 1) that live SSE followers and the JSON log read; the ``AgentRun`` record is persisted
in the document store (kind ``agent_run``) when the run starts and when it finishes. Events are
genuine: they are emitted only by the loop and tools as things actually happen, and the log is
sealed after ``run_finished``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any

from fastapi import FastAPI

from ..api.errors import ApiException, not_found
from ..config import Settings
from ..contracts.agent import (
    AgentEvent,
    AgentEventLog,
    AgentLimits,
    AgentRun,
    AgentRunKind,
    AgentUsage,
    CaseAgentRunRequest,
    EditProposal,
    ErrorEvent,
    ExploreRunRequest,
    RunFinishedEvent,
    RunStartedEvent,
)
from ..contracts.common import ApiError, ErrorCode, new_id
from ..contracts.investigation import InvestigationMode, PublicCase
from ..contracts.policy import (
    AGENT_MAX_MODEL_CALLS,
    AGENT_MAX_RETRIES,
    AGENT_MAX_TOOL_CALLS,
    AGENT_MAX_TOTAL_TOKENS,
    AGENT_WALL_TIME_S,
    CASE_CLOSE_AGENT_WAIT_S,
    CASE_EXPERIMENT_BUDGET,
)
from ..store.db import DocumentStore, utcnow
from ..store.session_state import load_session_state
from . import prompts
from .case_tools import CaseCitations, CaseRunContext, hint_toolset, investigate_toolset
from .client import ChatClient, create_chat_client
from .explore_tools import (
    AppExplorePort,
    ExploreCitations,
    ExploreRunContext,
    explore_toolset,
    load_session_revision,
)
from .loop import AgentLoop, CitationResolver, LoopResult, StopSignal
from .ports import CaseServicePort, ExplorePort
from .tools import ToolSet

log = logging.getLogger("resonance.agent")

ClientFactory = Callable[[Settings], ChatClient]

MAX_RETAINED_RUNS = 256
CANCEL_ENDPOINT_WAIT_S = 5.0


def limits_for(kind: AgentRunKind) -> AgentLimits:
    """Per-kind caps. Investigate/explore use the declared policy caps; a hint is a short,
    read-only run, so its caps are tighter (never looser than policy)."""
    if kind == "hint":
        return AgentLimits(
            max_model_calls=min(4, AGENT_MAX_MODEL_CALLS),
            max_tool_calls=min(4, AGENT_MAX_TOOL_CALLS),
            max_experiments=0,
            max_total_tokens=min(30_000, AGENT_MAX_TOTAL_TOKENS),
            wall_time_s=min(90.0, AGENT_WALL_TIME_S),
            max_retries=AGENT_MAX_RETRIES,
        )
    return AgentLimits(
        max_model_calls=AGENT_MAX_MODEL_CALLS,
        max_tool_calls=AGENT_MAX_TOOL_CALLS,
        max_experiments=CASE_EXPERIMENT_BUDGET,
        max_total_tokens=AGENT_MAX_TOTAL_TOKENS,
        wall_time_s=AGENT_WALL_TIME_S,
        max_retries=AGENT_MAX_RETRIES,
    )


class RunHandle:
    """In-memory state of one run: ordered events plus live-follow notification."""

    def __init__(self, run: AgentRun, session_id: str, stop: StopSignal) -> None:
        self.run = run
        self.session_id = session_id
        self.events: list[AgentEvent] = []
        self.stop = stop
        self.task: asyncio.Task[None] | None = None
        self.release: Callable[[], None] | None = None  # gives the case back (case runs)
        # Runs after the loop completes successfully, before the run is finalized.
        self.after_success: Callable[[], Awaitable[None]] | None = None
        self.finished = asyncio.Event()
        self._wake = asyncio.Event()

    @property
    def running(self) -> bool:
        return not self.finished.is_set()

    def emit(self, event_type: type[Any], **fields: Any) -> AgentEvent | None:
        """Append a genuine event. After ``run_finished`` the log is sealed: anything a
        cancelled task might still try to emit is dropped, never shown."""
        if self.finished.is_set():
            return None
        event: AgentEvent = event_type(
            seq=len(self.events) + 1, run_id=self.run.id, at=utcnow(), **fields
        )
        self.events.append(event)
        wake, self._wake = self._wake, asyncio.Event()
        wake.set()
        return event

    def snapshot(self) -> AgentRun:
        return self.run.model_copy(deep=True)

    async def follow(self, after: int = 0) -> AsyncIterator[AgentEvent]:
        """Yield events with ``seq > after`` and keep following until the run finishes."""
        index = max(0, after)
        while True:
            while index < len(self.events):
                yield self.events[index]
                index += 1
            if self.finished.is_set():
                return
            waiter = self._wake
            if index < len(self.events):
                continue
            await waiter.wait()


class AgentService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: DocumentStore,
        cases: CaseServicePort | None,
        explore: ExplorePort | None,
        client_factory: ClientFactory | None = None,
        limits: Callable[[AgentRunKind], AgentLimits] = limits_for,
    ) -> None:
        self._settings = settings
        self._limits = limits
        self._store = store
        self._cases = cases
        self._explore = explore
        self._client_factory: ClientFactory = client_factory or create_chat_client
        self._runs: dict[str, RunHandle] = {}

    # ----------------------------------------------------------------------------------
    # Starting runs
    # ----------------------------------------------------------------------------------

    async def start_case_run(
        self, session_id: str, case_id: str, body: CaseAgentRunRequest
    ) -> AgentRun:
        client = self._client_factory(self._settings)  # ai_unavailable (503) before anything
        run_id = new_id("run")
        stop = StopSignal()
        claimed = False
        try:
            cases = self._require_cases()
            case = await self._ready_case(cases, session_id, case_id, body.kind)
            # 409 if closed/owned, or if the mode does not allow this kind (hints: Guide me).
            cases.claim_for_agent(session_id, case_id, run_id, body.kind)
            claimed = True
            handle = self._new_handle(
                run_id,
                body.kind,
                session_id,
                client.model,
                stop,
                case_id=case.id,
                revision_id=None,
            )
        except BaseException:
            if claimed:
                with suppress(Exception):
                    self._require_cases().release_agent(session_id, case_id, run_id)
            await _close_quietly(client)
            raise
        ctx = CaseRunContext(
            session_id=session_id, case_id=case_id, run_id=run_id, cases=cases, cancel=stop.token
        )
        # Public, trusted case facts only (family/topology of the frozen intended design and
        # the hypothesis catalog): identical for every hidden condition of the family.
        context = prompts.case_context(
            case.intended_revision.template_id, [(h.id, h.label) for h in case.hypotheses]
        )
        if body.kind == "investigate":
            toolset = investigate_toolset(ctx)
            system = prompts.investigate_prompt(CASE_EXPERIMENT_BUDGET, context)
            turn = prompts.learner_turn(body.message, prompts.DEFAULT_INVESTIGATE_TURN)
        else:
            toolset = hint_toolset(ctx)
            system = prompts.hint_prompt(context)
            turn = prompts.learner_turn(body.message, prompts.DEFAULT_HINT_TURN)

        def release() -> None:
            cases.release_agent(session_id, case_id, run_id)

        handle.release = release
        if body.kind == "hint":
            # hints_used counts hints actually DELIVERED (B-05): a hint run is recorded only
            # when it completes with a visible answer, never when it fails or is cancelled.
            async def record_hint() -> None:
                await cases.record_hint(session_id, case_id)

            handle.after_success = record_hint
        self._launch(handle, client, toolset, system, turn, CaseCitations(ctx))
        return handle.snapshot()

    async def start_explore_run(self, session_id: str, body: ExploreRunRequest) -> AgentRun:
        client = self._client_factory(self._settings)  # ai_unavailable (503) before anything
        try:
            explore = self._require_explore()
            revision = load_session_revision(self._store, session_id, body.revision_id)
            if any(
                h.running and h.session_id == session_id and h.run.kind == "explore"
                for h in self._runs.values()
            ):
                raise ApiException(
                    ErrorCode.RUN_IN_PROGRESS,
                    "An AI explore run is already in progress. Wait for it or cancel it.",
                )
            goal = load_session_state(self._store, session_id).goal
            if body.goal_id is not None and body.goal_id != goal.id:
                raise not_found("Goal")
            run_id = new_id("run")
            handle = self._new_handle(
                run_id,
                "explore",
                session_id,
                client.model,
                StopSignal(),
                case_id=None,
                revision_id=revision.id,
            )
        except BaseException:
            await _close_quietly(client)
            raise
        ctx = ExploreRunContext(
            session_id=session_id,
            run_id=run_id,
            revision_id=revision.id,
            goal_id=goal.id,
            store=self._store,
            explore=explore,
            cancel=handle.stop.token,
        )
        self._launch(
            handle,
            client,
            explore_toolset(ctx),
            prompts.explore_prompt(revision.id, revision.template_id),
            prompts.learner_turn(body.message, ""),
            ExploreCitations(ctx),
        )
        return handle.snapshot()

    async def _ready_case(
        self, cases: CaseServicePort, session_id: str, case_id: str, kind: str
    ) -> PublicCase:
        """Check (and, for a case whose mode is not chosen yet, choose) the mode the request
        implies: Investigate for me = ``agent``, Guide me (hints) = ``guide``."""
        case = cases.get_case(session_id, case_id)  # 404 for unknown / other session
        action = "start_agent" if kind == "investigate" else "request_hint"
        if action not in case.available_actions and case.mode is None:
            if "choose_mode" in case.available_actions:
                mode: InvestigationMode = "agent" if kind == "investigate" else "guide"
                case = await cases.set_mode(session_id, case_id, mode)
        if action not in case.available_actions:
            if case.active_run_id is not None:
                raise ApiException(
                    ErrorCode.RUN_IN_PROGRESS, "An AI run is already working on this case."
                )
            what = "investigate" if kind == "investigate" else "give a hint for"
            raise ApiException(
                ErrorCode.CONFLICT, f"The AI can't {what} this case in its current state."
            )
        return case

    def _new_handle(
        self,
        run_id: str,
        kind: AgentRunKind,
        session_id: str,
        model: str,
        stop: StopSignal,
        *,
        case_id: str | None,
        revision_id: str | None,
    ) -> RunHandle:
        run = AgentRun(
            id=run_id,
            kind=kind,
            source="nebius",
            case_id=case_id,
            revision_id=revision_id,
            status="running",
            model=model,
            created_at=utcnow(),
            limits=self._limits(kind),
            usage=AgentUsage(),
        )
        handle = RunHandle(run, session_id, stop)
        self._store.put("agent_run", run.id, session_id, run)
        self._runs[run.id] = handle
        self._evict()
        handle.emit(RunStartedEvent, kind=kind, model=model)
        return handle

    def _launch(
        self,
        handle: RunHandle,
        client: ChatClient,
        toolset: ToolSet,
        system: str,
        turn: str,
        citations: CitationResolver,
    ) -> None:
        loop = AgentLoop(
            client=client,
            toolset=toolset,
            limits=handle.run.limits,
            sink=handle,
            stop=handle.stop,
            usage=handle.run.usage,
            resolve_citations=citations,
        )
        handle.task = asyncio.get_running_loop().create_task(
            self._drive(handle, loop, client, system, turn), name=f"agent-{handle.run.id}"
        )
        log.info("agent run %s started (%s)", handle.run.id, handle.run.kind)

    async def _drive(
        self, handle: RunHandle, loop: AgentLoop, client: ChatClient, system: str, turn: str
    ) -> None:
        result: LoopResult
        try:
            result = await loop.run(system, turn)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            result = _cancelled_result()
        except Exception as exc:  # noqa: BLE001 - report a safe error, never the raw text
            log.error("agent run %s failed: %s", handle.run.id, type(exc).__name__)
            result = LoopResult(
                status="failed",
                error=ApiError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="The AI run stopped because of an internal error.",
                ),
            )
        if result.status == "completed" and handle.after_success is not None:
            try:
                await handle.after_success()
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                log.warning("agent run %s: completion hook cancelled", handle.run.id)
            except Exception as exc:  # noqa: BLE001 - e.g. the case started closing
                log.warning(
                    "agent run %s: completion hook failed: %s", handle.run.id, type(exc).__name__
                )
        self._finalize(handle, result)
        await _close_quietly(client)

    def _finalize(self, handle: RunHandle, result: LoopResult) -> None:
        """Finish a run exactly once: stop its work, give the case back, persist the record,
        emit ``run_finished``, and seal the event log."""
        if handle.finished.is_set():
            return
        handle.stop.token.cancel()  # nothing started by this run may keep running
        if handle.release is not None:
            try:
                handle.release()
            except Exception as exc:  # noqa: BLE001
                log.error("agent run %s release failed: %s", handle.run.id, type(exc).__name__)
        run = handle.run
        run.status = result.status
        run.finished_at = utcnow()
        run.final_message = result.final_message
        run.error = result.error
        if result.error is not None and result.status in ("failed", "limit_reached"):
            handle.emit(ErrorEvent, error=result.error)
        try:
            self._store.put("agent_run", run.id, handle.session_id, run)
        except Exception as exc:  # noqa: BLE001
            log.error("agent run %s could not be saved: %s", run.id, type(exc).__name__)
        handle.emit(RunFinishedEvent, run=handle.snapshot())
        handle.finished.set()
        log.info(
            "agent run %s finished: %s (model calls %d, tool calls %d, tokens %d)",
            run.id,
            run.status,
            run.usage.model_calls,
            run.usage.tool_calls,
            run.usage.total_tokens,
        )

    # ----------------------------------------------------------------------------------
    # Reading runs
    # ----------------------------------------------------------------------------------

    def handle_for(self, session_id: str, run_id: str) -> RunHandle:
        handle = self._runs.get(run_id)
        if handle is None or handle.session_id != session_id:
            raise not_found("Agent run")
        return handle

    def get_run(self, session_id: str, run_id: str) -> AgentRun:
        handle = self._runs.get(run_id)
        if handle is not None and handle.session_id == session_id:
            return handle.snapshot()
        run = self._store.get("agent_run", run_id, session_id, AgentRun)
        if run is None:
            raise not_found("Agent run")
        if run.status == "running":
            # Persisted as running but not in memory: the server restarted mid-run (B-02).
            run.status = "failed"
            run.finished_at = utcnow()
            run.error = ApiError(
                code=ErrorCode.INTERNAL_ERROR,
                message="The server restarted before this run finished.",
            )
            self._store.put("agent_run", run.id, session_id, run)
        if run.case_id is not None:
            self._release_orphan(session_id, run.case_id, run.id)
        return run

    def is_live(self, run_id: str) -> bool:
        """Run-liveness predicate registered with the CaseService: True only for runs that
        are running in THIS process (an owner lost in a crash/restart is stale)."""
        handle = self._runs.get(run_id)
        return handle is not None and handle.running

    def _release_orphan(self, session_id: str, case_id: str, run_id: str) -> None:
        """Free a case still owned by a run that is not live here (idempotent: the
        CaseService releases only if this run is still the owner)."""
        if self._cases is None or self.is_live(run_id):
            return
        try:
            self._cases.release_agent(session_id, case_id, run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("agent run %s: releasing its case failed: %s", run_id, type(exc).__name__)

    def event_log(self, session_id: str, run_id: str) -> AgentEventLog:
        handle = self._runs.get(run_id)
        if handle is None or handle.session_id != session_id:
            self.get_run(session_id, run_id)  # 404 if it never existed for this session
            raise ApiException(
                ErrorCode.NOT_FOUND, "The event log for this run is no longer available."
            )
        return AgentEventLog(run=handle.snapshot(), events=list(handle.events))

    # ----------------------------------------------------------------------------------
    # Cancellation
    # ----------------------------------------------------------------------------------

    async def cancel(self, session_id: str, run_id: str) -> AgentRun:
        handle = self._runs.get(run_id)
        if handle is None or handle.session_id != session_id:
            return self.get_run(session_id, run_id)  # 404, or the finished persisted record
        if handle.running:
            handle.stop.request()
            with suppress(TimeoutError):
                await asyncio.wait_for(handle.finished.wait(), timeout=CANCEL_ENDPOINT_WAIT_S)
        return handle.snapshot()

    async def cancel_and_wait(self, run_id: str) -> None:
        """Run canceller registered with the CaseService (``close_case`` awaits it before the
        reveal). Sets the cancel token and waits for the run to stop cooperatively, then
        cancels the task. Within ``CASE_CLOSE_AGENT_WAIT_S`` the run is finalized (case
        released, ``run_finished`` emitted, log sealed), so no event of this run, and no case
        call by it, can follow the reveal."""
        handle = self._runs.get(run_id)
        if handle is None or not handle.running:
            return
        handle.stop.request()
        # Stay inside the CaseService's own CASE_CLOSE_AGENT_WAIT_S bound on this call.
        budget = max(1.0, CASE_CLOSE_AGENT_WAIT_S - 1.5)
        cooperative = budget * 0.7
        try:
            with suppress(TimeoutError):
                await asyncio.wait_for(handle.finished.wait(), timeout=cooperative)
            if handle.running and handle.task is not None:
                handle.task.cancel()
                with suppress(TimeoutError):
                    await asyncio.wait_for(handle.finished.wait(), timeout=budget - cooperative)
        finally:
            # Also runs if the caller gives up on us: the run must be over when we return.
            if handle.running:
                log.warning("agent run %s did not stop in time; finalizing as cancelled", run_id)
                if handle.task is not None:
                    handle.task.cancel()
                self._finalize(handle, _cancelled_result())

    async def shutdown(self) -> None:
        running = [h for h in self._runs.values() if h.running]
        for handle in running:
            handle.stop.request()
        for handle in running:
            with suppress(TimeoutError):
                await asyncio.wait_for(handle.finished.wait(), timeout=5.0)
            if handle.running:
                if handle.task is not None:
                    handle.task.cancel()
                self._finalize(handle, _cancelled_result())

    # ----------------------------------------------------------------------------------
    # Proposals
    # ----------------------------------------------------------------------------------

    def dismiss_proposal(self, session_id: str, proposal_id: str) -> EditProposal:
        proposal = self._store.get("proposal", proposal_id, session_id, EditProposal)
        if proposal is None:
            raise not_found("Proposal")
        if proposal.status == "dismissed":
            return proposal
        if proposal.status != "pending":
            raise ApiException(ErrorCode.CONFLICT, "This proposal was already applied.")
        updated = proposal.model_copy(update={"status": "dismissed"})
        self._store.put("proposal", proposal_id, session_id, updated)
        return updated

    # ----------------------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------------------

    def _require_cases(self) -> CaseServicePort:
        if self._cases is None:
            raise ApiException(
                ErrorCode.INTERNAL_ERROR, "Investigations are not available on this server."
            )
        return self._cases

    def _require_explore(self) -> ExplorePort:
        if self._explore is None:
            raise ApiException(
                ErrorCode.INTERNAL_ERROR, "Explore experiments are not available on this server."
            )
        return self._explore

    def _evict(self) -> None:
        if len(self._runs) <= MAX_RETAINED_RUNS:
            return
        for run_id in [rid for rid, h in self._runs.items() if not h.running]:
            if len(self._runs) <= MAX_RETAINED_RUNS:
                break
            del self._runs[run_id]


def _cancelled_result() -> LoopResult:
    return LoopResult(
        status="cancelled",
        error=ApiError(code=ErrorCode.CANCELLED, message="The run was cancelled."),
    )


async def _close_quietly(client: ChatClient) -> None:
    with suppress(Exception):
        await client.aclose()


# --------------------------------------------------------------------------------------
# Feature hooks (called by resonance.app)
# --------------------------------------------------------------------------------------


async def startup(app: FastAPI) -> None:
    state = app.state
    cases: CaseServicePort | None = getattr(state, "case_service", None)
    service = AgentService(
        settings=state.settings,
        store=state.store,
        cases=cases,
        explore=AppExplorePort(state),
    )
    state.agent_service = service
    if cases is not None:
        cases.register_run_canceller(service.cancel_and_wait)
        cases.register_run_liveness(service.is_live)


async def shutdown(app: FastAPI) -> None:
    service: AgentService | None = getattr(app.state, "agent_service", None)
    if service is not None:
        await service.shutdown()
