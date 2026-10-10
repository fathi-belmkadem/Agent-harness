"""Startup: turn a loaded config into a ready agent loop.

Each step opens one optional dependency and reports what it found. A missing
or unreachable one (Redis, an MCP server, the metrics port) is warned about
and skipped, so the harness still starts with what it has.
"""
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import redis

from .agent import AgentLoop, Context, Limits, MemorySetup
from .agent.prompt_builder import build_system_prompt
from .agent.subagents import SubagentProvider
from .config import AppConfig
from .console import plural, status, warn
from .llm_client import LLMClient
from .memory import (EpisodicArchive, MemoryProvider, RedisSessionStore, SessionProvider,
                     SQLiteMemory, connect as connect_redis)
from .memory.embeddings import EmbeddingClient
from .observability.console import ConsoleSink
from .observability.metrics import MetricsSink, serve as serve_metrics
from .observability.transcript import Transcript
from .skills import SkillProvider, load_skills
from .tools import ToolRegistry
from .tools.clarify import CLARIFY_TOOL
from .tools.permissions import terminal_confirm


@dataclass
class Harness:
    """A ready loop, the conversation it continues, and what to close on exit."""
    loop: AgentLoop
    context: Context
    closeables: list = field(default_factory=list)

    def close(self) -> None:
        for resource in self.closeables:
            if resource is not None:
                resource.close()


def build_harness(config: AppConfig, config_dir: Path, *, new_session: bool, interactive: bool,
                  live_panels: bool = True) -> Harness:
    """`live_panels`: draw each trace event as a panel on stderr while the agent works."""
    config.sandbox.root.mkdir(parents=True, exist_ok=True)
    panels = (ConsoleSink(stream=sys.stderr, color=sys.stderr.isatty()),) if live_panels else ()
    sinks = panels + _start_metrics(config)
    session_store, initial_messages = _open_session(config, new_session)
    memory, embedder, episodic = _open_memory(config, config_dir)
    # Only a real terminal can answer an `ask`; anything else resolves to deny.
    confirm = terminal_confirm if interactive else None
    llm = LLMClient(base_url=config.llm.base_url, model=config.llm.model,
                    access_token=config.llm.access_token, temperature=config.llm.temperature)

    tools, skills, mcp_providers = _build_tools(config, config_dir, memory, episodic,
                                                confirm=confirm, sinks=sinks)
    transcript = Transcript(Path(".transcripts") / f"{int(time.time())}.jsonl")
    loop = _build_loop(config, llm, tools, memory, episodic, session_store,
                       confirm=confirm, sinks=sinks, transcript=transcript)
    if config.subagents:
        _add_subagents(config, loop, llm, tools, memory,
                       confirm=confirm, sinks=sinks, transcript=transcript)

    context = Context(system_prompt=build_system_prompt(skills), messages=initial_messages)
    return Harness(loop, context, [*mcp_providers, llm, loop.memory.consolidation_llm, transcript,
                                   memory, episodic, session_store, embedder])


def _connect_mcp_servers(config, tools: ToolRegistry, *, confirm=None, emit=None) -> list:
    """Connect the configured MCP servers and register their tools.

    A server that is unreachable, missing an environment variable it needs,
    or blocked by `permissions.servers`, is reported and skipped: the harness
    starts with its built-in tools rather than failing because an optional
    dependency is down or denied.
    """
    if not config.mcp_servers:
        return []

    from .mcp import build_mcp_providers

    servers = []
    for server in config.mcp_servers:
        missing = server.missing_env(os.environ)
        if missing:
            warn(f"MCP server {server.name!r} skipped: {', '.join(missing)} not set")
        else:
            servers.append(server)

    def report(name: str, error: Exception) -> None:
        label = "blocked by permission policy" if isinstance(error, PermissionError) else "unavailable"
        warn(f"MCP server {name!r} {label}: {error}")

    providers = build_mcp_providers(
        servers, permissions=config.permissions.servers,
        confirm=confirm, emit=emit, on_error=report,
    )
    for provider in providers:
        try:
            added = tools.add_provider(provider)
            status(f"{provider.namespace}: {plural(len(added), 'tool')}")
        except Exception as e:  # noqa: BLE001 - one bad server must not stop startup
            report(provider.namespace, e)
    return providers


def _open_session(config, new_session: bool):
    """The Redis-backed conversation, or None with an empty history if Redis is down."""
    if not config.session.enabled:
        return None, []
    try:
        store = RedisSessionStore(connect_redis(config.session.url), config.session.session_id)
        messages = [] if new_session else store.load()
    except redis.exceptions.RedisError as e:
        warn(f"Redis unavailable, starting a fresh (non-persistent) session: {e}")
        return None, []
    if messages:
        status(f"session: resumed {plural(len(messages), 'prior message')}")
    return store, messages


