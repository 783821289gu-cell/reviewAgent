from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Condition
from uuid import uuid4

from models.review import AgentState, ReviewPosition, ReviewStatus


TERMINAL_STATUSES = {
    ReviewStatus.EVIDENCE_VERIFIED,
    ReviewStatus.HUMAN_REVIEW_PENDING,
    ReviewStatus.MEMORY_UPDATED,
    ReviewStatus.PARSE_FAILED,
    ReviewStatus.RETRIEVAL_FAILED,
    ReviewStatus.LLM_OUTPUT_INVALID,
    ReviewStatus.EVIDENCE_MISSING,
    ReviewStatus.NEED_MANUAL_REVIEW,
    ReviewStatus.REPORT_READY,
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
        payload["status"] = self.status.value
        if include_task:
            payload["task"] = self.task
        return payload


class ReviewEventStore:
    def __init__(self):
        self._states: dict[str, AgentState] = {}
        self._events: dict[str, list[ReviewEvent]] = {}
        self._condition = Condition()

    def create_task(self, file_name: str, file_type: str, review_position: ReviewPosition) -> AgentState:
        state = AgentState(
            task_id=f"task_{uuid4().hex[:12]}",
            status=ReviewStatus.START,
            file_name=file_name,
            file_type=file_type,
            review_position=review_position,
            message="审查任务已创建，等待上传处理。",
            logs=[],
            events=[],
        )
        with self._condition:
            self._states[state.task_id] = state
            self._events[state.task_id] = []
            self._append_event_locked(
                state.task_id,
                ReviewStatus.START,
                state.message,
                step_name="create_task",
                tool_name="",
            )
            return self._snapshot_locked(state.task_id)

    def update_task(
        self,
        task_id: str,
        status: ReviewStatus,
        message: str,
        step_name: str,
        tool_name: str = "",
        document: dict | None = None,
        clauses: list[dict] | None = None,
        matched_rules: list[dict] | None = None,
        review_contexts: list[dict] | None = None,
        analysis_results: list[dict] | None = None,
        risk_findings: list[dict] | None = None,
        evidence_results: list[dict] | None = None,
        report_file: dict | None = None,
        logs: list[dict] | None = None,
    ) -> AgentState:
        with self._condition:
            state = self._states[task_id]
            state.status = status
            state.message = message
            if document is not None:
                state.document = document
            if clauses is not None:
                state.clauses = clauses
            if matched_rules is not None:
                state.matched_rules = matched_rules
            if review_contexts is not None:
                state.review_contexts = review_contexts
            if analysis_results is not None:
                state.analysis_results = analysis_results
            if risk_findings is not None:
                state.risk_findings = risk_findings
            if evidence_results is not None:
                state.evidence_results = evidence_results
            if report_file is not None:
                state.report_file = report_file
            if logs is not None:
                state.logs = logs
            self._append_event_locked(task_id, status, message, step_name=step_name, tool_name=tool_name)
            return self._snapshot_locked(task_id)

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

    def _append_event_locked(
        self,
        task_id: str,
        status: ReviewStatus,
        message: str,
        step_name: str,
        tool_name: str,
    ) -> None:
        event_id = len(self._events[task_id])
        base_event = {
            "event_id": event_id,
            "task_id": task_id,
            "status": status.value,
            "message": message,
            "step_name": step_name,
            "tool_name": tool_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        task_snapshot = self._state_payload_locked(
            task_id,
            events=[item.to_dict() for item in self._events[task_id]] + [base_event],
        )
        event = ReviewEvent(
            event_id=event_id,
            task_id=task_id,
            status=status,
            message=message,
            step_name=step_name,
            tool_name=tool_name,
            created_at=base_event["created_at"],
            task=task_snapshot,
        )
        self._events[task_id].append(event)
        self._states[task_id].events = [item.to_dict() for item in self._events[task_id]]
        self._condition.notify_all()

    def _snapshot_locked(self, task_id: str) -> AgentState:
        state = self._states[task_id]
        return AgentState(
            task_id=state.task_id,
            status=state.status,
            file_name=state.file_name,
            file_type=state.file_type,
            review_position=state.review_position,
            message=state.message,
            document=state.document,
            clauses=state.clauses,
            matched_rules=state.matched_rules,
            review_contexts=state.review_contexts,
            analysis_results=state.analysis_results,
            risk_findings=state.risk_findings,
            evidence_results=state.evidence_results,
            report_file=state.report_file,
            logs=list(state.logs or []),
            events=list(state.events or []),
        )

    def _state_payload_locked(self, task_id: str, events: list[dict]) -> dict:
        state = self._states[task_id]
        return {
            "task_id": state.task_id,
            "status": state.status.value,
            "file_name": state.file_name,
            "file_type": state.file_type,
            "review_position": state.review_position.value,
            "message": state.message,
            "document": state.document,
            "clauses": state.clauses,
            "matched_rules": state.matched_rules,
            "review_contexts": state.review_contexts,
            "analysis_results": state.analysis_results,
            "risk_findings": state.risk_findings,
            "evidence_results": state.evidence_results,
            "report_file": state.report_file,
            "logs": list(state.logs or []),
            "events": events,
        }


review_event_store = ReviewEventStore()
