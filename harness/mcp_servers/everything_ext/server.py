"""A stdio MCP server that wraps the reference `server-everything` and adds `get_time`.

Speaks the server side of MCP over stdio (the harness connects to this as an
ordinary stdio MCP server). All tool listing/calls are forwarded to a child
`server-everything` process via `MCPClient`/`StdioTransport` -- the same
client the harness itself uses to talk to any MCP server -- except for
`get_time`, which is answered locally instead of forwarded.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...mcp.client import LEGACY_PROTOCOL_VERSION, MCPClient, MCPError, PROTOCOL_VERSION
from ...mcp.transport import StdioTransport

CHILD_COMMAND = ["npx", "-y", "@modelcontextprotocol/server-everything"]

GET_TIME_TOOL = {
    "name": "get_time",
    "description": (
        "Return the current date and time, optionally converted to an IANA "
        "timezone (e.g. 'Asia/Shanghai'). Defaults to UTC."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA timezone name, e.g. 'Asia/Shanghai'. Defaults to UTC.",
            },
        },
    },
}


def get_time(arguments: dict[str, Any]) -> str:
    tz_name = arguments.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown timezone {tz_name!r}") from None
    return datetime.now(tz).isoformat()


class EverythingExtServer:
    """Proxies a child `server-everything` process, plus the local `get_time` tool."""

    def __init__(self, child_command: list[str] | None = None, child: MCPClient | None = None) -> None:
        self._child = child or MCPClient(StdioTransport(child_command or CHILD_COMMAND), name="everything")
        self._child_ready = child is not None

    def _ensure_child(self) -> None:
        if not self._child_ready:
            self._child.discover()
            self._child_ready = True

    def _write(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def _reply(self, id_: Any, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": id_, "result": result})

    def _fail(self, id_: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})

    def _handle_discover(self, id_: Any) -> None:
        self._reply(id_, {
            "resultType": "complete",
            "supportedVersions": [PROTOCOL_VERSION],
            "capabilities": {"tools": {"listChanged": False}},
        })

    def _handle_initialize(self, id_: Any, params: dict[str, Any]) -> None:
        version = params.get("protocolVersion", LEGACY_PROTOCOL_VERSION)
        self._reply(id_, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "everything-ext", "version": "0.1"},
        })

    def _handle_tools_list(self, id_: Any) -> None:
        self._ensure_child()
        try:
            child_tools = self._child.list_tools()
        except (MCPError, Exception):
            child_tools = []
        self._reply(id_, {"tools": [*child_tools, GET_TIME_TOOL]})

    def _handle_tools_call(self, id_: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if name == "get_time":
            try:
                text = get_time(arguments)
            except ValueError as e:
                self._reply(id_, {"isError": True, "content": [{"type": "text", "text": str(e)}]})
            else:
                self._reply(id_, {"content": [{"type": "text", "text": text}]})
            return

        self._ensure_child()
        try:
            text = self._child.call_tool(name, arguments)
        except MCPError as e:
            self._reply(id_, {"isError": True, "content": [{"type": "text", "text": str(e)}]})
            return
        self._reply(id_, {"content": [{"type": "text", "text": text}]})

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        id_ = message.get("id")
        params = message.get("params") or {}

        if method == "server/discover":
            self._handle_discover(id_)
        elif method == "initialize":
            self._handle_initialize(id_, params)
        elif method == "notifications/initialized":
            return  # notification: no reply expected
        elif method == "tools/list":
            self._handle_tools_list(id_)
        elif method == "tools/call":
            self._handle_tools_call(id_, params)
        elif id_ is not None:
            self._fail(id_, -32601, f"method not found: {method}")

    def run(self) -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.handle(message)
        finally:
            self._child.close()


def main() -> None:
    EverythingExtServer().run()
