"""Skills: procedures the model can read on demand.

A skill is a Markdown file, `skills/<category>/<name>/SKILL.md`, with a short
frontmatter (`name`, `description`) and a body that names our tools in
backticks. The index (name and description) goes into the system prompt once
at startup; the body costs no context until the model asks for it through
`skill__read`. Loading is strict: a malformed skill fails at startup, where
the author sees it, not mid-turn.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .tools import Tool, tool

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_DESCRIPTION = 100


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path


def parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"{path}: SKILL.md must start with a '---' frontmatter block")
    meta = yaml.safe_load(match.group(1)) or {}
    name, description = str(meta.get("name", "")), str(meta.get("description", "")).strip()
    if not _NAME.match(name) or name != path.parent.name:
        raise ValueError(f"{path}: 'name' must be kebab-case and match the folder, got {name!r}")
    if not description or "\n" in description or not description.endswith(".") \
            or len(description) > MAX_DESCRIPTION:
        raise ValueError(f"{path}: 'description' must be one sentence ending with '.', "
                         f"at most {MAX_DESCRIPTION} characters")
    body = match.group(2).strip()
    if not body:
        raise ValueError(f"{path}: the skill has no body")
    return Skill(name=name, description=description, body=body, path=path)


def load_skills(root: Path) -> list[Skill]:
    """Every `*/<name>/SKILL.md` under `root`, sorted by name. Missing root: none."""
    if not root.is_dir():
        return []
    skills = [parse_skill(p) for p in sorted(root.glob("*/*/SKILL.md"))]
    names = [s.name for s in skills]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"duplicate skill names under {root}: {sorted(duplicates)}")
    return skills


class SkillProvider:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills = {s.name: s for s in skills}

    @property
    def namespace(self) -> str:
        return "skill"

    def load(self) -> list[Tool]:
        return [tool(
            "skill__read",
            "Read the full procedure for one of the skills listed in your instructions. "
            "Do this before starting a task a skill covers.",
            self._read,
            {"name": {"type": "string", "enum": sorted(self._skills)}}, ["name"],
        )]

    def _read(self, name: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            return f"No skill named {name!r}. Available: {', '.join(sorted(self._skills))}"
        return f"# {skill.name}\n\n{skill.body}"
