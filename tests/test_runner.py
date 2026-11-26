import hashlib

import pytest

from paperless_sources.config import SourceConfig
from paperless_sources.config import UploadSpec
from paperless_sources.paperless import TaskResult
from paperless_sources.paperless import UploadOptions
from paperless_sources.providers import ProviderError
from paperless_sources.providers import RemoteDocument
from paperless_sources.runner import SourceRunner
from paperless_sources.state import StateStore
from paperless_sources.state import Status

FILES = {"a": b"content a", "b": b"content b"}


class FakeProvider:
    name = "Fake"
    version = "1"
    author = "tests"
    url = ""
    config_fields = ()

    # Class level so the test can inspect what the runner did
    downloads: list[str] = []
    broken: set[str] = set()
    listing: list[str] = list(FILES)

    def __init__(self, config, http):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def list_documents(self):
        for remote_id in self.listing:
            yield RemoteDocument(remote_id=remote_id, filename=f"{remote_id}.pdf")

    def download(self, document, dest_dir):
        FakeProvider.downloads.append(document.remote_id)
        if document.remote_id in self.broken:
            raise OSError("disk full")
        path = dest_dir / document.filename
        path.write_bytes(FILES[document.remote_id])
        return path


class FakePaperless:
    def __init__(self):
        self.existing: dict[str, int] = {}
        self.uploads: list[tuple[str, UploadOptions]] = []
        self.tasks: dict[str, TaskResult | None] = {}

    def find_by_checksum(self, checksum):
        return self.existing.get(checksum)

    def resolve(self, endpoint, value):
        return {"Payslip": 7}[value] if isinstance(value, str) else value

    def upload(self, path, filename, options):
        self.uploads.append((filename, options))
        task_id = f"task-{len(self.uploads)}"
        self.tasks[task_id] = TaskResult(done=False, success=False)
        return task_id

    def task(self, task_id):
        return self.tasks.get(task_id)


@pytest.fixture(autouse=True)
def _reset_provider():
    FakeProvider.downloads = []
    FakeProvider.broken = set()
    FakeProvider.listing = list(FILES)


@pytest.fixture
def state(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as s:
        yield s


@pytest.fixture
def paperless():
    return FakePaperless()


def _runner(paperless, state, max_attempts=3, provider=FakeProvider):
    source = SourceConfig(
        name="src",
        provider="fake",
        config={},
        upload=UploadSpec(tags=("Payslip",)),
    )
    return SourceRunner(
        source,
        provider,
        paperless,
        state,
        http=None,
        max_attempts=max_attempts,
    )


def test_new_documents_are_uploaded_once(paperless, state):
    runner = _runner(paperless, state)

    stats = runner.run_cycle()

    assert stats.uploaded == 2
    assert [u[0] for u in paperless.uploads] == ["a.pdf", "b.pdf"]
    assert paperless.uploads[0][1].tags == (7,)
    assert state.get("src", "a").status == Status.QUEUED

    # Queued items are not downloaded again while the task runs
    runner.run_cycle()
    assert FakeProvider.downloads == ["a", "b"]
    assert len(paperless.uploads) == 2


def test_finished_task_marks_done(paperless, state):
    runner = _runner(paperless, state)
    runner.run_cycle()
    paperless.tasks["task-1"] = TaskResult(done=True, success=True, document_id=12)

    stats = runner.run_cycle()

    assert stats.completed == 1
    item = state.get("src", "a")
    assert item.status == Status.DONE
    assert item.document_id == 12


def test_existing_checksum_skips_upload(paperless, state):
    paperless.existing[hashlib.sha256(FILES["a"]).hexdigest()] = 99

    stats = _runner(paperless, state).run_cycle()

    assert stats.already_in_paperless == 1
    assert [u[0] for u in paperless.uploads] == ["b.pdf"]
    assert state.get("src", "a").document_id == 99


def test_failed_task_is_retried_until_max_attempts(paperless, state):
    FakeProvider.listing = ["a"]
    runner = _runner(paperless, state, max_attempts=2)

    runner.run_cycle()
    paperless.tasks["task-1"] = TaskResult(done=True, success=False, message="boom")
    runner.run_cycle()  # sees the failure, uploads again
    paperless.tasks["task-2"] = TaskResult(done=True, success=False, message="boom")
    runner.run_cycle()  # second failure, gives up
    runner.run_cycle()

    item = state.get("src", "a")
    assert item.status == Status.FAILED
    assert item.attempts == 2
    assert item.error == "boom"
    assert len(paperless.uploads) == 2


def test_download_error_fails_item_and_continues(paperless, state):
    FakeProvider.broken = {"a"}

    stats = _runner(paperless, state).run_cycle()

    assert stats.failed == 1
    assert stats.uploaded == 1
    assert state.get("src", "a").error == "disk full"


def test_provider_error_stops_the_source(paperless, state):
    class ExpiredProvider(FakeProvider):
        def download(self, document, dest_dir):
            raise ProviderError("session expired")

    with pytest.raises(ProviderError):
        _runner(paperless, state, provider=ExpiredProvider).run_cycle()
    assert paperless.uploads == []


def test_unknown_task_is_retried(paperless, state):
    FakeProvider.listing = ["a"]
    runner = _runner(paperless, state)
    runner.run_cycle()
    paperless.tasks.clear()

    runner.run_cycle()

    assert len(paperless.uploads) == 2
    assert state.get("src", "a").attempts == 1


def test_list_only_does_not_download_or_upload(paperless, state, caplog):
    caplog.set_level("INFO")
    runner = _runner(paperless, state)
    runner.run_cycle()
    paperless.tasks["task-1"] = TaskResult(done=True, success=True, document_id=1)
    runner.run_cycle()
    FakeProvider.downloads = []
    FakeProvider.listing = ["a", "b", "c"]
    FILES["c"] = b"content c"
    try:
        assert runner.list_only() == 3
    finally:
        del FILES["c"]

    assert FakeProvider.downloads == []
    assert len(paperless.uploads) == 2
    lines = [r.getMessage() for r in caplog.records if "[id " in r.getMessage()]
    assert "done" in lines[0]
    assert "queued" in lines[1]
    assert "new" in lines[2]
