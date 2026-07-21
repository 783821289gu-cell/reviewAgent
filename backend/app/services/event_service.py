from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Lock
from uuid import uuid4

from db.repositories import ReviewPersistence
from models.review import (
    AgentState,
    ReviewPosition,
    ReviewStatus,
    TaskCancelledError,
    trace_id_for_task,
)


TERMINAL_STATUSES = {
    ReviewStatus.EVIDENCE_VERIFIED,
    ReviewStatus.HUMAN_REVIEW_PENDING,
    ReviewStatus.MEMORY_UPDATED,
    ReviewStatus.PARSE_FAILED,
    ReviewStatus.RETRIEVAL_FAILED,
    ReviewStatus.LLM_OUTPUT_INVALID,
    ReviewStatus.EVIDENCE_MISSING,
    ReviewStatus.NEED_MANUAL_REVIEW,
    ReviewStatus.UNSUPPORTED_CONTRACT_TYPE,
    ReviewStatus.REPORT_READY,
    ReviewStatus.CANCELLED,
    ReviewStatus.NODE_TIMEOUT,
    ReviewStatus.TASK_TIMEOUT,
    ReviewStatus.TASK_ERROR,
}

ERROR_RETRY_CATEGORIES = {
    ReviewStatus.PARSE_FAILED: "parse",
    ReviewStatus.RETRIEVAL_FAILED: "retrieval",
    ReviewStatus.LLM_OUTPUT_INVALID: "llm_output",
    ReviewStatus.EVIDENCE_MISSING: "evidence",
    ReviewStatus.NODE_TIMEOUT: "node_timeout",
    ReviewStatus.TASK_TIMEOUT: "task_timeout",
    ReviewStatus.TASK_ERROR: "task",
}
ERROR_RETRY_LIMITS = {
    "parse": 2,
    "retrieval": 2,
    "llm_output": 2,
    "evidence": 2,
    "node_timeout": 2,
    "task_timeout": 2,
    "task": 2,
}
_PROCESS_EXECUTION_LOCK = Lock()
_PROCESS_EXECUTION_OWNERS: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class ReviewEvent:
    event_id: int
    task_id: str
    status: ReviewStatus
    message: str
    step_name: str
    tool_name: str
    created_at: str
    task: dict

    def to_dict(self, include_task: bool = False) -> dict:
        payload = {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "status": self.status.value,
            "message": self.message,
            "step_name": self.step_name,
            "tool_name": self.tool_name,
            "created_at": self.created_at,
        }
        if include_task:
            payload["task"] = deepcopy(self.task)
        return payload


