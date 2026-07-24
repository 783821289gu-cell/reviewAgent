from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import Connection, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import Engine

from db.postgres_models import (
    ClauseRow,
    DocumentRow,
    IdempotencyRecordRow,
    ReviewTaskRow,
    RiskFindingRow,
    StepLogRow,
    TaskEventRow,
)
from models.review import AgentState


ERROR_STATUSES = {
    "PARSE_FAILED",
    "RETRIEVAL_FAILED",
    "LLM_OUTPUT_INVALID",
    "EVIDENCE_MISSING",
    "NEED_MANUAL_REVIEW",
    "UNSUPPORTED_CONTRACT_TYPE",
    "NODE_TIMEOUT",
    "TASK_TIMEOUT",
    "TASK_ERROR",
}


class PostgresTaskRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def save(self, connection: Connection, state: AgentState, current_node: str) -> None:
        now = _utc_now()
        values = {
            "task_id": state.task_id,
            "trace_id": state.trace_id,
            "status": state.status.value,
            "file_name": state.file_name,
            "file_type": state.file_type,
            "review_position": state.review_position.value,
            "llm_mode": state.llm_mode.value,
            "message": state.message,
            "current_node": current_node,
            "error_message": state.message if state.status.value in ERROR_STATUSES else "",
            "contract_classification_json": state.contract_classification,
            "matched_rules_json": state.matched_rules,
            "review_contexts_json": state.review_contexts,
            "analysis_results_json": state.analysis_results,
            "evidence_results_json": state.evidence_results,
            "report_file_json": state.report_file,
            "retry_counts_json": dict(state.retry_counts or {}),
            "recovery_history_json": list(state.recovery_history or []),
            "cancel_requested_at": state.cancel_requested_at,
            "cancelled_at": state.cancelled_at,
            "cancel_reason": state.cancel_reason,
            "last_timeout_json": state.last_timeout,
            "progress_json": state.progress,
            "created_at": now,
            "updated_at": now,
        }
        statement = postgres_insert(ReviewTaskRow).values(**values)
        excluded = statement.excluded
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[ReviewTaskRow.task_id],
                set_={
                    column: getattr(excluded, column)
                    for column in values
                    if column not in {"task_id", "created_at"}
                },
            )
        )

    def list_all(self) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(ReviewTaskRow).order_by(
                    ReviewTaskRow.created_at,
                    ReviewTaskRow.task_id,
                )
            ).mappings()
            return [_task_payload(row) for row in rows]

    def get(self, task_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(ReviewTaskRow).where(ReviewTaskRow.task_id == task_id)
            ).mappings().first()
            return _task_payload(row) if row is not None else None

    def mark_recovery(
        self,
        connection: Connection,
        task_id: str,
        recovery_from_status: str,
    ) -> None:
        result = connection.execute(
            update(ReviewTaskRow)
            .where(ReviewTaskRow.task_id == task_id)
            .values(
                recovery_count=ReviewTaskRow.recovery_count + 1,
                recovery_from_status=recovery_from_status,
                updated_at=_utc_now(),
            )
        )
        if result.rowcount != 1:
            raise ValueError(f"task not found: {task_id}")

    def try_acquire_execution(self, task_id: str, execution_owner: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(ReviewTaskRow)
                .where(
                    ReviewTaskRow.task_id == task_id,
                    ReviewTaskRow.execution_owner == "",
                )
                .values(
                    execution_owner=execution_owner,
                    updated_at=_utc_now(),
                )
            )
            return result.rowcount == 1

    def release_execution(self, task_id: str, execution_owner: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(ReviewTaskRow)
                .where(
                    ReviewTaskRow.task_id == task_id,
                    ReviewTaskRow.execution_owner == execution_owner,
                )
                .values(execution_owner="", updated_at=_utc_now())
            )

    def clear_execution_leases(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(ReviewTaskRow)
                .where(ReviewTaskRow.execution_owner != "")
                .values(execution_owner="", updated_at=_utc_now())
            )


class PostgresDocumentRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def save_upload(
        self,
        connection: Connection,
        task_id: str,
        file_hash: str,
        stored_path: str,
    ) -> None:
        now = _utc_now()
        statement = postgres_insert(DocumentRow).values(
            task_id=task_id,
            file_hash=file_hash,
            stored_path=stored_path,
            document_json=None,
            parsed_at=None,
            created_at=now,
            updated_at=now,
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[DocumentRow.task_id],
                set_={
                    "file_hash": statement.excluded.file_hash,
                    "stored_path": statement.excluded.stored_path,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    def save_results(self, connection: Connection, state: AgentState) -> None:
        now = _utc_now()
        if state.document is not None:
            statement = postgres_insert(DocumentRow).values(
                task_id=state.task_id,
                file_hash="",
                stored_path="",
                document_json=state.document,
                parsed_at=now,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[DocumentRow.task_id],
                    set_={
                        "document_json": statement.excluded.document_json,
                        "parsed_at": func.coalesce(
                            DocumentRow.parsed_at,
                            statement.excluded.parsed_at,
                        ),
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )

        if state.clauses is None:
            return
        clause_ids = []
        for clause in state.clauses:
            clause_id = str(clause.get("clause_id", ""))
            if not clause_id:
                raise ValueError("persisted clause requires clause_id")
            clause_ids.append(clause_id)
            self._save_clause(connection, state.task_id, clause, now)
        _delete_missing(
            connection,
            ClauseRow,
            ClauseRow.task_id,
            ClauseRow.clause_id,
            state.task_id,
            clause_ids,
        )

    def save_progress(
        self,
        connection: Connection,
        state: AgentState,
        step_name: str,
    ) -> None:
        if step_name != "key_field_extract_progress":
            return
        clauses = list(state.clauses or [])
        if not clauses:
            connection.execute(
                delete(ClauseRow).where(ClauseRow.task_id == state.task_id)
            )
            return
        self._save_clause(connection, state.task_id, clauses[-1], _utc_now())

    def get_upload(self, task_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(DocumentRow.file_hash, DocumentRow.stored_path).where(
                    DocumentRow.task_id == task_id
                )
            ).mappings().first()
            return dict(row) if row is not None else None

    def get_document(self, task_id: str):
        with self.engine.connect() as connection:
            return connection.execute(
                select(DocumentRow.document_json).where(DocumentRow.task_id == task_id)
            ).scalar_one_or_none()

    def get_clauses(self, task_id: str) -> list[dict] | None:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(ClauseRow.payload_json)
                .where(ClauseRow.task_id == task_id)
                .order_by(ClauseRow.clause_id)
            ).scalars()
            payloads = list(rows)
            return payloads or None

    @staticmethod
    def _save_clause(
        connection: Connection,
        task_id: str,
        clause: dict,
        now: str,
    ) -> None:
        clause_id = str(clause.get("clause_id", ""))
        statement = postgres_insert(ClauseRow).values(
            task_id=task_id,
            clause_id=clause_id,
            clause_type=str(clause.get("clause_type", "")),
            title=str(clause.get("title", "")),
            text=str(clause.get("text", "")),
            key_fields_json=clause.get("key_fields") or {},
            source_location_json=clause.get("source_location") or {},
            payload_json=clause,
            created_at=now,
            updated_at=now,
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[ClauseRow.task_id, ClauseRow.clause_id],
                set_={
                    "clause_type": statement.excluded.clause_type,
                    "title": statement.excluded.title,
                    "text": statement.excluded.text,
                    "key_fields_json": statement.excluded.key_fields_json,
                    "source_location_json": statement.excluded.source_location_json,
                    "payload_json": statement.excluded.payload_json,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )


class PostgresReviewResultRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def save(self, connection: Connection, state: AgentState) -> None:
        self._save_risks(connection, state)
        self._save_logs(connection, state)

    def save_progress(
        self,
        connection: Connection,
        state: AgentState,
        step_name: str,
    ) -> None:
        if step_name == "evidence_verification_progress":
            self._save_risks(connection, state)
        self._save_new_logs(connection, state)

    def get_risks(self, task_id: str) -> list[dict] | None:
        with self.engine.connect() as connection:
            payloads = list(
                connection.execute(
                    select(RiskFindingRow.payload_json)
                    .where(RiskFindingRow.task_id == task_id)
                    .order_by(RiskFindingRow.risk_id)
                ).scalars()
            )
            return payloads or None

    def get_logs(self, task_id: str) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(StepLogRow)
                .where(StepLogRow.task_id == task_id)
                .order_by(StepLogRow.log_index)
            ).mappings()
            return [_log_payload(row) for row in rows]

    def get_idempotent_result(
        self,
        idempotency_key: str,
        operation: str,
    ) -> dict | None:
        with self.engine.connect() as connection:
            return connection.execute(
                select(IdempotencyRecordRow.result_json).where(
                    IdempotencyRecordRow.idempotency_key == idempotency_key,
                    IdempotencyRecordRow.operation == operation,
                )
            ).scalar_one_or_none()

    def save_idempotent_result(
        self,
        idempotency_key: str,
        operation: str,
        result: dict,
    ) -> dict:
        with self.engine.begin() as connection:
            connection.execute(
                postgres_insert(IdempotencyRecordRow)
                .values(
                    idempotency_key=idempotency_key,
                    operation=operation,
                    result_json=result,
                    created_at=_utc_now(),
                )
                .on_conflict_do_nothing(
                    index_elements=[IdempotencyRecordRow.idempotency_key]
                )
            )
            stored = connection.execute(
                select(IdempotencyRecordRow.result_json).where(
                    IdempotencyRecordRow.idempotency_key == idempotency_key,
                    IdempotencyRecordRow.operation == operation,
                )
            ).scalar_one_or_none()
            if stored is None:
                raise RuntimeError("idempotency result was not persisted")
            return stored

    def _save_risks(self, connection: Connection, state: AgentState) -> None:
        if state.risk_findings is None:
            return
        now = _utc_now()
        risk_ids = []
        for risk in state.risk_findings:
            risk_id = str(risk.get("risk_id", ""))
            if not risk_id:
                raise ValueError("persisted risk requires risk_id")
            risk_ids.append(risk_id)
            statement = postgres_insert(RiskFindingRow).values(
                task_id=state.task_id,
                risk_id=risk_id,
                status=str(risk.get("review_status", "")),
                severity=str(risk.get("severity", "")),
                clause_id=str(risk.get("clause_id", "")),
                payload_json=risk,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        RiskFindingRow.task_id,
                        RiskFindingRow.risk_id,
                    ],
                    set_={
                        "status": statement.excluded.status,
                        "severity": statement.excluded.severity,
                        "clause_id": statement.excluded.clause_id,
                        "payload_json": statement.excluded.payload_json,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )
        _delete_missing(
            connection,
            RiskFindingRow,
            RiskFindingRow.task_id,
            RiskFindingRow.risk_id,
            state.task_id,
            risk_ids,
        )

    def _save_logs(self, connection: Connection, state: AgentState) -> None:
        if state.logs is None:
            return
        now = _utc_now()
        for index, log in enumerate(state.logs):
            self._save_log(connection, state, index, log, now)
        connection.execute(
            delete(StepLogRow).where(
                StepLogRow.task_id == state.task_id,
                StepLogRow.log_index >= len(state.logs),
            )
        )

    def _save_new_logs(self, connection: Connection, state: AgentState) -> None:
        if state.logs is None:
            return
        existing_count = int(
            connection.execute(
                select(func.count())
                .select_from(StepLogRow)
                .where(StepLogRow.task_id == state.task_id)
            ).scalar_one()
        )
        if existing_count > len(state.logs):
            raise ValueError("incremental logs cannot remove persisted entries")
        now = _utc_now()
        for index in range(existing_count, len(state.logs)):
            self._save_log(connection, state, index, state.logs[index], now)

    @staticmethod
    def _save_log(
        connection: Connection,
        state: AgentState,
        index: int,
        log: dict,
        now: str,
    ) -> None:
        values = {
            "task_id": state.task_id,
            "log_index": index,
            "trace_id": str(log.get("trace_id", state.trace_id)),
            "step_id": str(log.get("step_id", "")),
            "step_name": str(log.get("step_name", "")),
            "tool_name": str(log.get("tool_name", "")),
            "status": str(log.get("status", "")),
            "latency_ms": int(log.get("latency_ms", 0)),
            "input_summary": str(log.get("input_summary", "")),
            "output_summary": str(log.get("output_summary", "")),
            "token_cost_summary": str(log.get("token_cost_summary", "")),
            "error_message": str(log.get("error_message", "")),
            "parent_step_id": str(log.get("parent_step_id", "")),
            "retry_index": int(log.get("retry_index", 0)),
            "idempotency_key": str(log.get("idempotency_key", "")),
            "trace_summary_json": log.get("trace_summary"),
            "created_at": now,
        }
        statement = postgres_insert(StepLogRow).values(**values)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[StepLogRow.task_id, StepLogRow.log_index],
                set_={
                    column: getattr(statement.excluded, column)
                    for column in values
                    if column not in {"task_id", "log_index", "created_at"}
                },
            )
        )


class PostgresEventRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def append(self, connection: Connection, event: dict) -> None:
        values = {
            "task_id": str(event["task_id"]),
            "event_id": int(event["event_id"]),
            "status": str(event["status"]),
            "message": str(event["message"]),
            "step_name": str(event.get("step_name", "")),
            "tool_name": str(event.get("tool_name", "")),
            "created_at": str(event["created_at"]),
            "payload_json": event,
        }
        result = connection.execute(
            postgres_insert(TaskEventRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[TaskEventRow.task_id, TaskEventRow.event_id]
            )
        )
        if result.rowcount == 1:
            return
        existing = connection.execute(
            select(TaskEventRow.payload_json).where(
                TaskEventRow.task_id == values["task_id"],
                TaskEventRow.event_id == values["event_id"],
            )
        ).scalar_one()
        if _canonical_json(existing) != _canonical_json(event):
            raise RuntimeError(
                "event idempotency conflict for "
                f"{values['task_id']}:{values['event_id']}"
            )

    def list_for_task(self, task_id: str) -> list[dict]:
        return self.list_after(task_id, 0)

    def list_after(
        self,
        task_id: str,
        next_event_id: int,
        *,
        limit: int | None = None,
    ) -> list[dict]:
        statement = (
            select(TaskEventRow.payload_json)
            .where(
                TaskEventRow.task_id == task_id,
                TaskEventRow.event_id >= next_event_id,
            )
            .order_by(TaskEventRow.event_id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self.engine.connect() as connection:
            return list(connection.execute(statement).scalars())

    def task_exists(self, task_id: str) -> bool:
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    select(ReviewTaskRow.task_id).where(
                        ReviewTaskRow.task_id == task_id
                    )
                ).scalar_one_or_none()
                is not None
            )


