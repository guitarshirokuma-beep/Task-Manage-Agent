"""MCP server: exposes the task store as five tools over stdio.

This module only translates between MCP and `TaskStore`. All task logic and
validation live in `store.py` / `models.py`, so they can be tested without MCP.

Every tool returns a plain-text message. Problems the caller can fix
(unknown id, bad date format, ...) are returned as explanatory text rather
than raised, so the LLM can read what happened and correct itself.
"""

import inspect
import logging
import sys
from collections.abc import Callable
from datetime import date
from functools import wraps
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from .models import Task, ValidationError
from .store import StoreError, TaskStore, default_data_path

# stdout is reserved for the MCP stdio protocol, so logs must go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("taskagent")

INSTRUCTIONS = """\
taskagent manages a personal to-do list.
- Task ids are random 8-character strings. Never guess an id: call list_tasks first
  and use the id shown there.
- Dates must be YYYY-MM-DD. Convert relative expressions such as "tomorrow" or
  "next Friday" into an absolute date yourself before calling a tool.
"""

# --- argument types (the descriptions become the tool's input schema) ------------

TaskId = Annotated[
    str,
    Field(
        description="The 8-character task id exactly as shown by list_tasks, e.g. 'a3f91c2e'.",
    ),
]
Title = Annotated[
    str,
    Field(description="Short description of what needs to be done, e.g. 'Submit the report'."),
]
DueDate = Annotated[
    str | None,
    Field(
        description=(
            "Deadline as a date in YYYY-MM-DD format, e.g. '2026-11-08'. "
            "Convert relative dates ('tomorrow', 'next Friday') to this format first; "
            "if you are unsure of today's date, list_tasks shows it on its first line. "
            "Omit if there is no deadline."
        ),
    ),
]
PriorityArg = Annotated[
    Literal["low", "normal", "high"],
    Field(description="How important the task is: 'low', 'normal' or 'high'."),
]
Tags = Annotated[
    list[str] | None,
    Field(description="Labels for grouping tasks, e.g. ['work', 'shopping']. Omit if none."),
]


# --- output formatting ----------------------------------------------------------------


def format_task(task: Task) -> str:
    parts = [f"status: {task.status}", f"priority: {task.priority}"]
    parts.append(f"due: {task.due}" if task.due else "due: none")
    if task.tags:
        parts.append("tags: " + ", ".join(task.tags))
    if task.completed_at:
        parts.append(f"completed_at: {task.completed_at}")
    return f"[{task.id}] {task.title} ({'; '.join(parts)})"


def not_found(task_id: str) -> str:
    return (
        f"No task with id '{task_id}' was found. Nothing was changed. "
        "Call list_tasks (with status='all' if the task may be completed) "
        "to see the valid ids, then try again."
    )


def today_line(today: date) -> str:
    return f"Today is {today.isoformat()} ({today.strftime('%A')})."


def describe_filters(status: str, tag: str | None, due_before: str | None) -> str:
    parts = [f"status={status}"]
    if tag is not None:
        parts.append(f"tag='{tag}'")
    if due_before is not None:
        parts.append(f"due_before={due_before}")
    return ", ".join(parts)


