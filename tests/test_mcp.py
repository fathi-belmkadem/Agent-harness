"""Tests for the MCP client and provider.

A fake transport stands in for a real server: it speaks the same JSON-RPC 2.0
messages an MCP server would, so the client is exercised end to end (request
shape, _meta fields, pagination, caching, error mapping) without a subprocess
or a network.
"""
from pathlib import Path

import pytest

from harness.mcp import MCPClient, MCPError, MCPProvider, PROTOCOL_VERSION
from harness.mcp.transport import MCPTransportError
from harness.tools import ToolRegistry

WEATHER_TOOL = {
    "name": "get_forecast",
    "title": "Weather Forecast",
    "description": "Return the forecast for a location.",
    "inputSchema": {
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
    },
}


class FakeTransport:
    """An in-process MCP server. Records requests, returns scripted results."""

    def __init__(self, tools=None, capabilities=None, pages=None):
        self.requests: list[dict] = []
        self._tools = [WEATHER_TOOL] if tools is None else tools
        self._capabilities = {"tools": {"listChanged": True}} if capabilities is None else capabilities
        self._pages = pages
        self.closed = False
        self.call_count = 0

    def send(self, message):
        self.requests.append(message)
        method = message["method"]

        if method == "server/discover":
            return self._ok(message, {
                "resultType": "complete",
                "supportedVersions": [PROTOCOL_VERSION],
                "capabilities": self._capabilities,
            })

        if method == "tools/list":
            if self._pages is not None:
                cursor = message["params"].get("cursor")
                page = self._pages[0] if cursor is None else self._pages[1]
                return self._ok(message, page)
            result = {"resultType": "complete", "tools": self._tools}
            return self._ok(message, result)

        if method == "tools/call":
            self.call_count += 1
            args = message["params"]["arguments"]
            return self._ok(message, {
                "content": [{"type": "text", "text": f"forecast for {args.get('location')}"}]
            })

        return {"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": f"method not found: {method}"}}

    @staticmethod
    def _ok(message, result):
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def close(self):
        self.closed = True


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


# -- protocol conformance -------------------------------------------------

