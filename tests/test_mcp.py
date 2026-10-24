"""Starts a real MCP server over stdio and checks Jarvis can discover and call its tools."""

import sys
import textwrap

import pytest

pytest.importorskip("mcp")

from jarvis.config import DEFAULTS  # noqa: E402
from jarvis.mcp_client import MCPHub, tool_name  # noqa: E402
from jarvis.tools import ToolBox  # noqa: E402

SERVER = textwrap.dedent('''
    try:
        from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
    except ImportError:
        from mcp.server.mcpserver import MCPServer as Server  # mcp 2.x

    mcp = Server("notes")

    @mcp.tool()
    def add_note(title: str, body: str) -> str:
        """Save a note."""
        return f"Saved note '{title}' with {len(body.split())} words."

    @mcp.tool()
    def list_notes() -> str:
        """List saved notes."""
        return "groceries, project ideas"

    mcp.run()
''')


def test_tool_names_are_safe_for_the_llm():
    assert tool_name("my-server", "read file") == "my-server__read_file"


def test_hub_discovers_and_calls_tools(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    hub = MCPHub({"notes": {"command": sys.executable, "args": [str(script)], "confirm": True,
                            "allow_tools": ["add_note", "list_notes"]}})
    try:
        hub.start()
        asked = []
        box = ToolBox(DEFAULTS["tools"], confirm=lambda what: asked.append(what) or True, announce=asked.append)
        assert hub.register(box) == 2
        assert "notes__add_note" in [s["function"]["name"] for s in box.specs()]
        out = box.call("notes__add_note", {"title": "ideas", "body": "build a second brain"})
        assert out == "Saved note 'ideas' with 4 words."
        assert asked[0] == "Checking notes."  # announced, then confirmed
        box_no = ToolBox(DEFAULTS["tools"], confirm=lambda w: False, announce=lambda t: None)
        hub.register(box_no)
        assert "declined" in box_no.call("notes__list_notes", {})
    finally:
        hub.close()
