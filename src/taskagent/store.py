"""Task storage: CRUD logic on top of a pluggable backend.

- `TaskStore` holds the CRUD logic and knows nothing about where data lives.
- A backend only reads and writes the whole JSON document.
  `JsonFileBackend` stores it in a local file; other backends (e.g. a cloud
  drive) can be added later without touching `TaskStore` or the MCP layer.

Every operation re-reads the document, so changes made outside this process
(for example by a sync client) are picked up.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .models import (
    Task,
    ValidationError,
    new_task_id,
    now_iso,
    validate_date,
    validate_priority,
    validate_status_filter,
    validate_tags,
    validate_title,
)

SCHEMA_VERSION = 1
DATA_PATH_ENV = "TASKAGENT_DATA_PATH"

_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}


class StoreError(RuntimeError):
    """The data file could not be read or written."""


def default_data_path() -> Path:
    """TASKAGENT_DATA_PATH if set, otherwise ~/.taskagent/tasks.json."""
    env = os.environ.get(DATA_PATH_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".taskagent" / "tasks.json"


class Backend(Protocol):
    def read(self) -> dict[str, Any]: ...

    def write(self, document: dict[str, Any]) -> None: ...


class JsonFileBackend:
    """Stores the task document in a local JSON file.

    Writes go to a temporary file in the same folder and then replace the
    target, so a crash never leaves a half-written tasks.json behind.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        replace_retries: int = 5,
        retry_delay: float = 0.1,
    ) -> None:
        self.path = Path(path)
        self.replace_retries = replace_retries
        self.retry_delay = retry_delay

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "tasks": []}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise StoreError(f"could not read {self.path}: {e}") from e
        if not text.strip():
            return {"version": SCHEMA_VERSION, "tasks": []}
        try:
            document = json.loads(text)
        except json.JSONDecodeError as e:
            raise StoreError(
                f"{self.path} is not valid JSON ({e}). Fix or move the file; "
                "it was left untouched."
            ) from e
        if not isinstance(document, dict) or not isinstance(document.get("tasks"), list):
            raise StoreError(f"{self.path} does not look like a taskagent data file.")
        if document.get("version") != SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} has unsupported version {document.get('version')!r} "
                f"(expected {SCHEMA_VERSION})."
            )
        return document

    def write(self, document: dict[str, Any]) -> None:
        folder = self.path.parent
        if not folder.exists():
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise StoreError(
                    f"folder {folder} does not exist and could not be created: {e}"
                ) from e

        fd, tmp_name = tempfile.mkstemp(prefix=".tasks-", suffix=".tmp", dir=folder)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(document, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            self._replace(tmp_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def _replace(self, tmp_path: Path) -> None:
        # On Windows, os.replace fails with PermissionError while another
        # process (antivirus, a sync client) briefly holds the target open.
        for attempt in range(self.replace_retries):
            try:
                os.replace(tmp_path, self.path)
                return
            except PermissionError as e:
                if attempt == self.replace_retries - 1:
                    raise StoreError(
                        f"could not write {self.path}: the file is locked by another "
                        f"program ({e}). Try again in a moment."
                    ) from e
                time.sleep(self.retry_delay * (attempt + 1))


class TaskStore:
    """CRUD operations on tasks.

    Methods that target a single task return None when the id does not exist,
    so the caller can explain the situation instead of failing.
    """

    def __init__(self, backend: Backend, *, clock: Callable[[], str] = now_iso) -> None:
        self.backend = backend
        self.clock = clock

    @classmethod
    def from_path(cls, path: str | os.PathLike[str] | None = None) -> TaskStore:
        return cls(JsonFileBackend(path if path is not None else default_data_path()))

    # --- internal helpers -------------------------------------------------

    def _load(self) -> list[Task]:
        document = self.backend.read()
        try:
            return [Task.from_dict(item) for item in document["tasks"]]
        except (ValidationError, TypeError, AttributeError) as e:
            raise StoreError(f"stored data is invalid: {e}") from e

    def _save(self, tasks: list[Task]) -> None:
        self.backend.write(
            {"version": SCHEMA_VERSION, "tasks": [t.to_dict() for t in tasks]}
        )

    @staticmethod
    def _find(tasks: list[Task], task_id: str) -> Task | None:
        task_id = task_id.strip()
        return next((t for t in tasks if t.id == task_id), None)

    # --- public API ---------------------------------------------------------

    def add(
        self,
        title: str,
        due: str | None = None,
        priority: str = "normal",
        tags: list[str] | None = None,
    ) -> Task:
        task_title = validate_title(title)
        task_due = None if due is None else validate_date(due)
        task_priority = validate_priority(priority)
        task_tags = validate_tags(tags if tags is not None else [])

        tasks = self._load()
        existing = {t.id for t in tasks}
        task_id = new_task_id()
        while task_id in existing:
            task_id = new_task_id()

        task = Task(
            id=task_id,
            title=task_title,
            priority=task_priority,
            due=task_due,
            tags=task_tags,
            created_at=self.clock(),
        )
        tasks.append(task)
        self._save(tasks)
        return task

    def get(self, task_id: str) -> Task | None:
        return self._find(self._load(), task_id)

    def list_tasks(
        self,
        status: str = "open",
        tag: str | None = None,
        due_before: str | None = None,
    ) -> list[Task]:
        """Return matching tasks, sorted by due date (no due date last), then priority.

        due_before is inclusive: a task due on that date is included.
        Tasks without a due date are excluded when due_before is given.
        """
        status = validate_status_filter(status)
        limit = None if due_before is None else validate_date(due_before, "due_before")
        tag = None if tag is None else tag.strip()

        result = []
        for task in self._load():
            if status != "all" and task.status != status:
                continue
            if tag and tag not in task.tags:
                continue
            if limit is not None and (task.due is None or task.due > limit):
                continue
            result.append(task)

        result.sort(
            key=lambda t: (
                t.due is None,
                t.due or "",
                _PRIORITY_ORDER[t.priority],
                t.created_at,
            )
        )
        return result

    def complete(self, task_id: str) -> Task | None:
        """Mark a task as done. Completing an already-done task changes nothing."""
        tasks = self._load()
        task = self._find(tasks, task_id)
        if task is None:
            return None
        if task.status != "done":
            task.status = "done"
            task.completed_at = self.clock()
            self._save(tasks)
        return task

    def update(
        self,
        task_id: str,
        title: str | None = None,
        due: str | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
        clear_due: bool = False,
    ) -> Task | None:
        """Change only the fields that are given (not None).

        clear_due=True removes the deadline. It cannot be combined with due.
        """
        if clear_due and due is not None:
            raise ValidationError("pass either due or clear_due, not both.")
        # Validate before loading so invalid input never touches the file.
        new_title = None if title is None else validate_title(title)
        new_due = None if due is None else validate_date(due)
        new_priority = None if priority is None else validate_priority(priority)
        new_tags = None if tags is None else validate_tags(tags)

        tasks = self._load()
        task = self._find(tasks, task_id)
        if task is None:
            return None
        if new_title is not None:
            task.title = new_title
        if new_due is not None:
            task.due = new_due
        if clear_due:
            task.due = None
        if new_priority is not None:
            task.priority = new_priority
        if new_tags is not None:
            task.tags = new_tags
        self._save(tasks)
        return task

    def all_tags(self) -> list[str]:
        """Every tag used by any task (open or done), in first-seen order."""
        tags: list[str] = []
        for task in self._load():
            for tag in task.tags:
                if tag not in tags:
                    tags.append(tag)
        return tags

    def delete(self, task_id: str) -> Task | None:
        """Remove a task and return it, or None if it did not exist."""
        tasks = self._load()
        task = self._find(tasks, task_id)
        if task is None:
            return None
        tasks.remove(task)
        self._save(tasks)
        return task
