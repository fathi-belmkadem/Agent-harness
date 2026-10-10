"""One end-to-end run through the harness with a scripted model. See docs/11_invariants.md for the I-numbers."""

from harness.agent.loop import AgentLoop, Limits
from harness.llm_client import LLMResponse
from harness.memory.compaction import compact, is_marker
from harness.tools import Tool, ToolRegistry
from harness.tools.clarify import CLARIFY_TOOL

from conftest import ScriptedLLM, call, ctx, say, tool_call

def heavy_turns(turns: int) -> list[dict]:
    """Complete turns, each with a 400-character tool result in the middle."""
    messages = []
    for i in range(turns):
        messages += [
            {"role": "user", "content": f"question {i}"},
            {"role": "assistant", "content": None, "tool_calls": [tool_call("read_file", {"path": "x"}, id_=f"c{i}")]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": "R" * 400},
            {"role": "assistant", "content": f"answer {i}"},
        ]
    return messages


def roles(context):
    return [m["role"] for m in context.messages]


def test_the_context_stays_within_budget_and_bad_responses_are_handled(transcript):
    """A long conversation is compacted before the call (I3): user messages
    verbatim, head and tail protected, markers keep alternation (I2) and are
    never re-compacted. Then the responses the loop cannot accept: an empty
    reply, a call once the budget is spent, and a question for the user."""
    context = ctx()
    context.messages.extend(heavy_turns(12))
    context.add_user("the latest question")
    asked = [m["content"] for m in context.messages if m["role"] == "user"]
    head = [dict(m) for m in context.messages[:2]]
    llm = ScriptedLLM([say("done")])

    AgentLoop(llm=llm, tools=ToolRegistry(), transcript=transcript,
              limits=Limits(context_budget_tokens=600, tail_token_budget=200)).run(context)

    sent = llm.calls[0]["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"] == asked
    assert sent[1:3] == head and sent[-1]["content"] == "the latest question"
    markers = [m for m in sent if is_marker(m)]
    assert markers and all(m["role"] == "assistant" for m in markers)
    assert all(not is_marker(sent[i - 1]) for i, m in enumerate(sent) if m["role"] == "tool")
    event = transcript.of("context_compacted")[0]
    assert event["tokens_before"] > event["tokens_after"]
    messages = heavy_turns(10)
    compact(messages, budget_tokens=200, tail_token_budget=100)
    once = [dict(m) for m in messages]
    compact(messages, budget_tokens=200, tail_token_budget=100)
    assert messages == once, "markers are not compacted again"

    empty = AgentLoop(llm=ScriptedLLM([say("   ")]), tools=ToolRegistry(), transcript=transcript).run(ctx("go"))
    assert empty.failed and transcript.of("failed")[-1]["error"] == "empty_response"

    ran = []
    tools = ToolRegistry()
    tools.register(Tool(name="echo", description="", parameters={"type": "object", "properties": {}},
                        handler=lambda **kw: ran.append(1) or "ok"))
    tools.register(CLARIFY_TOOL)

    def costs(response, tokens=10):
        return LLMResponse(content=response.content, tool_calls=response.tool_calls, total_tokens=tokens)

    textual = say('{"name": "echo", "arguments": {}}')
    over = AgentLoop(llm=ScriptedLLM([costs(call("echo")), costs(textual), say("never")]), tools=tools,
                     transcript=transcript, limits=Limits(max_tokens=15)).run(ctx("go"))
    assert over.budget_exceeded and ran == [1], "the recovered call was not run once the budget was spent"
    assert transcript.of("aborted")[-1]["reason"] == "budget_exceeded"
    paid = AgentLoop(llm=ScriptedLLM([costs(call("echo")), costs(say("the answer"))]), tools=tools, limits=Limits(max_tokens=15))
    assert paid.run(ctx("go")).final_message == "the answer", "an answer that arrives over budget is still returned"

    context = ctx("summarise the code")
    asked_back = AgentLoop(llm=ScriptedLLM([call("clarify", {"question": "Which folder?"}), say("never")]),
                           tools=tools, transcript=transcript).run(context)
    assert asked_back.clarified and asked_back.final_message == "Which folder?"
    assert roles(context) == ["user", "assistant", "tool", "assistant"]
    assert transcript.of("done")[-1]["clarified"] is True
