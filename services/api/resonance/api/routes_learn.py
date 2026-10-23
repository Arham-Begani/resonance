"""Learn mode (owner: audio & signal engineer). Logic lives in ``resonance.explore.learn``.

Contract (fixed by lead; propose changes before editing signatures):
- GET /api/learn/prompts?revision_id=: deterministic prediction questions for a session
  revision (component x multiplier changes that stay inside the template's supported range).
- POST /api/learn/attempts: record the learner's predictions, create the changed revision,
  measure the base (reusing its latest experiment if present) and the changed revision with
  ngspice, compare, grade each prediction from the MEASURED tone gains, and return a
  deterministic explanation and tradeoff that cite experiment IDs. 201.
- GET /api/learn/attempts/{attempt_id}: a recorded attempt (session-scoped).
Client disconnect cancels the simulations; frozen case revisions are refused (409).

Error choices: unknown or other-session revision/attempt → 404 ``not_found``; a frozen revision
→ 409 ``stale_revision``; a change that leaves the supported range (field
``change.multiplier``) or predictions that do not cover exactly the revision's tones (field
``predictions...``) → 422 ``validation_error``; simulator failures → the mapped 502/503/504,
client disconnect → 409 ``cancelled``. A failed attempt stores nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from ..circuits.revisions import get_revision
from ..contracts.internal import CancelToken, Simulator
from ..contracts.learn import LearnAttempt, LearnAttemptRequest, LearnPrompt
from ..explore import learn
from ..store.artifacts import ArtifactStore
from ..store.db import DocumentStore
from ..store.session_state import load_session_state
from .deps import get_artifacts, get_session_id, get_simulator, get_store
from .routes_experiments import cancel_on_disconnect
from .routes_meta import ERRORS

router = APIRouter(prefix="/api", tags=["learn"])

SessionId = Annotated[str, Depends(get_session_id)]
Store = Annotated[DocumentStore, Depends(get_store)]


@router.get(
    "/learn/prompts",
    response_model=list[LearnPrompt],
    operation_id="listLearnPrompts",
    responses=ERRORS,
)
async def list_learn_prompts(
    revision_id: Annotated[str, Query(description="Session revision to ask about.")],
    session_id: SessionId,
    store: Store,
) -> list[LearnPrompt]:
    return learn.learn_prompts(get_revision(store, session_id, revision_id))


@router.post(
    "/learn/attempts",
    response_model=LearnAttempt,
    status_code=201,
    operation_id="createLearnAttempt",
    responses=ERRORS,
)
async def create_learn_attempt(
    body: LearnAttemptRequest,
    request: Request,
    session_id: SessionId,
    store: Store,
    artifacts: Annotated[ArtifactStore, Depends(get_artifacts)],
    simulator: Annotated[Simulator, Depends(get_simulator)],
) -> LearnAttempt:
    revision = get_revision(store, session_id, body.revision_id)  # 404 if not this session's
    learn.plan_attempt(revision, body)  # validate (409/422) before anything is simulated
    goal = load_session_state(store, session_id).goal
    token = CancelToken()
    watcher = asyncio.create_task(cancel_on_disconnect(request, token))
    try:
        return await learn.run_learn_attempt(
            simulator=simulator,
            store=store,
            artifacts=artifacts,
            session_id=session_id,
            revision=revision,
            body=body,
            goal=goal,
            cancel=token,
        )
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


@router.get(
    "/learn/attempts/{attempt_id}",
    response_model=LearnAttempt,
    operation_id="getLearnAttempt",
    responses=ERRORS,
)
async def get_learn_attempt(attempt_id: str, session_id: SessionId, store: Store) -> LearnAttempt:
    return learn.get_learn_attempt(store, session_id, attempt_id)
