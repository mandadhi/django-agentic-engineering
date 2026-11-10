"""Agent execution through the Claude Agent SDK (Phase 2).

One SDK ``query()`` per workflow stage. The workflow engine
(:class:`runtime.execution.ExecutionManager`) decides what runs; the SDK runs the
agent; this package is the thin layer between them:

* :mod:`.plugin_loader`     — registered plugins → SDK ``plugins``; verifies the session loaded them;
* :mod:`.options_factory`   — agent Markdown → ``AgentDefinition`` → ``ClaudeAgentOptions``;
* :mod:`.security_hooks`    — PreToolUse/PostToolUse guard, approvals, audit, redaction;
* :mod:`.prompt`            — AgentTask → user prompt (``templates/agent-task.prompt.md``);
* :mod:`.output_schema`     — AgentResult contract → one draft-07 schema for structured output;
* :mod:`.result_processor`  — SDK messages → validated AgentResult + session/cost metadata;
* :mod:`.session_manager`   — which interrupted SDK session a stage attempt resumes;
* :mod:`.sdk_adapter`       — the only module that calls the SDK (replaceable);
* :mod:`.runner`            — :class:`AgentRunner`, wiring the above to the ExecutionManager.
"""

from .runner import AgentRunner, StageOutcome, WorkflowOutcome

__all__ = ["AgentRunner", "StageOutcome", "WorkflowOutcome"]
