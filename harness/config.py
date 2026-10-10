"""Configuration: one frozen dataclass per block of config.yaml.

Each dataclass holds its defaults and validates itself in `__post_init__`;
`_from_block` fills any of them from its YAML block. Secrets come from the
environment and are refused in YAML. Only `llm` and `sandbox` are required.
"""
import os
import types
import typing
from dataclasses import MISSING, dataclass, fields, replace
from pathlib import Path

import yaml

from .tools.permissions import PermissionsConfig, Policy, load_permissions, load_policy


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _block(raw: dict, key: str) -> dict:
    """The sub-mapping under `key`; an absent or null block reads as empty."""
    return raw.get(key) or {}


def _cast(type_, value):
    if isinstance(type_, types.UnionType):  # `int | None` -> int
        type_ = next(t for t in type_.__args__ if t is not type(None))
    origin = typing.get_origin(type_)
    if origin is not None:  # tuple[str, ...] -> tuple, dict[str, str] -> dict
        return origin(value)
    return type_(value) if type_ in (int, float, str, bool, Path) else value


def _from_block(cls, raw: dict, where: str, **given):
    """Build `cls` from a YAML block: keys in `given` win, missing keys take the
    dataclass default, and a missing required key is a ConfigError."""
    values = dict(given)
    for f in fields(cls):
        if f.name in values or not f.init:
            continue
        if raw.get(f.name) is not None:
            values[f.name] = _cast(f.type, raw[f.name])
        elif f.default is MISSING and f.default_factory is MISSING:
            raise ConfigError(f"Missing required config key: {where}.{f.name}")
    return cls(**values)


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    access_token: str
    temperature: float = 0.2
    max_iterations: int = 15
    max_tokens: int | None = None
    max_seconds: float | None = None


@dataclass(frozen=True)
class SandboxConfig:
    root: Path


@dataclass(frozen=True)
class SessionConfig:
    """The running conversation, in Redis. `REDIS_URL` overrides `url`."""
    enabled: bool = True
    url: str = "redis://localhost:6379/0"
    session_id: str = "default"
    context_budget_tokens: int | None = None

    def __post_init__(self):
        _check(self.context_budget_tokens is None or self.context_budget_tokens >= 1,
               "session.context_budget_tokens must be positive")


@dataclass(frozen=True)
class CompactionConfig:
    protect_head_messages: int = 2
    tail_token_budget: int = 6000

    def __post_init__(self):
        _check(self.protect_head_messages >= 0, "compaction.protect_head_messages cannot be negative")
        _check(self.tail_token_budget >= 0, "compaction.tail_token_budget cannot be negative")


@dataclass(frozen=True)
class ConsolidationConfig:
    """Once `every_n_turns` archived turns are unsummarized, `model` extracts
    facts from up to `max_turns_per_pass` of them; then unpinned facts unused
    for `archive_after_days` are archived. Nothing is deleted."""
    enabled: bool = True
    every_n_turns: int = 5
    max_turns_per_pass: int = 20
    model: str = "qwen3-next-80b-a3b-instruct"
    archive_after_days: int = 45

    def __post_init__(self):
        _check(self.every_n_turns >= 1, "memory.consolidation.every_n_turns must be positive")
        _check(self.max_turns_per_pass >= self.every_n_turns,
               "memory.consolidation.max_turns_per_pass must be at least every_n_turns")
        _check(self.archive_after_days >= 1, "memory.consolidation.archive_after_days must be positive")


@dataclass(frozen=True)
class EmbeddingsConfig:
    """`min_similarity` 0.30 sits between measured irrelevant (<= 0.21) and
    correct (0.32-0.53) matches."""
    enabled: bool = True
    model: str = "octen-embedding-8b"
    min_similarity: float = 0.30

    def __post_init__(self):
        _check(0 <= self.min_similarity <= 1, "memory.embeddings.min_similarity must be between 0 and 1")


@dataclass(frozen=True)
class EpisodicConfig:
    enabled: bool = True
    max_message_length: int = 4000

    def __post_init__(self):
        _check(self.max_message_length >= 1, "memory.episodic.max_message_length must be positive")


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    path: Path = Path(".memory/memory.sqlite3")
    retrieval_limit: int = 5
    max_entry_length: int = 2000
    consolidation: ConsolidationConfig = ConsolidationConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
    episodic: EpisodicConfig = EpisodicConfig()

    def __post_init__(self):
        _check(1 <= self.retrieval_limit <= 10, "memory.retrieval_limit must be between 1 and 10")
        _check(self.max_entry_length >= 1, "memory.max_entry_length must be positive")


@dataclass(frozen=True)
class MCPServerConfig:
    """`command` is for the stdio transport, `url` and `headers` for http. A
    server missing one of `requires_env` is skipped at boot."""
    name: str
    transport: str = "stdio"
    command: tuple[str, ...] = ()
    url: str | None = None
    headers: dict[str, str] | None = None
    timeout_s: float = 30.0
    enabled: bool = True
    requires_env: tuple[str, ...] = ()

    def __post_init__(self):
        _check(self.transport in {"stdio", "http"},
               f"MCP server {self.name!r}: unknown transport {self.transport!r} (use 'stdio' or 'http')")
        _check(self.transport != "stdio" or bool(self.command),
               f"MCP server {self.name!r}: stdio transport needs a 'command'")
        _check(self.transport != "http" or bool(self.url),
               f"MCP server {self.name!r}: http transport needs a 'url'")

    def missing_env(self, env: dict) -> list[str]:
        return [name for name in self.requires_env if not env.get(name)]


