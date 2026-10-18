"""Private SQLite store for hidden case state (evaluator boundary).

A separate database file (``Settings.evaluator_db_path``) from the public DocumentStore. Only
``resonance.challenges.evaluator`` opens it. Rows hold the seed, generator version, fault,
nominal and actual values, and whether the case has been closed (which permits the reveal).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ...contracts.circuits import TemplateId
from ...contracts.investigation import HypothesisId
from .faults import FaultSpec


@dataclass(frozen=True)
class HiddenCase:
    case_id: str
    seed: str
    generator_version: str
    template_id: TemplateId
    nominal: dict[str, float]
    actual: dict[str, float]
    fault: FaultSpec
    closed: bool


class EvaluatorStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hidden_cases (
                case_id TEXT PRIMARY KEY,
                seed TEXT NOT NULL,
                generator_version TEXT NOT NULL,
                template_id TEXT NOT NULL,
                nominal TEXT NOT NULL,
                actual TEXT NOT NULL,
                fault TEXT NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )

    def insert(self, case: HiddenCase) -> None:
        fault = {
            "hypothesis_id": case.fault.hypothesis_id,
            "component_id": case.fault.component_id,
            "multiplier": case.fault.multiplier,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO hidden_cases (case_id, seed, generator_version, template_id, "
                "nominal, actual, fault, closed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    case.case_id,
                    case.seed,
                    case.generator_version,
                    case.template_id,
                    json.dumps(case.nominal),
                    json.dumps(case.actual),
                    json.dumps(fault),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get(self, case_id: str) -> HiddenCase | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT case_id, seed, generator_version, template_id, nominal, actual, fault, "
                "closed FROM hidden_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        fault = json.loads(row[6])
        return HiddenCase(
            case_id=row[0],
            seed=row[1],
            generator_version=row[2],
            template_id=cast(TemplateId, row[3]),
            nominal={k: float(v) for k, v in json.loads(row[4]).items()},
            actual={k: float(v) for k, v in json.loads(row[5]).items()},
            fault=FaultSpec(
                hypothesis_id=cast(HypothesisId, fault["hypothesis_id"]),
                component_id=fault["component_id"],
                multiplier=fault["multiplier"],
            ),
            closed=bool(row[7]),
        )

    def mark_closed(self, case_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE hidden_cases SET closed = 1, closed_at = ? WHERE case_id = ?",
                (datetime.now(UTC).isoformat(), case_id),
            )

    def delete(self, case_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM hidden_cases WHERE case_id = ?", (case_id,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
