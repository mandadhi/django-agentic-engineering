"""Which SDK session, if any, a stage attempt should resume.

The store keeps session *identifiers* only; the conversation lives in the SDK's own
session transcript (``~/.claude/projects/<cwd>/<session>.jsonl`` by default), which
``ClaudeAgentOptions.resume`` reloads.

Rules (so a stage is never run twice and a finished run is never resumed):

* only the **latest** run of the stage is considered;
* it must have been *interrupted* — status ``cancelled`` with error
  ``runtime.interrupted``, which :meth:`ExecutionManager.resume` sets for runs that
  were in progress when their process stopped. Completed, failed, timed-out or
  deliberately cancelled runs start a fresh session;
* it must have a ``session_id`` (it got as far as the SDK ``init`` frame);
* no later run has already resumed it;
* the session transcript must still exist for the same workspace (checked through
  the adapter); otherwise the attempt starts fresh and this is logged.

Assumption (documented in the README): resume runs after the previous process has
stopped, or once ownership of the execution has otherwise been established. There
is no cross-process lock.
"""

from __future__ import annotations

from runtime.execution import INTERRUPTED
from runtime.models import AgentRun
from runtime.state import AgentRunStatus
from runtime.store import ExecutionStore

RESUMED_FROM = "resumed_from_run"


def find_resumable_run(store: ExecutionStore, execution_id: str, stage_id: str) -> AgentRun | None:
    runs = store.list_agent_runs(execution_id, stage_id)
    if not runs:
        return None
    latest = runs[-1]
    if latest.status is not AgentRunStatus.CANCELLED or latest.error is None or latest.error.code != INTERRUPTED:
        return None
    if not latest.session_id:
        return None
    if any(run.sdk_metadata.get(RESUMED_FROM) == latest.run_id for run in runs):
        return None
    return latest
