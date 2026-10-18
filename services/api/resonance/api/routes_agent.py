"""Agent runs and explore proposals (owner: investigation & AI engineer).

Contract (fixed by lead; propose changes before editing signatures):
- POST /api/cases/{id}/agent-runs {kind: investigate|hint}: start a bounded Nemotron run with
  the INVESTIGATE tool set (or the read-only HINT set). One active run per case.
- POST /api/explore/agent-runs: start a run with the EXPLORE tool set for a session revision.
- GET /api/agent-runs/{id}: run status, limits, measured usage.
- GET /api/agent-runs/{id}/events: SSE stream (event = type, id = seq, data = AgentEvent JSON);
  replays from Last-Event-ID; ends after run_finished.
- GET /api/agent-runs/{id}/events/log: the same events as JSON (reconnect/polling fallback).
- POST /api/agent-runs/{id}/cancel: cooperative cancellation of inference and simulation.
- POST /api/proposals/{id}/dismiss: dismiss a pending explore proposal.
Missing credentials → ai_unavailable (503) with setup instructions; never a scripted reply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from ..agent.service import AgentService, RunHandle
from ..contracts.agent import (
    AgentEventLog,
    AgentRun,
    CaseAgentRunRequest,
    EditProposal,
    ExploreRunRequest,
)
from ..contracts.common import ErrorCode
from .deps import get_session_id
from .errors import ApiException
from .routes_meta import ERRORS

router = APIRouter(prefix="/api", tags=["agent"])

SessionId = Annotated[str, Depends(get_session_id)]

# Browsers reconnect after this delay (ms) if the stream drops before run_finished.
SSE_RETRY_MS = 2000


def get_agent_service(request: Request) -> AgentService:
    service: AgentService | None = getattr(request.app.state, "agent_service", None)
    if service is None:
        raise ApiException(ErrorCode.INTERNAL_ERROR, "The AI lab partner is not set up.")
    return service


Service = Annotated[AgentService, Depends(get_agent_service)]


def _stream_handle(run_id: str, session_id: SessionId, service: Service) -> RunHandle:
    """Resolved before the stream starts, so an unknown run is a normal 404 JSON error."""
    return service.handle_for(session_id, run_id)


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id", "").strip()
    return int(raw) if raw.isdigit() else 0


@router.post(
    "/cases/{case_id}/agent-runs",
    response_model=AgentRun,
    status_code=201,
    operation_id="startCaseAgentRun",
    responses=ERRORS,
)
async def start_case_agent_run(
    case_id: str, body: CaseAgentRunRequest, session_id: SessionId, service: Service
) -> AgentRun:
    return await service.start_case_run(session_id, case_id, body)


@router.post(
    "/explore/agent-runs",
    response_model=AgentRun,
    status_code=201,
    operation_id="startExploreAgentRun",
    responses=ERRORS,
)
async def start_explore_agent_run(
    body: ExploreRunRequest, session_id: SessionId, service: Service
) -> AgentRun:
    return await service.start_explore_run(session_id, body)


@router.get(
    "/agent-runs/{run_id}", response_model=AgentRun, operation_id="getAgentRun", responses=ERRORS
)
async def get_agent_run(run_id: str, session_id: SessionId, service: Service) -> AgentRun:
    return service.get_run(session_id, run_id)


@router.get(
    "/agent-runs/{run_id}/events/log",
    response_model=AgentEventLog,
    operation_id="getAgentRunEventLog",
    responses=ERRORS,
)
async def get_agent_run_event_log(
    run_id: str, session_id: SessionId, service: Service
) -> AgentEventLog:
    return service.event_log(session_id, run_id)


@router.get(
    "/agent-runs/{run_id}/events",
    response_class=EventSourceResponse,
    operation_id="streamAgentRunEvents",
    responses={
        200: {"content": {"text/event-stream": {}}},
        # Unknown runs fail before streaming starts, as a normal JSON error.
        404: {
            "description": "Not Found",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
            },
        },
    },
)
async def stream_agent_run_events(
    request: Request, handle: Annotated[RunHandle, Depends(_stream_handle)]
) -> AsyncIterator[ServerSentEvent]:
    # FastAPI inserts ": ping" keep-alive comments whenever the stream is idle.
    first = True
    async for event in handle.follow(after=_last_event_id(request)):
        yield ServerSentEvent(
            event=event.type,
            id=str(event.seq),
            raw_data=event.model_dump_json(),
            retry=SSE_RETRY_MS if first else None,
        )
        first = False


@router.post(
    "/agent-runs/{run_id}/cancel",
    response_model=AgentRun,
    operation_id="cancelAgentRun",
    responses=ERRORS,
)
async def cancel_agent_run(run_id: str, session_id: SessionId, service: Service) -> AgentRun:
    return await service.cancel(session_id, run_id)


@router.post(
    "/proposals/{proposal_id}/dismiss",
    response_model=EditProposal,
    operation_id="dismissProposal",
    responses=ERRORS,
)
async def dismiss_proposal(
    proposal_id: str, session_id: SessionId, service: Service
) -> EditProposal:
    return service.dismiss_proposal(session_id, proposal_id)
