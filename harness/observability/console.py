"""Human-readable live observability for the interactive CLI."""
from __future__ import annotations

import sys
from typing import Any, TextIO


class ConsoleSink:
    """Render trace events as small, stable terminal panels.

    Progress goes to stderr so ``--json`` keeps stdout machine-readable. The
    sink is deliberately a normal trace sink: it receives the same redacted
    payload as the transcript and metrics sinks.
    """

    _WIDTH = 41

    def __init__(self, stream: TextIO | None = None, *, color: bool = True) -> None:
        self.stream = stream or sys.stderr
        self.color = color

    def record(self, event: dict[str, Any]) -> None:
        panel = self._panel(event.get("type"), event)
        if panel is None:
            return
        print(panel, file=self.stream, flush=True)

    def _panel(self, kind: str | None, event: dict[str, Any]) -> str | None:
        if kind == "turn_started":
            return self._box("Agent", ["Run started", "Status: running"])
        if kind == "reason":
            step = event.get("iteration", "?")
            tokens = event.get("total_tokens")
            suffix = f" | {tokens} tokens" if tokens is not None else ""
            return self._box("Agent Loop / Step Status", [f"Step {step}: model response{suffix}"])
        if kind == "act":
            tool = str(event.get("tool", "unknown"))
            status = "succeeded" if event.get("ok") else "failed"
            duration = event.get("duration_ms")
            detail = f"{tool}: {status}"
            if duration is not None:
                detail += f" ({duration} ms)"
            title = "MCP" if "__" in tool and not tool.startswith(("memory__", "session__", "delegate__", "skill__")) else "Tool Calls"
            return self._box(title, [detail])
        if kind == "permission_decision":
            resource = event.get("resource", event.get("tool", "unknown"))
            effective = event.get("effective", event.get("decision", "unknown"))
            scope = event.get("scope", "tool")
            title = "MCP" if scope == "server" else "Permissions"
            return self._box(title, [f"{scope} {resource}: {effective}"])
        if kind == "delegated":
            name = event.get("subagent", "unknown")
            status = "succeeded" if event.get("ok") else "failed"
            return self._box("Sub-agents", [f"{name}: {status}"])
        if kind in {"done", "failed", "aborted"}:
            status = {"done": "completed", "failed": "failed", "aborted": "aborted"}[kind]
            detail = f"Status: {status}"
            if event.get("reason"):
                detail += f" ({event['reason']})"
            return self._box("Agent", [detail])
        if kind in {"memory_retrieved", "memory_retrieval_skipped", "memory_error",
                    "turn_archived", "consolidated", "consolidation_error",
                    "session_save_error", "archive_error"}:
            title = "Errors" if kind.endswith("error") else "Memory"
            return self._box(title, [self._memory_detail(kind, event)])
        if kind in {"context_compacted", "context_trimmed"}:
            return self._box("Resources", [self._resource_detail(kind, event)])
        return None

    @staticmethod
    def _memory_detail(kind: str, event: dict[str, Any]) -> str:
        if kind == "memory_retrieved":
            count = event.get("count", 0)
            return f"Retrieved {count} {'memory' if count == 1 else 'memories'}"
        if kind == "memory_retrieval_skipped":
            return f"Retrieval skipped: {event.get('reason', 'not needed')}"
        if kind.endswith("error"):
            return f"{kind}: {event.get('error', 'unknown error')}"
        if kind == "consolidated":
            return "Consolidated memory"
        return "Archived turn"

    @staticmethod
    def _resource_detail(kind: str, event: dict[str, Any]) -> str:
        if kind == "context_compacted":
            return f"Context {event.get('tokens_before', '?')} -> {event.get('tokens_after', '?')} tokens"
        return f"Trimmed {event.get('dropped', 0)} messages"

    def _box(self, title: str, lines: list[str]) -> str:
        width = self._WIDTH
        header = f"╭─ {title} "
        header += "─" * max(0, width - len(header) - 1) + "╮"
        body = [f"│ {self._clip(line, width - 3):<{width - 3}}│" for line in lines]
        footer = f"╰{'─' * (width - 2)}╯"
        return "\n".join([header, *body, footer])

    @staticmethod
    def _clip(value: object, width: int) -> str:
        text = str(value)
        if len(text) <= width:
            return text
        return text[: max(0, width - 3)] + "..."