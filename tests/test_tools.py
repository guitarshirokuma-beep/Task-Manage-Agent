"""Tests for the MCP layer: tool schemas and the text each tool returns."""

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from taskagent.server import create_server
from taskagent.store import JsonFileBackend, TaskStore

TOOL_NAMES = {"add_task", "list_tasks", "complete_task", "update_task", "delete_task"}


@pytest.fixture
def store(tmp_path):
    return TaskStore(JsonFileBackend(tmp_path / "tasks.json"))


@pytest.fixture
def server(store):
    return create_server(store)


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
