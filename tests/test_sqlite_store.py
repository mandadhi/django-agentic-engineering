import sqlite3
from pathlib import Path

import pytest

from core.config import StorageSettings
from core.errors import ConfigError, NotFoundError, PayloadTooLargeError, StorageError
from runtime.models import ArtifactRecord, Execution, FailureInfo, LogEntry, StageState
from runtime.sqlite_store import MIGRATIONS, SCHEMA_VERSION, SQLiteExecutionStore
from runtime.state import ExecutionStatus, StageStatus
from runtime.store import ExecutionStore, open_store


def make_execution(store: SQLiteExecutionStore, execution_id: str = "e1") -> Execution:
    execution = Execution(execution_id=execution_id, workflow_id="wf", workflow_version="1.0.0",
                          status=ExecutionStatus.RUNNING, created_at="2026-11-03T10:00:00Z",
                          updated_at="2026-11-03T10:00:00Z", inputs={"repository": "acme"})
    store.create_execution(execution, [
        StageState(execution_id, "a", "agent-a", StageStatus.PENDING, 0, max_attempts=2),
        StageState(execution_id, "b", "agent-b", StageStatus.PENDING, 1),
    ])
    return execution


def test_implements_protocol(store: SQLiteExecutionStore) -> None:
    assert isinstance(store, ExecutionStore)


def test_open_store_from_settings(tmp_path: Path) -> None:
    store = open_store(StorageSettings("sqlite", tmp_path / "x.db"))
    assert isinstance(store, SQLiteExecutionStore)
    store.close()
    with pytest.raises(ConfigError):
        open_store(StorageSettings("postgres", None))


def test_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "h.db"
    store = SQLiteExecutionStore(path)
    execution = make_execution(store)
    stage = store.get_stage("e1", "a")
    stage.status, stage.attempts = StageStatus.FAILED, 1
    stage.error = FailureInfo("tool.timeout", "bandit timed out", retryable=True)
    store.update_stage(stage)
    store.append_log(LogEntry("e1", "stage.failed", "bandit timed out", level="ERROR", stage_id="a"))
    store.close()

    reopened = SQLiteExecutionStore(path)
    assert reopened.schema_version == SCHEMA_VERSION
    loaded = reopened.get_execution("e1")
    assert loaded.inputs == {"repository": "acme"} and loaded.status is ExecutionStatus.RUNNING
    assert loaded.workflow_version == execution.workflow_version
    [a, b] = reopened.list_stages("e1")
    assert (a.stage_id, a.status, a.attempts, a.error.code) == ("a", StageStatus.FAILED, 1, "tool.timeout")
    assert b.status is StageStatus.PENDING
    assert [e.event for e in reopened.list_logs("e1")] == ["stage.failed"]
    reopened.close()


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "h.db"
    SQLiteExecutionStore(path).close()
    SQLiteExecutionStore(path).close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(MIGRATIONS)


def test_transaction_rolls_back(store: SQLiteExecutionStore) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction():
            make_execution(store)
            raise RuntimeError("boom")
    with pytest.raises(NotFoundError):
        store.get_execution("e1")


def test_duplicate_execution_is_storage_error(store: SQLiteExecutionStore) -> None:
    make_execution(store)
    with pytest.raises(StorageError):
        make_execution(store)
    assert len(store.list_stages("e1")) == 2


def test_not_found(store: SQLiteExecutionStore) -> None:
    with pytest.raises(NotFoundError):
        store.get_execution("missing")
    with pytest.raises(NotFoundError):
        store.get_agent_run("missing")


def test_artifacts_are_references_only(store: SQLiteExecutionStore) -> None:
    make_execution(store)
    store.add_artifact(ArtifactRecord("art1", "e1", "file://var/artifacts/e1/report.md", "2026-11-03T10:00:00Z",
                                      stage_id="a", name="report", media_type="text/markdown", size_bytes=5_000_000))
    [record] = store.list_artifacts("e1")
    assert record.uri.endswith("report.md") and record.size_bytes == 5_000_000
    columns = {row[1]: row[2] for row in store._conn.execute("PRAGMA table_info(artifacts)")}
    assert "BLOB" not in columns.values() and "content" not in columns


def test_metadata_size_cap(tmp_path: Path) -> None:
    store = SQLiteExecutionStore(tmp_path / "h.db", max_json_bytes=1024)
    make_execution(store)
    with pytest.raises(PayloadTooLargeError, match="artifact reference"):
        store.add_checkpoint("e1", reason="manual", state={"blob": "x" * 2000})
    assert store.list_checkpoints("e1") == []
    store.close()


def test_list_executions_filters(store: SQLiteExecutionStore) -> None:
    make_execution(store, "e1")
    make_execution(store, "e2")
    done = store.get_execution("e2")
    done.status = ExecutionStatus.SUCCEEDED
    store.update_execution(done)
    assert [e.execution_id for e in store.list_executions(status=ExecutionStatus.RUNNING)] == ["e1"]
    assert len(store.list_executions(workflow_id="wf")) == 2
