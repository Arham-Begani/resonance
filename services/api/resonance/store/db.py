"""SQLite document store for anonymous session state and experiment metadata (owner: lead).

Every read is scoped by session_id: a document owned by another session behaves exactly like a
missing document (callers return 404). Hidden case state does NOT live here; the evaluator
keeps its own database file.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel

DocKind = Literal[
    "session",
    "revision",
    "experiment",
    "proposal",
    "goal",
    "case",
    "agent_run",
    "learn_attempt",
]

M = TypeVar("M", bound=BaseModel)


def utcnow() -> datetime:
    return datetime.now(UTC)


class DocumentStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS docs (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (kind, id)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS docs_session ON docs(session_id, kind, created_at)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def put(self, kind: DocKind, doc_id: str, session_id: str, model: BaseModel) -> None:
        now = utcnow().isoformat()
        data = model.model_dump_json()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO docs (kind, id, session_id, created_at, updated_at, data)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, id) DO UPDATE SET data = excluded.data,
                    updated_at = excluded.updated_at
                WHERE docs.session_id = excluded.session_id
                """,
                (kind, doc_id, session_id, now, now, data),
            )

    def get(self, kind: DocKind, doc_id: str, session_id: str, model: type[M]) -> M | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM docs WHERE kind = ? AND id = ? AND session_id = ?",
                (kind, doc_id, session_id),
            ).fetchone()
        return model.model_validate_json(row[0]) if row else None

    def list_docs(
        self,
        kind: DocKind,
        session_id: str,
        model: type[M],
        *,
        limit: int = 200,
        newest_first: bool = False,
    ) -> list[M]:
        order = "DESC" if newest_first else "ASC"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM docs WHERE kind = ? AND session_id = ? "  # noqa: S608
                f"ORDER BY created_at {order}, rowid {order} LIMIT ?",
                (kind, session_id, limit),
            ).fetchall()
        return [model.model_validate_json(r[0]) for r in rows]

    def delete(self, kind: DocKind, doc_id: str, session_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM docs WHERE kind = ? AND id = ? AND session_id = ?",
                (kind, doc_id, session_id),
            )

    # --- artifact index (used by ArtifactStore) ---

    def add_artifact(self, artifact_id: str, session_id: str, media_type: str, size: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO artifacts (id, session_id, media_type, size, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (artifact_id, session_id, media_type, size, utcnow().isoformat()),
            )

    def get_artifact(self, artifact_id: str, session_id: str) -> tuple[str, int] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT media_type, size FROM artifacts WHERE id = ? AND session_id = ?",
                (artifact_id, session_id),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def artifact_usage(self) -> tuple[int, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM artifacts"
            ).fetchone()
        return int(row[0]), int(row[1])

    def session_artifact_count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row[0])

    def oldest_session_artifacts(self, session_id: str, limit: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM artifacts WHERE session_id = ? "
                "ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def oldest_artifacts(self, limit: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM artifacts ORDER BY created_at ASC, rowid ASC LIMIT ?", (limit,)
            ).fetchall()
        return [r[0] for r in rows]

    def remove_artifact(self, artifact_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
