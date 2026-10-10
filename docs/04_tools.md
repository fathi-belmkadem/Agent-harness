# Tools

`harness/tools/`. The registry that merges every tool source, the protocol a
source implements, the built-in filesystem tools, and the sandbox that
bounds them.

## The registry

`ToolRegistry` holds every tool the model can call, from every source, in
one flat namespace. The model sees one list of schemas and calls one name;
where a tool came from is the registry's concern, not the model's.

| method | does |
|---|---|
| `add_provider(provider)` | load every tool a source offers; a name collision between sources is a hard error |
| `register(tool)` | add one tool directly, outside any provider |
| `schemas()` | the OpenAI-format `tools` list for the API call |
| `dispatch(name, arguments)` | run one call and return a `ToolResult` |
| `subset(patterns)` | a new registry holding only the tools whose names match a glob; used to build a sub-agent's tools |
| `names()`, `origin_of(name)` | inspection |

`dispatch` never raises. An unknown name, a missing required argument, or a
handler exception each come back as `ToolResult(ok=False, content=...)`, and
that text becomes the observation the model reads. An unknown name lists
what does exist, so the model can correct itself instead of guessing again.
This is invariant I7.

## The provider protocol

A source of tools implements two things:

```python
class ToolProvider(Protocol):
    @property
    def namespace(self) -> str: ...
    def load(self) -> list[Tool]: ...
```

Four providers exist, each living in the package that owns what it wraps:

| provider | file | tools |
|---|---|---|
| `FilesystemProvider` | `tools/provider.py` | the built-ins below |
| `MCPProvider` | `mcp/provider.py` | one per tool the server advertises, as `<server>__<tool>` |
| `MemoryProvider`, `SessionProvider` | `memory/tools.py` | `memory__*` and `session__*` |
| `SubagentProvider` | `agent/subagents.py` | `delegate__<role>` per configured role |

One rule keeps this free of import cycles: `tools/` imports from nothing
above it. Every other package imports `Tool` from `tools/`; `tools/` never
imports from `agent/`, `memory/` or `mcp/`.

## The built-in tools

Deliberately un-namespaced: they are the harness's own contract with the
model and predate MCP.

| tool | does |
|---|---|
| `create_folder` | make a directory and any missing parents |
| `write_file` | create or overwrite a text file |
| `append_file` | append to an existing file |
| `read_file` | return a file's text |
| `replace_in_file` | replace exactly one occurrence of a string |
| `copy_file` | copy within the workspace |
| `file_exists`, `get_file_info` | inspection |
| `list_directory`, `find_files`, `search_in_files` | navigation and search |
| `export_pdf` | render text or an existing file to PDF, returning a `file://` link |
| `clarify` | ask the user one question and end the turn (`tools/clarify.py`, ADR-023); registered by `startup.py` next to the filesystem tools |

There is no delete. Not a delete tool with a permission on it; no tool. The
spec requires that the agent cannot delete, and the surest way to satisfy
"cannot" is for the capability not to exist.

## The sandbox

Every path argument passes through `resolve_path(root, relative)` in
`tools/sandbox.py` before it touches the filesystem. Absolute paths are
refused. Relative paths are resolved and then checked to still lie under the
root, which is what stops `../../etc/passwd`. A violation is a
`SandboxViolation`, which `dispatch` turns into an observation like any
other error.

The root is `sandbox.root` in config, `workspace/` by default, and is
bind-mounted from the host in Compose so the agent's output is visible
outside the container. The container itself is the second boundary: the
lab's requirement that agent-executed code be isolated from the host.

## PDF export

`export_pdf` in `tools/pdf.py` uses `fpdf2`. It needs a Unicode-capable
font for anything outside ASCII; the Docker image installs `fonts-dejavu`,
and a local run without one falls back to core fonts. One fpdf2 trap is
recorded in the code: a full-width `multi_cell` does not reset the cursor,
and the tool had to set the next position explicitly.

## Permissions

Every dispatch is preceded by a policy decision. See `07_permissions.md`.

## Where the decisions are recorded

Invariant I7 in `docs/11_invariants.md`. The delete-free registry is a lab
requirement rather than a decision, and the tests in
`tests/test_5_tools.py::test_every_tool_source_merges_into_one_sandboxed_registry` pins it.
