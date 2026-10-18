"""Bounded local artifact directory (owner: lead).

Artifacts are stored as ``<artifact_id>.bin`` with random IDs and no metadata beyond media
type and size, so file names, sizes, and headers reveal nothing about hidden circuits.
Reads are session-scoped. Bounds: each session keeps at most ``max_per_session`` artifacts
(its own oldest are evicted first, so one busy session cannot evict another session's audio);
the global count/size bounds evict the globally oldest only as a last resort.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from ..contracts.common import new_id
from .db import DocumentStore

_ARTIFACT_ID = re.compile(r"^art_[A-Za-z0-9_-]{8,64}$")


class ArtifactStore:
    def __init__(
        self,
        root: Path,
        index: DocumentStore,
        max_count: int,
        max_bytes: int,
        max_per_session: int | None = None,
    ) -> None:
        self._root = root
        self._index = index
        self._max_count = max_count
        self._max_bytes = max_bytes
        self._max_per_session = max_per_session or max_count
        self._lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, artifact_id: str) -> Path:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("invalid artifact id")
        return self._root / f"{artifact_id}.bin"

    def put(self, session_id: str, data: bytes, media_type: str = "audio/wav") -> str:
        artifact_id = new_id("art")
        with self._lock:
            self._evict_session(session_id)
            self._evict(incoming_bytes=len(data))
            path = self._path(artifact_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            self._index.add_artifact(artifact_id, session_id, media_type, len(data))
        return artifact_id

    def open(self, artifact_id: str, session_id: str) -> tuple[Path, str] | None:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            return None
        meta = self._index.get_artifact(artifact_id, session_id)
        if meta is None:
            return None
        path = self._path(artifact_id)
        if not path.is_file():
            return None
        return path, meta[0]

    @staticmethod
    def url_for(artifact_id: str) -> str:
        return f"/api/artifacts/{artifact_id}"

    def _remove(self, artifact_id: str) -> None:
        try:
            self._path(artifact_id).unlink(missing_ok=True)
        finally:
            self._index.remove_artifact(artifact_id)

    def _evict_session(self, session_id: str) -> None:
        excess = self._index.session_artifact_count(session_id) + 1 - self._max_per_session
        if excess > 0:
            for victim in self._index.oldest_session_artifacts(session_id, limit=excess):
                self._remove(victim)

    def _evict(self, incoming_bytes: int) -> None:
        count, total = self._index.artifact_usage()
        while count + 1 > self._max_count or total + incoming_bytes > self._max_bytes:
            victims = self._index.oldest_artifacts(limit=16)
            if not victims:
                break
            for victim in victims:
                self._remove(victim)
            count, total = self._index.artifact_usage()
