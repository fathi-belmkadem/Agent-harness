"""One end-to-end run through the harness with a scripted model. See docs/11_invariants.md for the I-numbers."""

import json
from pathlib import Path

import pytest

from harness.agent.context import Context
from harness.agent.loop import AgentLoop
from harness.agent.prompt_builder import CORE, build_system_prompt
from harness.cli import one_shot
from harness.config import ConfigError, load_config
from harness.llm_client import LLMCallFailed
from harness.skills import SkillProvider, load_skills, parse_skill
from harness.tools import ToolRegistry

from conftest import ScriptedLLM, say


PROJECT = Path(__file__).resolve().parents[1]
ENV = {"AGENT_ACCESS_TOKEN": "t"}


def test_startup_config_skills_and_one_shot(tmp_path, capsys):
    """What runs before the first turn: the shipped config loads and bad
    configs are refused by name; skills go into the prompt once (I1) and are
    read on demand; one-shot mode turns an outcome into an exit code and JSON."""
    shipped = load_config(PROJECT / "config.yaml", env=ENV)
    assert {s.name for s in shipped.subagents} == {"researcher", "writer"} and shipped.delegation.max_parallel == 1
    assert next(s for s in shipped.mcp_servers if s.name == "brave-search").requires_env == ("BRAVE_API_KEY",)
    minimal = "llm:\n  base_url: http://x\n  model: m\nsandbox:\n  root: ws\n"
    for block, match in [
        ("llm:\n  base_url: http://x\n  model: m\n  access_token: leaked\n", "must not be set in config.yaml"),
        ("memory:\n  retrieval_limit: 11\n", "retrieval_limit"),
        ("delegation:\n  max_parallel: 0\n", "max_parallel"),
        ("permissions:\n  default: maybe\n", "permissions.default"),
        ("mcp:\n  servers:\n    - {name: s, transport: stdio}\n", "needs a 'command'"),
        ("subagents:\n  - {name: r, system_prompt: p}\n", "no tools"),
    ]:
        (tmp_path / "c.yaml").write_text(minimal + block, encoding="utf-8")
        with pytest.raises(ConfigError, match=match):
            load_config(tmp_path / "c.yaml", env=ENV)
    with pytest.raises(ConfigError, match="AGENT_ACCESS_TOKEN"):
        (tmp_path / "c.yaml").write_text(minimal, encoding="utf-8")
        load_config(tmp_path / "c.yaml", env={})

    skills = load_skills(PROJECT / "skills")
    assert {s.name for s in skills} >= {"mcp-report", "remember-decisions"}
    prompt = build_system_prompt(skills)
    assert prompt.startswith(CORE) and "- `mcp-report`:" in prompt and prompt == build_system_prompt(skills)
    tools = ToolRegistry()
    tools.add_provider(SkillProvider(skills))
    assert "## Procedure" in tools.dispatch("skill__read", {"name": "mcp-report"}).content
    assert "Available:" in tools.dispatch("skill__read", {"name": "nope"}).content
    bad = tmp_path / "cat" / "alpha" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("---\nname: other\ndescription: no full stop\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="match the folder"):
        parse_skill(bad)

    loop = AgentLoop(llm=ScriptedLLM([say("all done")]), tools=ToolRegistry())
    context = Context(system_prompt="test")
    assert one_shot(loop, context, "do it", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["final_message"] == "all done" and payload["run_id"] and payload["turn_id"]
    assert payload["failed"] is False and payload["clarified"] is False
    assert context.messages[0] == {"role": "user", "content": "do it"}

    class FailingLLM:
        def chat(self, messages, tools=None, **kwargs):
            raise LLMCallFailed("endpoint is down")

    assert one_shot(AgentLoop(llm=FailingLLM(), tools=ToolRegistry()), Context(system_prompt="t"), "go", as_json=True) == 1
    assert json.loads(capsys.readouterr().out)["failed"] is True
