"""Connect Jarvis to Model Context Protocol servers.

Each MCP server (filesystem, git, calendar, browser, notes...) is a separate
program that advertises tools. Jarvis starts them, asks what tools they offer,
and registers each one in the ToolBox as `<server>__<tool>`, so the LLM can
call them like any built-in tool.

The MCP SDK is async while the rest of Jarvis is plain threads, so all MCP
sessions live on one background asyncio loop and calls are bridged with
run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from contextlib import AsyncExitStack
from typing import Any

from jarvis.tools import Tool, ToolBox

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]")


def tool_name(server: str, tool: str) -> str:
    return _SAFE_NAME.sub("_", f"{server}__{tool}")[:64]


def result_text(result: Any, max_chars: int = 4000) -> str:
    """Flatten an MCP CallToolResult into plain text for the LLM."""
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else f"[{getattr(item, 'type', 'item')} content]")
    out = "\n".join(parts).strip() or "(no output)"
    if getattr(result, "is_error", None) or getattr(result, "isError", False):
        out = f"Error: {out}"
    return out[:max_chars] + ("\n...(truncated)" if len(out) > max_chars else "")


def _schema(tool: Any) -> dict:
    # mcp 2.x renamed inputSchema to input_schema.
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    return schema or {"type": "object", "properties": {}}


class MCPHub:
    def __init__(self, servers: dict[str, dict], timeout: float = 30):
        self.servers = servers or {}
        self.timeout = timeout
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="mcp")
        self.stack = AsyncExitStack()
        self.sessions: dict[str, Any] = {}
        self.tools: dict[str, list[Any]] = {}

    def _run(self, coro, timeout: float | None = None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout or self.timeout)

    def start(self) -> None:
        self.thread.start()
        for name, cfg in self.servers.items():
            if not cfg.get("enabled", True):
                continue
            try:
                self._run(self._connect(name, cfg), timeout=cfg.get("startup_timeout", 60))
                print(f"  [mcp] {name}: {len(self.tools[name])} tools")
            except Exception as exc:
                print(f"  [mcp] {name} failed to start: {exc}")

    async def _connect(self, name: str, cfg: dict) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=cfg["command"],
            args=[str(a) for a in cfg.get("args", [])],
            env={**os.environ, **{k: str(v) for k, v in (cfg.get("env") or {}).items()}},
        )
        read, write = await self.stack.enter_async_context(stdio_client(params))
        session = await self.stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = (await session.list_tools()).tools
        allow = set(cfg.get("allow_tools") or [])
        self.sessions[name] = session
        self.tools[name] = [t for t in listed if not allow or t.name in allow]

    def call(self, server: str, tool: str, arguments: dict) -> str:
        result = self._run(self.sessions[server].call_tool(tool, arguments))
        return result_text(result)

    def register(self, box: ToolBox) -> int:
        """Add every discovered MCP tool to the ToolBox. Returns how many."""
        count = 0
        for server, tools in self.tools.items():
            cfg = self.servers[server]
            for t in tools:
                box.register(Tool(
                    name=tool_name(server, t.name),
                    description=f"[{server}] {t.description or t.name}"[:1024],
                    parameters=_schema(t),
                    handler=lambda args, s=server, n=t.name: self.call(s, n, args),
                    confirm=bool(cfg.get("confirm", True)),
                    announce=(lambda args, s=server: f"Checking {s.replace('_', ' ')}.")
                    if cfg.get("announce", True) else None,
                ))
                count += 1
        return count

    def close(self) -> None:
        if not self.thread.is_alive():
            return
        try:
            self._run(self.stack.aclose(), timeout=10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
