# Skills

A skill is a procedure the model reads on demand: `skills/<category>/<name>/SKILL.md`.
At startup the harness puts every skill's name and description into the system
prompt; the body is fetched with `skill__read` only when a task matches, so a
skill costs no context until it is used.

## Writing one

```markdown
---
name: mcp-report                       # kebab-case, must equal the folder name
description: One sentence ending with a period, at most 100 characters.
---

## When to use
## Procedure                           # numbered steps, our tools in backticks: `write_file`
## Pitfalls
```

Rules enforced by the loader and checked in `tests/test_6_startup.py` (a broken skill fails the suite, and
the harness refuses to start): the frontmatter block is present, `name`
matches the folder, `description` is one sentence with a period, the body is
non-empty, names are unique, and the body mentions at least one tool.
