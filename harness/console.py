"""Terminal output shared by startup and the CLI: status lines and warnings."""
import sys

from colorama import Fore, Style


def status(text: str) -> None:
    print(f"{Fore.CYAN}·{Style.RESET_ALL} {text}")


def warn(text: str) -> None:
    print(f"{Fore.YELLOW}! {text}{Style.RESET_ALL}", file=sys.stderr)


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"
