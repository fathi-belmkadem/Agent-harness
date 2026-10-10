"""Tests for the `everything_ext` wrapper: forwarding plus the local `get_time` tool."""
from datetime import datetime

import pytest

from harness.mcp import MCPClient
from harness.mcp_servers.everything_ext.server import EverythingExtServer, get_time

CHILD_TOOL = {"name": "echo", "description": "Echoes input.", "inputSchema": {"type": "object"}}


class FakeChildTransport:
    """Stands in for the real `server-everything` child process."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def send(self, message):
        method = message["method"]
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [CHILD_TOOL]}}
        if method == "tools/call":
            args = message["params"]["arguments"]
            self.calls.append((message["params"]["name"], args))
            return {"jsonrpc": "2.0", "id": message["id"],
                    "result": {"content": [{"type": "text", "text": f"echo:{args}"}]}}
        return {"jsonrpc": "2.0", "id": message["id"], "result": {}}

    def close(self):
        pass


@pytest.fixture
def server():
    return EverythingExtServer(child=MCPClient(FakeChildTransport()))


def test_get_time_defaults_to_utc():
    result = get_time({})
    assert datetime.fromisoformat(result).utcoffset().total_seconds() == 0


def test_get_time_accepts_a_timezone():
    result = get_time({"timezone": "Asia/Shanghai"})
    assert datetime.fromisoformat(result).utcoffset().total_seconds() == 8 * 3600


def test_get_time_rejects_an_unknown_timezone():
    with pytest.raises(ValueError, match="unknown timezone"):
        get_time({"timezone": "Not/AZone"})


def test_tools_list_merges_child_tools_with_get_time(server, capsys):
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    result = _read_reply(capsys)["result"]
    names = [t["name"] for t in result["tools"]]
    assert names == ["echo", "get_time"]


def test_get_time_call_is_answered_locally_not_forwarded(server, capsys):
    server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "get_time", "arguments": {"timezone": "UTC"}},
    })
    result = _read_reply(capsys)["result"]
    assert "content" in result and not result.get("isError")
    assert server._child._transport.calls == []  # not forwarded to the child


def test_other_tool_calls_are_forwarded_to_the_child(server, capsys):
    server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"msg": "hi"}},
    })
    result = _read_reply(capsys)["result"]
    assert result["content"][0]["text"] == "echo:{'msg': 'hi'}"
    assert server._child._transport.calls == [("echo", {"msg": "hi"})]


def _read_reply(capsys):
    import json
    out = capsys.readouterr().out.strip()
    return json.loads(out)
