"""Typed, immutable view of a WorkflowDefinition, with DAG helpers.

Instances are built only from documents that already passed the
``workflow-definition`` contract, so parsing here does not re-validate types.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import WorkflowDefaults
from .errors import HarnessError, RegistryLookupError, suggest

#: ``${inputs.<name>}`` in a stage objective or constraint is replaced by that workflow input.
INPUT_REF = re.compile(r"\$\{inputs\.([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class ExpectedOutput:
    name: str
    type: str
    description: str = ""
    required: bool = True
    schema_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "type": self.type, "required": self.required}
        if self.description:
            data["description"] = self.description
        if self.schema_ref:
            data["schema_ref"] = self.schema_ref
        return data


@dataclass(frozen=True)
class Constraint:
    id: str
    description: str
    severity: str = "must"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "description": self.description, "severity": self.severity}


@dataclass(frozen=True)
class StageDefinition:
    id: str
    agent_id: str
    objective: str
    name: str
    description: str
    depends_on: tuple[str, ...]
    constraints: tuple[Constraint, ...]
    expected_outputs: tuple[ExpectedOutput, ...]
    context_scopes: tuple[str, ...] | None
    context_tags: tuple[str, ...] | None
    context_token_budget: int | None
    approval_required: bool
    max_attempts: int
    timeout_seconds: int
    inputs: tuple[str, ...] = ()

    def required_inputs(self) -> tuple[str, ...]:
        """Inputs the stage needs: its ``inputs`` list plus every ``${inputs.x}`` it references."""
        refs = list(self.inputs)
        for text in (self.objective, *(c.description for c in self.constraints)):
            refs += INPUT_REF.findall(text)
        return tuple(dict.fromkeys(refs))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], defaults: WorkflowDefaults) -> "StageDefinition":
        ctx = data.get("context", {})
        return cls(
            id=data["id"],
            agent_id=data["agent_id"],
            objective=data["objective"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            depends_on=tuple(data.get("depends_on", ())),
            constraints=tuple(Constraint(c["id"], c["description"], c.get("severity", "must"))
                              for c in data.get("constraints", ())),
            expected_outputs=tuple(
                ExpectedOutput(o["name"], o["type"], o.get("description", ""), o.get("required", True), o.get("schema_ref"))
                for o in data["expected_outputs"]
            ),
            context_scopes=tuple(ctx["scopes"]) if "scopes" in ctx else None,
            context_tags=tuple(ctx["tags"]) if "tags" in ctx else None,
            context_token_budget=ctx.get("token_budget"),
            approval_required=data.get("approval_required", defaults.approval_required),
            max_attempts=data.get("max_attempts", defaults.max_attempts),
            timeout_seconds=data.get("timeout_seconds", defaults.stage_timeout_seconds),
            inputs=tuple(data.get("inputs", ())),
        )


def render_inputs(text: str, inputs: Mapping[str, Any]) -> str:
    """Substitute ``${inputs.x}``. References are validated at load time and values at start_execution."""
    return INPUT_REF.sub(lambda m: str(inputs.get(m.group(1), m.group(0))), text)


class WorkflowCycleError(HarnessError):
    """Stage dependencies form a cycle."""

    def __init__(self, workflow_id: str, stages: Iterable[str]) -> None:
        self.stages = tuple(sorted(stages))
        super().__init__(f"Workflow '{workflow_id}' has a dependency cycle among stages: {', '.join(self.stages)}")


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    version: str
    name: str
    description: str
    status: str
    stages: tuple[StageDefinition, ...]
    inputs_schema: Mapping[str, Any] | None
    source: Path | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], defaults: WorkflowDefaults,
                  source: Path | None = None) -> "WorkflowDefinition":
        return cls(
            id=data["id"],
            version=data["version"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            status=data.get("status", "draft"),
            stages=tuple(StageDefinition.from_dict(s, defaults) for s in data["stages"]),
            inputs_schema=data.get("inputs"),
            source=source,
        )

    @property
    def stage_ids(self) -> tuple[str, ...]:
        return tuple(stage.id for stage in self.stages)

    def stage(self, stage_id: str) -> StageDefinition:
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        raise RegistryLookupError(f"stage in workflow '{self.id}'", stage_id, suggest(stage_id, self.stage_ids))

    def topological_order(self) -> tuple[str, ...]:
        """Stage ids in dependency order (definition order breaks ties). Raises on cycles."""
        known = set(self.stage_ids)
        indegree = {s.id: sum(1 for d in s.depends_on if d in known) for s in self.stages}
        dependents: dict[str, list[str]] = {s.id: [] for s in self.stages}
        for stage in self.stages:
            for dep in stage.depends_on:
                if dep in known:
                    dependents[dep].append(stage.id)
        queue = deque(s.id for s in self.stages if indegree[s.id] == 0)
        order: list[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for nxt in dependents[current]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if len(order) != len(self.stages):
            raise WorkflowCycleError(self.id, known - set(order))
        return tuple(order)

    def runnable_stages(self, satisfied: Iterable[str], pending: Iterable[str]) -> tuple[str, ...]:
        """Pending stages whose dependencies are all in ``satisfied`` (succeeded or skipped)."""
        done, todo = set(satisfied), set(pending)
        return tuple(
            stage_id for stage_id in self.topological_order()
            if stage_id in todo and all(dep in done for dep in self.stage(stage_id).depends_on)
        )
