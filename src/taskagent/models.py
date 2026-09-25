"""Task data structure and validation.

This module has no dependency on MCP or on how tasks are stored.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Literal, get_args

Priority = Literal["low", "normal", "high"]
Status = Literal["open", "done"]
StatusFilter = Literal["open", "done", "all"]

PRIORITIES: tuple[str, ...] = get_args(Priority)
STATUSES: tuple[str, ...] = get_args(Status)
STATUS_FILTERS: tuple[str, ...] = get_args(StatusFilter)

ID_LENGTH = 8


class ValidationError(ValueError):
    """Raised when a task field has an invalid value.

    The message is written so that it can be shown to an LLM as-is,
    telling it what was wrong and what is accepted instead.
    """


def new_task_id() -> str:
    """Return a new task id: the first 8 hex characters of a uuid4."""
    return uuid.uuid4().hex[:ID_LENGTH]


def now_iso() -> str:
    """Current local time as ISO 8601 with UTC offset, e.g. 2026-09-22T18:00:00+09:00."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_title(title: Any) -> str:
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("title must be a non-empty string.")
    return title.strip()


def validate_priority(priority: Any) -> str:
    if priority not in PRIORITIES:
        raise ValidationError(
            f"priority must be one of {', '.join(PRIORITIES)} (got {priority!r})."
        )
    return priority


def validate_status_filter(status: Any) -> str:
    if status not in STATUS_FILTERS:
        raise ValidationError(
            f"status must be one of {', '.join(STATUS_FILTERS)} (got {status!r})."
        )
    return status


def validate_date(value: Any, field_name: str = "due") -> str:
    """Accept only a calendar date in YYYY-MM-DD form.

    Natural-language dates ("tomorrow") are intentionally rejected:
    interpreting them is the caller's (LLM's) job, not the server's.
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string in YYYY-MM-DD format.")
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"{field_name} must be a date in YYYY-MM-DD format, e.g. 2026-12-31 "
            f"(got {value!r}). Convert relative dates like 'tomorrow' before calling."
        ) from None
    # date.fromisoformat also accepts forms like 20261108; normalize to YYYY-MM-DD.
    return parsed.isoformat()


def validate_tags(tags: Any) -> list[str]:
    """Strip whitespace, drop empty tags and remove duplicates (keeping order)."""
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValidationError("tags must be a list of strings.")
    result: list[str] = []
    for tag in tags:
        tag = tag.strip()
        if tag and tag not in result:
            result.append(tag)
    return result


@dataclass
class Task:
    id: str
    title: str
    status: Status = "open"
    priority: Priority = "normal"
    due: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Build a Task from stored JSON, validating every field."""
        try:
            task_id = data["id"]
            title = data["title"]
        except KeyError as e:
            raise ValidationError(f"stored task is missing field {e.args[0]!r}.") from None
        status = data.get("status", "open")
        if status not in STATUSES:
            raise ValidationError(f"stored task {task_id!r} has invalid status {status!r}.")
        due = data.get("due")
        return cls(
            id=str(task_id),
            title=validate_title(title),
            status=status,
            priority=validate_priority(data.get("priority", "normal")),
            due=None if due is None else validate_date(due),
            tags=validate_tags(data.get("tags", [])),
            created_at=data.get("created_at") or now_iso(),
            completed_at=data.get("completed_at"),
        )
