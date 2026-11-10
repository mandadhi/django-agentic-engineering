"""Storage abstraction for execution metadata.

:class:`ExecutionStore` is the only interface the runtime uses, so a PostgreSQL
implementation can be added without touching :mod:`runtime.execution`.
:func:`open_store` picks the implementation from framework-config ``storage``.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from core.config import StorageSettings
from core.errors import ConfigError

from .models import AgentRun, ArtifactRecord, Checkpoint, Execution, LogEntry, StageState
from .records import RunRecord
from .state import AgentRunStatus, ExecutionStatus


@runtime_checkable
class ExecutionStore(Protocol):
    def transaction(self) -> AbstractContextManager[None]:
        """Group writes atomically. Nested calls join the outer transaction."""

    # executions
    def create_execution(self, execution: Execution, stages: Sequence[StageState]) -> None: ...
    def get_execution(self, execution_id: str) -> Execution: ...
    def update_execution(self, execution: Execution) -> None: ...
    def list_executions(self, *, workflow_id: str | None = None, status: ExecutionStatus | None = None,
                        limit: int = 100) -> list[Execution]: ...

    # stages
    def get_stage(self, execution_id: str, stage_id: str) -> StageState: ...
    def list_stages(self, execution_id: str) -> list[StageState]: ...
    def update_stage(self, stage: StageState) -> None: ...

    # agent runs
    def save_agent_run(self, run: AgentRun) -> None: ...
    def get_agent_run(self, run_id: str) -> AgentRun: ...
    def list_agent_runs(self, execution_id: str, stage_id: str | None = None) -> list[AgentRun]: ...

    # run records (Phase 5.3): read-only RunRecord contract views of agent runs, metadata only
    def get_run_record(self, run_id: str) -> RunRecord: ...
    def list_run_records(self, *, execution_id: str | None = None, workflow_id: str | None = None,
                         stage_id: str | None = None, agent_id: str | None = None,
                         status: AgentRunStatus | None = None, error_code: str | None = None,
                         limit: int = 100, newest_first: bool = True) -> list[RunRecord]: ...

    # checkpoints
    def add_checkpoint(self, execution_id: str, *, reason: str, state: Mapping[str, Any],
                       stage_id: str | None = None) -> Checkpoint: ...
    def latest_checkpoint(self, execution_id: str) -> Checkpoint | None: ...
    def list_checkpoints(self, execution_id: str) -> list[Checkpoint]: ...

    # logs and artifact references
    def append_log(self, entry: LogEntry) -> None: ...
    def list_logs(self, execution_id: str) -> list[LogEntry]: ...
    def add_artifact(self, record: ArtifactRecord) -> None: ...
    def list_artifacts(self, execution_id: str) -> list[ArtifactRecord]: ...

    def close(self) -> None: ...


def open_store(settings: StorageSettings) -> ExecutionStore:
    """Instantiate the configured backend."""
    if settings.backend == "sqlite":
        from .sqlite_store import SQLiteExecutionStore

        if settings.sqlite_path is None:
            raise ConfigError("storage.sqlite.path is required for the sqlite backend")
        return SQLiteExecutionStore(settings.sqlite_path, max_json_bytes=settings.max_json_bytes)
    raise ConfigError(f"Unsupported storage backend '{settings.backend}'")
