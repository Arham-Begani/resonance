"""Explore experiments (owner: audio & signal engineer).

Contract (fixed by lead; propose changes before editing signatures):
- POST /api/experiments runs the explore bundle on a session revision: ngspice AC sweep
  (gain, phase, source current), exact-frequency AC at the demo tones, two-tone transient,
  uniform resampling, tone fits, spectrum, waveform window, Input/Output audio comparison with
  the shared gain, consistency checks, and optional goal evaluation. Returns 201 with the
  record. Simulator failures return a structured error; the previous results remain valid.
  If the client disconnects, the simulation is cancelled.
- GET /api/experiments/{id} returns a recorded experiment.
- GET /api/experiments/compare?a=&b= compares two recorded experiments deterministically and
  renders previous-vs-current output clips with the shared gain.

Error choices:
- Unknown revision (or another session's) → 404 ``not_found``.
- A frozen revision (the intended design of an open case) → 409 ``stale_revision`` (enforced
  in ``explore.service`` for every caller): explore never measures a case design.
- ``goal_id`` that is not the session's CURRENT goal → 409 ``conflict`` naming ``goal_id``
  (goals are replaced, not versioned, so a mismatching ID is a stale goal; reload the
  workbench). Omit ``goal_id`` to skip goal evaluation.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from ..circuits.revisions import get_revision, revision_to_circuit
from ..contracts.common import ErrorCode
from ..contracts.experiments import ExperimentComparison, ExperimentRecord, ExperimentRunRequest
from ..contracts.internal import CancelToken, Simulator
from ..explore import service
from ..store.artifacts import ArtifactStore
from ..store.db import DocumentStore
from ..store.session_state import load_session_state
from .deps import get_artifacts, get_session_id, get_simulator, get_store
from .errors import ApiException
from .routes_meta import ERRORS

router = APIRouter(prefix="/api", tags=["experiments"])

DISCONNECT_POLL_S = 0.25


async def cancel_on_disconnect(request: Request, token: CancelToken) -> None:
    """Trip ``token`` when the client goes away so ngspice stops early (shared with Learn)."""
    while not token.cancelled:
        if await request.is_disconnected():
            token.cancel()
            return
        await asyncio.sleep(DISCONNECT_POLL_S)


@router.post(
    "/experiments",
    response_model=ExperimentRecord,
    status_code=201,
    operation_id="runExperiment",
    responses=ERRORS,
)
async def run_experiment(
    body: ExperimentRunRequest,
    request: Request,
    session_id: Annotated[str, Depends(get_session_id)],
    store: Annotated[DocumentStore, Depends(get_store)],
    artifacts: Annotated[ArtifactStore, Depends(get_artifacts)],
    simulator: Annotated[Simulator, Depends(get_simulator)],
) -> ExperimentRecord:
    revision = get_revision(store, session_id, body.revision_id)  # 404 if not this session's
    goal = None
    if body.goal_id is not None:
        state = load_session_state(store, session_id)
        if state.goal.id != body.goal_id:
            raise ApiException(
                ErrorCode.CONFLICT,
                "That goal is no longer the current goal. Reload the workbench and try again.",
                field="goal_id",
            )
        goal = state.goal
    token = CancelToken()
    watcher = asyncio.create_task(cancel_on_disconnect(request, token))
    try:
        return await service.run_explore_experiment(
            simulator=simulator,
            store=store,
            artifacts=artifacts,
            session_id=session_id,
            revision=revision,
            circuit=revision_to_circuit(revision),
            goal=goal,
            cancel=token,
        )
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


@router.get(
    "/experiments/compare",
    response_model=ExperimentComparison,
    operation_id="compareExperiments",
    responses=ERRORS,
)
async def compare_experiments(
    a: Annotated[str, Query(description="Earlier experiment (A).")],
    b: Annotated[str, Query(description="Later experiment (B).")],
    session_id: Annotated[str, Depends(get_session_id)],
    store: Annotated[DocumentStore, Depends(get_store)],
    artifacts: Annotated[ArtifactStore, Depends(get_artifacts)],
) -> ExperimentComparison:
    record_a = service.get_experiment(store, session_id, a)
    record_b = service.get_experiment(store, session_id, b)
    rev_a = get_revision(store, session_id, record_a.revision_id)
    rev_b = get_revision(store, session_id, record_b.revision_id)
    return await asyncio.to_thread(
        service.compare_experiments,
        artifacts=artifacts,
        session_id=session_id,
        a=record_a,
        b=record_b,
        rev_a=rev_a,
        rev_b=rev_b,
    )


@router.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentRecord,
    operation_id="getExperiment",
    responses=ERRORS,
)
async def get_experiment(
    experiment_id: str,
    session_id: Annotated[str, Depends(get_session_id)],
    store: Annotated[DocumentStore, Depends(get_store)],
) -> ExperimentRecord:
    return service.get_experiment(store, session_id, experiment_id)
