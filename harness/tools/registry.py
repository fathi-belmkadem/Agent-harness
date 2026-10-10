from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable


@dataclass
class ToolResult:
    ok: bool
    content: str


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema, sent to the LLM
    handler: Callable[..., Any]


def tool(name: str, description: str, handler: Callable[..., Any],
         properties: dict | None = None, required: list[str] | tuple[str, ...] = ()) -> Tool:
    """A tool whose parameters are a flat JSON-schema object."""
    return Tool(name=name, description=description, handler=handler,
                parameters={"type": "object", "properties": dict(properties or {}),
                            "required": list(required)})


if TYPE_CHECKING:  # avoids a circular import: provider imports Tool from here
    from .provider import ToolProvider


class ToolRegistry:
    """Merges tools from one or more providers and dispatches calls to them."""

    def __init__(self, sandbox_root: Path | None = None) -> None:
        self.sandbox_root = sandbox_root
        self._tools: dict[str, Tool] = {}
        self._origin: dict[str, str] = {}
        self._mcp_servers: dict[str, str] = {}
        self._providers: list["ToolProvider"] = []

        # The built-in filesystem tools come with the sandbox root.
        if sandbox_root is not None:
            from .provider import FilesystemProvider
            self.add_provider(FilesystemProvider(sandbox_root))

    def add_provider(self, provider: "ToolProvider") -> list[str]:
        """Register every tool a provider offers. Returns the names added."""
        self._providers.append(provider)
        return self._load_provider(provider)

    def _load_provider(self, provider: "ToolProvider") -> list[str]:
        added: list[str] = []
        for tool in provider.load():
            existing = self._origin.get(tool.name)
            if existing is not None and existing != provider.namespace:
                raise ValueError(
                    f"tool name collision: '{tool.name}' offered by "
                    f"'{provider.namespace}' is already registered by '{existing}'"
                )
            self._tools[tool.name] = tool
            self._origin[tool.name] = provider.namespace
            if (mcp_server := getattr(provider, "mcp_server", None)) is not None:
                self._mcp_servers[tool.name] = mcp_server
            added.append(tool.name)
        return added

    def subset(self, patterns: list[str]) -> "ToolRegistry":
        """A registry holding only the tools whose names match a pattern.

        The same Tool objects, so no handler is duplicated. Delegation tools
        are never included: a sub-agent cannot delegate, which is what keeps
        the agent tree at depth one.
        """
        from fnmatch import fnmatchcase
        child = ToolRegistry()
        child.sandbox_root = self.sandbox_root
        for name, tool in self._tools.items():
            if name.startswith("delegate__"):
                continue
            if any(fnmatchcase(name, pattern) for pattern in patterns):
                child._tools[name] = tool
                child._origin[name] = self._origin.get(name, "direct")
                if name in self._mcp_servers:
                    child._mcp_servers[name] = self._mcp_servers[name]
        return child

    def origin_of(self, name: str) -> str | None:
        """Which provider owns this tool."""
        return self._origin.get(name)

    def mcp_server_of(self, name: str) -> str | None:
        """Return the MCP server that owns a tool, if it is an MCP tool."""
        return self._mcp_servers.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def register(self, tool: Tool) -> None:
        """Register a single tool directly, outside any provider."""
        self._tools[tool.name] = tool
        self._origin.setdefault(tool.name, "direct")

    def register_mcp_server(self, tool_name: str, server_name: str) -> None:
        """Record MCP ownership for a tool already registered in the registry."""
        if tool_name in self._tools:
            self._mcp_servers[tool_name] = server_name

    def schemas(self) -> list[dict]:

        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def dispatch(self, name: str, arguments: dict) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            # Naming what does exist gives the model a way to correct itself on
            # the next step instead of guessing again.
            return ToolResult(
                ok=False,
                content=f"Unknown tool: {name}. Available tools: {', '.join(self.names())}",
            )

        if missing := [
            p for p in tool.parameters.get("required", []) if p not in arguments
        ]:
            return ToolResult(ok=False, content=f"Missing required argument(s): {', '.join(missing)}")

        try:
            result = tool.handler(**arguments)
            return ToolResult(ok=True, content=str(result))
        except Exception as e:  # noqa: BLE001 — tool errors must surface to the LLM, not crash the loop
            return ToolResult(ok=False, content=f"{type(e).__name__}: {e}")
