"""Learn mode (owner: audio & signal engineer). STUB bodies — implement them.

Contract (fixed by lead; propose changes before editing signatures):
- GET /api/learn/prompts?revision_id=: deterministic prediction questions for a session
  revision (component x multiplier changes that stay inside the template's supported range).
- POST /api/learn/attempts: record the learner's predictions, create the changed revision,
  measure the base (reusing its latest experiment if present) and the changed revision with
  ngspice, compare, grade each prediction from the MEASURED tone gains, and return a
  deterministic explanation and tradeoff that cite experiment IDs. 201.
- GET /api/learn/attempts/{attempt_id}: a recorded attempt (session-scoped).
Client disconnect cancels the simulations; frozen case revisions are refused (409).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from ..contracts.internal import Simulator
from ..contracts.learn import LearnAttempt, LearnAttemptRequest, LearnPrompt
from ..store.artifacts import ArtifactStore
from ..store.db import DocumentStore
from .deps import get_artifacts, get_session_id, get_simulator, get_store
from .errors import not_implemented
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
    raise not_implemented("Learn prompts")


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
    raise not_implemented("Learn attempts")


@router.get(
    "/learn/attempts/{attempt_id}",
    response_model=LearnAttempt,
    operation_id="getLearnAttempt",
    responses=ERRORS,
)
async def get_learn_attempt(attempt_id: str, session_id: SessionId, store: Store) -> LearnAttempt:
    raise not_implemented("Reading learn attempts")
