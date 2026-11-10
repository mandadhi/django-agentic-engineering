"""Exception hierarchy and the ValidationIssue record shared by every validator.

Validators collect *all* problems before raising, so a single run reports every
broken reference instead of failing on the first one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ValidationIssue:
    """One problem found in a config file, workflow definition or contract instance."""

    source: str    # file path (relative to the harness root) or contract name
    pointer: str   # JSON-pointer-like location, e.g. "agents/2/skills/0"; "" = document root
    message: str
    code: str = "schema"  # schema | version | kind | duplicate-id | unknown-agent | ...

    def __str__(self) -> str:
        return f"{self.source}: {self.pointer or '<root>'}: {self.message} [{self.code}]"


class HarnessError(Exception):
    """Base class for every error raised by the harness."""


class _IssueError(HarnessError):
    """An error that carries a list of ValidationIssues."""

    def __init__(self, message: str, issues: Iterable[ValidationIssue] = ()) -> None:
        self.issues: tuple[ValidationIssue, ...] = tuple(issues)
        super().__init__(message)

    def __str__(self) -> str:
        head = self.args[0] if self.args else self.__class__.__name__
        if not self.issues:
            return head
        lines = "\n".join(f"  - {issue}" for issue in self.issues)
        return f"{head} ({len(self.issues)} issue(s)):\n{lines}"


class ConfigError(_IssueError):
    """A configuration file is unreadable, malformed or fails its schema."""


class ConfigVersionError(ConfigError):
    """A configuration file declares a schema_version this framework does not support."""


class ReferenceValidationError(ConfigError):
    """Configs or workflows reference agents, skills, stages or files that do not exist."""


class ContractValidationError(_IssueError):
    """An instance (AgentTask, AgentResult, ...) does not satisfy its contract."""


class InputValidationError(ContractValidationError):
    """Workflow inputs are missing, of the wrong type or not allowed by the workflow's inputs schema."""


class RegistryLookupError(HarnessError):
    """Lookup of an unknown id in a registry."""

    def __init__(self, kind: str, item_id: str, suggestions: Sequence[str] = ()) -> None:
        self.kind = kind
        self.item_id = item_id
        self.suggestions = tuple(suggestions)
        hint = f" Did you mean: {', '.join(self.suggestions)}?" if self.suggestions else ""
        super().__init__(f"Unknown {kind} '{item_id}'.{hint}")


class InvalidTransitionError(HarnessError):
    """A state change not allowed by the runtime's transition tables."""


class StorageError(HarnessError):
    """Persistence failure in an ExecutionStore."""


class NotFoundError(StorageError):
    """Requested execution, stage, run or checkpoint does not exist."""


class PayloadTooLargeError(HarnessError):
    """Content exceeds the configured inline/metadata cap and must be stored by reference."""


def suggest(name: str, candidates: Iterable[str], limit: int = 3) -> list[str]:
    """Return close matches for ``name`` — used for 'did you mean' hints."""
    import difflib

    return difflib.get_close_matches(name, list(candidates), n=limit, cutoff=0.6)
