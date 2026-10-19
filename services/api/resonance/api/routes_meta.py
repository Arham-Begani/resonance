"""Health, templates, unit parsing, workbench session state, artifacts (owner: lead)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from .. import __version__
from ..agent.status import ai_status
from ..circuits import templates as circuit_templates
from ..circuits import units
from ..config import Settings
from ..contracts.agent import (
    CurrentRevisionRequest,
    EditProposal,
    GoalResponse,
    HealthResponse,
    SimulatorStatus,
    WorkbenchState,
)
from ..contracts.circuits import (
    CircuitTemplate,
    Revision,
    TemplateId,
    ValueParseRequest,
    ValueParseResult,
)
from ..contracts.common import ErrorCode, ErrorResponse
from ..contracts.experiments import (
    DesignSuggestion,
    DesignSuggestionRequest,
    ExperimentRecord,
    ExperimentSummary,
    GoalInput,
)
from ..contracts.internal import Simulator
from ..explore import goals, suggest
from ..store.artifacts import ArtifactStore
from ..store.db import DocumentStore
from ..store.session_state import load_session_state, save_session_state
from .deps import get_artifacts, get_session_id, get_settings_dep, get_simulator, get_store
from .errors import ApiException, not_found

router = APIRouter(prefix="/api", tags=["meta"])

ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
}

SessionId = Annotated[str, Depends(get_session_id)]
Store = Annotated[DocumentStore, Depends(get_store)]


@router.get("/health", response_model=HealthResponse, operation_id="getHealth")
async def get_health(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    simulator: Annotated[Simulator, Depends(get_simulator)],
) -> HealthResponse:
    try:
        info = simulator.info()
        sim = SimulatorStatus(
            available=True, version=info.version, message=f"ngspice {info.version} is ready."
        )
    except Exception:  # noqa: BLE001 - report unavailability, never the raw error
        sim = SimulatorStatus(
            available=False,
            version=None,
            message="ngspice was not found. Run scripts/install-ngspice.py or set NGSPICE_BIN.",
        )
    ai = ai_status(settings)
    status = "ok" if sim.available and ai.available else "degraded"
    return HealthResponse(status=status, version=__version__, simulator=sim, ai=ai)


@router.get("/templates", response_model=list[CircuitTemplate], operation_id="listTemplates")
async def list_templates() -> list[CircuitTemplate]:
    return circuit_templates.list_templates()


@router.post(
    "/units/parse", response_model=ValueParseResult, operation_id="parseValue", responses=ERRORS
)
async def parse_value(body: ValueParseRequest) -> ValueParseResult:
    return units.parse_value_request(body)


@router.get("/workbench", response_model=WorkbenchState, operation_id="getWorkbench")
async def get_workbench(session_id: SessionId, store: Store) -> WorkbenchState:
    state = load_session_state(store, session_id)
    revisions = store.list_docs("revision", session_id, Revision, limit=100, newest_first=True)
    revisions.reverse()
    records = store.list_docs(
        "experiment", session_id, ExperimentRecord, limit=100, newest_first=True
    )
    records.reverse()
    summaries = [
        ExperimentSummary(
            id=r.id,
            revision_id=r.revision_id,
            status=r.status,
            created_at=r.created_at,
            goal_passed=r.goal.passed if r.goal else None,
        )
        for r in records
    ]
    proposals = [
        p
        for p in store.list_docs("proposal", session_id, EditProposal, limit=50)
        if p.status == "pending"
    ]
    known = {r.id: r for r in revisions}
    current = state.current_revision_id if state.current_revision_id in known else None
    template_id = _feasibility_template(known.get(current or ""), revisions)
    return WorkbenchState(
        revisions=revisions,
        current_revision_id=current,
        experiments=summaries,
        goal=state.goal,
        feasibility=goals.analyze_feasibility(state.goal, template_id),
        proposals=proposals,
        active_case_id=state.active_case_id,
    )


@router.put(
    "/workbench/current-revision",
    response_model=WorkbenchState,
    operation_id="setCurrentRevision",
    responses=ERRORS,
)
async def set_current_revision(
    body: CurrentRevisionRequest, session_id: SessionId, store: Store
) -> WorkbenchState:
    """Point the workbench at one of this session's revisions (after an edit, or undo)."""
    revision = store.get("revision", body.revision_id, session_id, Revision)
    if revision is None:
        raise not_found("Revision")
    if revision.frozen:
        raise ApiException(
            ErrorCode.STALE_REVISION,
            "That design belongs to an investigation and can't be edited on the workbench.",
            field="revision_id",
        )
    state = load_session_state(store, session_id)
    state.current_revision_id = revision.id
    save_session_state(store, session_id, state)
    return await get_workbench(session_id, store)


@router.put(
    "/workbench/goal", response_model=GoalResponse, operation_id="setGoal", responses=ERRORS
)
async def set_goal(body: GoalInput, session_id: SessionId, store: Store) -> GoalResponse:
    goal = goals.goal_from_input(body, origin="user")
    state = load_session_state(store, session_id)
    state.goal = goal
    save_session_state(store, session_id, state)
    current = (
        store.get("revision", state.current_revision_id, session_id, Revision)
        if state.current_revision_id
        else None
    )
    template_id = _feasibility_template(current, [])
    return GoalResponse(goal=goal, feasibility=goals.analyze_feasibility(goal, template_id))


@router.post(
    "/workbench/goal/suggest",
    response_model=DesignSuggestion,
    operation_id="suggestDesign",
    responses=ERRORS,
)
async def suggest_design(
    body: DesignSuggestionRequest, session_id: SessionId, store: Store
) -> DesignSuggestion:
    """Goal to standard part values for the given revision (analytical preview; apply it as a
    revision and run an experiment to measure it)."""
    revision = store.get("revision", body.revision_id, session_id, Revision)
    if revision is None:
        raise not_found("Revision")
    state = load_session_state(store, session_id)
    return suggest.suggest_design(revision, state.goal, vary=body.vary)


def _feasibility_template(current: Revision | None, revisions: list[Revision]) -> TemplateId:
    """Feasibility is analysed for the circuit family on the workbench right now."""
    if current is not None:
        return current.template_id
    return revisions[-1].template_id if revisions else "rc_lowpass"


@router.get(
    "/artifacts/{artifact_id}",
    response_class=FileResponse,
    operation_id="getArtifact",
    responses={200: {"content": {"audio/wav": {}}}, 404: {"model": ErrorResponse}},
)
async def get_artifact(
    artifact_id: str,
    session_id: SessionId,
    artifacts: Annotated[ArtifactStore, Depends(get_artifacts)],
) -> FileResponse:
    opened = artifacts.open(artifact_id, session_id)
    if opened is None:
        raise not_found("Audio clip")
    path, media_type = opened
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600", "Content-Disposition": "inline"},
    )
