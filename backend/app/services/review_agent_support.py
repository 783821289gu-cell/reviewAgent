from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence
from uuid import uuid4

from db.errors import RecoveryError
from models.contract import clause_from_dict
from models.log import StepLog
from models.planner import PlannerAction, PlannerReasonCode
from models.review import (
    AgentState,
    LLMMode,
    NodeExecutionTimeoutError,
    ReviewPosition,
    ReviewStatus,
    TaskCancelledError,
    TaskExecutionTimeoutError,
)
from services.context_builder import build_review_context
from services.event_service import ReviewEventStore, review_event_store
from services.log_service import ToolExecutionControl, invoke_tool
from services.review_batch_executor import OrderedBatchExecutor
from services.runtime_log_service import write_runtime_log
from tools.registry import tool_registry


EXECUTION_INTERRUPTS = (
    TaskCancelledError,
    NodeExecutionTimeoutError,
    TaskExecutionTimeoutError,
)

RECOVERABLE_STATUS_ORDER = {
    ReviewStatus.START: 0,
    ReviewStatus.UPLOAD_RECEIVED: 1,
    ReviewStatus.DOCUMENT_PARSED: 2,
    ReviewStatus.CONTRACT_TYPE_CLASSIFIED: 3,
    ReviewStatus.CLAUSES_STRUCTURED: 4,
    ReviewStatus.PLAYBOOK_RETRIEVED: 5,
    ReviewStatus.CONTEXT_BUILT: 6,
    ReviewStatus.RISK_ANALYZED: 7,
}


@dataclass(frozen=True)
class _ToolOutcome:
    item: object
    output: object | None
    logs: list[StepLog]
    error: Exception | None


class _LoggedOutcome(Protocol):
    @property
    def logs(self) -> Sequence[StepLog]: ...

    @property
    def error(self) -> Exception | None: ...