class ReviewEventStore:
    def __init__(self, persistence: ReviewPersistence | None = None):
        self.persistence = persistence
        self._states: dict[str, AgentState] = {}
        self._events: dict[str, list[ReviewEvent]] = {}
        self._execution_owners: dict[str, str] = {}
        self._condition = Condition()

    @property
    def db_path(self) -> str | None:
        return self.persistence.db_path if self.persistence is not None else None

    def create_task(
        self,
        file_name: str,
        file_type: str,
        review_position: ReviewPosition,
        content: bytes | None = None,
    ) -> AgentState:
        task_id = f"task_{uuid4().hex[:12]}"
        state = AgentState(
            task_id=task_id,
            trace_id=trace_id_for_task(task_id),
            status=ReviewStatus.START,
            file_name=file_name,
            file_type=file_type,
            review_position=review_position,
            message="审查任务已创建，等待上传处理。",
            logs=[],
            events=[],
        )
        with self._condition:
            event = self._new_event_locked(
                state,
                [],
                ReviewStatus.START,
                state.message,
                step_name="create_task",
                tool_name="",
            )
            state.events = [event.to_dict()]
            if self.persistence is not None:
                self.persistence.create_task(state, event.to_dict(include_task=True), content)
            self._states[state.task_id] = state
            self._events[state.task_id] = [event]
            self._condition.notify_all()
            return self._snapshot_locked(state.task_id)

    def update_task(
        self,
        task_id: str,
        status: ReviewStatus,
        message: str,
        step_name: str,
        tool_name: str = "",
        document: dict | None = None,
        contract_classification: dict | None = None,
        clauses: list[dict] | None = None,
        matched_rules: list[dict] | None = None,
        review_contexts: list[dict] | None = None,
        analysis_results: list[dict] | None = None,
        risk_findings: list[dict] | None = None,
        evidence_results: list[dict] | None = None,
        report_file: dict | None = None,
        logs: list[dict] | None = None,
        recovery_from_status: str | None = None,
        retry_counts: dict | None = None,
        recovery_history: list[dict] | None = None,
        cancel_requested_at: str | None = None,
        cancelled_at: str | None = None,
        cancel_reason: str | None = None,
        last_timeout: dict | None = None,
    ) -> AgentState:
        with self._condition:
            state = deepcopy(self._states[task_id])
            if state.status in {ReviewStatus.CANCEL_REQUESTED, ReviewStatus.CANCELLED} and status not in {
                ReviewStatus.CANCEL_REQUESTED,
                ReviewStatus.CANCELLED,
            }:
                raise TaskCancelledError(f"task cancellation is active: {task_id}")
            state.status = status
            state.message = message
            if document is not None:
                state.document = deepcopy(document)
            if contract_classification is not None:
                state.contract_classification = deepcopy(contract_classification)
            if clauses is not None:
                state.clauses = deepcopy(clauses)
            if matched_rules is not None:
                state.matched_rules = deepcopy(matched_rules)
            if review_contexts is not None:
                state.review_contexts = deepcopy(review_contexts)
            if analysis_results is not None:
                state.analysis_results = deepcopy(analysis_results)
            if risk_findings is not None:
                state.risk_findings = deepcopy(risk_findings)
            if evidence_results is not None:
                state.evidence_results = deepcopy(evidence_results)
            if report_file is not None:
                state.report_file = deepcopy(report_file)
            if logs is not None:
                state.logs = deepcopy(logs)
            if recovery_from_status is not None:
                state.recovery_count += 1
                state.recovery_from_status = recovery_from_status
            if retry_counts is not None:
                state.retry_counts = deepcopy(retry_counts)
            category = ERROR_RETRY_CATEGORIES.get(status)
            previous_status = self._states[task_id].status
            if category and previous_status != status and retry_counts is None:
                state.retry_counts = dict(state.retry_counts or {})
                state.retry_counts[category] = int(state.retry_counts.get(category, 0)) + 1
            if recovery_history is not None:
                state.recovery_history = deepcopy(recovery_history)
            if cancel_requested_at is not None:
                state.cancel_requested_at = cancel_requested_at
            if cancelled_at is not None:
                state.cancelled_at = cancelled_at
            if cancel_reason is not None:
                state.cancel_reason = cancel_reason
            if last_timeout is not None:
                state.last_timeout = deepcopy(last_timeout)
            existing_events = self._events[task_id]
            event = self._new_event_locked(
                state,
                existing_events,
                status,
                message,
                step_name=step_name,
                tool_name=tool_name,
            )
            state.events = [item.to_dict() for item in existing_events] + [event.to_dict()]
            if self.persistence is not None:
                self.persistence.save_state_and_event(
                    state,
                    event.to_dict(include_task=True),
                    recovery_from_status=recovery_from_status,
                )
            self._states[task_id] = state
            self._events[task_id] = [*existing_events, event]
            self._condition.notify_all()
            return self._snapshot_locked(task_id)

    def load_persisted(self) -> list[AgentState]:
        if self.persistence is None:
            return []
        loaded_states = []
        with self._condition:
            if self._execution_owners:
                raise RuntimeError("cannot reload persisted state while tasks are executing")
            store_key = self._store_key()
            with _PROCESS_EXECUTION_LOCK:
                clear_execution_leases = not any(
                    key[0] == store_key for key in _PROCESS_EXECUTION_OWNERS
                )
            self._states.clear()
            self._events.clear()
            self._execution_owners.clear()
            for state, event_payloads in self.persistence.load_states(
                clear_execution_leases=clear_execution_leases,
            ):
                events = [
                    ReviewEvent(
                        event_id=int(payload["event_id"]),
                        task_id=str(payload["task_id"]),
                        status=ReviewStatus(payload["status"]),
                        message=str(payload["message"]),
                        step_name=str(payload.get("step_name", "")),
                        tool_name=str(payload.get("tool_name", "")),
                        created_at=str(payload["created_at"]),
                        task=dict(payload.get("task") or state.to_dict()),
                    )
                    for payload in event_payloads
                ]
                state.events = [event.to_dict() for event in events]
                self._states[state.task_id] = state
                self._events[state.task_id] = events
                loaded_states.append(self._snapshot_locked(state.task_id))
        return loaded_states

    def pending_tasks(self) -> list[AgentState]:
        with self._condition:
            return [
                self._snapshot_locked(task_id)
                for task_id, state in self._states.items()
                if state.status not in TERMINAL_STATUSES
            ]

    def record_recovery(self, task_id: str) -> AgentState:
        with self._condition:
            state = self._states.get(task_id)
            if state is None:
                raise ValueError(f"task not found: {task_id}")
            recovery_from = state.status.value
        if self.persistence is None:
            raise ValueError("recovery requires persistent event store")
        return self.update_task(
            task_id,
            state.status,
            f"任务从 {recovery_from} 节点恢复执行。",
            step_name="recovery_started",
            recovery_from_status=recovery_from,
        )

    def request_cancel(self, task_id: str, reason: str) -> AgentState:
        state = self.get_task(task_id)
        if state is None:
            raise ValueError(f"task not found: {task_id}")
        if state.status in {ReviewStatus.CANCEL_REQUESTED, ReviewStatus.CANCELLED}:
            return state
        if state.status in TERMINAL_STATUSES:
            raise ValueError(f"task is already terminal: {state.status.value}")
        now = datetime.now(timezone.utc).isoformat()
        return self.update_task(
            task_id,
            ReviewStatus.CANCEL_REQUESTED,
            f"Task cancellation was requested ({reason}); no new node will start.",
            step_name="cancel_requested",
            cancel_requested_at=now,
            cancel_reason=reason,
        )

    def finalize_cancel(self, task_id: str, logs: list[dict] | None = None) -> AgentState:
        state = self.get_task(task_id)
        if state is None:
            raise ValueError(f"task not found: {task_id}")
        if state.status == ReviewStatus.CANCELLED:
            return state
        return self.update_task(
            task_id,
            ReviewStatus.CANCELLED,
            "Task was cancelled; completed results and trace were retained.",
            step_name="task_cancelled",
            cancelled_at=datetime.now(timezone.utc).isoformat(),
            logs=logs,
        )

    def raise_if_cancelled(self, task_id: str) -> None:
        state = self.get_task(task_id)
        if state is not None and state.status in {
            ReviewStatus.CANCEL_REQUESTED,
            ReviewStatus.CANCELLED,
        }:
            raise TaskCancelledError(f"task cancellation is active: {task_id}")

    def record_manual_recovery(
        self,
        task_id: str,
        *,
        resume_from: ReviewStatus,
        operator_action: str,
        reason: str,
    ) -> AgentState:
        state = self.get_task(task_id)
        if state is None:
            raise ValueError(f"task not found: {task_id}")
        category = ERROR_RETRY_CATEGORIES.get(state.status)
        if category is None:
            raise ValueError(f"task status cannot be manually recovered: {state.status.value}")
        retry_count = int((state.retry_counts or {}).get(category, 0))
        retry_limit = ERROR_RETRY_LIMITS[category]
        if retry_count >= retry_limit:
            raise ValueError(
                f"retry budget exhausted for {category}: {retry_count}/{retry_limit}"
            )
        now = datetime.now(timezone.utc).isoformat()
        history = list(state.recovery_history or [])
        history.append(
            {
                "operator_action": operator_action,
                "reason": reason,
                "recovery_from_status": state.status.value,
                "resume_from_status": resume_from.value,
                "retry_category": category,
                "retry_count_before_recovery": retry_count,
                "retry_limit": retry_limit,
                "created_at": now,
            }
        )
        return self.update_task(
            task_id,
            resume_from,
            f"Manual recovery accepted from {state.status.value} at {resume_from.value}: "
            f"{operator_action} / {reason}",
            step_name="manual_recovery_started",
            recovery_from_status=state.status.value,
            recovery_history=history,
        )

    def try_acquire_execution(self, task_id: str, execution_owner: str) -> bool:
        with self._condition:
            state = self._states.get(task_id)
            if state is None or task_id in self._execution_owners:
                return False
            execution_key = self._execution_key(task_id)
            with _PROCESS_EXECUTION_LOCK:
                if execution_key in _PROCESS_EXECUTION_OWNERS:
                    return False
                _PROCESS_EXECUTION_OWNERS[execution_key] = execution_owner
            try:
                if (
                    self.persistence is not None
                    and not self.persistence.task_repository.try_acquire_execution(
                        task_id,
                        execution_owner,
                    )
                ):
                    with _PROCESS_EXECUTION_LOCK:
                        _PROCESS_EXECUTION_OWNERS.pop(execution_key, None)
                    return False
            except Exception:
                with _PROCESS_EXECUTION_LOCK:
                    _PROCESS_EXECUTION_OWNERS.pop(execution_key, None)
                raise
            self._execution_owners[task_id] = execution_owner
            state.execution_active = True
            self._condition.notify_all()
            return True

    def release_execution(self, task_id: str, execution_owner: str) -> None:
        with self._condition:
            if self._execution_owners.get(task_id) != execution_owner:
                return
            execution_key = self._execution_key(task_id)
            try:
                if self.persistence is not None:
                    self.persistence.task_repository.release_execution(task_id, execution_owner)
            finally:
                self._execution_owners.pop(task_id, None)
                with _PROCESS_EXECUTION_LOCK:
                    if _PROCESS_EXECUTION_OWNERS.get(execution_key) == execution_owner:
                        _PROCESS_EXECUTION_OWNERS.pop(execution_key, None)
                state = self._states.get(task_id)
                if state is not None:
                    state.execution_active = False
                self._condition.notify_all()

    def is_execution_active(self, task_id: str) -> bool:
        with _PROCESS_EXECUTION_LOCK:
            return self._execution_key(task_id) in _PROCESS_EXECUTION_OWNERS

    def _execution_key(self, task_id: str) -> tuple[str, str]:
        return self._store_key(), task_id

    def _store_key(self) -> str:
        if self.db_path is None:
            return f"memory:{id(self)}"
        return str(Path(self.db_path).resolve()).casefold()

    def get_task(self, task_id: str) -> AgentState | None:
        with self._condition:
            if task_id not in self._states:
                return None
            return self._snapshot_locked(task_id)

    def get_task_payload(self, task_id: str) -> dict | None:
        with self._condition:
            state = self._states.get(task_id)
            if state is None:
                return None
            events = [event.to_dict() for event in self._events.get(task_id, [])]
            return deepcopy(self._state_payload(state, events))

    def wait_for_events(self, task_id: str, next_index: int, timeout_seconds: float = 15.0) -> list[dict]:
        with self._condition:
            if task_id not in self._events:
                return []
            if len(self._events[task_id]) <= next_index:
                self._condition.wait(timeout=timeout_seconds)
            return [event.to_dict(include_task=True) for event in self._events.get(task_id, [])[next_index:]]

    def is_terminal(self, task_id: str) -> bool:
        with self._condition:
            state = self._states.get(task_id)
            return state is not None and state.status in TERMINAL_STATUSES

    def notify_waiters(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _new_event_locked(
        self,
        state: AgentState,
        existing_events: list[ReviewEvent],
        status: ReviewStatus,
        message: str,
        step_name: str,
        tool_name: str,
    ) -> ReviewEvent:
        event_id = len(existing_events)
        base_event = {
            "event_id": event_id,
            "task_id": state.task_id,
            "status": status.value,
            "message": message,
            "step_name": step_name,
            "tool_name": tool_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        task_snapshot = self._state_payload(
            state,
            events=[item.to_dict() for item in existing_events] + [base_event],
        )
        return ReviewEvent(
            event_id=event_id,
            task_id=state.task_id,
            status=status,
            message=message,
            step_name=step_name,
            tool_name=tool_name,
            created_at=base_event["created_at"],
            task=task_snapshot,
        )

    def _snapshot_locked(self, task_id: str) -> AgentState:
        return deepcopy(self._states[task_id])

    def _state_payload(self, state: AgentState, events: list[dict]) -> dict:
        return {
            "task_id": state.task_id,
            "trace_id": state.trace_id,
            "status": state.status.value,
            "file_name": state.file_name,
            "file_type": state.file_type,
            "review_position": state.review_position.value,
            "message": state.message,
            "document": state.document,
            "contract_classification": state.contract_classification,
            "clauses": state.clauses,
            "matched_rules": state.matched_rules,
            "review_contexts": state.review_contexts,
            "analysis_results": state.analysis_results,
            "risk_findings": state.risk_findings,
            "evidence_results": state.evidence_results,
            "report_file": state.report_file,
            "logs": list(state.logs or []),
            "events": events,
            "recovery_count": state.recovery_count,
            "recovery_from_status": state.recovery_from_status,
            "retry_counts": dict(state.retry_counts or {}),
            "retry_limits": dict(ERROR_RETRY_LIMITS),
            "recovery_history": list(state.recovery_history or []),
            "cancel_requested_at": state.cancel_requested_at,
            "cancelled_at": state.cancelled_at,
            "cancel_reason": state.cancel_reason,
            "execution_active": state.execution_active and state.status not in TERMINAL_STATUSES,
            "last_timeout": state.last_timeout,
        }


review_event_store = ReviewEventStore()
