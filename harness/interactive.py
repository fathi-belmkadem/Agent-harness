"""Cross-platform terminal input for the REPL and permission prompts."""
from __future__ import annotations

import sys
import re
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.defaults import create_output


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class InteractivePrompt:
    """Provide editable input when attached to a terminal.

    ``input()`` remains the fallback for pipes, tests, and scripted runs where
    terminal escape sequences must be treated as ordinary input.
    """

    def __init__(self, *, stdin: TextIO | None = None, output: TextIO | None = None) -> None:
        self.stdin = stdin or sys.stdin
        self.output = output or sys.stdout
        self._session: PromptSession[str] | None = None

    @property
    def interactive(self) -> bool:
        return self.stdin.isatty() and self.output.isatty()

    def prompt(self, message: str) -> str:
        if not self.interactive:
            return input(_ANSI_ESCAPE.sub("", message))
        if self._session is None:
            self._session = PromptSession(output=create_output(self.output))
        formatted = ANSI(message) if "\x1b[" in message else message
        return self._session.prompt(formatted, color_depth=ColorDepth.DEFAULT)


_default_prompt = InteractivePrompt()


def prompt(message: str) -> str:
    """Read one line with terminal editing when available."""
    return _default_prompt.prompt(message)