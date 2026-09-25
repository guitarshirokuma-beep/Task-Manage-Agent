"""Tests for the MCP layer: tool schemas and the text each tool returns."""

from datetime import date

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from taskagent.server import create_server
from taskagent.store import JsonFileBackend, TaskStore

TOOL_NAMES = {"add_task", "list_tasks", "complete_task", "update_task", "delete_task"}


@pytest.fixture
def store(tmp_path):
    return TaskStore(JsonFileBackend(tmp_path / "tasks.json"))


FIXED_TODAY = date(2026, 9, 25)  # a Friday


@pytest.fixture
def server(store):
    return create_server(store, today=lambda: FIXED_TODAY)


def call(server, name, **arguments):
    """Call a tool through MCP and return its text output."""
    result = anyio.run(server.call_tool, name, arguments)
    assert not result.is_error, result
    return "\n".join(block.text for block in result.content)


def call_raw(server, name, **arguments):
    return anyio.run(server.call_tool, name, arguments)


def tools_by_name(server):
    return {t.name: t for t in anyio.run(server.list_tools)}


# --- schema: what the LLM sees ------------------------------------------------------


def test_exactly_the_five_tools_are_registered(server):
    assert set(tools_by_name(server)) == TOOL_NAMES


def test_every_tool_and_argument_has_a_description(server):
    for tool in tools_by_name(server).values():
        assert tool.description and len(tool.description) > 40, tool.name
        for arg, schema in tool.input_schema.get("properties", {}).items():
            assert schema.get("description"), f"{tool.name}.{arg} has no description"


@pytest.mark.parametrize(
    "tool, required",
    [
        ("add_task", ["title"]),
        ("list_tasks", []),
        ("complete_task", ["id"]),
        ("update_task", ["id"]),
        ("delete_task", ["id"]),
    ],
)
def test_required_arguments(server, tool, required):
    schema = tools_by_name(server)[tool].input_schema
    assert sorted(schema.get("required", [])) == sorted(required)


def _enum_values(prop):
    if "enum" in prop:
        return set(prop["enum"])
    for option in prop.get("anyOf", []):
        if "enum" in option:
            return set(option["enum"])
    return set()


def test_enums_are_explicit(server):
    tools = tools_by_name(server)
    add = tools["add_task"].input_schema["properties"]
    assert _enum_values(add["priority"]) == {"low", "normal", "high"}
    assert add["priority"].get("default") == "normal"
    status = tools["list_tasks"].input_schema["properties"]["status"]
    assert _enum_values(status) == {"open", "done", "all"}
    assert status.get("default") == "open"
    assert _enum_values(tools["update_task"].input_schema["properties"]["priority"]) == {
        "low",
        "normal",
        "high",
    }


def test_id_tools_tell_the_llm_to_call_list_tasks_first(server):
    tools = tools_by_name(server)
    for name in ("complete_task", "update_task", "delete_task"):
        assert "list_tasks" in tools[name].description


def test_date_arguments_mention_the_format(server):
    tools = tools_by_name(server)
    assert "YYYY-MM-DD" in tools["add_task"].input_schema["properties"]["due"]["description"]
    assert "YYYY-MM-DD" in tools["list_tasks"].input_schema["properties"]["due_before"]["description"]
    assert "YYYY-MM-DD" in tools["update_task"].input_schema["properties"]["due"]["description"]


# --- behaviour through MCP ---------------------------------------------------------------


def test_add_then_list(server, store):
    text = call(server, "add_task", title="ESを提出する", due="2026-11-08", priority="high", tags=["就活"])
    task = store.list_tasks()[0]

    assert task.id in text
    listed = call(server, "list_tasks")
    assert task.id in listed
    assert "ESを提出する" in listed
    assert "2026-11-08" in listed


def test_list_empty(server):
    assert "No tasks" in call(server, "list_tasks")


def test_complete_flow(server, store):
    task = store.add("Finish")
    assert "Marked task" in call(server, "complete_task", id=task.id)
    assert "already done" in call(server, "complete_task", id=task.id)
    assert "No tasks" in call(server, "list_tasks")
    assert task.id in call(server, "list_tasks", status="done")


