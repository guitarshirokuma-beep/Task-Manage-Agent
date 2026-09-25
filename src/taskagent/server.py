"""MCP server entry point.

Phase 1: a single `ping` tool to confirm the server is reachable from an MCP host.
The task tools are added in later steps.
"""

import logging
import sys

from mcp.server.mcpserver import MCPServer

# stdout is reserved for the MCP stdio protocol, so logs must go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("taskagent")

mcp = MCPServer("taskagent")


@mcp.tool()
def ping() -> str:
    """Check that the taskagent server is running.

    Use this only to verify the connection. It takes no arguments and returns "pong".
    """
    return "pong"


def main() -> None:
    logger.info("taskagent: starting MCP server (stdio)")
    mcp.run("stdio")


if __name__ == "__main__":
    main()
