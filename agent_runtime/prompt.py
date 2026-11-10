"""Render an AgentTask into the user prompt of an SDK query.

The agent's *instructions* are its Markdown body (the SDK AgentDefinition prompt);
this prompt only carries the task: objective, constraints, inputs, expected
outputs, context references and the result protocol. Layout lives in
``templates/agent-task.prompt.md`` (``string.Template`` placeholders).
"""

from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import Any, Mapping

from core import HARNESS_HOME

TEMPLATE = HARNESS_HOME / "templates" / "agent-task.prompt.md"

RESUME_NOTE = ("\nThis session was interrupted before you returned a result. Continue the task from where you "
               "left off (do not repeat completed work) and finish by returning the AgentResult as described above.")


def _bullets(lines: list[str], empty: str = "None.") -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else empty


def _context(block: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for ref in block.get("refs", []):
        desc = f" — {ref['description']}" if ref.get("description") else ""
        parts.append(f"- [{ref['scope']}] `{ref['id']}`: load from `{ref['uri']}`{desc}")
    for item in block.get("inline", []):
        parts.append(f"- [{item['scope']}] `{item['id']}`:\n\n  ```text\n  "
                     + item["content"].replace("\n", "\n  ") + "\n  ```")
    if block.get("summary"):
        parts.append(f"- Summary of context that did not fit the budget:\n\n  {block['summary']}")
    return "\n".join(parts) if parts else "No additional context was assembled for this stage."


def render_task_prompt(task: Mapping[str, Any], *, workspace: Path, artifact_dir: Path, resumed: bool = False,
                       template: Path = TEMPLATE) -> str:
    constraints = [f"({c.get('severity', 'must')}) {c['description']}" for c in task.get("constraints", [])]
    outputs = [f"`{o['name']}` ({o['type']}{', required' if o.get('required', True) else ', optional'})"
               + (f": {o['description']}" if o.get("description") else "") for o in task["expected_outputs"]]
    inputs = (task.get("extensions") or {}).get("dae.inputs") or {}
    input_lines = [f"`{k}` = {json.dumps(v, ensure_ascii=False)}" for k, v in inputs.items()]
    values = {
        "task_id": task["task_id"], "agent_id": task["agent_id"],
        "workflow_id": task.get("workflow_id", "-"), "stage_id": task.get("stage_id", "-"),
        "attempt": task.get("attempt", 1), "workspace": workspace.as_posix(), "artifact_dir": artifact_dir.as_posix(),
        "objective": task["objective"], "constraints": _bullets(constraints), "inputs": _bullets(input_lines),
        "expected_outputs": _bullets(outputs), "context": _context(task["context"]),
        "resume_note": RESUME_NOTE if resumed else "",
    }
    return Template(template.read_text(encoding="utf-8")).substitute(values).strip() + "\n"