def test_update_only_given_fields(server, store):
    task = store.add("Old", priority="low")
    text = call(server, "update_task", id=task.id, title="New")
    assert "Updated task" in text
    updated = store.get(task.id)
    assert (updated.title, updated.priority) == ("New", "low")


def test_update_without_fields_explains(server, store):
    task = store.add("T")
    assert "No fields to change" in call(server, "update_task", id=task.id)


def test_delete(server, store):
    task = store.add("Remove me")
    assert "Deleted task" in call(server, "delete_task", id=task.id)
    assert store.get(task.id) is None


@pytest.mark.parametrize("tool", ["complete_task", "update_task", "delete_task"])
def test_unknown_id_is_explained_not_raised(server, tool):
    args = {"id": "zzzzzzzz"}
    if tool == "update_task":
        args["title"] = "x"
    text = call(server, tool, **args)
    assert "No task with id 'zzzzzzzz'" in text
    assert "list_tasks" in text


def test_bad_date_is_explained_not_raised(server, store):
    text = call(server, "add_task", title="T", due="tomorrow")
    assert "Invalid input" in text
    assert "YYYY-MM-DD" in text
    assert store.list_tasks() == []


def test_bad_due_before_is_explained(server):
    assert "Invalid input" in call(server, "list_tasks", due_before="next week")


def test_filters_through_mcp(server, store):
    job = store.add("Job", tags=["job"], due="2026-10-10")
    store.add("Home", tags=["home"], due="2026-12-01")
    assert job.id in call(server, "list_tasks", tag="job")
    listed = call(server, "list_tasks", due_before="2026-10-31")
    assert job.id in listed and "Home" not in listed


def test_invalid_enum_is_rejected_by_schema(server, store):
    # Arguments that do not match the input schema are rejected before the tool runs.
    # Over MCP the client receives this as an error result it can read and correct.
    with pytest.raises(ToolError, match="priority"):
        call_raw(server, "add_task", title="T", priority="urgent")
    assert store.list_tasks() == []


# --- Phase 4 improvements ---------------------------------------------------------------


def test_list_starts_with_today(server, store):
    store.add("T")
    assert call(server, "list_tasks").splitlines()[0] == "Today is 2026-09-25 (Friday)."


def test_empty_list_also_shows_today_and_filters(server):
    text = call(server, "list_tasks", status="done", due_before="2026-10-01")
    assert "Today is 2026-09-25 (Friday)." in text
    assert "status=done" in text and "due_before=2026-10-01" in text


def test_invalid_date_message_includes_today(server):
    text = call(server, "add_task", title="T", due="来週")
    assert "Invalid input" in text
    assert "Today is 2026-09-25 (Friday)." in text


def test_unknown_tag_lists_existing_tags(server, store):
    store.add("A", tags=["就活", "ES"])
    store.add("B", tags=["買い物"])
    text = call(server, "list_tasks", tag="就職活動")
    assert "No task has the tag '就職活動'" in text
    assert "Existing tags: 就活, ES, 買い物" in text


def test_known_tag_with_no_open_tasks_does_not_list_tags(server, store):
    task = store.add("A", tags=["job"])
    store.complete(task.id)
    text = call(server, "list_tasks", tag="job")
    assert "No tasks match" in text
    assert "Existing tags" not in text


def test_unknown_tag_when_no_tags_exist(server, store):
    store.add("A")
    assert "No task has any tags yet." in call(server, "list_tasks", tag="x")


def test_clear_due_through_mcp(server, store):
    task = store.add("T", due="2026-10-10")
    text = call(server, "update_task", id=task.id, clear_due=True)
    assert "due: none" in text
    assert store.get(task.id).due is None


def test_clear_due_and_due_together_is_explained(server, store):
    task = store.add("T", due="2026-10-10")
    text = call(server, "update_task", id=task.id, due="2026-10-11", clear_due=True)
    assert "Invalid input" in text
    assert store.get(task.id).due == "2026-10-10"


def test_clear_due_is_documented(server):
    props = tools_by_name(server)["update_task"].input_schema["properties"]
    assert props["clear_due"]["type"] == "boolean"
    assert props["clear_due"].get("default") is False
