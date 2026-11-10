"""Read YAML frontmatter from agent ``.md`` files and ``SKILL.md`` files.

Agents and skills are defined in Markdown, as everywhere else in this repository.
Whatever an agent file declares about itself (``name``, ``description``, ``tools``,
``disallowedTools``, ``model``, ``sdlc_phase``) is read from the file and never
duplicated in the registry. These are the same fields the Claude Agent SDK's
``AgentDefinition`` and Claude Code subagents use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class FrontmatterError(ValueError):
    """The file has no frontmatter block, or the block is not a YAML mapping."""


def read_frontmatter(path: Path) -> dict[str, Any]:
    return read_markdown(path)[0]


def read_markdown(path: Path) -> tuple[dict[str, Any], str]:
    """Frontmatter mapping and the Markdown body after it (the agent's instructions)."""
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("no YAML frontmatter (file must start with '---')")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        raise FrontmatterError("frontmatter is not closed with '---'") from None
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"invalid YAML in frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter is not a mapping")
    return data, "\n".join(lines[end + 1:]).strip()


def as_list(value: Any) -> tuple[str, ...] | None:
    """Accept both ``tools: Read, Grep`` and ``tools: [Read, Grep]``. ``None`` = not declared."""
    if value is None:
        return None
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return tuple(str(part) for part in value)
