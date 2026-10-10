# Permissions

`harness/tools/permissions.py`, enforced in `harness/agent/phases/act.py`.
Which MCP servers may connect, which tools an agent may call, and what happens
when permission is not granted.

## The model

One policy, three outcomes, one place it is enforced.

A `Policy` is a default decision plus an ordered list of rules. A rule is a
shell glob over the tool name and a decision. `decide(tool_name)` returns
the first matching rule's decision, or the default, along with which one
applied.

| decision | effect |
|---|---|
| `allow` | the tool runs |
| `deny` | the tool does not run; the model gets an observation saying so |
| `ask` | a human is asked on the terminal; yes runs it, anything else is a deny |

The same `Policy` model is used for two explicit scopes. Server rules are
checked before a transport is created; tool rules are checked before dispatch.

```yaml
permissions:
  servers:
    default: deny
    rules:
      - match: "brave-search"
        decision: ask
  tools:
    default: allow
    rules:
      - match: "brave-search__*"
        decision: deny
```

## Where it is enforced

Every tool call in the system goes through one function, `_dispatch` in the
act phase, whether it came from a normal model reply, from a tool call
recovered out of text, or from a sub-agent. `resolve()` runs there, before
the registry is called. There is no second path.

A denied call becomes `ToolResult(ok=False, "... denied by the permission
policy")` and that text is the observation. The model reads it on the next
iteration and routes around it, the same as any other tool failure. Nothing
raises.

## Ask, and who answers

`ask` needs someone to ask. The CLI passes `terminal_confirm`, which prints
the tool name and its arguments and reads one line; only an explicit `y` or
`yes` is a yes.

When there is no terminal there is no one to ask. A run with `--prompt`,
a Compose run with `-T`, and every sub-agent whose parent has no
terminal: in all of these the confirm callable is `None`, and **`ask`
resolves to `deny`.** A run that cannot ask must not act.

## Every decision is traced

Each decision emits one `permission_decision` event:

```json
{"type": "permission_decision", "scope": "tool", "resource": "export_pdf",
 "tool": "export_pdf", "decision": "ask", "source": "rule:export_pdf",
 "answer": null, "effective": "deny"}
```

`decision` is what the policy said; `effective` is what happened after the
human, or the absence of one, was consulted. Server events use
`scope: "server"` and the server name as `resource`. Metrics count scope,
decision, and effective result.

## Two levels

The top-level policy in `config.yaml` governs the parent agent. Each
sub-agent carries its own policy from its config block, so a role can be
told to ask before a tool the parent may use freely, or denied a tool
entirely. The delegation call itself is a tool in the parent, so
`delegate__*: ask` puts a human gate in front of every delegation. When
delegations run in parallel (`delegation.max_parallel > 1`) a child has no
terminal to ask on, so its `ask` resolves to deny like any run that cannot ask.

This is the lecture's multi-tier model without a tier system: the
combination of which tools a role's subset *contains* and what its policy
*allows* gives read-only, sandbox-edit and gated tiers as configuration.

An unknown decision word, or a rule without `match`, is a `ConfigError` at
startup.

## Not built

Per-argument rules such as "deny writes under this path", remembered
answers ("always allow"), per-user policies. Each is real; none is in the
spec.

## Where the decisions are recorded

ADR-018 in `docs/12_ADR.md`. Tests in `tests/test_4_subagents.py::test_subagents_are_isolated_tools_governed_by_their_own_policy`, including
the recovered-call path and the no-terminal case.
