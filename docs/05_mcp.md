# MCP

`harness/mcp/`. How external tool servers are connected, discovered, and
made to look like every other tool.

## What it is for

The Model Context Protocol is the standard way a tool server describes its
tools and accepts calls: JSON-RPC over a transport, with a handshake that
negotiates a protocol version, a `tools/list` that returns schemas, and a
`tools/call` that runs one. Any server that speaks it — Brave Search, the
reference "everything" server, one the team writes — plugs in by adding a
block to `config.yaml`. No code changes.

## The three layers

```
config.yaml ──► build_mcp_providers ──► MCPProvider ──► ToolRegistry
                                            │
                                         MCPClient        JSON-RPC, handshake, tools/list, tools/call
                                            │
                                     StdioTransport | HttpTransport
```

**Transport** (`transport.py`) moves JSON-RPC messages. `StdioTransport`
starts the server as a subprocess and talks over its stdin and stdout.
`HttpTransport` POSTs each message to a URL. Both expose `send` and `close`.

The stdio transport reads the server's stdout on a background thread. This
matters: servers push notifications onto the same stream as responses, and a
blind read after a request would consume one of those as the answer. Every
line is queued and the caller picks out the response with its own id.
Notifications are dropped; nothing consumes them.

On Windows, `npx` and other npm shims are `.cmd` files that `CreateProcess`
will not find by bare name. The transport resolves the command through
`shutil.which` first, which applies the same `PATHEXT` rules the shell does.

**Client** (`client.py`) speaks the protocol. `discover()` runs the
handshake: it tries the current revision first and falls back to the
2024-11-05 handshake if the server rejects it, recording which was
negotiated. `list_tools()` follows pagination to the end. `call_tool()`
flattens the server's content array into the one string the loop expects
and raises `MCPError` on a server-reported failure.

**Provider** (`provider.py`) is the adapter to the registry. The configured
server policy must resolve to `allow` before a transport is created.
Only then does `MCPProvider` implement the `ToolProvider` protocol: `load()` calls `list_tools` and
wraps each definition as a `Tool` named `<server>__<tool>`, with a handler
that calls `call_tool`. `build_mcp_providers(configs, on_error)` connects
every configured server and skips any that fail, reporting each.

## Namespacing

Every MCP tool is `<server name>__<remote name>`: `brave-search__brave_web_search`.
Two servers offering a tool with the same name cannot collide, the model can
tell where a tool comes from, and permission rules and sub-agent tool subsets
can address a whole server with one glob, `brave-search__*`.

## Failure is never fatal

| failure | what happens |
|---|---|
| server permission is denied | no process or HTTP discovery is started; the server is blocked |
| a server fails to start or handshake | reported at startup, skipped; the harness runs with its other tools |
| a server drops mid-session | the next call returns `ToolResult(ok=False)`; the model reads it |
| a tool returns `isError` | `MCPError`, converted by the registry into an observation |
| a server sends a notification | dropped |

An optional external dependency being down must not stop the harness from
starting. Brave Search is the common case: its server starts fine without
`BRAVE_API_KEY` and then fails on every call, so its config entry lists
`requires_env: [BRAVE_API_KEY]` and the harness skips it at boot with one
line when the variable is unset.

## `everything`: wrapped with extension tool, not bare

The `everything` entry no longer launches the npm reference server directly.
It runs `harness/mcp_servers/everything_ext`, a thin stdio MCP server that
starts the real `server-everything` as a child process, forwards every
`tools/list`/`tools/call` to it unchanged, and additionally answers one tool
itself: `get_time` (current time, optionally converted to an IANA timezone).
This is the pattern for adding a tool to a server we don't own the source of
without forking it: proxy the child, intercept the one new tool name.

## Configuration

```yaml
mcp:
  servers:
    - name: everything
      transport: stdio
      command: ["python", "-m", "harness.mcp_servers.everything_ext"]
      timeout_s: 60
    - name: brave-search
      transport: http
      url: "http://brave-search:8080/mcp"
      timeout_s: 60
    # - name: remote
    #   transport: http
    #   url: "https://example.org/mcp"
    #   headers: {Authorization: "Bearer ..."}
```

Keys never go in this file. In the Compose setup, the `brave-search` service
starts the HTTP server and reads `BRAVE_API_KEY` from the repo-root `.env`.
Each contributor should use their own key. Sharing search without distributing
keys requires a separately deployed, authenticated MCP gateway.

MCP server permission is configured under `permissions.servers`, while
individual tool permission is configured under `permissions.tools`. A server
must pass the first check before its transport is created; a tool must pass
the second check before `tools/call` is sent.

## What was removed

Tools are listed once, at startup. There is no cache and no "tool list
changed" handling: nothing lists twice.

## Where the decisions are recorded

ADR-018 (permissions apply to MCP tools through the same choke point) in
`docs/12_ADR.md`. The test `tests/test_5_tools.py::test_every_tool_source_merges_into_one_sandboxed_registry` runs against a
fake transport and cover both handshakes, pagination, error conversion, and
the dead-server case.