@dataclass(frozen=True)
class SubagentConfig:
    """One role the parent can delegate to: the same loop with its own
    prompt, a tool subset, its own cap and its own policy."""
    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] = ()
    max_iterations: int = 8
    permissions: Policy = Policy()

    def __post_init__(self):
        _check(self.name.isidentifier(),
               f"subagent name must be an identifier (it becomes a tool name): {self.name!r}")
        _check(bool(self.tools),
               f"subagent {self.name!r} lists no tools; a sub-agent with nothing to do is a prompt")


@dataclass(frozen=True)
class DelegationConfig:
    """Above 1, independent `delegate__*` calls in one response run
    concurrently, and children cannot prompt (their `ask` resolves to deny)."""
    max_parallel: int = 1

    def __post_init__(self):
        _check(self.max_parallel >= 1, "delegation.max_parallel must be at least 1")


@dataclass(frozen=True)
class SkillsConfig:
    path: Path = Path("skills")  # relative to the config file


@dataclass(frozen=True)
class ObservabilityConfig:
    metrics_port: int | None = None  # None disables the Prometheus endpoint

    def __post_init__(self):
        _check(self.metrics_port is None or 1 <= self.metrics_port <= 65535,
               "observability.metrics_port must be a valid port")


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig
    sandbox: SandboxConfig
    session: SessionConfig = SessionConfig()
    compaction: CompactionConfig = CompactionConfig()
    memory: MemoryConfig = MemoryConfig()
    mcp_servers: tuple[MCPServerConfig, ...] = ()
    permissions: PermissionsConfig = PermissionsConfig()
    subagents: tuple[SubagentConfig, ...] = ()
    delegation: DelegationConfig = DelegationConfig()
    skills: SkillsConfig = SkillsConfig()
    observability: ObservabilityConfig = ObservabilityConfig()


def _parse_memory(raw: dict) -> MemoryConfig:
    return _from_block(
        MemoryConfig, raw, "memory",
        consolidation=_from_block(ConsolidationConfig, _block(raw, "consolidation"), "memory.consolidation"),
        embeddings=_from_block(EmbeddingsConfig, _block(raw, "embeddings"), "memory.embeddings"),
        episodic=_from_block(EpisodicConfig, _block(raw, "episodic"), "memory.episodic"),
    )


def _parse_mcp_servers(raw: dict) -> tuple[MCPServerConfig, ...]:
    servers = []
    for entry in raw.get("servers") or []:
        _check(isinstance(entry, dict) and "name" in entry, f"Each MCP server needs a 'name': {entry!r}")
        servers.append(_from_block(MCPServerConfig, entry, f"mcp server {entry['name']!r}"))
    return tuple(servers)


def _parse_subagents(entries: list) -> tuple[SubagentConfig, ...]:
    configs = []
    for entry in entries:
        _check(isinstance(entry, dict) and bool(entry.get("name")) and bool(entry.get("system_prompt")),
               f"Each subagent needs 'name' and 'system_prompt': {entry!r}")
        name = str(entry["name"])
        try:
            permissions = load_policy(entry.get("permissions"))
        except ValueError as e:
            raise ConfigError(f"subagent {name!r}: {e}") from e
        configs.append(_from_block(
            SubagentConfig, entry, f"subagent {name!r}",
            name=name,
            description=str(entry.get("description", "")).strip() or f"Delegate a task to the {name} sub-agent.",
            system_prompt=str(entry["system_prompt"]).strip(),
            permissions=permissions,
        ))
    names = [c.name for c in configs]
    for name in names:
        _check(names.count(name) == 1, f"duplicate subagent name: {name!r}")
    return tuple(configs)


def _access_token(raw: dict, env: dict) -> str:
    _check("access_token" not in _block(raw, "llm"),
           "access_token must not be set in config.yaml — "
           "set the AGENT_ACCESS_TOKEN environment variable instead.")
    token = env.get("AGENT_ACCESS_TOKEN")
    _check(bool(token), "AGENT_ACCESS_TOKEN environment variable is required.")
    return token


def load_config(path: str | Path, env: dict | None = None) -> AppConfig:
    env = os.environ if env is None else env
    path = Path(path)
    _check(path.exists(), f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    llm = _from_block(LLMConfig, _block(raw, "llm"), "llm", access_token=_access_token(raw, env))
    if env.get("LLM_BASE_URL"):
        llm = replace(llm, base_url=env["LLM_BASE_URL"])
    sandbox = _from_block(SandboxConfig, _block(raw, "sandbox"), "sandbox")
    session = _from_block(SessionConfig, _block(raw, "session"), "session")
    if env.get("REDIS_URL"):
        session = replace(session, url=env["REDIS_URL"])
    # `servers` gates which MCP servers are connected, `tools` every dispatched call.
    try:
        permissions = load_permissions(raw.get("permissions"))
    except ValueError as e:
        raise ConfigError(str(e)) from e

    return AppConfig(
        llm=llm,
        sandbox=replace(sandbox, root=sandbox.root.resolve()),
        session=session,
        compaction=_from_block(CompactionConfig, _block(raw, "compaction"), "compaction"),
        memory=_parse_memory(_block(raw, "memory")),
        mcp_servers=_parse_mcp_servers(_block(raw, "mcp")),
        permissions=permissions,
        subagents=_parse_subagents(raw.get("subagents") or []),
        delegation=_from_block(DelegationConfig, _block(raw, "delegation"), "delegation"),
        skills=_from_block(SkillsConfig, _block(raw, "skills"), "skills"),
        observability=_from_block(ObservabilityConfig, _block(raw, "observability"), "observability"),
    )
