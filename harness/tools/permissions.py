"""Permission policies shared by MCP server connections and tool calls.

One choke point. Every tool call — normal, recovered-from-text, or made by a
sub-agent — passes through `act._dispatch`, and the decision is made there.
Rules match tool names with shell globs, which fits the existing namespacing:
`brave-search__*` covers one MCP server, `*__*` covers every MCP tool.

`ask` needs someone to ask. When there is no terminal — `--prompt`, a Compose
run with `-T` — it resolves to deny. A run that cannot ask must not act.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Callable

ALLOW = "allow"
DENY = "deny"
ASK = "ask"
DECISIONS = (ALLOW, DENY, ASK)

# Called with (resource_name, arguments); returns True to proceed. None means
# nobody is there to answer.
Confirm = Callable[[str, dict], bool]


@dataclass(frozen=True)
class Policy:
    default: str = ALLOW
    rules: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def decide(self, tool_name: str) -> tuple[str, str]:
        for pattern, decision in self.rules:
            if fnmatchcase(tool_name, pattern):
                return decision, f"rule:{pattern}"
        return self.default, "default"


@dataclass(frozen=True)
class PermissionsConfig:
    """The two permission layers, both backed by the same Policy model."""
    servers: Policy = Policy(default=DENY)
    tools: Policy = Policy()


def load_policy(raw: dict | None) -> Policy:
    raw = raw or {}
    default = str(raw.get("default", ALLOW))
    if default not in DECISIONS:
        raise ValueError(f"permissions.default must be one of {DECISIONS}, got {default!r}")
    rules = []
    for entry in raw.get("rules", []) or []:
        pattern, decision = entry.get("match"), entry.get("decision")
        if not pattern or decision not in DECISIONS:
            raise ValueError(f"permission rule needs 'match' and a decision in {DECISIONS}: {entry!r}")
        rules.append((str(pattern), str(decision)))
    return Policy(default=default, rules=tuple(rules))


def load_permissions(raw: dict | None) -> PermissionsConfig:
    """Load grouped server and tool policies from application configuration."""
    raw = raw or {}
    if "servers" in raw or "tools" in raw:
        return PermissionsConfig(
            servers=load_policy(raw["servers"]) if "servers" in raw else Policy(default=DENY),
            tools=load_policy(raw["tools"]) if "tools" in raw else Policy(),
        )
    # Existing flat configurations controlled tools only.
    return PermissionsConfig(tools=load_policy(raw))


def resolve(
    policy: Policy | None,
    resource_name: str,
    arguments: dict,
    confirm: Confirm | None,
    emit: Callable[..., None],
    scope: str = "tool",
) -> bool:
    """Decide and trace one call. True means dispatch it."""
    if policy is None:
        return True
    decision, source = policy.decide(resource_name)
    event: dict = {"type": "permission_decision", "scope": scope,
                   "resource": resource_name, "decision": decision, "source": source}
    if scope == "tool":
        event["tool"] = resource_name
    if decision == ASK:
        if confirm is None:
            event["answer"] = None
            event["effective"] = DENY
            emit(**event)
            return False
        answer = bool(confirm(resource_name, arguments))
        event["answer"] = answer
        event["effective"] = ALLOW if answer else DENY
        emit(**event)
        return answer
    event["effective"] = decision
    emit(**event)
    return decision == ALLOW


def terminal_confirm(tool_name: str, arguments: dict) -> bool:
    """The CLI's prompt. Anything but an explicit yes is no."""
    import json
    from ..interactive import prompt

    shown = json.dumps(arguments, ensure_ascii=False)
    if len(shown) > 200:
        shown = shown[:197] + "..."
    try:
        answer = prompt(f"  allow {tool_name}({shown})? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")
