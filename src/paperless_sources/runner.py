"""One sync cycle for one source.

The server-side version would be a Celery task with the same steps, run by
beat per source, with a Django model in place of StateStore.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from paperless_sources.paperless import UploadOptions
from paperless_sources.providers import ProviderError
from paperless_sources.state import ProcessedItem
from paperless_sources.state import Status

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from paperless_sources.config import SourceConfig
    from paperless_sources.config import UploadSpec
    from paperless_sources.paperless import PaperlessClient
    from paperless_sources.providers import RemoteDocument
    from paperless_sources.providers import SourceProvider
    from paperless_sources.state import StateStore

logger = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class CycleStats:
    uploaded: int = 0
    already_in_paperless: int = 0
    completed: int = 0
    failed: int = 0


class SourceRunner:
    def __init__(
        self,
        source: SourceConfig,
        provider_class: type[SourceProvider],
        paperless: PaperlessClient,
        state: StateStore,
        http: httpx.Client,
        *,
        max_attempts: int,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._source = source
        self._provider_class = provider_class
        self._paperless = paperless
        self._state = state
        self._http = http
        self._max_attempts = max_attempts
        self._should_stop = should_stop

    @property
    def name(self) -> str:
        return self._source.name

    def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        self._check_queued(stats)
        # Resolved every cycle, so a tag created after start-up is picked up
        options = _resolve(self._paperless, self._source.upload)

        provider = self._provider_class(self._source.config, self._http)
        with provider:
            for document in provider.list_documents():
                if self._should_stop():
                    break
                if not self._wants(document):
                    continue
                self._process(provider, document, options, stats)

        logger.info(
            "%s: %d uploaded, %d already in paperless, %d completed, %d failed",
            self._source.name,
            stats.uploaded,
            stats.already_in_paperless,
            stats.completed,
            stats.failed,
        )
        return stats

    def list_only(self) -> int:
        """Log what the next cycle would import. No download, no Paperless call."""
        count = 0
        provider = self._provider_class(self._source.config, self._http)
        with provider:
            for document in provider.list_documents():
                if self._should_stop():
                    break
                count += 1
                logger.info(
                    "%s: %-12s %s  [id %s, modified %s]",
                    self._source.name,
                    self._plan(document),
                    "/".join((*document.path, document.filename)),
                    document.remote_id,
                    document.modified.isoformat() if document.modified else "?",
                )
        logger.info("%s: %d documents listed", self._source.name, count)
        return count

    def _plan(self, document: RemoteDocument) -> str:
        item = self._state.get(self._source.name, document.remote_id)
        if item is None:
            return "new"
        if item.status == Status.FAILED:
            verdict = "retry" if self._wants(document) else "gave-up"
            return f"{verdict} {item.attempts}/{self._max_attempts}"
        return str(item.status)

    def _check_queued(self, stats: CycleStats) -> None:
        for item in self._state.queued(self._source.name):
            assert item.task_id is not None
            result = self._paperless.task(item.task_id)
            if result is None:
                # The tasks list can be cleared by an admin. Nothing to wait for
                logger.warning(
                    "%s: task %s for %s is unknown to paperless, retrying",
                    self._source.name,
                    item.task_id,
                    item.filename,
                )
                self._fail(item, "task unknown to paperless")
                stats.failed += 1
            elif not result.done:
                continue
            elif result.success:
                item.status = Status.DONE
                item.document_id = result.document_id
                item.error = None
                self._state.save(item)
                stats.completed += 1
                if result.duplicate:
                    logger.info(
                        "%s: %s was a duplicate of document %s",
                        self._source.name,
                        item.filename,
                        result.document_id,
                    )
            else:
                self._fail(item, result.message or "consume task failed")
                stats.failed += 1

    def _wants(self, document: RemoteDocument) -> bool:
        item = self._state.get(self._source.name, document.remote_id)
        if item is None:
            return True
        if item.status == Status.FAILED:
            return item.attempts < self._max_attempts
        return False

    def _process(
        self,
        provider: SourceProvider,
        document: RemoteDocument,
        options: UploadOptions,
        stats: CycleStats,
    ) -> None:
        item = self._state.get(self._source.name, document.remote_id) or ProcessedItem(
            source=self._source.name,
            remote_id=document.remote_id,
            filename=document.filename,
            status=Status.FAILED,
        )
        item.filename = document.filename
        try:
            with tempfile.TemporaryDirectory(prefix="paperless-sources-") as tmp:
                path = provider.download(document, Path(tmp))
                item.checksum = _sha256(path)
                existing = self._paperless.find_by_checksum(item.checksum)
                if existing is not None:
                    item.status = Status.DONE
                    item.document_id = existing
                    item.error = None
                    self._state.save(item)
                    stats.already_in_paperless += 1
                    return
                item.task_id = self._paperless.upload(path, path.name, options)
        except ProviderError:
            # A provider error means the session or the remote is broken. Stop this source
            raise
        except Exception as e:
            logger.exception("%s: %s failed", self._source.name, document.filename)
            self._fail(item, str(e))
            stats.failed += 1
            return

        item.status = Status.QUEUED
        item.error = None
        self._state.save(item)
        stats.uploaded += 1
        logger.info(
            "%s: uploaded %s, task %s",
            self._source.name,
            document.filename,
            item.task_id,
        )

    def _fail(self, item: ProcessedItem, error: str) -> None:
        item.status = Status.FAILED
        item.attempts += 1
        item.error = error
        self._state.save(item)
        if item.attempts >= self._max_attempts:
            logger.error(
                "%s: giving up on %s after %d attempts: %s",
                self._source.name,
                item.filename,
                item.attempts,
                error,
            )


def _resolve(paperless: PaperlessClient, spec: UploadSpec) -> UploadOptions:
    def one(endpoint: str, value: int | str | None) -> int | None:
        return None if value is None else paperless.resolve(endpoint, value)

    return UploadOptions(
        correspondent=one("correspondents", spec.correspondent),
        document_type=one("document_types", spec.document_type),
        storage_path=one("storage_paths", spec.storage_path),
        tags=tuple(paperless.resolve("tags", t) for t in spec.tags),
        custom_fields=tuple(
            paperless.resolve("custom_fields", f) for f in spec.custom_fields
        ),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
