"""Per-session workbench state persisted in the document store (owner: lead)."""

from __future__ import annotations

from pydantic import BaseModel

from ..contracts.common import CaseId, RevisionId
from ..contracts.experiments import Goal
from ..explore import goals
from .db import DocumentStore


class SessionState(BaseModel):
    goal: Goal
    active_case_id: CaseId | None = None
    current_revision_id: RevisionId | None = None


def load_session_state(store: DocumentStore, session_id: str) -> SessionState:
    state = store.get("session", session_id, session_id, SessionState)
    if state is None:
        state = SessionState(goal=goals.default_goal())
        store.put("session", session_id, session_id, state)
    return state


def save_session_state(store: DocumentStore, session_id: str, state: SessionState) -> None:
    store.put("session", session_id, session_id, state)