class ReviewAgentSupport:
    """Shared execution mechanics; workflow control belongs to LangGraph."""

    def __init__(
        self,
        event_store: ReviewEventStore = review_event_store,
        *,
        node_timeout_seconds: float = 90.0,
        llm_max_concurrency: int = 2,
    ):
        if node_timeout_seconds <= 0:
            raise ValueError("node timeout must be positive")
        if not 1 <= llm_max_concurrency <= 4:
            raise ValueError("llm_max_concurrency must be between 1 and 4")
        self.event_store = event_store
        self.node_timeout_seconds = node_timeout_seconds
        self.llm_max_concurrency = llm_max_concurrency
        self._batch_executor = OrderedBatchExecutor()

    def run_sync(
        self,
        file_name: str,
        file_type: str,
        content: bytes,
        review_position: ReviewPosition,
        llm_mode: LLMMode = LLMMode.LOCAL_STRUCTURED,
    ) -> AgentState:
        state = self.event_store.create_task(
            file_name,
            file_type,
            review_position,
            content=content,
            llm_mode=llm_mode,
        )
        state = self.event_store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "上传已接收，开始执行文档解析。",
            step_name="upload_received",
        )
        execution_owner = self._acquire_execution(state.task_id)
        self.run(state.task_id, content, execution_owner, True)
        final_state = self.event_store.get_task(state.task_id)
        if final_state is None:
            raise ValueError("审查任务状态丢失。")
        return final_state

    def materialize_legacy_evidence_failures(
        self,
        states: list[AgentState],
    ) -> list[str]:
        materialized_task_ids = []
        for state in states:
            if (
                state.status != ReviewStatus.EVIDENCE_MISSING
                or list(state.risk_findings or [])
            ):
                continue
            candidates = _materialize_manual_review_candidates(
                list(state.analysis_results or []),
                list(state.evidence_results or []),
            )
            if not candidates:
                continue
            self.event_store.update_task(
                state.task_id,
                ReviewStatus.HUMAN_REVIEW_PENDING,
                f"已恢复 {len(candidates)} 条证据候选，等待人工逐条处理。",
                step_name="evidence_manual_review_materialized",
                tool_name="verify_evidence",
                risk_findings=candidates,
                progress=_human_review_progress(candidates),
            )
            materialized_task_ids.append(state.task_id)
        return materialized_task_ids

    def _publish_progress(
        self,
        state: AgentState,
        logs: list[StepLog],
        *,
        stage: str,
        stage_label: str,
        completed: int,
        total: int,
        current_item: str,
        message: str,
        tool_name: str = "",
        progress_state: str = "running",
        completed_item_ids: list[str] | None = None,
        **state_updates,
    ) -> AgentState:
        progress = {
            "stage": stage,
            "stage_label": stage_label,
            "state": progress_state,
            "completed": max(0, min(completed, total)) if total > 0 else 0,
            "total": max(0, total),
            "current_item": current_item,
            "message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if completed_item_ids is not None:
            progress["completed_item_ids"] = list(completed_item_ids)
        updated = self.event_store.update_task(
            state.task_id,
            state.status,
            message,
            step_name=f"{stage}_progress",
            tool_name=tool_name,
            logs=[log.to_dict() for log in logs],
            progress=progress,
            **state_updates,
        )
        write_runtime_log(
            "task_progress",
            task_id=state.task_id,
            trace_id=state.trace_id,
            stage=stage,
            completed=progress["completed"],
            total=progress["total"],
            current_item=current_item,
            state=progress_state,
        )
        return updated

    def _invoke_independent_batch(
        self,
        task_id: str,
        items: list[object],
        *,
        tool_name: str,
        input_builder: Callable[[object], dict],
        step_name: str,
        execution_control: ToolExecutionControl,
        parent_step_id: str,
        max_workers: int,
    ) -> list[_ToolOutcome]:
        def execute(item: object) -> _ToolOutcome:
            item_logs: list[StepLog] = []
            try:
                output = invoke_tool(
                    task_id,
                    tool_registry,
                    tool_name,
                    input_builder(item),
                    item_logs,
                    step_name=step_name,
                    execution_control=execution_control,
                    parent_step_id=parent_step_id,
                )
                return _ToolOutcome(item, output, item_logs, None)
            except Exception as exc:
                return _ToolOutcome(item, None, item_logs, exc)

        return self._batch_executor.map_ordered(
            items,
            execute,
            max_workers=max_workers,
        )

    @staticmethod
    def _merge_batch_logs(
        logs: list[StepLog],
        outcomes: Sequence[_LoggedOutcome],
    ) -> None:
        for outcome in outcomes:
            logs.extend(outcome.logs)

    def _batch_width(self, state: AgentState) -> int:
        if state.llm_mode != LLMMode.DEEPSEEK:
            return 1
        return self.llm_max_concurrency

    def _acquire_execution(self, task_id: str) -> str:
        execution_owner = f"exec_{uuid4().hex}"
        if not self.event_store.try_acquire_execution(task_id, execution_owner):
            raise RuntimeError(f"task execution is already active: {task_id}")
        return execution_owner

    def _record_unexpected_failure(self, task_id: str, logs: list[StepLog]) -> None:
        current = self.event_store.get_task(task_id)
        last_tool = logs[-1].tool_name if logs else ""
        is_parse_failure = (
            current is not None
            and current.status in {ReviewStatus.START, ReviewStatus.UPLOAD_RECEIVED}
            and last_tool in {"", "parse_document"}
        )
        self._update_terminal_or_cancel(
            task_id,
            ReviewStatus.PARSE_FAILED if is_parse_failure else ReviewStatus.TASK_ERROR,
            (
                "合同解析失败，请确认文件为可读取的 DOCX 或 PDF。"
                if is_parse_failure
                else "任务执行发生未分类错误，需要人工恢复。"
            ),
            logs,
            step_name="parse_failed" if is_parse_failure else "task_error",
            tool_name=last_tool,
        )

    def _update_terminal_or_cancel(
        self,
        task_id: str,
        status: ReviewStatus,
        message: str,
        logs: list[StepLog],
        **payload,
    ) -> AgentState:
        try:
            return self.event_store.update_task(
                task_id,
                status,
                message,
                logs=[log.to_dict() for log in logs],
                **payload,
            )
        except TaskCancelledError:
            return self.event_store.finalize_cancel(
                task_id,
                logs=[log.to_dict() for log in logs],
            )


def _before(current_status: ReviewStatus, target_status: ReviewStatus) -> bool:
    if current_status not in RECOVERABLE_STATUS_ORDER:
        raise RecoveryError(f"任务状态 {current_status.value} 不能作为恢复起点。")
    return RECOVERABLE_STATUS_ORDER[current_status] < RECOVERABLE_STATUS_ORDER[target_status]


def _validate_recovery_checkpoint(state: AgentState) -> None:
    if state.status not in RECOVERABLE_STATUS_ORDER:
        raise RecoveryError(f"任务状态 {state.status.value} 无法安全恢复。")

    required_payloads = [
        (ReviewStatus.DOCUMENT_PARSED, state.document, "文档解析结果"),
        (
            ReviewStatus.CONTRACT_TYPE_CLASSIFIED,
            state.contract_classification,
            "合同类型识别结果",
        ),
        (ReviewStatus.CLAUSES_STRUCTURED, state.clauses, "条款结构化结果"),
        (ReviewStatus.PLAYBOOK_RETRIEVED, state.matched_rules, "Playbook 检索结果"),
        (ReviewStatus.CONTEXT_BUILT, state.review_contexts, "风险上下文"),
        (ReviewStatus.RISK_ANALYZED, state.analysis_results, "风险分析结果"),
    ]
    current_order = RECOVERABLE_STATUS_ORDER[state.status]
    for checkpoint, payload, label in required_payloads:
        if current_order >= RECOVERABLE_STATUS_ORDER[checkpoint] and payload is None:
            raise RecoveryError(
                f"{label}缺失，无法从 {state.status.value} 节点安全恢复。"
            )


def _manual_recovery_checkpoint(state: AgentState) -> ReviewStatus:
    if state.status in {ReviewStatus.NODE_TIMEOUT, ReviewStatus.TASK_TIMEOUT}:
        resume_from = str((state.last_timeout or {}).get("resume_from_status", ""))
        try:
            checkpoint = ReviewStatus(resume_from)
        except ValueError as exc:
            raise RecoveryError(
                "timeout recovery checkpoint is missing or invalid"
            ) from exc
        if checkpoint not in RECOVERABLE_STATUS_ORDER:
            raise RecoveryError(f"timeout recovery checkpoint is unsafe: {resume_from}")
        return checkpoint
    checkpoints = {
        ReviewStatus.PARSE_FAILED: ReviewStatus.UPLOAD_RECEIVED,
        ReviewStatus.RETRIEVAL_FAILED: ReviewStatus.CLAUSES_STRUCTURED,
        ReviewStatus.LLM_OUTPUT_INVALID: ReviewStatus.CONTEXT_BUILT,
        ReviewStatus.EVIDENCE_MISSING: ReviewStatus.RISK_ANALYZED,
        ReviewStatus.TASK_ERROR: ReviewStatus.UPLOAD_RECEIVED,
    }
    checkpoint = checkpoints.get(state.status)
    if checkpoint is None:
        raise RecoveryError(
            f"task status cannot be manually recovered: {state.status.value}"
        )
    return checkpoint


def _invoke_planner(
    task_id: str,
    logs: list[StepLog],
    reason_code: PlannerReasonCode,
    current_status: ReviewStatus,
    target_clause_id: str,
    clauses: list[dict],
    retry_count: int,
    failure_reason: str,
) -> dict:
    return invoke_tool(
        task_id,
        tool_registry,
        "plan_review_action",
        {
            "trigger_reason": reason_code.value,
            "current_status": current_status.value,
            "target_clause_id": target_clause_id,
            "contract_clause_ids": [
                str(clause.get("clause_id", ""))
                for clause in clauses
                if isinstance(clause, dict) and str(clause.get("clause_id", ""))
            ],
            "retry_count": retry_count,
            "failure_reason": failure_reason,
        },
        logs,
        step_name="planner_route",
    )


def _append_planner_trace(
    review_context: dict,
    decision: dict,
    retry_count: int,
) -> None:
    trace = list(review_context.get("planner_trace") or [])
    trace.append(
        {
            "action": decision["action"],
            "reason_code": decision["reason_code"],
            "target_clause_id": decision["target_clause_id"],
            "query_adjustment_fields": sorted(decision["query_adjustments"]),
            "confidence": decision["confidence"],
            "retry_count_before_action": retry_count,
        }
    )
    review_context["planner_trace"] = trace
    if decision["action"] in {
        PlannerAction.RETRIEVE_AGAIN.value,
        PlannerAction.ANALYZE_AGAIN.value,
    }:
        review_context["planner_retry_count"] = retry_count + 1
    else:
        review_context["planner_retry_count"] = retry_count


def _append_critic_trace(
    review_context: dict,
    finding: dict,
    critic_result: dict,
) -> None:
    trace = list(review_context.get("critic_trace") or [])
    trace.append(
        {
            "risk_id": str(finding.get("risk_id", "")),
            "decision": critic_result["decision"],
            "reason_code": critic_result["reason_code"],
        }
    )
    review_context["critic_trace"] = trace


def _planner_retry_count(review_contexts: list[dict]) -> int:
    counts = [
        int(context.get("planner_retry_count", 0))
        for context in review_contexts
        if isinstance(context, dict)
        and isinstance(context.get("planner_retry_count", 0), int)
        and not isinstance(context.get("planner_retry_count", 0), bool)
    ]
    return max(counts, default=0)


def _restored_clause_prefix(clauses: list, persisted_clauses: list[dict]) -> list:
    if len(persisted_clauses) > len(clauses):
        raise RecoveryError("持久化关键字段进度超过当前条款数量。")
    restored = [clause_from_dict(item) for item in persisted_clauses]
    expected_ids = [clause.clause_id for clause in clauses[: len(restored)]]
    restored_ids = [clause.clause_id for clause in restored]
    if restored_ids != expected_ids:
        raise RecoveryError("持久化关键字段进度不是当前条款的连续前缀。")
    return restored


def _restored_analysis_prefix(
    review_contexts: list[dict],
    persisted_results: list[dict],
) -> list[dict]:
    if len(persisted_results) > len(review_contexts):
        raise RecoveryError("持久化风险分析结果超过 Review Context 数量。")
    expected_ids = [
        str(context.get("context_id", ""))
        for context in review_contexts[: len(persisted_results)]
    ]
    restored_ids = [_analysis_result_key(result) for result in persisted_results]
    if restored_ids != expected_ids or len(set(restored_ids)) != len(restored_ids):
        raise RecoveryError("持久化风险分析结果不是当前上下文的连续前缀。")
    return [dict(result) for result in persisted_results]


def _analysis_result_key(finding: dict) -> str:
    if not isinstance(finding, dict):
        raise ValueError("risk finding must be a dict")
    clause_id = str(finding.get("clause_id", "")).strip()
    rule_ids = finding.get("matched_rule_ids")
    if (
        not clause_id
        or not isinstance(rule_ids, list)
        or len(rule_ids) != 1
        or not isinstance(rule_ids[0], str)
        or not rule_ids[0].strip()
    ):
        raise ValueError("risk finding identity is invalid")
    return f"{clause_id}:{rule_ids[0].strip()}"


def _evidence_verification_summary(evidence_result: dict) -> dict:
    is_valid = bool(evidence_result.get("is_valid"))
    return {
        "status": "AUTO_VERIFIED" if is_valid else "AUTO_VERIFICATION_FAILED",
        "is_valid": is_valid,
        "failure_reason": str(evidence_result.get("failure_reason", "")),
        "resolution": "AUTOMATIC" if is_valid else "PENDING_HUMAN",
        "source_location": dict(evidence_result.get("source_location") or {}),
    }


def _manual_review_candidate(
    finding: dict,
    evidence_result: dict | None = None,
    *,
    failure_reason: str = "",
) -> dict:
    candidate = dict(finding)
    candidate["review_status"] = "NEED_MANUAL_REVIEW"
    candidate.setdefault("include_in_report", False)
    if evidence_result is not None:
        candidate["evidence_verification"] = _evidence_verification_summary(
            evidence_result
        )
    else:
        candidate["evidence_verification"] = {
            "status": "NOT_VERIFIED",
            "is_valid": False,
            "failure_reason": failure_reason or "尚未完成自动证据校验。",
            "resolution": "PENDING_HUMAN",
            "source_location": {},
        }
    return candidate


def _materialize_manual_review_candidates(
    analysis_results: list[dict],
    evidence_results: list[dict],
    existing_risks: list[dict] | None = None,
    *,
    default_failure_reason: str = "",
) -> list[dict]:
    latest_evidence = {}
    for result in evidence_results:
        if not isinstance(result, dict):
            continue
        risk_id = str(result.get("risk_id", "")).strip()
        if risk_id:
            latest_evidence[risk_id] = result

    existing_by_id = {
        str(risk.get("risk_id", "")): dict(risk)
        for risk in (existing_risks or [])
        if isinstance(risk, dict) and str(risk.get("risk_id", "")).strip()
    }
    candidates = []
    included_ids = set()
    for finding in analysis_results:
        if not isinstance(finding, dict) or finding.get("review_status") == "NO_RISK":
            continue
        risk_id = str(finding.get("risk_id", "")).strip()
        if not risk_id or risk_id in included_ids:
            continue
        if risk_id in existing_by_id:
            candidate = existing_by_id[risk_id]
        else:
            evidence_result = latest_evidence.get(risk_id)
            if evidence_result and evidence_result.get("is_valid"):
                candidate = dict(finding)
                candidate["evidence_verification"] = _evidence_verification_summary(
                    evidence_result
                )
                if evidence_result.get("source_location"):
                    candidate["evidence_location"] = evidence_result["source_location"]
            else:
                candidate = _manual_review_candidate(
                    finding,
                    evidence_result,
                    failure_reason=default_failure_reason,
                )
        candidates.append(candidate)
        included_ids.add(risk_id)

    for risk_id, risk in existing_by_id.items():
        if risk_id not in included_ids:
            candidates.append(risk)
    return candidates


def _human_review_progress(risks: list[dict]) -> dict:
    pending = [
        risk
        for risk in risks
        if not str((risk.get("feedback") or {}).get("user_action", "")).strip()
    ]
    total = len(risks)
    completed = total - len(pending)
    return {
        "stage": "human_review",
        "stage_label": "人工复核证据",
        "completed": completed,
        "total": total,
        "current_item": _finding_progress_item(pending[0]) if pending else "",
        "completed_item_ids": [
            str(risk.get("risk_id", ""))
            for risk in risks
            if str((risk.get("feedback") or {}).get("user_action", "")).strip()
        ],
        "state": "waiting" if pending else "completed",
    }


def _restored_evidence_completion_ids(
    progress: dict,
    analysis_results: list[dict],
    evidence_candidates: list[dict],
) -> list[str]:
    if str(progress.get("stage", "")) != "evidence_verification":
        return []
    analysis_ids = {_analysis_result_key(finding) for finding in analysis_results}
    raw_ids = progress.get("completed_item_ids")
    if raw_ids is None:
        completed = progress.get("completed", 0)
        if isinstance(completed, bool) or not isinstance(completed, int):
            raise RecoveryError("持久化证据进度完成数无效。")
        if completed < 0 or completed > len(evidence_candidates):
            raise RecoveryError("持久化证据进度完成数超出候选范围。")
        return [
            _analysis_result_key(finding)
            for finding in evidence_candidates[:completed]
        ]
    if (
        not isinstance(raw_ids, list)
        or not all(isinstance(item, str) and item.strip() for item in raw_ids)
    ):
        raise RecoveryError("持久化证据进度标识无效。")
    restored_ids = [item.strip() for item in raw_ids]
    if len(restored_ids) != len(set(restored_ids)):
        raise RecoveryError("持久化证据进度包含重复标识。")
    if any(item not in analysis_ids for item in restored_ids):
        raise RecoveryError("持久化证据进度不属于当前风险分析结果。")
    completed = progress.get("completed", len(restored_ids))
    if completed != len(restored_ids):
        raise RecoveryError("持久化证据进度数量与标识不一致。")
    return restored_ids


def _context_progress_item(current_clause: dict, matched_rule: dict) -> str:
    clause_id = str(current_clause.get("clause_id", ""))
    rule_id = str(matched_rule.get("rule_id", ""))
    return " / ".join(item for item in (clause_id, rule_id) if item)


def _review_context_progress_item(review_context: dict) -> str:
    return _context_progress_item(
        review_context.get("current_clause") or {},
        review_context.get("matched_rule") or {},
    )


def _finding_progress_item(finding: dict) -> str:
    risk_id = str(finding.get("risk_id", ""))
    clause_id = str(finding.get("clause_id", ""))
    return " / ".join(item for item in (clause_id, risk_id) if item)


def _retrieval_is_insufficient(
    review_context: dict,
    clauses: list[dict],
) -> bool:
    return len(clauses) > 1 and not list(review_context.get("related_clauses") or [])


def _rebuild_review_context(
    task_id: str,
    logs: list[StepLog],
    review_context: dict,
    clauses: list[dict],
    query_adjustments: dict,
    embedding_cache: dict,
    retry_count: int,
) -> dict:
    current_clause = review_context["current_clause"]
    matched_rule = review_context["matched_rule"]
    related_clauses = invoke_tool(
        task_id,
        tool_registry,
        "retrieve_related_clauses",
        {
            "contract_type": review_context["contract_type"],
            "current_clause": current_clause,
            "clauses": clauses,
            "risk_type": matched_rule["risk_type"],
            "playbook_check_point": matched_rule["check_point"],
            "limit": 3,
            "query_adjustments": query_adjustments,
            "retry_count": retry_count,
            "embedding_cache": embedding_cache,
        },
        logs,
        step_name="planner_related_clause_retrieval",
    )
    rebuilt = build_review_context(
        contract_type=review_context["contract_type"],
        review_position=review_context["review_position"],
        current_clause=current_clause,
        matched_rule=matched_rule,
        related_clauses=related_clauses,
        related_memory=list(review_context.get("related_memory") or []),
    )
    rebuilt["planner_trace"] = list(review_context.get("planner_trace") or [])
    rebuilt["planner_retry_count"] = retry_count
    rebuilt["critic_trace"] = list(review_context.get("critic_trace") or [])
    return rebuilt


def _evidence_planner_reason(evidence_result: dict) -> PlannerReasonCode:
    failure_reason = str(evidence_result.get("failure_reason", ""))
    if failure_reason in {
        "risk_type does not match matched rule",
        "matched rule id missing from finding",
        "risk_reason is not related to evidence_text",
    }:
        return PlannerReasonCode.ANALYZER_VERIFIER_CONFLICT
    return PlannerReasonCode.EVIDENCE_MISSING


def _is_low_confidence(finding: dict) -> bool:
    try:
        confidence = float(finding.get("confidence", 0))
    except (TypeError, ValueError):
        return True
    return confidence < 0.7


def _requires_manual_review(finding: dict) -> bool:
    if finding.get("review_status") == "NEED_MANUAL_REVIEW":
        return True
    if finding.get("severity") == "高":
        return True
    try:
        return float(finding.get("confidence", 1)) < 0.7
    except (TypeError, ValueError):
        return True
