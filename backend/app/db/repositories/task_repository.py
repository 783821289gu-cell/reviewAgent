import json
import sqlite3
from datetime import datetime, timezone

from db.sqlite import connect
from models.review import AgentState


ERROR_STATUSES = {
    "PARSE_FAILED",
    "RETRIEVAL_FAILED",
    "LLM_OUTPUT_INVALID",
    "EVIDENCE_MISSING",
    "NEED_MANUAL_REVIEW",
    "UNSUPPORTED_CONTRACT_TYPE",
    "TASK_ERROR",
}


class TaskRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def save(self, connection: sqlite3.Connection, state: AgentState, current_node: str) -> None:
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO review_tasks (
                task_id, trace_id, status, file_name, file_type, review_position, message,
                current_node, error_message, contract_classification_json,
                matched_rules_json, review_contexts_json,
                analysis_results_json, evidence_results_json, report_file_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                trace_id = excluded.trace_id,
                status = excluded.status,
                file_name = excluded.file_name,
                file_type = excluded.file_type,
                review_position = excluded.review_position,
                message = excluded.message,
                current_node = excluded.current_node,
                error_message = excluded.error_message,
                contract_classification_json = excluded.contract_classification_json,
                matched_rules_json = excluded.matched_rules_json,
                review_contexts_json = excluded.review_contexts_json,
                analysis_results_json = excluded.analysis_results_json,
                evidence_results_json = excluded.evidence_results_json,
                report_file_json = excluded.report_file_json,
                updated_at = excluded.updated_at
            """,
            (
                state.task_id,
                state.trace_id,
                state.status.value,
                state.file_name,
                state.file_type,
                state.review_position.value,
                state.message,
                current_node,
                state.message if state.status.value in ERROR_STATUSES else "",
                _json_dump(state.contract_classification),
                _json_dump(state.matched_rules),
                _json_dump(state.review_contexts),
                _json_dump(state.analysis_results),
                _json_dump(state.evidence_results),
                _json_dump(state.report_file),
                now,
                now,
            ),
        )

    def list_all(self) -> list[dict]:
        connection = connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT * FROM review_tasks ORDER BY created_at, task_id"
            ).fetchall()
            return [_task_payload(row) for row in rows]
        finally:
            connection.close()

    def get(self, task_id: str) -> dict | None:
        connection = connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT * FROM review_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return _task_payload(row) if row is not None else None
        finally:
            connection.close()

    def mark_recovery(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        recovery_from_status: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE review_tasks
            SET recovery_count = recovery_count + 1,
                recovery_from_status = ?,
                updated_at = ?
            WHERE task_id = ?
            """,
            (recovery_from_status, _utc_now(), task_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"task not found: {task_id}")


def _task_payload(row: sqlite3.Row) -> dict:
    return {
        "task_id": str(row["task_id"]),
        "trace_id": str(row["trace_id"]),
        "status": str(row["status"]),
        "file_name": str(row["file_name"]),
        "file_type": str(row["file_type"]),
        "review_position": str(row["review_position"]),
        "message": str(row["message"]),
        "current_node": str(row["current_node"]),
        "contract_classification": _json_load(row["contract_classification_json"]),
        "matched_rules": _json_load(row["matched_rules_json"]),
        "review_contexts": _json_load(row["review_contexts_json"]),
        "analysis_results": _json_load(row["analysis_results_json"]),
        "evidence_results": _json_load(row["evidence_results_json"]),
        "report_file": _json_load(row["report_file_json"]),
        "recovery_count": int(row["recovery_count"]),
        "recovery_from_status": str(row["recovery_from_status"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _json_dump(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(value: str | None):
    if value is None:
        return None
    return json.loads(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
