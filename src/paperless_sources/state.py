"""Record of what was already sent to Paperless-ngx.

One row per (source, remote_id). This is the equivalent of ProcessedMail in
paperless_mail and would become a Django model in the server.
"""

from __future__ import annotations

import dataclasses
import datetime
import sqlite3
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Self

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import TracebackType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_item (
    source      TEXT NOT NULL,
    remote_id   TEXT NOT NULL,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL,
    checksum    TEXT,
    task_id     TEXT,
    document_id INTEGER,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (source, remote_id)
)
"""


class Status(StrEnum):
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"


@dataclasses.dataclass(slots=True)
class ProcessedItem:
    source: str
    remote_id: str
    filename: str
    status: Status
    checksum: str | None = None
    task_id: str | None = None
    document_id: int | None = None
    attempts: int = 0
    error: str | None = None


class StateError(Exception):
    pass


class StateStore:
    def __init__(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            self._db.execute(_SCHEMA)
        except (OSError, sqlite3.Error) as e:
            raise StateError(f"Cannot open {path.resolve()}: {e}") from e

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def get(self, source: str, remote_id: str) -> ProcessedItem | None:
        row = self._db.execute(
            "SELECT * FROM processed_item WHERE source = ? AND remote_id = ?",
            (source, remote_id),
        ).fetchone()
        return _to_item(row) if row else None

    def queued(self, source: str) -> Iterator[ProcessedItem]:
        rows = self._db.execute(
            "SELECT * FROM processed_item WHERE source = ? AND status = ?",
            (source, Status.QUEUED),
        ).fetchall()
        return (_to_item(r) for r in rows)

    def save(self, item: ProcessedItem) -> None:
        # Autocommit: each item is durable on its own, so a crash loses at most one
        self._db.execute(
            """
            INSERT INTO processed_item (
                source, remote_id, filename, status, checksum, task_id,
                document_id, attempts, error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, remote_id) DO UPDATE SET
                filename = excluded.filename,
                status = excluded.status,
                checksum = excluded.checksum,
                task_id = excluded.task_id,
                document_id = excluded.document_id,
                attempts = excluded.attempts,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                item.source,
                item.remote_id,
                item.filename,
                item.status,
                item.checksum,
                item.task_id,
                item.document_id,
                item.attempts,
                item.error,
                datetime.datetime.now(datetime.UTC).isoformat(),
            ),
        )


def _to_item(row: sqlite3.Row) -> ProcessedItem:
    return ProcessedItem(
        source=row["source"],
        remote_id=row["remote_id"],
        filename=row["filename"],
        status=Status(row["status"]),
        checksum=row["checksum"],
        task_id=row["task_id"],
        document_id=row["document_id"],
        attempts=row["attempts"],
        error=row["error"],
    )
