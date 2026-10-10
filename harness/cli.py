import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv

from .agent import AgentLoop, Context, RunResult
from .config import ConfigError, load_config
from .console import plural
from .interactive import InteractivePrompt
from .startup import build_harness


def _repo_root() -> Path:
    """The directory holding the shared `.env`, one level above the project.
    Found by name (the directory holding `config.yaml` and `harness/`), so a
    layout change fails loudly instead of silently pointing one level off."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.yaml").is_file() and (parent / "harness").is_dir():
            return parent.parent
    return here.parents[2]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description="Agentic file-system CLI.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--new-session", action="store_true",
        help="Start a fresh conversation instead of resuming the one persisted in Redis.",
    )
    parser.add_argument(
        "--prompt",
        help="Run a single turn with this prompt and exit, instead of opening the REPL.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="With --prompt, print the run result as JSON on stdout (machine-readable).",
    )
    parser.add_argument(
        "--no-observability", action="store_true",
        help="Disable live Agent observability panels on stderr.",
    )
    return parser


def _status_line(result: RunResult) -> str:
    """A one-line outcome summary, coloured by how the run actually ended."""
    if result.failed:
        colour, label, when = Fore.RED, "✗ failed", "after"
    elif result.budget_exceeded:
        colour, label, when = Fore.YELLOW, "! budget exceeded", "after"
    elif result.hit_iteration_cap:
        colour, label, when = Fore.YELLOW, "! iteration cap reached", "after"
    else:
        colour, label, when = Fore.GREEN, "✓ done", "in"
    steps = plural(result.iterations_used, "iteration")
    return f"{colour}{label}{Style.RESET_ALL} {Style.DIM}{when} {steps}{Style.RESET_ALL}"


def repl(loop: AgentLoop, context: Context, prompt: InteractivePrompt | None = None, *,
         live_panels: bool = False) -> None:
    prompt = prompt or InteractivePrompt()
    print(f"{Fore.CYAN}{Style.BRIGHT}Agent ready.{Style.RESET_ALL} "
          f"{Style.DIM}Type your task, or 'exit' / Ctrl-D to quit.{Style.RESET_ALL}\n")
    while True:
        try:
            user_input = prompt.prompt(f"{Fore.GREEN}{Style.BRIGHT}you>{Style.RESET_ALL} ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print(f"\n{Style.DIM}(interrupted){Style.RESET_ALL}")
            continue
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break

        context.add_user(user_input)

        # The indicator gets its own line: writing it after the prompt and
        # then carriage-returning would erase the user's typed input. With
        # live panels it is skipped: they already show progress, and clearing
        # "the line above" would erase the last panel instead.
        show_progress = sys.stdout.isatty() and not live_panels
        if show_progress:
            print(f"{Style.DIM}thinking...{Style.RESET_ALL}", flush=True)

        result = loop.run(context)

        if show_progress:
            # Move up to the indicator line and clear it.
            print("\033[F\033[K", end="")

        print(f"{Fore.CYAN}{Style.BRIGHT}agent>{Style.RESET_ALL} {result.final_message}")
        print(f"{_status_line(result)}\n")


def one_shot(loop: AgentLoop, context: Context, prompt: str, *, as_json: bool) -> int:
    """Run exactly one turn and return the process exit code.

    The non-interactive entry point: scripted use drives this
    rather than feeding stdin to the REPL, so a run's outcome is a value
    rather than something to scrape off a terminal.
    """
    context.add_user(prompt)
    result = loop.run(context)

    if as_json:
        print(json.dumps({**asdict(result), "run_id": loop.trace.run_id,
                          "turn_id": loop.trace.turn_id}, indent=2))
    else:
        print(result.final_message)
        print(_status_line(result))

    return 1 if result.failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # strip=None: ANSI codes are removed automatically when output is piped.
    colorama_init(autoreset=False, strip=None)
    # One shared .env at the repo root; real environment variables win.
    load_dotenv(_repo_root() / ".env", override=False)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"{Fore.RED}Configuration error:{Style.RESET_ALL} {e}", file=sys.stderr)
        return 1

    live_panels = not args.no_observability
    harness = build_harness(config, Path(args.config).resolve().parent, new_session=args.new_session,
                            interactive=args.prompt is None and sys.stdin.isatty(),
                            live_panels=live_panels)
    try:
        if args.prompt is not None:
            return one_shot(harness.loop, harness.context, args.prompt, as_json=args.json)
        repl(harness.loop, harness.context, live_panels=live_panels)
        return 0
    finally:
        harness.close()


if __name__ == "__main__":
    raise SystemExit(main())
