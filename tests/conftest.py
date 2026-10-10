"""Fakes for the things a test cannot have: the LLM, Redis, an MCP server, the
embedding endpoint. Everything else in a test is the real code."""
import json
import threading
import time

import pytest

from harness.agent.context import Context
from harness.llm_client import LLMResponse
from harness.mcp import PROTOCOL_VERSION
from harness.observability.transcript import Transcript


# -- the model -----------------------------------------------------------------

class ScriptedLLM:
    """Returns queued responses in order (repeating the last) and records every request."""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.calls = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages],
                           "tools": [t["function"]["name"] for t in (tools or [])], **kwargs})
        item = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return item

    def close(self):
        pass


class RoutedLLM:
    """One script per conversation, chosen by a substring of the latest user
    message. For parallel sub-agents, where one shared script would be consumed
    in a nondeterministic order across threads."""

    def __init__(self, routes: dict[str, list]):
        self._routes = {key: ScriptedLLM(script) for key, script in routes.items()}
        self._lock = threading.Lock()

    def chat(self, messages, tools=None, **kwargs):
        latest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        route = next(self._routes[key] for key in self._routes if key in latest)
        with self._lock:
            return route.chat(messages, tools, **kwargs)

    def calls(self, key):
        return self._routes[key].calls

    def close(self):
        pass


def say(text, **tokens) -> LLMResponse:
    return LLMResponse(content=text, tool_calls=None, total_tokens=1, **tokens)


def tool_call(name, args=None, id_="c1") -> dict:
    arguments = args if isinstance(args, str) else json.dumps(args or {})
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": arguments}}


def call(name, args=None, id_="c1") -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[tool_call(name, args, id_)], total_tokens=1)


def ctx(*user_messages, system="S") -> Context:
    context = Context(system_prompt=system)
    for text in user_messages:
        context.add_user(text)
    return context


# -- the trace -----------------------------------------------------------------

class EventLog(Transcript):
    """A transcript that can read back what it wrote."""

    def __init__(self, path):
        super().__init__(path)
        self.path = path

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def of(self, type_) -> list[dict]:
        return [e for e in self.events() if e["type"] == type_]


class ExplodingSink:
    def record(self, event):
        raise RuntimeError("telemetry backend is down")


# -- the embedder ------------------------------------------------------------------

# A four-axis space: the Python fact and the servers question share no word
# stem but sit at cosine 0.9; anything unknown lands on the noise axis.
PYTHON_FACT = "the user prefers Python for backend work"
SERVERS_QUERY = "which language suits servers"
_VECTORS = {
    PYTHON_FACT: [1.0, 0.0, 0.0, 0.0],
    SERVERS_QUERY: [0.9, 0.0, 0.435889894, 0.0],
    "Python": [1.0, 0.0, 0.0, 0.0],
}


class FakeEmbedder:
    def __init__(self):
        self.fails = False
        self.available = True
        self.last_error = None

    def embed(self, text):
        if self.fails:
            self.available = False
            return None
        return _VECTORS.get(text, [0.0, 0.0, 0.0, 1.0])


# -- Redis -------------------------------------------------------------------------

class FakeRedis:
    """Only what RedisSessionStore uses."""

    def __init__(self):
        self.lists = {}

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]

    def delete(self, key):
        self.lists.pop(key, None)

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)

    def pipeline(self):
        redis, ops = self, []

        class Pipeline:
            def delete(self, key):
                ops.append(lambda: redis.delete(key)); return self

            def rpush(self, key, *values):
                ops.append(lambda: redis.rpush(key, *values)); return self

            def execute(self):
                for op in ops:
                    op()
        return Pipeline()

    def ping(self):
        return True

    def close(self):
        pass


# -- an MCP server ---------------------------------------------------------------

WEATHER_TOOL = {
    "name": "get_forecast",
    "description": "Return the forecast for a location.",
    "inputSchema": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]},
}


def rpc_ok(message, result):
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def rpc_error(message, code, text):
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": code, "message": text}}


class FakeTransport:
    """A current-protocol server speaking JSON-RPC 2.0. Records requests."""

    def __init__(self, tools=None):
        self.requests = []
        self._tools = [WEATHER_TOOL] if tools is None else tools

    def send(self, message):
        self.requests.append(message)
        method = message["method"]
        if method == "server/discover":
            return rpc_ok(message, {"resultType": "complete", "supportedVersions": [PROTOCOL_VERSION],
                                    "capabilities": {"tools": {"listChanged": True}}})
        if method == "tools/list":
            return rpc_ok(message, {"resultType": "complete", "tools": self._tools})
        if method == "tools/call":
            return self.call(message)
        return rpc_error(message, -32601, f"method not found: {method}")

    def call(self, message):
        location = message["params"]["arguments"].get("location")
        return rpc_ok(message, {"content": [{"type": "text", "text": f"forecast for {location}"}]})

    def close(self):
        pass


class LegacyTransport(FakeTransport):
    """A 2024-11-05 server: no server/discover, requires notifications/initialized."""

    def __init__(self):
        super().__init__()
        self.initialized = False

    def send(self, message):
        self.requests.append(message)
        method = message["method"]
        if method == "initialize":
            return rpc_ok(message, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "legacy", "version": "1.0"}})
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method == "tools/list" and self.initialized:
            return rpc_ok(message, {"tools": [WEATHER_TOOL]})
        return rpc_error(message, -32601, "Method not found")


# -- fixtures --------------------------------------------------------------------

@pytest.fixture
def transcript(tmp_path) -> EventLog:
    log = EventLog(tmp_path / "t.jsonl")
    yield log
    log.close()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def slow_tool(name, delay=0.0):
    from harness.tools import Tool

    def run(**kw):
        time.sleep(delay)
        return f"{name} ran"
    return Tool(name=name, description="", parameters={"type": "object", "properties": {}}, handler=run)
