"""SQLite implementation of :class:`runtime.store.ExecutionStore`.

* Schema is versioned: ``MIGRATIONS`` is applied in order and recorded in
  ``schema_migrations``, so opening an older database upgrades it in place.
* Foreign keys on, WAL journal, explicit ``BEGIN IMMEDIATE`` transactions;
  nested :meth:`transaction` blocks join the outer one.
* JSON columns are capped at ``max_json_bytes``: the database holds execution
  metadata and artifact references, never generated artifacts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from core.errors import NotFoundError, PayloadTooLargeError, StorageError
from core.log import get_logger

from .models import AgentRun, ArtifactRecord, Checkpoint, Execution, FailureInfo, LogEntry, StageState, utcnow
from .records import RunRecord, build_run_record
from .state import AgentRunStatus, ExecutionStatus, StageStatus

log = get_logger(__name__)

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, """
    CREATE TABLE executions (
        execution_id     TEXT PRIMARY KEY,
        workflow_id      TEXT NOT NULL,
        workflow_version TEXT NOT NULL,
        status           TEXT NOT NULL,
        inputs           TEXT NOT NULL DEFAULT '{}',
        metadata         TEXT NOT NULL DEFAULT '{}',
        error            TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        started_at       TEXT,
        completed_at     TEXT
    );
    CREATE INDEX ix_executions_workflow ON executions(workflow_id, status);

    CREATE TABLE stages (
        execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
        stage_id     TEXT NOT NULL,
        agent_id     TEXT NOT NULL,
        status       TEXT NOT NULL,
        position     INTEGER NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 1,
        error        TEXT,
        approved_by  TEXT,
        started_at   TEXT,
        completed_at TEXT,
        PRIMARY KEY (execution_id, stage_id)
    );

    CREATE TABLE agent_runs (
        run_id       TEXT PRIMARY KEY,
        execution_id TEXT NOT NULL,
        stage_id     TEXT NOT NULL,
        agent_id     TEXT NOT NULL,
        task_id      TEXT NOT NULL UNIQUE,
        attempt      INTEGER NOT NULL,
        status       TEXT NOT NULL,
        task         TEXT NOT NULL,
        result       TEXT,
        error        TEXT,
        created_at   TEXT NOT NULL,
        started_at   TEXT,
        completed_at TEXT,
        FOREIGN KEY (execution_id, stage_id) REFERENCES stages(execution_id, stage_id) ON DELETE CASCADE
    );
    CREATE INDEX ix_agent_runs_stage ON agent_runs(execution_id, stage_id);

    CREATE TABLE checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        execution_id  TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
        sequence      INTEGER NOT NULL,
        stage_id      TEXT,
        reason        TEXT NOT NULL,
        state         TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        UNIQUE (execution_id, sequence)
    );

    CREATE TABLE execution_logs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
        timestamp    TEXT NOT NULL,
        level        TEXT NOT NULL,
        event        TEXT NOT NULL,
        message      TEXT NOT NULL,
        stage_id     TEXT,
        run_id       TEXT,
        data         TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX ix_logs_execution ON execution_logs(execution_id, id);

    CREATE TABLE artifacts (
        artifact_id  TEXT PRIMARY KEY,
        execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
        stage_id     TEXT,
        run_id       TEXT,
        name         TEXT,
        uri          TEXT NOT NULL,
        media_type   TEXT,
        sha256       TEXT,
        size_bytes   INTEGER,
        description  TEXT,
        created_at   TEXT NOT NULL
    );
    CREATE INDEX ix_artifacts_execution ON artifacts(execution_id);
    """),
    # v2 (Phase 2): Agent SDK execution metadata. Additive columns only, so existing
    # rows are preserved and read back with NULL / '{}'. Session *identifiers* are
    # stored; the conversation stays in the SDK's own session transcript.
    (2, """
    ALTER TABLE agent_runs ADD COLUMN session_id TEXT;
    ALTER TABLE agent_runs ADD COLUMN cost_usd REAL;
    ALTER TABLE agent_runs ADD COLUMN duration_ms INTEGER;
    ALTER TABLE agent_runs ADD COLUMN num_turns INTEGER;
    ALTER TABLE agent_runs ADD COLUMN sdk_metadata TEXT NOT NULL DEFAULT '{}';
    CREATE INDEX ix_agent_runs_session ON agent_runs(session_id);
    """),
    # v3 (Phase 5.3): RunRecord execution contract. Additive only — v1/v2 rows stay readable.
    # * typed token columns (NULL = not reported; never back-filled with 0);
    # * error_code: a query column derived from ``error`` in the same write (see save_agent_run),
    #   so it cannot diverge; the full FailureInfo stays in ``error``;
    # * existing rows are back-filled from the metadata v2 already stored (integers only);
    # * indexes for the retrieval API: recent runs, per-agent history, failures by code,
    #   recent executions.
    (3, """
    ALTER TABLE agent_runs ADD COLUMN input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0);
    ALTER TABLE agent_runs ADD COLUMN output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0);
    ALTER TABLE agent_runs ADD COLUMN error_code TEXT;
    UPDATE agent_runs SET
        input_tokens = CASE WHEN json_type(sdk_metadata, '$.usage.input_tokens') = 'integer'
                                 AND json_extract(sdk_metadata, '$.usage.input_tokens') >= 0
                            THEN json_extract(sdk_metadata, '$.usage.input_tokens') END,
        output_tokens = CASE WHEN json_type(sdk_metadata, '$.usage.output_tokens') = 'integer'
                                  AND json_extract(sdk_metadata, '$.usage.output_tokens') >= 0
                             THEN json_extract(sdk_metadata, '$.usage.output_tokens') END,
        error_code = CASE WHEN error IS NOT NULL THEN json_extract(error, '$.code') END;
    CREATE INDEX ix_agent_runs_created ON agent_runs(created_at);
    CREATE INDEX ix_agent_runs_agent ON agent_runs(agent_id, created_at);
    CREATE INDEX ix_agent_runs_error ON agent_runs(error_code);
    CREATE INDEX ix_executions_created ON executions(created_at);
    """),
)

_AGENT_RUN_COLUMNS = ("run_id", "execution_id", "stage_id", "agent_id", "task_id", "attempt", "status", "task",
                      "result", "error", "created_at", "started_at", "completed_at", "session_id", "cost_usd",
                      "duration_ms", "num_turns", "sdk_metadata", "input_tokens", "output_tokens", "error_code")
# Columns a later save may change (identity, task and created_at are immutable).
_AGENT_RUN_MUTABLE = ("status", "result", "error", "started_at", "completed_at", "session_id", "cost_usd",
                      "duration_ms", "num_turns", "sdk_metadata", "input_tokens", "output_tokens", "error_code")
# RunRecord queries never load the AgentTask or the AgentResult document (only its status).
_RECORD_SELECT = ("SELECT r.run_id, r.execution_id, r.stage_id, r.agent_id, r.task_id, r.attempt, r.status, r.error, "
                  "r.created_at, r.started_at, r.completed_at, r.session_id, r.cost_usd, r.duration_ms, r.num_turns, "
                  "r.sdk_metadata, r.input_tokens, r.output_tokens, json_extract(r.result, '$.status') AS result_status, "
                  "e.workflow_id, e.workflow_version "
                  "FROM agent_runs r JOIN executions e ON e.execution_id = r.execution_id")

SCHEMA_VERSION = MIGRATIONS[-1][0]


class SQLiteExecutionStore:
    def __init__(self, path: Path | str, *, max_json_bytes: int = 65536) -> None:
        self.path = Path(path)
        self.max_json_bytes = max_json_bytes
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        except sqlite3.Error as exc:
            raise StorageError(f"Cannot open SQLite database {path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ------------------------------------------------------------------ plumbing

    def _migrate(self) -> None:
        with self.transaction():
            self._conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            applied = {row[0] for row in self._conn.execute("SELECT version FROM schema_migrations")}
            for version, ddl in MIGRATIONS:
                if version in applied:
                    continue
                for statement in filter(str.strip, ddl.split(";")):
                    self._conn.execute(statement)
                self._conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, utcnow()))
                log.info("store.migrated", extra={"db": str(self.path), "schema_version": version})

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            with self.transaction():
                return self._conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"Integrity error: {exc}") from exc
        except sqlite3.Error as exc:
            raise StorageError(f"SQLite error: {exc}") from exc

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(f"SQLite error: {exc}") from exc

    def _json(self, value: Any, what: str) -> str | None:
        if value is None:
            return None
        text = json.dumps(value, sort_keys=True, default=str)
        if len(text.encode("utf-8")) > self.max_json_bytes:
            raise PayloadTooLargeError(
                f"{what} is {len(text)} bytes (limit {self.max_json_bytes}); store large content as an artifact reference"
            )
        return text

    @staticmethod
    def _load(text: str | None) -> Any:
        return json.loads(text) if text else None

    def _error(self, error: FailureInfo | None) -> str | None:
        return self._json(error.to_dict(), "error") if error else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ executions

    def create_execution(self, execution: Execution, stages: Sequence[StageState]) -> None:
        with self.transaction():
            self._execute(
                "INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (execution.execution_id, execution.workflow_id, execution.workflow_version, execution.status.value,
                 self._json(execution.inputs, "inputs"), self._json(execution.metadata, "metadata"),
                 self._error(execution.error), execution.created_at, execution.updated_at,
                 execution.started_at, execution.completed_at),
            )
            for stage in stages:
                self._execute(
                    "INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (stage.execution_id, stage.stage_id, stage.agent_id, stage.status.value, stage.position,
                     stage.attempts, stage.max_attempts, self._error(stage.error), stage.approved_by,
                     stage.started_at, stage.completed_at),
                )

    def _execution(self, row: sqlite3.Row) -> Execution:
        return Execution(
            execution_id=row["execution_id"], workflow_id=row["workflow_id"],
            workflow_version=row["workflow_version"], status=ExecutionStatus(row["status"]),
            inputs=self._load(row["inputs"]) or {}, metadata=self._load(row["metadata"]) or {},
            error=FailureInfo.from_dict(self._load(row["error"])), created_at=row["created_at"],
            updated_at=row["updated_at"], started_at=row["started_at"], completed_at=row["completed_at"],
        )

    def get_execution(self, execution_id: str) -> Execution:
        rows = self._query("SELECT * FROM executions WHERE execution_id = ?", (execution_id,))
        if not rows:
            raise NotFoundError(f"execution '{execution_id}' not found")
        return self._execution(rows[0])

    def update_execution(self, execution: Execution) -> None:
        cursor = self._execute(
            "UPDATE executions SET status=?, metadata=?, error=?, updated_at=?, started_at=?, completed_at=? "
            "WHERE execution_id=?",
            (execution.status.value, self._json(execution.metadata, "metadata"), self._error(execution.error),
             execution.updated_at, execution.started_at, execution.completed_at, execution.execution_id),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(f"execution '{execution.execution_id}' not found")

    def list_executions(self, *, workflow_id: str | None = None, status: ExecutionStatus | None = None,
                        limit: int = 100) -> list[Execution]:
        sql, params = "SELECT * FROM executions WHERE 1=1", []
        if workflow_id:
            sql += " AND workflow_id = ?"
            params.append(workflow_id)
        if status:
            sql += " AND status = ?"
            params.append(ExecutionStatus(status).value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._execution(r) for r in self._query(sql, params)]

    # ------------------------------------------------------------------ stages

    def _stage(self, row: sqlite3.Row) -> StageState:
        return StageState(
            execution_id=row["execution_id"], stage_id=row["stage_id"], agent_id=row["agent_id"],
            status=StageStatus(row["status"]), position=row["position"], attempts=row["attempts"],
            max_attempts=row["max_attempts"], error=FailureInfo.from_dict(self._load(row["error"])),
            approved_by=row["approved_by"], started_at=row["started_at"], completed_at=row["completed_at"],
        )

    def get_stage(self, execution_id: str, stage_id: str) -> StageState:
        rows = self._query("SELECT * FROM stages WHERE execution_id = ? AND stage_id = ?", (execution_id, stage_id))
        if not rows:
            raise NotFoundError(f"stage '{stage_id}' of execution '{execution_id}' not found")
        return self._stage(rows[0])

    def list_stages(self, execution_id: str) -> list[StageState]:
        return [self._stage(r) for r in
                self._query("SELECT * FROM stages WHERE execution_id = ? ORDER BY position", (execution_id,))]

    def update_stage(self, stage: StageState) -> None:
        cursor = self._execute(
            "UPDATE stages SET status=?, attempts=?, error=?, approved_by=?, started_at=?, completed_at=? "
            "WHERE execution_id=? AND stage_id=?",
            (stage.status.value, stage.attempts, self._error(stage.error), stage.approved_by, stage.started_at,
             stage.completed_at, stage.execution_id, stage.stage_id),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(f"stage '{stage.stage_id}' of execution '{stage.execution_id}' not found")

    # ------------------------------------------------------------------ agent runs

    def save_agent_run(self, run: AgentRun) -> None:
        columns = ", ".join(_AGENT_RUN_COLUMNS)
        placeholders = ", ".join("?" for _ in _AGENT_RUN_COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in _AGENT_RUN_MUTABLE)
        self._execute(
            f"INSERT INTO agent_runs ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(run_id) DO UPDATE SET {updates}",
            (run.run_id, run.execution_id, run.stage_id, run.agent_id, run.task_id, run.attempt, run.status.value,
             self._json(run.task, "AgentTask"), self._json(run.result, "AgentResult"), self._error(run.error),
             run.created_at, run.started_at, run.completed_at, run.session_id, run.cost_usd, run.duration_ms,
             run.num_turns, self._json(dict(run.sdk_metadata or {}), "SDK metadata"), run.input_tokens,
             run.output_tokens, run.error.code if run.error else None),
        )

    def _run(self, row: sqlite3.Row) -> AgentRun:
        return AgentRun(
            run_id=row["run_id"], execution_id=row["execution_id"], stage_id=row["stage_id"],
            agent_id=row["agent_id"], task_id=row["task_id"], attempt=row["attempt"],
            status=AgentRunStatus(row["status"]), task=self._load(row["task"]), result=self._load(row["result"]),
            error=FailureInfo.from_dict(self._load(row["error"])), created_at=row["created_at"],
            started_at=row["started_at"], completed_at=row["completed_at"], session_id=row["session_id"],
            cost_usd=row["cost_usd"], duration_ms=row["duration_ms"], num_turns=row["num_turns"],
            sdk_metadata=self._load(row["sdk_metadata"]) or {}, input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
        )

    # ------------------------------------------------------------------ run records (Phase 5.3)

    def _record(self, row: sqlite3.Row) -> RunRecord:
        run = AgentRun(
            run_id=row["run_id"], execution_id=row["execution_id"], stage_id=row["stage_id"],
            agent_id=row["agent_id"], task_id=row["task_id"], attempt=row["attempt"],
            status=AgentRunStatus(row["status"]), task={}, created_at=row["created_at"],
            error=FailureInfo.from_dict(self._load(row["error"])), started_at=row["started_at"],
            completed_at=row["completed_at"], session_id=row["session_id"], cost_usd=row["cost_usd"],
            duration_ms=row["duration_ms"], num_turns=row["num_turns"],
            sdk_metadata=self._load(row["sdk_metadata"]) or {}, input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
        )
        return build_run_record(run, workflow_id=row["workflow_id"], workflow_version=row["workflow_version"],
                                result_status=row["result_status"])

    def get_run_record(self, run_id: str) -> RunRecord:
        rows = self._query(_RECORD_SELECT + " WHERE r.run_id = ?", (run_id,))
        if not rows:
            raise NotFoundError(f"agent run '{run_id}' not found")
        return self._record(rows[0])

    def list_run_records(self, *, execution_id: str | None = None, workflow_id: str | None = None,
                         stage_id: str | None = None, agent_id: str | None = None,
                         status: AgentRunStatus | None = None, error_code: str | None = None,
                         limit: int = 100, newest_first: bool = True) -> list[RunRecord]:
        """RunRecords matching every given filter. ``newest_first=False`` gives chronological history."""
        sql, params = _RECORD_SELECT + " WHERE 1=1", []
        for column, value in (("r.execution_id", execution_id), ("e.workflow_id", workflow_id),
                              ("r.stage_id", stage_id), ("r.agent_id", agent_id), ("r.error_code", error_code),
                              ("r.status", AgentRunStatus(status).value if status else None)):
            if value:
                sql += f" AND {column} = ?"
                params.append(value)
        order = "DESC" if newest_first else "ASC"
        sql += f" ORDER BY r.created_at {order}, r.rowid {order} LIMIT ?"
        params.append(max(int(limit), 0))
        return [self._record(r) for r in self._query(sql, params)]

    def get_agent_run(self, run_id: str) -> AgentRun:
        rows = self._query("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,))
        if not rows:
            raise NotFoundError(f"agent run '{run_id}' not found")
        return self._run(rows[0])

    def list_agent_runs(self, execution_id: str, stage_id: str | None = None) -> list[AgentRun]:
        sql, params = "SELECT * FROM agent_runs WHERE execution_id = ?", [execution_id]
        if stage_id:
            sql += " AND stage_id = ?"
            params.append(stage_id)
        return [self._run(r) for r in self._query(sql + " ORDER BY created_at, attempt", params)]

    # ------------------------------------------------------------------ checkpoints

    def add_checkpoint(self, execution_id: str, *, reason: str, state: Mapping[str, Any],
                       stage_id: str | None = None) -> Checkpoint:
        payload = self._json(dict(state), "checkpoint state")
        with self.transaction():
            row = self._query("SELECT COALESCE(MAX(sequence), 0) + 1 FROM checkpoints WHERE execution_id = ?",
                              (execution_id,))
            checkpoint = Checkpoint(checkpoint_id=uuid.uuid4().hex, execution_id=execution_id, sequence=row[0][0],
                                    reason=reason, state=dict(state), created_at=utcnow(), stage_id=stage_id)
            self._execute("INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
                          (checkpoint.checkpoint_id, execution_id, checkpoint.sequence, stage_id, reason, payload,
                           checkpoint.created_at))
        return checkpoint

    def _checkpoint(self, row: sqlite3.Row) -> Checkpoint:
        return Checkpoint(checkpoint_id=row["checkpoint_id"], execution_id=row["execution_id"],
                          sequence=row["sequence"], reason=row["reason"], state=self._load(row["state"]),
                          created_at=row["created_at"], stage_id=row["stage_id"])

    def latest_checkpoint(self, execution_id: str) -> Checkpoint | None:
        rows = self._query("SELECT * FROM checkpoints WHERE execution_id = ? ORDER BY sequence DESC LIMIT 1",
                           (execution_id,))
        return self._checkpoint(rows[0]) if rows else None

    def list_checkpoints(self, execution_id: str) -> list[Checkpoint]:
        return [self._checkpoint(r) for r in
                self._query("SELECT * FROM checkpoints WHERE execution_id = ? ORDER BY sequence", (execution_id,))]

    # ------------------------------------------------------------------ logs & artifacts

    def append_log(self, entry: LogEntry) -> None:
        self._execute(
            "INSERT INTO execution_logs (execution_id, timestamp, level, event, message, stage_id, run_id, data) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (entry.execution_id, entry.timestamp, entry.level, entry.event, entry.message, entry.stage_id,
             entry.run_id, self._json(dict(entry.data), "log data")),
        )

    def list_logs(self, execution_id: str) -> list[LogEntry]:
        return [
            LogEntry(execution_id=r["execution_id"], event=r["event"], message=r["message"], level=r["level"],
                     timestamp=r["timestamp"], stage_id=r["stage_id"], run_id=r["run_id"],
                     data=self._load(r["data"]) or {}, id=r["id"])
            for r in self._query("SELECT * FROM execution_logs WHERE execution_id = ? ORDER BY id", (execution_id,))
        ]

    def add_artifact(self, record: ArtifactRecord) -> None:
        if not record.uri:
            raise StorageError("artifact records need a uri")
        self._execute(
            "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (record.artifact_id, record.execution_id, record.stage_id, record.run_id, record.name, record.uri,
             record.media_type, record.sha256, record.size_bytes, record.description, record.created_at),
        )

    def list_artifacts(self, execution_id: str) -> list[ArtifactRecord]:
        return [
            ArtifactRecord(artifact_id=r["artifact_id"], execution_id=r["execution_id"], uri=r["uri"],
                           created_at=r["created_at"], stage_id=r["stage_id"], run_id=r["run_id"], name=r["name"],
                           media_type=r["media_type"], sha256=r["sha256"], size_bytes=r["size_bytes"],
                           description=r["description"])
            for r in self._query("SELECT * FROM artifacts WHERE execution_id = ? ORDER BY created_at, artifact_id",
                                 (execution_id,))
        ]
