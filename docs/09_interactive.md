# Interactive terminal input

`harness/interactive.py` is the terminal input adapter used by the CLI REPL and
permission confirmations. It keeps terminal editing details out of
`harness/cli.py` and `harness/tools/permissions.py` while preserving the same
prompt semantics in interactive and scripted environments.

## Responsibilities

`InteractivePrompt` provides one `prompt(message)` operation:

- When stdin and output are TTYs, it uses `prompt_toolkit.PromptSession`.
- Arrow keys, history, Home/End, Delete, Ctrl-C and Ctrl-D are handled by the
  terminal editor instead of being returned as escape sequences.
- When either stream is not a TTY, it falls back to Python's `input()` so pipes,
  tests and one-shot automation remain simple.
- A prompt session is reused for the lifetime of the adapter, so command
  history is available between REPL turns.

The module-level `prompt()` function uses one process-level default adapter.
The REPL creates its own adapter so its session is explicit and testable.

## ANSI prompt handling

The CLI builds the `you>` prompt with `colorama`. Those color codes are ANSI
escape sequences, not ordinary prompt text. Before passing a TTY prompt to
prompt_toolkit, `InteractivePrompt` wraps it in `prompt_toolkit`'s `ANSI`
formatted-text object. This renders the color instead of showing literal text
such as `^[[32m`.

For non-TTY input, ANSI sequences are removed before the fallback `input()`
call. This keeps redirected output and tests readable.

## Call flow

```text
cli.repl()
    |
    v
InteractivePrompt.prompt("you> ")
    |
    +-- TTY      -> PromptSession -> editable terminal line
    |
    +-- non-TTY  -> input()       -> pipe or scripted input
```

Permission checks use the same adapter through `terminal_confirm()`:

```text
resolve(permission)
    |
    +-- allow/deny -> continue without a prompt
    |
    +-- ask        -> interactive.prompt("allow ...? [y/N]")
```

An `ask` decision still resolves to deny when no confirmation callback is
available. This preserves the non-interactive fail-closed behavior described
in `07_permissions.md` and `12_ADR.md`.

## Docker and terminal requirements

The container must have a pseudo-TTY for editable input:

```bash
docker compose up -d --build
docker compose attach agent-harness
```

Do not use `docker compose run -T` for the REPL because `-T` disables the
pseudo-TTY. One-shot mode does not need a TTY:

```bash
docker compose run --rm agent-harness --prompt "check the workspace" --json
```

The dependency is pinned in `requirements.txt`:

```text
prompt_toolkit==3.0.51
```

## Tests

`tests/test_interactive.py` covers the TTY PromptSession path, the non-TTY
fallback, ANSI formatting and ANSI stripping. Existing CLI and permission tests
continue to exercise the adapter through the normal application paths.