def _resolve(path: Path, config_dir: Path) -> Path:
    """Paths in config.yaml are relative to the config file."""
    return path if path.is_absolute() else config_dir / path


def _open_memory(config, config_dir: Path):
    """The facts store, its embedder and the episodic archive: all optional,
    all on one SQLite file."""
    if not config.memory.enabled:
        return None, None, None
    path = _resolve(config.memory.path, config_dir)
    similarity = config.memory.embeddings.min_similarity

    embedder = None
    if config.memory.embeddings.enabled:
        # Same proxy and credential as the chat model.
        embedder = EmbeddingClient(base_url=config.llm.base_url, model=config.memory.embeddings.model,
                                   access_token=config.llm.access_token)
    memory = SQLiteMemory(path, max_entry_length=config.memory.max_entry_length,
                          embeddings=embedder, min_similarity=similarity)
    episodic = None
    if config.memory.episodic.enabled:
        episodic = EpisodicArchive(path, max_message_length=config.memory.episodic.max_message_length,
                                   embeddings=embedder, min_similarity=similarity)
    return memory, embedder, episodic


def _load_skills(config, config_dir: Path) -> list:
    path = _resolve(config.skills.path, config_dir)
    skills = load_skills(path)
    if skills:
        status("skills: " + ", ".join(s.name for s in skills))
    elif not path.is_dir():
        warn(f"skills directory not found: {path}")
    return skills


def _start_metrics(config) -> tuple:
    """Started first: Redis, memory and MCP take seconds, and a scraper should
    find the endpoint up from the first moment the process is (I6)."""
    port = config.observability.metrics_port
    if port is None:
        return ()
    error = serve_metrics(port)
    if error:
        warn(f"metrics endpoint unavailable: {error}")
        return ()
    status(f"metrics: http://0.0.0.0:{port}/metrics")
    return (MetricsSink(),)


def _build_tools(config, config_dir: Path, memory, episodic, *, confirm, sinks):
    """The registry: clarify, skills, the memory tools, then MCP servers."""
    tools = ToolRegistry(sandbox_root=config.sandbox.root)
    tools.register(CLARIFY_TOOL)
    skills = _load_skills(config, config_dir)
    if skills:
        tools.add_provider(SkillProvider(skills))
    if memory is not None:
        tools.add_provider(MemoryProvider(memory))
    if episodic is not None:
        tools.add_provider(SessionProvider(episodic))
    # No loop exists yet, so a denied or unreachable MCP server reaches the
    # panels and Prometheus by recording straight into the sinks.
    def startup_emit(**event):
        for sink in sinks:
            try:
                sink.record(event)
            except Exception:  # noqa: BLE001 - startup telemetry is fail-open
                pass

    mcp_providers = _connect_mcp_servers(config, tools, confirm=confirm, emit=startup_emit)
    return tools, skills, mcp_providers


def _build_loop(config, llm, tools, memory, episodic, session_store, *,
                confirm, sinks, transcript) -> AgentLoop:
    limits = Limits(
        max_iterations=config.llm.max_iterations, max_tokens=config.llm.max_tokens,
        max_seconds=config.llm.max_seconds,
        context_budget_tokens=config.session.context_budget_tokens,
        tail_token_budget=config.compaction.tail_token_budget,
        protect_head_messages=config.compaction.protect_head_messages,
    )
    consolidation_llm = LLMClient(base_url=config.llm.base_url, model=config.memory.consolidation.model,
                                  access_token=config.llm.access_token, temperature=0.0)
    memory_setup = MemorySetup(
        facts=memory, retrieval_limit=config.memory.retrieval_limit,
        episodic=episodic, session_store=session_store, session_id=uuid.uuid4().hex,
        consolidation=config.memory.consolidation, consolidation_llm=consolidation_llm,
    )
    return AgentLoop(
        llm=llm, tools=tools, limits=limits, memory=memory_setup,
        permissions=config.permissions.tools, confirm=confirm,
        max_parallel_tools=config.delegation.max_parallel,
        transcript=transcript, extra_sinks=sinks,
    )


def _add_subagents(config, loop: AgentLoop, llm, tools, memory, *, confirm, sinks, transcript) -> None:
    """After the loop (children nest under its run id) and after MCP (so a
    role's tool globs can match remote tools). The registry is live."""
    parallel = config.delegation.max_parallel
    tools.add_provider(SubagentProvider(
        config.subagents, llm=llm, parent_tools=tools, parent_run_id=loop.trace.run_id,
        emit=loop._log, transcript=transcript, extra_sinks=sinks, memory=memory,
        memory_retrieval_limit=config.memory.retrieval_limit, confirm=confirm,
        max_seconds=config.llm.max_seconds, parallel=parallel > 1,
    ))
    status("sub-agents: " + ", ".join(c.name for c in config.subagents)
            + (f" (up to {parallel} in parallel)" if parallel > 1 else ""))
