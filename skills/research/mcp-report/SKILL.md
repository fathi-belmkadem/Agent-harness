---
name: mcp-report
description: Research a topic with memory and web search, write a Markdown report, export it as PDF.
---

## When to use

The user asks for a report, summary or write-up that should draw on what the
project already knows and on current external sources, and wants a file at
the end.

## Procedure

1. `memory__search` for prior decisions or constraints on the topic before
   doing anything else. If a stored decision answers the question, say so
   and build on it rather than re-researching.
2. If a `session__search` hit is likely (the user says "last time", "we
   discussed"), search the archive and `session__read_turn` the relevant
   exchange.
3. Gather external evidence with the search tools present
   (`brave-search__brave_web_search`, then a fetch tool for the page that
   matters). One or two good sources beat ten shallow ones. Note the URL of
   each source you use.
4. Write the report with `write_file` to `reports/<topic>.md`. Structure:
   a two-sentence summary, what the project already decided, what the
   sources add, a recommendation, and a Sources list with URLs.
5. Export with `export_pdf`, passing `source_path` (the file you just wrote),
   never re-typing the content. Report the `file://` link it returns.
6. If the work produced a durable decision, `memory__remember` it with
   `kind: decision` and, if it replaces an earlier one, `supersedes` set to
   that id.

## Pitfalls

- Do not claim a search or a write happened unless the tool call is in this
  turn. If a tool is unavailable, say which one and continue with what is.
- `export_pdf` with `content` re-renders text you already wrote; use
  `source_path`.
- The workspace is sandboxed; paths are relative to it.
