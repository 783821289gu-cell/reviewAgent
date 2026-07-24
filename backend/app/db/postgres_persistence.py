from hashlib import sha256
import logging
import ntpath
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, make_url

from db.postgres_models import APP_SCHEMA, Base
from db.postgres_repositories import (
    PostgresDocumentRepository,
    PostgresEventRepository,
    PostgresReviewResultRepository,
    PostgresTaskRepository,
)
from db.repositories import RecoveryError
from models.review import AgentState, LLMMode, ReviewPosition, ReviewStatus


LOGGER = logging.getLogger(__name__)


class PostgresReviewPersistence:
    def __init__(
        self,
        database_url: str,
        upload_dir: str,
        *,
        event_notifier=None,
        engine: Engine | None = None,
    ):
        normalized_url = str(database_url).strip()
        if not normalized_url:
            raise ValueError("PostgreSQL database URL is required")
        self.engine = engine or create_engine(normalized_url, pool_pre_ping=True)
        if self.engine.dialect.name != "postgresql":
            if engine is None:
                self.engine.dispose()
            raise ValueError("PostgresReviewPersistence requires PostgreSQL")
        self.store_key = make_url(normalized_url).render_as_string(hide_password=True)
        self.db_path = None
        self.upload_dir = Path(upload_dir).resolve()
        self.event_notifier = event_notifier
        self.task_repository = PostgresTaskRepository(self.engine)
        self.document_repository = PostgresDocumentRepository(self.engine)
        self.review_result_repository = PostgresReviewResultRepository(self.engine)
        self.event_repository = PostgresEventRepository(self.engine)
        self._require_schema()

    def create_task(self, state: AgentState, event: dict, content: bytes | None) -> None:
        if ntpath.basename(state.file_name) != state.file_name:
            raise ValueError("client file paths are not allowed in task storage")
        upload_path, temporary_path, file_hash, stored_path = self._write_upload(
            state,
            content,
        )
        try:
            with self.engine.begin() as connection:
                self.task_repository.save(
                    connection,
                    state,
                    current_node=str(event["step_name"]),
                )
                if content is not None:
                    self.document_repository.save_upload(
                        connection,
                        state.task_id,
                        file_hash,
                        stored_path,
                    )
                self.review_result_repository.save(connection, state)
                self.event_repository.append(connection, event)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if upload_path is not None:
                upload_path.unlink(missing_ok=True)
            raise
        self._publish_committed_event(event)

    def save_state_and_event(
        self,
        state: AgentState,
        event: dict,
        recovery_from_status: str | None = None,
    ) -> None:
        with self.engine.begin() as connection:
            self.task_repository.save(
                connection,
                state,
                current_node=str(event["step_name"]),
            )
            if recovery_from_status is not None:
                self.task_repository.mark_recovery(
                    connection,
                    state.task_id,
                    recovery_from_status,
                )
            if _is_transient_progress_event(event):
                self.document_repository.save_progress(
                    connection,
                    state,
                    str(event["step_name"]),
                )
                self.review_result_repository.save_progress(
                    connection,
                    state,
                    str(event["step_name"]),
                )
            else:
                self.document_repository.save_results(connection, state)
                self.review_result_repository.save(connection, state)
            self.event_repository.append(connection, event)
        self._publish_committed_event(event)

    def load_states(
        self,
        *,
        clear_execution_leases: bool = True,
    ) -> list[tuple[AgentState, list[dict]]]:
        if clear_execution_leases:
            self.task_repository.clear_execution_leases()
        persisted = []
        for task in self.task_repository.list_all():
            try:
                status = ReviewStatus(task["status"])
                review_position = ReviewPosition(task["review_position"])
                llm_mode = LLMMode(task["llm_mode"])
            except ValueError as exc:
                raise RecoveryError(
                    f"task {task['task_id']} has invalid persisted enum data: {exc}"
                ) from exc
            events = self.event_repository.list_for_task(task["task_id"])
            state = AgentState(
                task_id=task["task_id"],
                trace_id=task["trace_id"],
                status=status,
                file_name=task["file_name"],
                file_type=task["file_type"],
                review_position=review_position,
                message=task["message"],
                llm_mode=llm_mode,
                document=self.document_repository.get_document(task["task_id"]),
                contract_classification=task["contract_classification"],
                clauses=self.document_repository.get_clauses(task["task_id"]),
                matched_rules=task["matched_rules"],
                review_contexts=task["review_contexts"],
                analysis_results=task["analysis_results"],
                risk_findings=self.review_result_repository.get_risks(task["task_id"]),
                evidence_results=task["evidence_results"],
                report_file=task["report_file"],
                logs=self.review_result_repository.get_logs(task["task_id"]),
                events=[_event_without_task(event) for event in events],
                recovery_count=task["recovery_count"],
                recovery_from_status=task["recovery_from_status"],
                retry_counts=task["retry_counts"],
                recovery_history=task["recovery_history"],
                cancel_requested_at=task["cancel_requested_at"],
                cancelled_at=task["cancelled_at"],
                cancel_reason=task["cancel_reason"],
                execution_active=False,
                last_timeout=task["last_timeout"],
                progress=task["progress"],
            )
            persisted.append((state, events))
        return persisted

    def load_upload(self, task_id: str) -> bytes:
        upload = self.document_repository.get_upload(task_id)
        if upload is None or not upload["stored_path"]:
            raise RecoveryError("upload record is missing; the task cannot resume")
        upload_path = self._upload_path(str(upload["stored_path"]))
        if not upload_path.is_file():
            raise RecoveryError("uploaded file is missing; the task cannot resume")
        try:
            content = upload_path.read_bytes()
        except OSError as exc:
            reason = str(exc.strerror or exc.__class__.__name__)
            raise RecoveryError(
                f"uploaded file cannot be read ({reason}); the task cannot resume"
            ) from exc
        if sha256(content).hexdigest() != str(upload["file_hash"]):
            raise RecoveryError(
                "uploaded file SHA-256 does not match; the task cannot resume"
            )
        return content

    def dispose(self) -> None:
        self.engine.dispose()

    def _write_upload(
        self,
        state: AgentState,
        content: bytes | None,
    ) -> tuple[Path | None, Path | None, str, str]:
        if content is None:
            return None, None, "", ""
        stored_path = f"{state.task_id}.{state.file_type}"
        upload_path = self._upload_path(stored_path)
        temporary_path = upload_path.with_suffix(f"{upload_path.suffix}.tmp")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        try:
            temporary_path.write_bytes(content)
            temporary_path.replace(upload_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            upload_path.unlink(missing_ok=True)
            raise
        return upload_path, temporary_path, sha256(content).hexdigest(), stored_path

    def _upload_path(self, stored_path: str) -> Path:
        if not stored_path or Path(stored_path).name != stored_path:
            raise RecoveryError("upload storage path is unsafe")
        resolved = (self.upload_dir / stored_path).resolve()
        if resolved.parent != self.upload_dir:
            raise RecoveryError("upload storage path escapes the configured directory")
        return resolved

    def _publish_committed_event(self, event: dict) -> None:
        if self.event_notifier is None:
            return
        sent = self.event_notifier.publish(
            str(event["task_id"]),
            int(event["event_id"]),
        )
        if not sent:
            LOGGER.warning(
                "PostgreSQL event committed without Redis notification",
                extra={
                    "task_id": str(event["task_id"]),
                    "event_id": int(event["event_id"]),
                },
            )

    def _require_schema(self) -> None:
        with self.engine.connect() as connection:
            tables = set(inspect(connection).get_table_names(schema=APP_SCHEMA))
        required = {
            table.name
            for table in Base.metadata.tables.values()
            if table.schema == APP_SCHEMA
        }
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError(
                "PostgreSQL app schema is not upgraded; missing tables: "
                + ", ".join(missing)
            )


def _event_without_task(event: dict) -> dict:
    return {key: value for key, value in event.items() if key != "task"}


def _is_transient_progress_event(event: dict) -> bool:
    return str(event.get("step_name", "")).endswith("_progress")
