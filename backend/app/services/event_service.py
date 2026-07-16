from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Condition
from uuid import uuid4

from db.repositories import ReviewPersistence
from models.review import AgentState, ReviewPosition, ReviewStatus, trace_id_for_task


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
    ReviewStatus.TASK_ERROR,
}


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
    ) -> AgentState:
        with self._condition:
            state = deepcopy(self._states[task_id])
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
            self._states.clear()
            self._events.clear()
            for state, event_payloads in self.persistence.load_states():
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

    def get_task(self, task_id: str) -> AgentState | None:
        with self._condition:
            if task_id not in self._states:
                return None
            return self._snapshot_locked(task_id)

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
        }


review_event_store = ReviewEventStore()
