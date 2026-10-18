"""Lead-owned infrastructure: session-scoped documents and the bounded artifact store."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from resonance.store.artifacts import ArtifactStore
from resonance.store.db import DocumentStore


class Doc(BaseModel):
    value: int


def test_documents_are_session_scoped(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path / "db.sqlite3")
    store.put("revision", "rev_aaaaaaaaaaaa", "session-a", Doc(value=1))

    assert store.get("revision", "rev_aaaaaaaaaaaa", "session-a", Doc) == Doc(value=1)
    # Another session sees exactly what a missing document looks like.
    assert store.get("revision", "rev_aaaaaaaaaaaa", "session-b", Doc) is None
    assert store.list_docs("revision", "session-b", Doc) == []


def test_put_cannot_overwrite_another_sessions_document(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path / "db.sqlite3")
    store.put("case", "case_aaaaaaaaaaaa", "session-a", Doc(value=1))
    store.put("case", "case_aaaaaaaaaaaa", "session-b", Doc(value=666))

    assert store.get("case", "case_aaaaaaaaaaaa", "session-a", Doc) == Doc(value=1)
    assert store.get("case", "case_aaaaaaaaaaaa", "session-b", Doc) is None


def test_list_preserves_insertion_order(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path / "db.sqlite3")
    for i in range(5):
        store.put("experiment", f"exp_{i:012d}", "s", Doc(value=i))
    assert [d.value for d in store.list_docs("experiment", "s", Doc)] == [0, 1, 2, 3, 4]
    newest = store.list_docs("experiment", "s", Doc, limit=2, newest_first=True)
    assert [d.value for d in newest] == [4, 3]


def test_artifacts_are_random_session_scoped_and_reject_bad_ids(tmp_path: Path) -> None:
    index = DocumentStore(tmp_path / "db.sqlite3")
    artifacts = ArtifactStore(tmp_path / "art", index, max_count=10, max_bytes=10_000)
    a = artifacts.put("session-a", b"RIFFdata")
    b = artifacts.put("session-a", b"RIFFdata")

    assert a != b and a.startswith("art_") and b.startswith("art_")
    opened = artifacts.open(a, "session-a")
    assert opened is not None and opened[0].read_bytes() == b"RIFFdata"
    assert artifacts.open(a, "session-b") is None
    for bad in ("../db.sqlite3", "art_../../x", "art_" + "a" * 70, "rev_aaaaaaaaaaaa", ""):
        assert artifacts.open(bad, "session-a") is None
    # File names carry nothing but the opaque ID.
    assert sorted(p.name for p in (tmp_path / "art").iterdir()) == sorted([f"{a}.bin", f"{b}.bin"])


def test_artifact_store_evicts_oldest_within_bounds(tmp_path: Path) -> None:
    index = DocumentStore(tmp_path / "db.sqlite3")
    artifacts = ArtifactStore(tmp_path / "art", index, max_count=3, max_bytes=250)
    ids = [artifacts.put("s", bytes(100)) for _ in range(4)]

    count, total = index.artifact_usage()
    assert count <= 3 and total <= 250
    assert artifacts.open(ids[0], "s") is None  # oldest evicted
    assert artifacts.open(ids[-1], "s") is not None
    assert len(list((tmp_path / "art").iterdir())) == count


def test_one_session_cannot_evict_another_sessions_artifacts(tmp_path: Path) -> None:
    index = DocumentStore(tmp_path / "db.sqlite3")
    artifacts = ArtifactStore(
        tmp_path / "art", index, max_count=100, max_bytes=100_000, max_per_session=3
    )
    quiet = artifacts.put("quiet-session", b"open case clip")
    busy = [artifacts.put("busy-session", bytes(10)) for _ in range(10)]

    assert artifacts.open(quiet, "quiet-session") is not None
    assert index.session_artifact_count("busy-session") == 3
    assert [artifacts.open(i, "busy-session") is not None for i in busy] == [False] * 7 + [True] * 3
