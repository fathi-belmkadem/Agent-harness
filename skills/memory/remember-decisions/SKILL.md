---
name: remember-decisions
description: When and how to store, update and retire long-term memories.
---

## When to use

The user states a preference, makes a project decision, corrects something
you had wrong, or asks you outright to remember something.

## Procedure

1. Store only what is durable: a preference, a decision, a correction, a
   fact about the project. Not the current task, not a summary of this
   conversation, not something the archive already holds as a turn.
2. Pick the `kind`: `preference` (how the user wants things), `decision`
   (something the team chose), `fact` (true about the project or the user),
   `project_context`, or `instruction` (a standing rule from the user).
3. If the user says it must *always* apply, set `pinned: true` so
   maintenance never ages it out.
4. Before storing a fact that *changes* a value you already remembered
   ("now use Python 3.12"), `memory__search` for the old one and pass its id
   as `supersedes`. Do not store a second, contradicting copy.
5. If the user says something you remembered is wrong and there is no
   replacement, `memory__forget` it by id.
6. Only after `memory__remember` returns an id may you tell the user it is
   remembered. Quote the id.

## Pitfalls

- "I'll remember that" without a tool call is a false statement. Call the
  tool, then reply.
- One memory per fact. Do not bundle three preferences into one entry;
  they age and supersede independently.
