"""Paperless-ngx REST client: the only part that a server-side version replaces.

Inside Paperless-ngx, upload() becomes consume_file with a ConsumableDocument,
find_by_checksum() a Document query, and task() goes away because the
consume task result is known directly.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING
from typing import Any

import httpx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# v9 is the oldest version Paperless-ngx 3.x accepts, and 2.x speaks it too
API_VERSION = 9


class PaperlessError(Exception):
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class UploadOptions:
    """Fields of post_document, already resolved to IDs."""

    correspondent: int | None = None
    document_type: int | None = None
    storage_path: int | None = None
    tags: tuple[int, ...] = ()
    custom_fields: tuple[int, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class TaskResult:
    done: bool
    success: bool
    document_id: int | None = None
    duplicate: bool = False
    message: str | None = None


class PaperlessClient:
    def __init__(self, url: str, token: str, *, timeout: float = 60.0) -> None:
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={
                "Authorization": f"Token {token}",
                "Accept": f"application/json; version={API_VERSION}",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def find_by_checksum(self, checksum: str) -> int | None:
        """Return the ID of a document whose original has this SHA-256.

        NOTE: only sees documents the token's user can view, and not the trash
        """
        body = self._get(
            "/api/documents/",
            params={"checksum__iexact": checksum, "page_size": 1},
        )
        results = body.get("results") or []
        return int(results[0]["id"]) if results else None

    def resolve(self, endpoint: str, value: int | str) -> int:
        """Return the ID of a tag, correspondent and so on, given its ID or name."""
        if isinstance(value, int):
            return value
        body = self._get(f"/api/{endpoint}/", params={"name__iexact": value})
        results = body.get("results") or []
        if len(results) != 1:
            raise PaperlessError(
                f"{endpoint}: expected one match for {value!r}, got {len(results)}",
            )
        return int(results[0]["id"])

    def upload(self, path: Path, filename: str, options: UploadOptions) -> str:
        """Queue the file for consumption and return the Celery task ID."""
        data: dict[str, Any] = {}
        for key in ("correspondent", "document_type", "storage_path"):
            if (value := getattr(options, key)) is not None:
                data[key] = str(value)
        if options.tags:
            data["tags"] = [str(t) for t in options.tags]
        if options.custom_fields:
            data["custom_fields"] = [str(f) for f in options.custom_fields]

        with path.open("rb") as f:
            response = self._http.post(
                "/api/documents/post_document/",
                data=data,
                files={"document": (filename, f)},
            )
        _check(response)
        task_id = response.json()
        if not isinstance(task_id, str):
            raise PaperlessError(f"post_document: unexpected answer {task_id!r}")
        return task_id

    def task(self, task_id: str) -> TaskResult | None:
        """Return the consume result, or None if Paperless does not know the task."""
        tasks = self._get("/api/tasks/", params={"task_id": task_id})
        if not tasks:
            return None
        task = tasks[0]
        status = task.get("status")
        message = task.get("result")
        document_id = task.get("related_document")
        document_id = int(document_id) if document_id is not None else None

        if status in {"PENDING", "STARTED", "RETRY", "RECEIVED"}:
            return TaskResult(done=False, success=False)
        if status == "SUCCESS":
            return TaskResult(
                done=True,
                success=True,
                document_id=document_id,
                message=message,
            )
        # Paperless reports a rejected duplicate as a failure. For us the file is in
        if task.get("duplicate_documents") or "duplicate" in (message or "").lower():
            return TaskResult(
                done=True,
                success=True,
                document_id=document_id,
                duplicate=True,
                message=message,
            )
        return TaskResult(done=True, success=False, message=message)

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        response = self._http.get(url, params=params)
        _check(response)
        return response.json()


def _check(response: httpx.Response) -> None:
    if response.is_error:
        raise PaperlessError(
            f"{response.request.method} {response.request.url.path}: "
            f"HTTP {response.status_code} {response.text[:200]}",
        )
