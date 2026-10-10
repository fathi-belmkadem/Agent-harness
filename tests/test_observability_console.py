from io import StringIO

from harness.observability.console import ConsoleSink


def test_console_sink_renders_required_observability_categories():
    stream = StringIO()
    sink = ConsoleSink(stream, color=False)
    events = [
        {"type": "turn_started"},
        {"type": "reason", "iteration": 2, "total_tokens": 12},
        {"type": "act", "tool": "read_file", "ok": True, "duration_ms": 4.2},
        {"type": "act", "tool": "weather__forecast", "ok": False, "duration_ms": 9.0},
        {"type": "delegated", "subagent": "researcher", "ok": True},
        {"type": "permission_decision", "scope": "tool", "tool": "write_file",
         "effective": "deny"},
        {"type": "memory_retrieved", "count": 2},
        {"type": "archive_error", "error": "disk full"},
        {"type": "context_compacted", "tokens_before": 100, "tokens_after": 70},
        {"type": "done"},
    ]

    for event in events:
        sink.record(event)

    output = stream.getvalue()
    for label in ("Agent", "Agent Loop / Step Status", "Tool Calls", "MCP", "Sub-agents",
                  "Permissions", "Memory", "Errors", "Resources"):
        assert f"╭─ {label} " in output
    assert "Run started" in output
    assert "weather__forecast: failed" in output
    assert "Status: completed" in output


def test_console_sink_ignores_unknown_events_and_keeps_panel_width():
    stream = StringIO()
    ConsoleSink(stream, color=False).record({"type": "unknown"})
    assert stream.getvalue() == ""

    ConsoleSink(stream, color=False).record({"type": "turn_started"})
    lines = stream.getvalue().splitlines()
    assert all(len(line) == 41 for line in lines)