"""Prometheus metrics, counted from the same trace events the transcript gets.

Nothing in the loop knows metrics exist (ADR-020). Each metric answers one
question and the dashboard shows each question once. Fail-open (I6): a
malformed event is ignored, and a port that cannot be bound is reported once.
"""
import threading
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, disable_created_metrics, start_http_server

from ..agent.phases.response_check import CLAIM_FAMILIES

# No panel reads the per-series `*_created` timestamps; they only double /metrics.
disable_created_metrics()

# A turn is seconds to minutes, a tool call milliseconds to seconds.
_TURN_BUCKETS = (1, 2, 5, 10, 20, 30, 60, 120, 300)
_TOOL_BUCKETS = (0.005, 0.02, 0.1, 0.5, 1, 2, 5, 15, 60)

_AGENTS = ("parent", "subagent")
_OUTCOMES = ("done", "failed", "aborted")
_RECALL_RESULTS = ("hit", "miss", "skipped")
_FACT_RESULTS = ("stored", "superseded", "rejected", "archived")
_MODEL_ISSUES = ("malformed_tool_call", *CLAIM_FAMILIES)
_ERRORS = ("memory_error", "archive_error", "consolidation_error", "session_save_error")


class MetricsSink:
    def __init__(self, registry=None) -> None:
        kw = {"registry": registry} if registry is not None else {}
        self.active = Gauge("harness_runs_active", "Turns currently executing", ["agent"], **kw)
        self.runs = Counter("harness_runs_total", "Turns that ended, by outcome", ["outcome", "agent"], **kw)
        self.turn_seconds = Histogram("harness_turn_seconds", "Wall time per turn", ["agent"],
                                      buckets=_TURN_BUCKETS, **kw)
        self.llm_calls = Counter("harness_llm_calls_total", "Chat completions requested", ["agent"], **kw)
        self.llm_tokens = Counter("harness_llm_tokens_total", "Tokens reported by the provider", ["direction"], **kw)
        self.tool_calls = Counter("harness_tool_calls_total", "Tool dispatches", ["tool", "status"], **kw)
        self.tool_seconds = Histogram("harness_tool_seconds", "Tool call duration", ["tool"],
                                      buckets=_TOOL_BUCKETS, **kw)
        self.permissions = Counter("harness_permission_decisions_total", "Policy outcomes",
                                   ["scope", "decision", "effective"], **kw)
        self.recalls = Counter("harness_memory_recalls_total", "Automatic recall, once per turn", ["result"], **kw)
        self.facts = Counter("harness_memory_facts_total", "Facts changed by consolidation", ["result"], **kw)
        self.model_issues = Counter("harness_model_issues_total", "Model replies the harness caught and corrected",
                                    ["kind"], **kw)
        self.errors = Counter("harness_errors_total", "Internal failures a run survived", ["kind"], **kw)
        self._lock = threading.Lock()  # parallel sub-agents record from worker threads
        self._preregister()

    def _preregister(self) -> None:
        """Expose every known series at 0 from the start. A series that first
        appears at 1 has no earlier sample, so `increase()` never shows it: a
        single rare event (one error, one claim) would be invisible."""
        for agent in _AGENTS:
            self.active.labels(agent)
            self.llm_calls.labels(agent)
            self.turn_seconds.labels(agent)
            for outcome in _OUTCOMES:
                self.runs.labels(outcome, agent)
        for direction in ("prompt", "completion"):
            self.llm_tokens.labels(direction)
        for scope in ("tool", "server"):
            for decision in ("allow", "deny", "ask"):
                for effective in ("allow", "deny"):
                    self.permissions.labels(scope, decision, effective)
        for counter, values in ((self.recalls, _RECALL_RESULTS), (self.facts, _FACT_RESULTS),
                                (self.model_issues, _MODEL_ISSUES), (self.errors, _ERRORS)):
            for value in values:
                counter.labels(value)

    def record(self, event: dict[str, Any]) -> None:
        try:
            with self._lock:
                self._record(event)
        except Exception:  # noqa: BLE001 - telemetry must never fail a run
            pass

    def _record(self, e: dict[str, Any]) -> None:
        kind = e.get("type")
        agent = "subagent" if e.get("parent_run_id") else "parent"

        if kind == "turn_started":
            self.active.labels(agent).inc()
        elif kind in _OUTCOMES:
            self.active.labels(agent).dec()
            self.runs.labels(kind, agent).inc()
            if _is_number(e.get("duration_ms")):
                self.turn_seconds.labels(agent).observe(e["duration_ms"] / 1000)
        elif kind == "reason":
            self.llm_calls.labels(agent).inc()
            for direction in ("prompt", "completion"):
                if _is_number(e.get(f"{direction}_tokens")):
                    self.llm_tokens.labels(direction).inc(e[f"{direction}_tokens"])
        elif kind == "act":
            tool = str(e.get("tool", "unknown"))
            # Both statuses exist from a tool's first call, so its first error shows.
            self.tool_calls.labels(tool, "ok")
            self.tool_calls.labels(tool, "error")
            self.tool_calls.labels(tool, "ok" if e.get("ok") else "error").inc()
            if _is_number(e.get("duration_ms")):
                self.tool_seconds.labels(tool).observe(e["duration_ms"] / 1000)
        elif kind == "permission_decision":
            self.permissions.labels(str(e.get("scope", "tool")), str(e.get("decision")),
                                    str(e.get("effective"))).inc()
        elif kind == "memory_retrieved":
            self.recalls.labels("hit" if e.get("count") else "miss").inc()
        elif kind == "memory_retrieval_skipped":
            self.recalls.labels("skipped").inc()
        elif kind == "consolidated":
            for result in _FACT_RESULTS:
                if _is_number(e.get(result)) and e[result] > 0:
                    self.facts.labels(result).inc(e[result])
        elif kind == "unbacked_claim":
            self.model_issues.labels(str(e.get("claim", "unknown"))).inc()
        elif kind == "malformed_tool_call":
            self.model_issues.labels("malformed_tool_call").inc()
        elif kind in _ERRORS:
            self.errors.labels(kind).inc()


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def serve(port: int) -> str | None:
    """Expose /metrics on `port`. Returns an error string instead of raising."""
    try:
        start_http_server(port)
        return None
    except OSError as e:
        return f"{type(e).__name__}: {e}"
