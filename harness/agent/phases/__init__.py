"""The turn, one phase per module.

Each phase is a plain function over explicit arguments, so it can be tested
without constructing a loop. Tracing is passed in as an `emit` callable, which
keeps the fail-open policy (I6) in one place: the emitter.
"""
from .act import execute_recovered_call, execute_tool_calls
from .context_fit import fit_context
from .finalize import archive_turn, maybe_consolidate, save_session
from .recall import build_recall_block
from .response_check import is_blank, parse_textual_tool_call, tool_to_force, usable_tool_calls

__all__ = [
    "archive_turn",
    "build_recall_block",
    "execute_recovered_call",
    "execute_tool_calls",
    "fit_context",
    "is_blank",
    "maybe_consolidate",
    "parse_textual_tool_call",
    "save_session",
    "tool_to_force",
    "usable_tool_calls",
]
