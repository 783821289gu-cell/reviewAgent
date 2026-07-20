import json
import sqlite3
from datetime import datetime, timezone

from db.sqlite import connect
from models.review import AgentState


class ReviewResultRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def save(self, connection: sqlite3.Connection, state: AgentState) -> None:
        self._save_risks(connection, state)
        self._save_logs(connection, state)

    def _save_risks(self, connection: sqlite3.Connection, state: AgentState) -> None:
        if state.risk_findings is None:
            return
        now = _utc_now()
        risk_ids = []
        for risk in state.risk_findings:
            risk_id = str(risk.get("risk_id", ""))
            if not risk_id:
                raise ValueError("persisted risk requires risk_id")
            risk_ids.append(risk_id)
            connection.execute(
                """
                INSERT INTO risk_findings (
                    task_id, risk_id, status, severity, clause_id, payload_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, risk_id) DO UPDATE SET
                    status = excluded.status,
                    severity = excluded.severity,
                    clause_id = excluded.clause_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.task_id,
                    risk_id,
                    str(risk.get("review_status", "")),
                    str(risk.get("severity", "")),
                    str(risk.get("clause_id", "")),
                    _json_dump(risk),
                    now,
                    now,
                ),
            )
        _delete_missing(connection, "risk_findings", state.task_id, "risk_id", risk_ids)

    def _save_logs(self, connection: sqlite3.Connection, state: AgentState) -> None:
        if state.logs is None:
            return
        now = _utc_now()
        for index, log in enumerate(state.logs):
            connection.execute(
                """
                INSERT INTO step_logs (
                    task_id, log_index, trace_id, step_id,
                    step_name, tool_name, status, latency_ms,
                    input_summary, output_summary, token_cost_summary, error_message,
                    parent_step_id, retry_index, idempotency_key, trace_summary_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, log_index) DO UPDATE SET
                    trace_id = excluded.trace_id,
                    step_id = excluded.step_id,
                    step_name = excluded.step_name,
                    tool_name = excluded.tool_name,
                    status = excluded.status,
                    latency_ms = excluded.latency_ms,
                    input_summary = excluded.input_summary,
                    output_summary = excluded.output_summary,
                    token_cost_summary = excluded.token_cost_summary,
                    error_message = excluded.error_message,
                    parent_step_id = excluded.parent_step_id,
                    retry_index = excluded.retry_index,
                    idempotency_key = excluded.idempotency_key,
                    trace_summary_json = excluded.trace_summary_json
                """,
                (
                    state.task_id,
                    index,
                    str(log.get("trace_id", state.trace_id)),
                    str(log.get("step_id", "")),
                    str(log.get("step_name", "")),
                    str(log.get("tool_name", "")),
                    str(log.get("status", "")),
                    int(log.get("latency_ms", 0)),
                    str(log.get("input_summary", "")),
                    str(log.get("output_summary", "")),
                    str(log.get("token_cost_summary", "")),
                    str(log.get("error_message", "")),
                    str(log.get("parent_step_id", "")),
                    int(log.get("retry_index", 0)),
                    str(log.get("idempotency_key", "")),
                    _json_dump(log.get("trace_summary")),
                    now,
                ),
            )
        connection.execute(
            "DELETE FROM step_logs WHERE task_id = ? AND log_index >= ?",
            (state.task_id, len(state.logs)),
        )

    def get_risks(self, task_id: str) -> list[dict] | None:
        connection = connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT payload_json FROM risk_findings WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows] if rows else None
        finally:
            connection.close()

    def get_logs(self, task_id: str) -> list[dict]:
        connection = connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT * FROM step_logs WHERE task_id = ? ORDER BY log_index",
                (task_id,),
            ).fetchall()
            return [
                {
                    "task_id": str(row["task_id"]),
                    "trace_id": str(row["trace_id"]),
                    "step_id": str(row["step_id"]),
                    "step_name": str(row["step_name"]),
                    "tool_name": str(row["tool_name"]),
                    "status": str(row["status"]),
                    "latency_ms": int(row["latency_ms"]),
                    "input_summary": str(row["input_summary"]),
                    "output_summary": str(row["output_summary"]),
                    "token_cost_summary": str(row["token_cost_summary"]),
                    "error_message": str(row["error_message"]),
                    "parent_step_id": str(row["parent_step_id"]),
                    "retry_index": int(row["retry_index"]),
                    "idempotency_key": str(row["idempotency_key"]),
                    "trace_summary": (
                        json.loads(row["trace_summary_json"])
                        if row["trace_summary_json"] is not None
                        else None
                    ),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def get_idempotent_result(self, idempotency_key: str, operation: str) -> dict | None:
        connection = connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT result_json FROM idempotency_records
                WHERE idempotency_key = ? AND operation = ?
                """,
                (idempotency_key, operation),
            ).fetchone()
            return json.loads(row["result_json"]) if row is not None else None
        finally:
            connection.close()

    def save_idempotent_result(self, idempotency_key: str, operation: str, result: dict) -> dict:
        connection = connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO idempotency_records (
                    idempotency_key, operation, result_json, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (idempotency_key, operation, _json_dump(result), _utc_now()),
            )
            row = connection.execute(
                """
                SELECT result_json FROM idempotency_records
                WHERE idempotency_key = ? AND operation = ?
                """,
                (idempotency_key, operation),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("idempotency result was not persisted")
            return json.loads(row["result_json"])
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _delete_missing(
    connection: sqlite3.Connection,
    table_name: str,
    task_id: str,
    id_column: str,
    identifiers: list[str],
) -> None:
    if not identifiers:
        connection.execute(f"DELETE FROM {table_name} WHERE task_id = ?", (task_id,))
        return
    placeholders = ",".join("?" for _ in identifiers)
    connection.execute(
        f"DELETE FROM {table_name} WHERE task_id = ? AND {id_column} NOT IN ({placeholders})",
        (task_id, *identifiers),
    )


def _json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
