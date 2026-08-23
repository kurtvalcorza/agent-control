from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any

from .events import Event, EventType


class EventStoreError(RuntimeError):
    pass


class SQLiteEventStore:
    """Append-only event store with optimistic per-run sequencing."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                actor TEXT NOT NULL,
                causation_id TEXT,
                correlation_id TEXT,
                payload TEXT NOT NULL,
                UNIQUE(run_id, sequence)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events(run_id, sequence)"
        )

    def append(self, event: Event, *, expected_sequence: int | None = None) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS seq FROM events WHERE run_id = ?",
                    (event.run_id,),
                ).fetchone()
                current = int(row["seq"])
                if expected_sequence is not None and current != expected_sequence:
                    raise EventStoreError(
                        f"concurrent modification for {event.run_id}: "
                        f"expected {expected_sequence}, found {current}"
                    )
                if event.sequence != current + 1:
                    raise EventStoreError(
                        f"event sequence must be {current + 1}, got {event.sequence}"
                    )
                self._conn.execute(
                    """
                    INSERT INTO events
                    (id, run_id, sequence, type, occurred_at, schema_version, actor,
                     causation_id, correlation_id, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.run_id,
                        event.sequence,
                        event.type.value,
                        event.occurred_at,
                        event.schema_version,
                        event.actor,
                        event.causation_id,
                        event.correlation_id,
                        json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def load(self, run_id: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence", (run_id,)
        ).fetchall()
        return [
            Event(
                id=row["id"],
                run_id=row["run_id"],
                sequence=row["sequence"],
                type=EventType(row["type"]),
                occurred_at=row["occurred_at"],
                schema_version=str(row["schema_version"]),
                actor=row["actor"],
                causation_id=row["causation_id"],
                correlation_id=row["correlation_id"],
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]

    def list_run_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT run_id, MIN(rowid) AS first_row FROM events GROUP BY run_id ORDER BY first_row"
        ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def raw_events(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM events ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._conn.close()
