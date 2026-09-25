import anyio

from taskagent.server import mcp, ping


def test_ping_returns_pong():
    assert ping() == "pong"


def test_ping_is_registered_as_tool():
    tools = anyio.run(mcp.list_tools)
    names = [t.name for t in tools]
    assert "ping" in names
