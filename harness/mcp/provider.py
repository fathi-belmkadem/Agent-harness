"""Adapts an MCP server into a ToolProvider.

The registry already merges tools from arbitrary sources, so MCP support is an
adapter rather than a new execution path: fetch the server's tool definitions,
wrap each one in a handler that calls tools/call, and hand them over. The loop,
the context and dispatch are untouched.
"""
from __future__ import annotations

from typing import Any, Callable

from ..config import MCPServerConfig
from ..tools.registry import Tool
from ..tools.permissions import Confirm, Policy, resolve
from .client import MCPClient, MCPError
from .transport import MCPTransportError

# Separates the server name from the remote tool name: weather__get_forecast.
NAMESPACE_SEPARATOR = "__"


class MCPProvider:
    """Exposes one MCP server's tools to the registry."""

    def __init__(self, name: str, client: MCPClient) -> None:
        self._name = name
        self._client = client

    @property
    def namespace(self) -> str:
        return self._name

    @property
    def mcp_server(self) -> str:
        return self._name

    def load(self) -> list[Tool]:
        """Fetch the server's current tools, adapted to the harness's Tool type.

        Raises MCPError / MCPTransportError if the server cannot be reached;
        the caller decides whether that is fatal or merely a skipped provider.
        """
        if not self._client.supports_tools():
            return []

        return [self._adapt(definition) for definition in self._client.list_tools()]

    def _adapt(self, definition: dict[str, Any]) -> Tool:
        remote_name = definition["name"]
        return Tool(
            name=f"{self._name}{NAMESPACE_SEPARATOR}{remote_name}",
            description=definition.get("description") or definition.get("title") or remote_name,
            parameters=definition.get("inputSchema") or {"type": "object", "properties": {}},
            handler=self._make_handler(remote_name),
        )

    def _make_handler(self, remote_name: str) -> Callable[..., str]:
        """Bind one remote tool to a callable the registry can invoke.

        Errors are left to propagate: ToolRegistry.dispatch already converts any
        exception into ToolResult(ok=False), so a dead server becomes an
        observation the agent can read and route around rather than a crash.
        """
        def handler(**kwargs: Any) -> str:
            return self._client.call_tool(remote_name, kwargs)

        handler.__name__ = f"mcp_{self._name}_{remote_name}"
        return handler

    def close(self) -> None:
        self._client.close()


def build_mcp_providers(
    server_configs: list[MCPServerConfig],
    permissions: Policy | None = None,
    confirm: Confirm | None = None,
    emit: Callable[..., None] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
) -> list[MCPProvider]:
    """Build a provider per configured server, skipping ones that fail.

    A server being down must not stop the harness from starting: the failure is
    reported through `on_error` and the remaining providers still load. Config
    shape (transport kind, command/url presence) is already validated by
    `MCPServerConfig`, so the only failures expected here are actual
    connection problems.
    """
    from .transport import HttpTransport, StdioTransport

    providers: list[MCPProvider] = []
    for entry in server_configs:
        if not entry.enabled:
            continue

        if permissions is not None and not resolve(
            permissions, entry.name,
            {"transport": entry.transport, "url": entry.url},
            confirm, emit or (lambda **event: None), scope="server",
        ):
            if on_error is not None:
                on_error(entry.name, PermissionError("blocked by the permission policy"))
            continue

        try:
            if entry.transport == "stdio":
                transport = StdioTransport(command=list(entry.command), timeout_s=entry.timeout_s)
            else:
                transport = HttpTransport(url=entry.url, timeout_s=entry.timeout_s, headers=entry.headers)

            client = MCPClient(transport, name=entry.name)
            client.discover()
            providers.append(MCPProvider(entry.name, client))
        except (MCPError, MCPTransportError) as e:
            if on_error is not None:
                on_error(entry.name, e)

    return providers
