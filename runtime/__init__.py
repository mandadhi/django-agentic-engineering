"""Execution runtime: persistent workflow / stage / agent-run state, checkpoints and resume.

The runtime records *what happened*; it does not invoke agents. The agent runner
(:mod:`agent_runtime.runner`) drives :class:`runtime.execution.ExecutionManager` and hands it
AgentResult documents. Storage is reached only through the
:class:`runtime.store.ExecutionStore` protocol (SQLite today).
"""