def _delete_missing(
    connection: Connection,
    model,
    task_column,
    id_column,
    task_id: str,
    identifiers: list[str],
) -> None:
    statement = delete(model).where(task_column == task_id)
    if identifiers:
        statement = statement.where(id_column.not_in(identifiers))
    connection.execute(statement)


def _task_payload(row: Any) -> dict:
    return {
        "task_id": str(row["task_id"]),
        "trace_id": str(row["trace_id"]),
        "status": str(row["status"]),
        "file_name": str(row["file_name"]),
        "file_type": str(row["file_type"]),
        "review_position": str(row["review_position"]),
        "llm_mode": str(row["llm_mode"]),
        "message": str(row["message"]),
        "current_node": str(row["current_node"]),
        "error_message": str(row["error_message"]),
        "contract_classification": row["contract_classification_json"],
        "matched_rules": row["matched_rules_json"],
        "review_contexts": row["review_contexts_json"],
        "analysis_results": row["analysis_results_json"],
        "evidence_results": row["evidence_results_json"],
        "report_file": row["report_file_json"],
        "recovery_count": int(row["recovery_count"]),
        "recovery_from_status": str(row["recovery_from_status"]),
        "retry_counts": dict(row["retry_counts_json"] or {}),
        "recovery_history": list(row["recovery_history_json"] or []),
        "cancel_requested_at": str(row["cancel_requested_at"]),
        "cancelled_at": str(row["cancelled_at"]),
        "cancel_reason": str(row["cancel_reason"]),
        "execution_owner": str(row["execution_owner"]),
        "last_timeout": row["last_timeout_json"],
        "progress": row["progress_json"],
    }


def _log_payload(row: Any) -> dict:
    return {
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
        "trace_summary": row["trace_summary_json"],
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