def test_every_request_carries_the_required_meta_fields():
    transport = FakeTransport()
    MCPClient(transport).list_tools()

    meta = transport.requests[-1]["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == PROTOCOL_VERSION
    assert "io.modelcontextprotocol/clientInfo" in meta
    assert "io.modelcontextprotocol/clientCapabilities" in meta


def test_requests_are_valid_jsonrpc_with_unique_ids():
    transport = FakeTransport()
    client = MCPClient(transport)
    client.discover()
    client.list_tools()

    ids = [r["id"] for r in transport.requests]
    assert all(r["jsonrpc"] == "2.0" for r in transport.requests)
    assert len(ids) == len(set(ids)), "JSON-RPC ids must be unique per request"


def test_discover_reports_server_capabilities():
    client = MCPClient(FakeTransport())
    assert client.supports_tools() is True


def test_a_server_without_tools_is_detected():
    client = MCPClient(FakeTransport(capabilities={"resources": {}}))
    assert client.supports_tools() is False


def test_tools_list_follows_pagination():
    pages = [
        {"tools": [WEATHER_TOOL], "nextCursor": "page2"},
        {"tools": [{**WEATHER_TOOL, "name": "get_alerts"}]},
    ]
    tools = MCPClient(FakeTransport(pages=pages)).list_tools()
    assert [t["name"] for t in tools] == ["get_forecast", "get_alerts"]


def test_a_jsonrpc_error_becomes_an_mcp_error():
    client = MCPClient(FakeTransport())
    with pytest.raises(MCPError, match="method not found"):
        client._request("prompts/list")


def test_content_array_is_flattened_to_text():
    result = MCPClient(FakeTransport()).call_tool("get_forecast", {"location": "Passau"})
    assert result == "forecast for Passau"


# -- the provider ---------------------------------------------------------

def test_mcp_tools_are_namespaced_and_adapted():
    provider = MCPProvider("weather", MCPClient(FakeTransport()))
    tools = provider.load()

    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "weather__get_forecast"
    assert tool.description == "Return the forecast for a location."
    assert tool.parameters["required"] == ["location"]


def test_provider_tools_work_through_the_registry(workspace):
    transport = FakeTransport()
    registry = ToolRegistry(sandbox_root=workspace)
    registry.add_provider(MCPProvider("weather", MCPClient(transport)))

    assert len(registry.names()) == 13
    assert registry.origin_of("weather__get_forecast") == "weather"
    assert registry.mcp_server_of("weather__get_forecast") == "weather"

    result = registry.dispatch("weather__get_forecast", {"location": "Passau"})
    assert result.ok is True
    assert result.content == "forecast for Passau"
    assert transport.call_count == 1


def test_namespacing_prevents_shadowing_a_builtin(workspace):
    """A server offering 'read_file' must not replace the sandboxed one."""
    transport = FakeTransport(tools=[{**WEATHER_TOOL, "name": "read_file"}])
    registry = ToolRegistry(sandbox_root=workspace)
    registry.add_provider(MCPProvider("rogue", MCPClient(transport)))

    assert registry.origin_of("read_file") == "builtin"
    assert registry.origin_of("rogue__read_file") == "rogue"


def test_a_dead_server_becomes_a_tool_error_not_a_crash(workspace):
    """dispatch's no-crash invariant must hold for remote tools too."""
    class DeadTransport(FakeTransport):
        def send(self, message):
            if message["method"] == "tools/call":
                raise MCPTransportError("MCP server exited with code 1")
            return super().send(message)

    registry = ToolRegistry(sandbox_root=workspace)
    registry.add_provider(MCPProvider("weather", MCPClient(DeadTransport())))

    result = registry.dispatch("weather__get_forecast", {"location": "Passau"})
    assert result.ok is False
    assert "exited with code 1" in result.content


# -- protocol negotiation against older servers ---------------------------

class LegacyTransport:
    """A 2024-11-05 server: no server/discover, requires notifications/initialized."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.initialized = False

    def send(self, message):
        self.requests.append(message)
        method = message["method"]

        if method == "server/discover":
            return {"jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": -32601, "message": "Method not found"}}

        if method == "initialize":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "legacy", "version": "1.0"},
            }}

        if method == "notifications/initialized":
            self.initialized = True
            return None

        if not self.initialized:
            return {"jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": -32002, "message": "Server not initialized"}}

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": message["id"],
                    "result": {"tools": [WEATHER_TOOL]}}

        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": message["id"],
                    "result": {"content": [{"type": "text", "text": "legacy ok"}]}}

        return {"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": "Method not found"}}

    def close(self):
        pass


def test_client_falls_back_to_the_legacy_handshake():
    """Deployed servers still speak 2024-11-05; we must negotiate down to them."""
    transport = LegacyTransport()
    client = MCPClient(transport)
    client.discover()

    assert client.protocol_version == "2024-11-05"
    methods = [r["method"] for r in transport.requests]
    assert methods[:3] == ["server/discover", "initialize", "notifications/initialized"]


def test_legacy_initialized_notification_is_sent_before_other_calls():
    """Skipping it makes servers reject everything with 'Server not initialized'."""
    transport = LegacyTransport()
    client = MCPClient(transport)
    client.discover()

    assert transport.initialized is True
    assert [t["name"] for t in client.list_tools()] == ["get_forecast"]


def test_legacy_requests_carry_no_meta_block():
    """_meta is a 2026-07-28 field; older servers do not expect it."""
    transport = LegacyTransport()
    client = MCPClient(transport)
    client.discover()
    client.list_tools()

    listed = next(r for r in transport.requests if r["method"] == "tools/list")
    assert "_meta" not in listed["params"]


def test_is_error_results_become_tool_errors(workspace):
    """A server reporting isError must not look like a successful call."""
    class ErroringTransport(FakeTransport):
        def send(self, message):
            if message["method"] == "tools/call":
                return {"jsonrpc": "2.0", "id": message["id"], "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "upstream API rejected the request"}],
                }}
            return super().send(message)

    registry = ToolRegistry(sandbox_root=workspace)
    registry.add_provider(MCPProvider("weather", MCPClient(ErroringTransport())))

    result = registry.dispatch("weather__get_forecast", {"location": "Passau"})
    assert result.ok is False
    assert "upstream API rejected" in result.content
