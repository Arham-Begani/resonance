"""Circuit revisions (owner: circuit & simulation engineer).

Contract (fixed by lead; propose changes before editing signatures):
- POST /api/revisions creates an immutable revision from a template default or a parent.
  Values are parsed with explicit SI units and validated against template bounds; errors are
  validation_error (422) naming ``values.<i>.value``. Frozen (case) revisions cannot be parents
  (stale_revision, 409). Revisions are session-scoped (others' IDs → 404).
- GET /api/revisions/{revision_id} returns a revision owned by this session.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ..circuits import revisions
from ..contracts.circuits import Revision, RevisionCreateRequest
from ..store.db import DocumentStore
from .deps import get_session_id, get_store
from .routes_meta import ERRORS

router = APIRouter(prefix="/api", tags=["revisions"])


@router.post(
    "/revisions",
    response_model=Revision,
    status_code=201,
    operation_id="createRevision",
    responses=ERRORS,
)
async def create_revision(
    body: RevisionCreateRequest,
    session_id: Annotated[str, Depends(get_session_id)],
    store: Annotated[DocumentStore, Depends(get_store)],
) -> Revision:
    return revisions.create_revision(store, session_id, body)


@router.get(
    "/revisions/{revision_id}",
    response_model=Revision,
    operation_id="getRevision",
    responses=ERRORS,
)
async def get_revision(
    revision_id: str,
    session_id: Annotated[str, Depends(get_session_id)],
    store: Annotated[DocumentStore, Depends(get_store)],
) -> Revision:
    return revisions.get_revision(store, session_id, revision_id)
