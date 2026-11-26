import pytest

from paperless_sources.state import ProcessedItem
from paperless_sources.state import StateError
from paperless_sources.state import StateStore
from paperless_sources.state import Status


def test_unwritable_path_raises_state_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(StateError, match="Cannot open"):
        StateStore(blocker / "state.sqlite3")


def test_save_and_reload(tmp_path):
    path = tmp_path / "sub" / "state.sqlite3"
    with StateStore(path) as state:
        state.save(ProcessedItem("s", "r1", "a.pdf", Status.QUEUED, task_id="t"))
    with StateStore(path) as state:
        item = state.get("s", "r1")
        assert item.status == Status.QUEUED
        assert item.task_id == "t"
        assert [i.remote_id for i in state.queued("s")] == ["r1"]
