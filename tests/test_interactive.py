from io import StringIO

import harness.interactive as interactive
from harness.interactive import InteractivePrompt


class NonTTY(StringIO):
    def isatty(self):
        return False


class TTY(StringIO):
    def isatty(self):
        return True


def test_non_tty_uses_input_fallback(monkeypatch):
    prompt = InteractivePrompt(stdin=NonTTY(), output=NonTTY())
    monkeypatch.setattr("builtins.input", lambda message: "piped input")

    assert prompt.prompt("you> ") == "piped input"


def test_tty_uses_prompt_session(monkeypatch):
    calls = []

    class FakeSession:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def prompt(self, message, **kwargs):
            calls.append(("prompt", message, kwargs))
            return "editable input"

    monkeypatch.setattr(interactive, "PromptSession", FakeSession)
    monkeypatch.setattr(interactive, "create_output", lambda stream: ("output", stream))
    prompt = InteractivePrompt(stdin=TTY(), output=TTY())

    assert prompt.prompt("you> ") == "editable input"
    assert calls[0][0] == "init"
    assert calls[0][1]["output"] is not prompt.output
    assert calls[1][0] == "prompt"


def test_ansi_prompt_is_formatted_in_tty_and_stripped_without_tty(monkeypatch):
    calls = []

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def prompt(self, message, **kwargs):
            calls.append(message)
            return "ok"

    monkeypatch.setattr(interactive, "PromptSession", FakeSession)
    monkeypatch.setattr(interactive, "create_output", lambda stream: object())
    tty_prompt = InteractivePrompt(stdin=TTY(), output=TTY())
    assert tty_prompt.prompt("\x1b[32myou>\x1b[0m ") == "ok"
    assert calls[0].__class__.__name__ == "ANSI"

    plain_prompt = InteractivePrompt(stdin=NonTTY(), output=NonTTY())
    monkeypatch.setattr("builtins.input", lambda message: message)
    assert plain_prompt.prompt("\x1b[32myou>\x1b[0m ") == "you> "