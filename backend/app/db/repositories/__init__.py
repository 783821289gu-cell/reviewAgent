from hashlib import sha256
import ntpath
from pathlib import Path

from db.repositories.document_repository import DocumentRepository
from db.repositories.event_repository import EventRepository
from db.repositories.memory_repository import MemoryRepository
from db.repositories.review_result_repository import ReviewResultRepository
from db.repositories.task_repository import TaskRepository
from db.sqlite import connect
from models.review import AgentState, LLMMode, ReviewPosition, ReviewStatus


class RecoveryError(ValueError):
    pass


class ReviewPersistence:
    def __init__(self, db_path: str, upload_dir: str):
        self.db_path = db_path
        self.upload_dir = Path(upload_dir).resolve()
        self.task_repository = TaskRepository(db_path)
        self.document_repository = DocumentRepository(db_path)
        self.review_result_repository = ReviewResultRepository(db_path)
        self.event_repository = EventRepository(db_path)
        self.memory_repository = MemoryRepository(db_path)
        connection = connect(db_path)
        connection.close()

    def create_task(self, state: AgentState, event: dict, content: bytes | None) -> None:
        if ntpath.basename(state.file_name) != state.file_name:
            raise ValueError("客户端文件路径不允许写入任务仓储。")
        stored_path = ""
        file_hash = ""
        upload_path = None
        temporary_path = None
        if content is not None:
            stored_path = f"{state.task_id}.{state.file_type}"
            upload_path = self._upload_path(stored_path)
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            temporary_path = upload_path.with_suffix(f"{upload_path.suffix}.tmp")
            try:
                temporary_path.write_bytes(content)
                temporary_path.replace(upload_path)
                file_hash = sha256(content).hexdigest()
            except Exception:
                temporary_path.unlink(missing_ok=True)
                upload_path.unlink(missing_ok=True)
                raise

        connection = connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.task_repository.save(connection, state, current_node=event["step_name"])
            if content is not None:
                self.document_repository.save_upload(
                    connection,
                    state.task_id,
                    file_hash,
                    stored_path,
                )
            self.review_result_repository.save(connection, state)
            self.event_repository.append(connection, event)
            connection.commit()
        except Exception:
            connection.rollback()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if upload_path is not None:
                upload_path.unlink(missing_ok=True)
            raise
        finally:
            connection.close()

    def save_state_and_event(
        self,
        state: AgentState,
        event: dict,
        recovery_from_status: str | None = None,
    ) -> None:
        connection = connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.task_repository.save(connection, state, current_node=event["step_name"])
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
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
                raise RecoveryError(f"任务 {task['task_id']} 的持久化枚举值无效：{exc}") from exc
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
            raise RecoveryError("上传文件记录缺失，无法安全恢复。")
        upload_path = self._upload_path(upload["stored_path"])
        if not upload_path.is_file():
            raise RecoveryError("上传文件缺失，无法安全恢复。")
        try:
            content = upload_path.read_bytes()
        except OSError as exc:
            reason = str(exc.strerror or exc.__class__.__name__)
            raise RecoveryError(f"上传文件无法读取（{reason}），无法安全恢复。") from exc
        if sha256(content).hexdigest() != upload["file_hash"]:
            raise RecoveryError("上传文件 SHA-256 不一致，无法安全恢复。")
        return content

    def _upload_path(self, stored_path: str) -> Path:
        if not stored_path or Path(stored_path).name != stored_path:
            raise RecoveryError("上传文件存储路径不安全，无法访问。")
        resolved = (self.upload_dir / stored_path).resolve()
        if resolved.parent != self.upload_dir:
            raise RecoveryError("上传文件存储路径越界，无法访问。")
        return resolved


def _event_without_task(event: dict) -> dict:
    return {key: value for key, value in event.items() if key != "task"}


def _is_transient_progress_event(event: dict) -> bool:
    return str(event.get("step_name", "")).endswith("_progress")


__all__ = [
    "DocumentRepository",
    "EventRepository",
    "MemoryRepository",
    "RecoveryError",
    "ReviewPersistence",
    "ReviewResultRepository",
    "TaskRepository",
]