def make_error_handler(today: Callable[[], date]) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Decorator factory: turn expected errors into readable text instead of a failed call."""

    def explain_errors(fn: Callable[..., str]) -> Callable[..., str]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> str:
            try:
                return fn(*args, **kwargs)
            except ValidationError as e:
                # Most invalid input is a date the caller failed to convert,
                # so tell it what "today" is to make the retry easy.
                return f"Invalid input: {e} Nothing was changed. {today_line(today())}"
            except StoreError as e:
                logger.error("storage error: %s", e)
                return f"Storage error: {e}"

        # The docstring becomes the tool description; strip the source indentation.
        wrapper.__doc__ = inspect.cleandoc(fn.__doc__ or "")
        return wrapper

    return explain_errors


# --- server ------------------------------------------------------------------------------


def create_server(
    store: TaskStore | None = None,
    *,
    today: Callable[[], date] = date.today,
) -> MCPServer:
    """Build the MCP server.

    Tests pass their own store and clock; normal use reads TASKAGENT_DATA_PATH
    and the machine's local date.
    """
    store = store if store is not None else TaskStore.from_path()
    explain_errors = make_error_handler(today)
    server = MCPServer("taskagent", instructions=INSTRUCTIONS)

    @server.tool(annotations=ToolAnnotations(title="Add task", read_only_hint=False))
    @explain_errors
    def add_task(
        title: Title,
        due: DueDate = None,
        priority: PriorityArg = "normal",
        tags: Tags = None,
    ) -> str:
        """Add one new task to the to-do list and return its generated id.

        Use this when the user asks to remember, add or schedule something to do.
        Add one task per call. Do not use this to change an existing task
        (use update_task) or to mark one finished (use complete_task).
        """
        task = store.add(title, due=due, priority=priority, tags=tags)
        return f"Added task {task.id}.\n{format_task(task)}"

    @server.tool(annotations=ToolAnnotations(title="List tasks", read_only_hint=True))
    @explain_errors
    def list_tasks(
        status: Annotated[
            Literal["open", "done", "all"],
            Field(
                description=(
                    "Which tasks to return: 'open' (not finished, the default), "
                    "'done' (completed) or 'all'."
                ),
            ),
        ] = "open",
        tag: Annotated[
            str | None,
            Field(description="Only return tasks that have this exact tag. Omit for all tags."),
        ] = None,
        due_before: Annotated[
            str | None,
            Field(
                description=(
                    "Only return tasks due on or before this date (YYYY-MM-DD). "
                    "Tasks without a due date are excluded when this is set."
                ),
            ),
        ] = None,
    ) -> str:
        """Return tasks matching the filters, sorted by due date and then priority.

        Use this to show the user their tasks, and ALWAYS call it first to find a
        task's id before calling complete_task, update_task or delete_task.
        The first line of the result is today's date, which you can use to convert
        relative dates. If a tag filter matches nothing, the existing tags are listed.
        """
        tasks = store.list_tasks(status=status, tag=tag, due_before=due_before)
        header = today_line(today())
        if not tasks:
            message = f"{header}\nNo tasks match ({describe_filters(status, tag, due_before)})."
            if tag is not None:
                known = store.all_tags()
                if tag.strip() not in known:
                    message += (
                        f"\nNo task has the tag '{tag}'. Existing tags: {', '.join(known)}."
                        if known
                        else "\nNo task has any tags yet."
                    )
            return message
        lines = [header, f"{len(tasks)} task(s):"] + [format_task(t) for t in tasks]
        return "\n".join(lines)

    @server.tool(
        annotations=ToolAnnotations(title="Complete task", read_only_hint=False, idempotent_hint=True)
    )
    @explain_errors
    def complete_task(id: TaskId) -> str:
        """Mark a task as done.

        Use this when the user says a task is finished. Call list_tasks first to
        get the task's id; never guess it. Completed tasks are kept (use
        delete_task only when the user wants a task removed entirely).
        """
        before = store.get(id)
        if before is None:
            return not_found(id)
        if before.status == "done":
            return f"Task {before.id} was already done. Nothing was changed.\n{format_task(before)}"
        task = store.complete(id)
        if task is None:  # removed between the two calls
            return not_found(id)
        return f"Marked task {task.id} as done.\n{format_task(task)}"

    @server.tool(
        annotations=ToolAnnotations(title="Update task", read_only_hint=False, idempotent_hint=True)
    )
    @explain_errors
    def update_task(
        id: TaskId,
        title: Annotated[str | None, Field(description="New title. Omit to keep the current one.")] = None,
        due: Annotated[
            str | None,
            Field(description="New deadline in YYYY-MM-DD format. Omit to keep the current one."),
        ] = None,
        priority: Annotated[
            Literal["low", "normal", "high"] | None,
            Field(description="New priority: 'low', 'normal' or 'high'. Omit to keep the current one."),
        ] = None,
        tags: Annotated[
            list[str] | None,
            Field(
                description=(
                    "New full list of tags; it replaces the current tags. "
                    "Omit to keep the current ones."
                ),
            ),
        ] = None,
        clear_due: Annotated[
            bool,
            Field(
                description=(
                    "Set to true to remove the task's deadline. Do not pass due at the same time."
                ),
            ),
        ] = False,
    ) -> str:
        """Change some fields of an existing task. Only the fields you pass are changed.

        Use this to rename a task or change its deadline, priority or tags,
        or to remove its deadline (clear_due=true).
        Call list_tasks first to get the task's id; never guess it.
        To mark a task finished, use complete_task instead.
        """
        if title is None and due is None and priority is None and tags is None and not clear_due:
            return (
                "No fields to change were given. Pass at least one of "
                "title, due, priority, tags or clear_due. Nothing was changed."
            )
        task = store.update(
            id, title=title, due=due, priority=priority, tags=tags, clear_due=clear_due
        )
        if task is None:
            return not_found(id)
        return f"Updated task {task.id}.\n{format_task(task)}"

    @server.tool(
        annotations=ToolAnnotations(title="Delete task", read_only_hint=False, destructive_hint=True)
    )
    @explain_errors
    def delete_task(id: TaskId) -> str:
        """Permanently remove a task from the list. This cannot be undone.

        Use this only when the user explicitly wants a task removed (for example,
        it was added by mistake). For finished tasks, use complete_task instead.
        Call list_tasks first to get the task's id; never guess it.
        """
        task = store.delete(id)
        if task is None:
            return not_found(id)
        return f"Deleted task {task.id}.\n{format_task(task)}"

    return server


mcp = create_server()


def main() -> None:
    logger.info("taskagent: starting MCP server (stdio), data file: %s", default_data_path())
    mcp.run("stdio")


if __name__ == "__main__":
    main()
