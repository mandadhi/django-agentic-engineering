"""Errors raised while preparing or executing an agent through the Agent SDK.

Each carries the stable ``code`` and ``retryable`` flag that end up in the run's
``FailureInfo`` and in ``AgentResult.errors`` — the workflow engine decides retries
from them; the runner never retries by itself.
"""

from __future__ import annotations

from core.errors import HarnessError


class AgentRuntimeError(HarnessError):
    code = "runtime.error"
    retryable = False


class AgentDefinitionError(AgentRuntimeError):
    """The agent's Markdown cannot be turned into an SDK AgentDefinition (no body, unreadable, ...)."""

    code = "agent.invalid_definition"


class AgentDefinitionConflictError(AgentRuntimeError):
    """A loaded plugin defines the name the harness runs its own agent definition under (AGT-02)."""

    code = "agent.definition_conflict"


class PluginResolutionError(AgentRuntimeError):
    """A registered plugin or skill is missing or invalid, checked before the SDK is started."""

    code = "plugin.unresolved"


class PluginLoadError(AgentRuntimeError):
    """The SDK session reported plugin load errors, or did not load an expected plugin/skill."""

    code = "plugin.load_failed"


class SecurityViolationError(AgentRuntimeError):
    """A critical security check failed or could not be completed — the run is aborted (fail closed)."""

    code = "security.violation"


class AgentTimeoutError(AgentRuntimeError):
    code = "agent.timeout"
    retryable = True

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(f"agent execution exceeded its timeout of {seconds:g}s and was cancelled")


class RunCancelledError(AgentRuntimeError):
    """The execution was cancelled (``execution cancel``) while the agent was running."""

    code = "runtime.cancelled"
    retryable = True


class ResultProcessingError(AgentRuntimeError):
    """The agent's response could not be turned into a valid AgentResult."""

    code = "result.invalid"
    retryable = True
