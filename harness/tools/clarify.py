"""`clarify`: ask the user one question and end the turn.

The alternative to guessing. A model that is unsure otherwise picks an
interpretation and runs with it, and the wrong guess only surfaces after the
tools have acted. Calling `clarify` stops the loop immediately: the question
becomes the turn's final message, traced as `done` with `clarified: true`, and
the next user message answers it. The tool does nothing itself; the loop
recognises the name.
"""
from .registry import tool

CLARIFY = "clarify"

CLARIFY_TOOL = tool(
    CLARIFY,
    "Ask the user one question and stop. Use it when the request is ambiguous in a "
    "way that changes what you would do, instead of guessing.",
    lambda question: "Asked the user. Their next message is the answer.",
    {"question": {"type": "string", "description": "One specific question."}}, ["question"],
)


def clarification(calls: list[dict]) -> str | None:
    """The question, if one of `calls` is a clarify call."""
    import json
    for call in calls:
        if call["function"]["name"] == CLARIFY:
            raw = call["function"].get("arguments")
            try:
                arguments = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            except ValueError:
                arguments = {}
            return str(arguments.get("question", "")).strip() or "Could you clarify what you need?"
    return None
