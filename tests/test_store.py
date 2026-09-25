import json
import os

import pytest

from taskagent.models import ValidationError
from taskagent.store import (
    DATA_PATH_ENV,
    JsonFileBackend,
    StoreError,
    TaskStore,
    default_data_path,
)


class FakeClock:
    """Returns increasing timestamps so ordering and completed_at are predictable."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"2026-10-01T09:00:{self.n:02d}+09:00"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "tasks.json"


@pytest.fixture
def store(path):
    return TaskStore(JsonFileBackend(path), clock=FakeClock())


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- basic CRUD -------------------------------------------------------------


def test_starts_empty_when_file_is_missing(store, path):
    assert not path.exists()
    assert store.list_tasks() == []


def test_add_persists_task(store, path):
    task = store.add("Write README", due="2026-10-20", priority="high", tags=["dev"])

    assert len(task.id) == 8
    assert task.status == "open"
    assert task.completed_at is None
    data = read_json(path)
    assert data["version"] == 1
    assert data["tasks"][0]["title"] == "Write README"
    assert data["tasks"][0]["due"] == "2026-10-20"


def test_add_uses_defaults(store):
    task = store.add("Buy milk")
    assert task.priority == "normal"
    assert task.due is None
    assert task.tags == []


def test_data_survives_new_store_instance(store, path):
    task = store.add("Persist me")
    again = TaskStore.from_path(path)
    assert again.get(task.id).title == "Persist me"


def test_complete_marks_done_and_hides_from_default_list(store):
    task = store.add("Finish it")
    done = store.complete(task.id)

    assert done.status == "done"
    assert done.completed_at is not None
    assert store.list_tasks() == []
    assert [t.id for t in store.list_tasks(status="done")] == [task.id]
    assert len(store.list_tasks(status="all")) == 1


def test_complete_twice_keeps_first_completed_at(store):
    task = store.add("Once")
    first = store.complete(task.id).completed_at
    assert store.complete(task.id).completed_at == first


def test_update_changes_only_given_fields(store):
    task = store.add("Old title", due="2026-10-10", priority="low", tags=["a"])
    updated = store.update(task.id, title="New title")

    assert updated.title == "New title"
    assert updated.due == "2026-10-10"
    assert updated.priority == "low"
    assert updated.tags == ["a"]


def test_update_all_fields(store):
    task = store.add("T")
    updated = store.update(task.id, title="T2", due="2026-12-01", priority="high", tags=["x", "y"])
    assert (updated.title, updated.due, updated.priority, updated.tags) == (
        "T2",
        "2026-12-01",
        "high",
        ["x", "y"],
    )


def test_delete_removes_task(store):
    keep = store.add("Keep")
    drop = store.add("Drop")

    deleted = store.delete(drop.id)

    assert deleted.id == drop.id
    assert [t.id for t in store.list_tasks(status="all")] == [keep.id]


# --- unknown ids --------------------------------------------------------------


@pytest.mark.parametrize("op", ["get", "complete", "update", "delete"])
def test_unknown_id_returns_none(store, op):
    store.add("Something")
    assert getattr(store, op)("zzzzzzzz") is None


def test_unknown_id_does_not_modify_file(store, path):
    store.add("Something")
    before = path.read_text(encoding="utf-8")
    store.delete("zzzzzzzz")
    assert path.read_text(encoding="utf-8") == before


def test_id_is_trimmed(store):
    task = store.add("Spaces")
    assert store.get(f"  {task.id} ").id == task.id


# --- filtering and sorting ------------------------------------------------------


def test_filter_by_tag(store):
    a = store.add("A", tags=["job"])
    store.add("B", tags=["home"])
    assert [t.id for t in store.list_tasks(tag="job")] == [a.id]


def test_due_before_is_inclusive_and_skips_tasks_without_due(store):
    early = store.add("Early", due="2026-10-01")
    on_day = store.add("On day", due="2026-10-15")
    store.add("Late", due="2026-10-16")
    store.add("No due")

    result = store.list_tasks(due_before="2026-10-15")
    assert [t.id for t in result] == [early.id, on_day.id]


def test_sorted_by_due_then_priority_then_created(store):
    no_due = store.add("No due", priority="high")
    later = store.add("Later", due="2026-11-01")
    same_low = store.add("Same day low", due="2026-10-10", priority="low")
    same_high = store.add("Same day high", due="2026-10-10", priority="high")

    ids = [t.id for t in store.list_tasks()]
    assert ids == [same_high.id, same_low.id, later.id, no_due.id]


# --- validation ------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_title_rejected(store, bad):
    with pytest.raises(ValidationError):
        store.add(bad)


@pytest.mark.parametrize("bad", ["urgent", "HIGH", ""])
def test_invalid_priority_rejected(store, bad):
    with pytest.raises(ValidationError):
        store.add("T", priority=bad)


@pytest.mark.parametrize("bad", ["tomorrow", "明日", "2026/11/08", "2026-13-01", "11-08"])
def test_invalid_due_rejected(store, bad):
    with pytest.raises(ValidationError):
        store.add("T", due=bad)


def test_invalid_status_filter_rejected(store):
    with pytest.raises(ValidationError):
        store.list_tasks(status="closed")


def test_invalid_update_does_not_touch_file(store, path):
    task = store.add("T")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        store.update(task.id, due="next week")
    assert path.read_text(encoding="utf-8") == before


def test_tags_are_trimmed_and_deduplicated(store):
    task = store.add("T", tags=[" job ", "job", "", "es"])
    assert task.tags == ["job", "es"]


def test_title_is_trimmed(store):
    assert store.add("  Trim me  ").title == "Trim me"


# --- file handling ------------------------------------------------------------------


def test_japanese_is_stored_readably(store, path):
    store.add("ESを提出する", tags=["就活"])
    text = path.read_text(encoding="utf-8")
    assert "ESを提出する" in text
    assert "\\u" not in text


def test_empty_file_is_treated_as_no_tasks(path):
    path.write_text("", encoding="utf-8")
    assert TaskStore.from_path(path).list_tasks() == []


def test_broken_json_raises_and_is_left_untouched(path):
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        TaskStore.from_path(path).add("T")
    assert path.read_text(encoding="utf-8") == "{not json"


def test_unsupported_version_raises(path):
    path.write_text(json.dumps({"version": 99, "tasks": []}), encoding="utf-8")
    with pytest.raises(StoreError):
        TaskStore.from_path(path).list_tasks()


def test_missing_folder_is_created(tmp_path):
    path = tmp_path / "nested" / "dir" / "tasks.json"
    TaskStore.from_path(path).add("T")
    assert path.exists()


def test_no_temp_files_left_behind(store, path):
    store.add("A")
    store.add("B")
    assert [p.name for p in path.parent.iterdir()] == ["tasks.json"]


def test_replace_retries_on_permission_error(store, path, monkeypatch):
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr("taskagent.store.os.replace", flaky_replace)
    monkeypatch.setattr("taskagent.store.time.sleep", lambda s: None)

    store.add("Retry me")

    assert calls["n"] == 3
    assert read_json(path)["tasks"][0]["title"] == "Retry me"


def test_replace_gives_up_and_keeps_old_file(store, path, monkeypatch):
    store.add("Original")
    before = path.read_text(encoding="utf-8")

    def always_locked(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr("taskagent.store.os.replace", always_locked)
    monkeypatch.setattr("taskagent.store.time.sleep", lambda s: None)

    with pytest.raises(StoreError):
        store.add("New")
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in path.parent.iterdir()] == ["tasks.json"]


# --- data path ------------------------------------------------------------------------


def test_default_path_uses_env(monkeypatch, tmp_path):
    target = tmp_path / "custom.json"
    monkeypatch.setenv(DATA_PATH_ENV, str(target))
    assert default_data_path() == target


def test_default_path_without_env(monkeypatch):
    monkeypatch.delenv(DATA_PATH_ENV, raising=False)
    assert default_data_path().name == "tasks.json"
    assert default_data_path().parent.name == ".taskagent"
