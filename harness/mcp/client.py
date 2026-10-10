import itertools
from typing import Any

from .transport import MCPTransportError, Transport

PROTOCOL_VERSION = "2026-07-28"

# What deployed servers, including the official reference servers, still speak.
LEGACY_PROTOCOL_VERSION = "2024-11-05"

CLIENT_INFO = {"name": "group4-agent-harness", "version": "0.1"}

# We do not implement elicitation, so we advertise no client capabilities.
CLIENT_CAPABILITIES: dict[str, Any] = {}


class MCPError(Exception):
    """The server returned a JSON-RPC error, or sent something unusable."""


class MCPClient:
    def __init__(self, transport: Transport, name: str = "mcp") -> None:
        self._transport = transport
        self._name = name
        self._ids = itertools.count(1)
        self._capabilities: dict[str, Any] | None = None
        self._negotiated_version: str | None = None

    # -- protocol plumbing ------------------------------------------------

    def _meta(self) -> dict[str, Any]:
        return {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": CLIENT_CAPABILITIES,
        }

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        include_meta: bool = True,
    ) -> dict[str, Any]:
        body = dict(params or {})
        # The per-request _meta block is a 2026-07-28 feature. Older servers do
        # not expect it, so it is only sent while the version is still unknown
        # (the discovery attempt itself) or once negotiated to that revision.
        if include_meta and self._negotiated_version in (None, PROTOCOL_VERSION):
            body["_meta"] = self._meta()

        message = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": body,
        }
        response = self._transport.send(message)
        if response is None:
            raise MCPError(f"{method}: server sent no response")
        if "error" in response:
            error = response["error"]
            raise MCPError(
                f"{method}: {error.get('message', 'unknown error')} "
                f"(code {error.get('code', '?')})"
            )
        if "result" not in response:
            raise MCPError(f"{method}: response had neither result nor error")
        return response["result"]

    # -- the three calls the harness needs --------------------------------

    def discover(self) -> dict[str, Any]:
        """Establish the connection, negotiating whichever protocol the server speaks.

        The 2026-07-28 revision is stateless and uses `server/discover`. Servers
        deployed today — including the official reference servers — still speak
        2024-11-05, which needs the older `initialize` handshake followed by a
        mandatory `notifications/initialized` notice. We try the newer method
        first and fall back, so the harness works against both.
        """
        try:
            result = self._request("server/discover")
        except MCPError as e:
            text = str(e).lower()
            if "method not found" not in text and "-32601" not in text:
                raise
            return self._initialize_legacy()
        except MCPTransportError as e:
            # Some Streamable HTTP servers reject the newer discovery method
            # at the HTTP layer instead of returning a JSON-RPC method error.
            # Fall back to the standard initialize handshake in that case.
            if "400 bad request" not in str(e).lower():
                raise
            return self._initialize_legacy()

        self._negotiated_version = PROTOCOL_VERSION
        self._capabilities = result.get("capabilities", {})

        supported = result.get("supportedVersions", [])
        if supported and PROTOCOL_VERSION not in supported:
            raise MCPError(
                f"server supports {supported}, this client speaks {PROTOCOL_VERSION}"
            )
        return result

    def _initialize_legacy(self) -> dict[str, Any]:
        """The 2024-11-05 handshake: initialize, then notifications/initialized.

        The notification is mandatory — servers reject every later request with
        "Server not initialized" until it arrives.
        """
        result = self._request(
            "initialize",
            {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": CLIENT_CAPABILITIES,
                "clientInfo": CLIENT_INFO,
            },
            include_meta=False,
        )
        self._negotiated_version = result.get("protocolVersion", LEGACY_PROTOCOL_VERSION)
        self._capabilities = result.get("capabilities", {})
        self._notify("notifications/initialized")
        return result

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification: no id, no response expected."""
        self._transport.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    @property
    def protocol_version(self) -> str:
        """Which protocol revision this connection settled on."""
        return self._negotiated_version or PROTOCOL_VERSION

    def supports_tools(self) -> bool:
        """Whether the server advertised the tools primitive."""
        if self._capabilities is None:
            self.discover()
        return "tools" in (self._capabilities or {})

    def list_tools(self) -> list[dict]:
        """tools/list, following pagination to the end. Called once per
        provider load; there is no cache because nothing lists twice."""
        tools: list[dict] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """tools/call, flattened to the single string the loop expects."""
        result = self._request("tools/call", {"name": name, "arguments": arguments})

        if result.get("isError"):
            raise MCPError(f"{name}: {self._flatten(result.get('content', []))}")
        return self._flatten(result.get("content", []))

    @staticmethod
    def _flatten(content: list[dict]) -> str:
        """MCP returns a content array; the tool registry expects one string."""
        parts = []
        for item in content:
            kind = item.get("type")
            if kind == "text":
                parts.append(item.get("text", ""))
            elif kind == "resource":
                resource = item.get("resource", {})
                parts.append(resource.get("text") or f"[resource {resource.get('uri', '?')}]")
            else:
                parts.append(f"[unsupported content type: {kind}]")
        return "\n".join(p for p in parts if p)

    def close(self) -> None:
        self._transport.close()

    def __repr__(self) -> str:
        return f"<MCPClient {self._name!r}>"
