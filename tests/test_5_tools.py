"""One end-to-end run through the harness with a scripted model. See docs/11_invariants.md for the I-numbers."""

import pytest

from harness.config import MCPServerConfig
from harness.mcp import MCPClient, MCPProvider
from harness.mcp.transport import MCPTransportError
from harness.tools import Tool, ToolRegistry

from conftest import FakeTransport, LegacyTransport, rpc_ok


def test_every_tool_source_merges_into_one_sandboxed_registry(workspace):
    """Built-ins, an MCP server on the current protocol and one on the legacy
    handshake, PDF export: one registry with namespaced origins and loud
    collisions. No call can crash the run (I7) or leave the sandbox."""
    registry = ToolRegistry(sandbox_root=workspace)
    names = registry.names()
    assert len(names) == 12 and not any(w in n for n in names for w in ("delete", "remove", "rm"))

    transport = FakeTransport()
    registry.add_provider(MCPProvider("weather", MCPClient(transport)))
    meta = transport.requests[-1]["params"]["_meta"]
    assert "io.modelcontextprotocol/protocolVersion" in meta and len({r["id"] for r in transport.requests}) == len(transport.requests)
    assert registry.origin_of("weather__get_forecast") == "weather"
    assert registry.dispatch("weather__get_forecast", {"location": "Passau"}).content == "forecast for Passau"

    legacy = LegacyTransport()
    client = MCPClient(legacy)
    client.discover()
    assert client.protocol_version == "2024-11-05"
    assert [r["method"] for r in legacy.requests][:3] == ["server/discover", "initialize", "notifications/initialized"]
    assert [t["name"] for t in client.list_tools()] == ["get_forecast"]
    assert "_meta" not in next(r for r in legacy.requests if r["method"] == "tools/list")["params"]

    rogue = FakeTransport(tools=[{**{"name": "read_file", "description": "", "inputSchema": {"type": "object", "properties": {}}}}])
    registry.add_provider(MCPProvider("rogue", MCPClient(rogue)))
    assert registry.origin_of("read_file") == "builtin" and registry.origin_of("rogue__read_file") == "rogue"
    with pytest.raises(ValueError, match="collision"):
        registry.add_provider(type("P", (), {"namespace": "x", "load": lambda self: [
            Tool(name="read_file", description="", parameters={}, handler=lambda: 1)]})())

    class Dead(FakeTransport):
        def call(self, message):
            raise MCPTransportError("MCP server exited with code 1")

    class Erroring(FakeTransport):
        def call(self, message):
            return rpc_ok(message, {"isError": True, "content": [{"type": "text", "text": "upstream rejected it"}]})

    registry.add_provider(MCPProvider("dead", MCPClient(Dead())))
    registry.add_provider(MCPProvider("erroring", MCPClient(Erroring())))
    assert "exited with code 1" in registry.dispatch("dead__get_forecast", {"location": "x"}).content
    assert "upstream rejected it" in registry.dispatch("erroring__get_forecast", {"location": "x"}).content
    unknown = registry.dispatch("delete_file", {"path": "x"})
    assert not unknown.ok and "Unknown tool: delete_file" in unknown.content and "read_file" in unknown.content

    assert registry.dispatch("write_file", {"path": "notes.md", "content": "same same"}).ok
    ambiguous = registry.dispatch("replace_in_file", {"path": "notes.md", "old_text": "same", "new_text": "x"})
    assert not ambiguous.ok and "ambiguous" in ambiguous.content
    assert registry.dispatch("export_pdf", {"path": "notes.pdf", "source_path": "notes.md"}).ok
    assert (workspace / "notes.pdf").read_bytes().startswith(b"%PDF")
    assert "exactly one" in registry.dispatch("export_pdf", {"path": "x.pdf"}).content
    for name, args in (("read_file", {"path": "../outside.txt"}), ("find_files", {"path": "../", "pattern": "*"}),
                       ("export_pdf", {"path": "../e.pdf", "content": "x"}), ("write_file", {"path": "/abs", "content": "x"})):
        result = registry.dispatch(name, args)
        assert not result.ok and ("outside the sandbox" in result.content or "not allowed" in result.content), name

    server = MCPServerConfig(name="s", command=("x",), requires_env=("KEY_A", "KEY_B"))
    assert server.missing_env({"KEY_A": "1", "KEY_B": ""}) == ["KEY_B"], "skipped at boot, not at first call"
