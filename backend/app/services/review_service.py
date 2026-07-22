from dataclasses import replace
from datetime import datetime, timezone
from threading import Thread
from time import perf_counter
from uuid import uuid4

from db.repositories import RecoveryError
from models.contract import clause_from_dict, contract_document_from_dict
from models.log import StepLog
from models.planner import PlannerAction, PlannerReasonCode
from models.risk import CriticDecision, CriticReasonCode
from models.review import (
    AgentState,
    LLMMode,
    NodeExecutionTimeoutError,
    ReviewPosition,
    ReviewStatus,
    TaskCancelledError,
    TaskExecutionTimeoutError,
)
from parsers.pdf_parser import PdfParseError
from providers.llm_provider import (
    LLMOutputInvalidError,
    bind_llm_mode,
    reset_llm_mode,
)
from services.context_builder import build_review_context
from services.event_service import ReviewEventStore, review_event_store
from services.log_service import (
    ToolExecutionControl,
    bind_execution_control,
    invoke_tool,
    reset_execution_control,
)
from services.planner_service import PlannerOutputInvalidError
from services.risk_critic import CriticOutputInvalidError
from services.runtime_log_service import write_runtime_log
from tools.registry import tool_registry


EXECUTION_INTERRUPTS = (
    TaskCancelledError,
    NodeExecutionTimeoutError,
    TaskExecutionTimeoutError,
)


class ReviewOrchestratorAgent:
    def __init__(
        self,
        event_store: ReviewEventStore = review_event_store,
        *,
        node_timeout_seconds: float = 90.0,
        task_timeout_seconds: float = 900.0,
    ):
        if node_timeout_seconds <= 0 or task_timeout_seconds <= 0:
            raise ValueError("execution timeouts must be positive")
        self.event_store = event_store
        self.node_timeout_seconds = node_timeout_seconds
        self.task_timeout_seconds = task_timeout_seconds

    def start(
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
        try:
            Thread(
                target=self.run,
                args=(state.task_id, content, execution_owner, True),
                daemon=True,
            ).start()
        except Exception:
            self.event_store.release_execution(state.task_id, execution_owner)
            raise
        return state

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

    def recover_pending_tasks(self, async_mode: bool = True) -> list[str]:
        if self.event_store.persistence is None:
            return []
        recovered_task_ids = []
        for state in self.event_store.pending_tasks():
            if state.status == ReviewStatus.CANCEL_REQUESTED:
                self.event_store.finalize_cancel(state.task_id)
                continue
            try:
                execution_owner = self._acquire_execution(state.task_id)
            except RuntimeError:
                continue
            try:
                _validate_recovery_checkpoint(state)
                content = self.event_store.persistence.load_upload(state.task_id)
                self.event_store.record_recovery(state.task_id)
            except RecoveryError as exc:
                self.event_store.update_task(
                    state.task_id,
                    ReviewStatus.NEED_MANUAL_REVIEW,
                    str(exc),
                    step_name="recovery_blocked",
                )
                self.event_store.release_execution(state.task_id, execution_owner)
                continue
            except Exception:
                self.event_store.release_execution(state.task_id, execution_owner)
                raise

            recovered_task_ids.append(state.task_id)
            if async_mode:
                try:
                    Thread(
                        target=self.run,
                        args=(state.task_id, content, execution_owner, True),
                        daemon=True,
                    ).start()
                except Exception:
                    self.event_store.release_execution(state.task_id, execution_owner)
                    raise
            else:
                self.run(state.task_id, content, execution_owner, True)
        return recovered_task_ids

    def recover_task(
        self,
        task_id: str,
        *,
        resume_from: ReviewStatus | None,
        operator_action: str,
        reason: str,
    ) -> AgentState:
        if self.event_store.persistence is None:
            raise ValueError("manual recovery requires persistent task storage")
        execution_owner = self._acquire_execution(task_id)
        try:
            current = self.event_store.get_task(task_id)
            if current is None:
                raise ValueError(f"task not found: {task_id}")
            expected_resume_from = _manual_recovery_checkpoint(current)
            if resume_from is not None and resume_from != expected_resume_from:
                raise ValueError(
                    f"recovery checkpoint must be {expected_resume_from.value} "
                    f"for {current.status.value}"
                )
            resume_from = expected_resume_from
            candidate = replace(current, status=resume_from)
            _validate_recovery_checkpoint(candidate)
            content = self.event_store.persistence.load_upload(task_id)
            state = self.event_store.record_manual_recovery(
                task_id,
                resume_from=resume_from,
                operator_action=operator_action,
                reason=reason,
            )
            Thread(
                target=self.run,
                args=(task_id, content, execution_owner, True),
                daemon=True,
            ).start()
            return state
        except Exception:
            self.event_store.release_execution(task_id, execution_owner)
            raise

    def run(
        self,
        task_id: str,
        content: bytes,
        execution_owner: str = "",
        execution_acquired: bool = False,
    ) -> None:
        resolved_owner = execution_owner or f"exec_{uuid4().hex}"
        if not execution_acquired and not self.event_store.try_acquire_execution(
            task_id,
            resolved_owner,
        ):
            return
        state = self.event_store.get_task(task_id)
        if state is None:
            self.event_store.release_execution(task_id, resolved_owner)
            return

        execution_control = ToolExecutionControl(
            cancel_check=lambda: self.event_store.raise_if_cancelled(task_id),
            task_started_at=perf_counter(),
            task_timeout_seconds=self.task_timeout_seconds,
            node_timeout_seconds=self.node_timeout_seconds,
            execution_retry_index=state.recovery_count,
        )
        llm_mode_token = bind_llm_mode(state.llm_mode.value)
        control_token = bind_execution_control(execution_control)
        logs = [StepLog(**item) for item in (state.logs or [])]
        write_runtime_log(
            "task_started",
            task_id=task_id,
            trace_id=state.trace_id,
            llm_mode=state.llm_mode.value,
            node_timeout_seconds=self.node_timeout_seconds,
            task_timeout_seconds=self.task_timeout_seconds,
            recovery_count=state.recovery_count,
        )
        try:
            if state.status == ReviewStatus.START:
                state = self.event_store.update_task(
                    task_id,
                    ReviewStatus.UPLOAD_RECEIVED,
                    "上传已接收，开始执行文档解析。",
                    step_name="upload_received",
                )

            document = None
            if _before(state.status, ReviewStatus.DOCUMENT_PARSED):
                state = self._publish_progress(
                    state,
                    logs,
                    stage="document_parse",
                    stage_label="解析合同文档",
                    completed=0,
                    total=1,
                    current_item="document",
                    message="正在解析合同文档。",
                    tool_name="parse_document",
                )
                document = invoke_tool(
                    task_id,
                    tool_registry,
                    "parse_document",
                    {
                        "file_id": task_id,
                        "file_name": state.file_name,
                        "file_type": state.file_type,
                        "content": content,
                    },
                    logs,
                    step_name="document_parse",
                )
                state = self.event_store.update_task(
                    task_id,
                    ReviewStatus.DOCUMENT_PARSED,
                    "文档解析完成。",
                    step_name="document_parsed",
                    tool_name="parse_document",
                    document=document.to_dict(),
                    logs=[log.to_dict() for log in logs],
                )
            elif state.status in {
                ReviewStatus.DOCUMENT_PARSED,
                ReviewStatus.CONTRACT_TYPE_CLASSIFIED,
            }:
                document = contract_document_from_dict(state.document)

            if _before(state.status, ReviewStatus.CONTRACT_TYPE_CLASSIFIED):
                if document is None:
                    raise RecoveryError("持久化文档缺失，无法执行合同类型识别。")
                try:
                    state = self._publish_progress(
                        state,
                        logs,
                        stage="contract_type_classification",
                        stage_label="识别合同类型",
                        completed=0,
                        total=1,
                        current_item="document",
                        message="正在识别合同类型。",
                        tool_name="classify_contract_type",
                        document=document.to_dict(),
                    )
                    contract_classification = invoke_tool(
                        task_id,
                        tool_registry,
                        "classify_contract_type",
                        {"document": document},
                        logs,
                        step_name="contract_type_classification",
                    )
                except EXECUTION_INTERRUPTS:
                    raise
                except Exception as exc:
                    self.event_store.update_task(
                        task_id,
                        ReviewStatus.NEED_MANUAL_REVIEW,
                        f"合同类型识别失败，需要人工确认：{exc}",
                        step_name="contract_type_classification_failed",
                        tool_name="classify_contract_type",
                        logs=[log.to_dict() for log in logs],
                    )
                    return

                if contract_classification["decision"] == "UNSUPPORTED_CONTRACT_TYPE":
                    self.event_store.update_task(
                        task_id,
                        ReviewStatus.UNSUPPORTED_CONTRACT_TYPE,
                        "当前文件不是支持的 NDA/保密协议，已停止正式风险审查。",
                        step_name="unsupported_contract_type",
                        tool_name="classify_contract_type",
                        contract_classification=contract_classification,
                        risk_findings=[],
                        logs=[log.to_dict() for log in logs],
                    )
                    return
                if contract_classification["decision"] == "NEED_MANUAL_REVIEW":
                    self.event_store.update_task(
                        task_id,
                        ReviewStatus.NEED_MANUAL_REVIEW,
                        "合同类型特征不足，无法确认是 NDA/保密协议，需要人工复核。",
                        step_name="contract_type_manual_review",
                        tool_name="classify_contract_type",
                        contract_classification=contract_classification,
                        risk_findings=[],
                        logs=[log.to_dict() for log in logs],
                    )
                    return

                state = self.event_store.update_task(
                    task_id,
                    ReviewStatus.CONTRACT_TYPE_CLASSIFIED,
                    "已确认合同类型为 NDA，开始执行条款结构化。",
                    step_name="contract_type_classified",
                    tool_name="classify_contract_type",
                    contract_classification=contract_classification,
                    logs=[log.to_dict() for log in logs],
                )
            else:
                contract_classification = state.contract_classification

            contract_type = str((contract_classification or {}).get("contract_type", ""))
            if contract_type != "NDA":
                raise RecoveryError("持久化合同类型不是 NDA，无法进入 NDA 风险审查链路。")

            if _before(state.status, ReviewStatus.CLAUSES_STRUCTURED):
                if document is None:
                    raise RecoveryError("持久化文档缺失，无法从条款结构化节点恢复。")
                try:
                    clauses = invoke_tool(
                        task_id,
                        tool_registry,
                        "extract_clauses",
                        {"document": document},
                        logs,
                        step_name="clause_structure",
                    )

                    clauses_with_fields = []
                    clause_total = len(clauses)
                    state = self._publish_progress(
                        state,
                        logs,
                        stage="key_field_extract",
                        stage_label="提取条款关键字段",
                        completed=0,
                        total=clause_total,
                        current_item=clauses[0].clause_id if clauses else "",
                        message=(
                            f"正在处理 {clauses[0].clause_id}（0/{clause_total}）。"
                            if clauses
                            else "合同中没有可处理条款。"
                        ),
                        tool_name="extract_key_fields",
                        clauses=[],
                    )
                    for clause_index, clause in enumerate(clauses):
                        key_fields = invoke_tool(
                            task_id,
                            tool_registry,
                            "extract_key_fields",
                            {"clause": clause},
                            logs,
                            step_name="key_field_extract",
                        )
                        clauses_with_fields.append(replace(clause, key_fields=key_fields))
                        completed = clause_index + 1
                        next_item = (
                            clauses[completed].clause_id
                            if completed < clause_total
                            else ""
                        )
                        state = self._publish_progress(
                            state,
                            logs,
                            stage="key_field_extract",
                            stage_label="提取条款关键字段",
                            completed=completed,
                            total=clause_total,
                            current_item=next_item,
                            message=(
                                f"正在处理 {next_item}（{completed}/{clause_total}）。"
                                if next_item
                                else f"条款关键字段提取完成（{completed}/{clause_total}）。"
                            ),
                            tool_name="extract_key_fields",
                            clauses=[item.to_dict() for item in clauses_with_fields],
                        )
                except LLMOutputInvalidError as exc:
                    self.event_store.update_task(
                        task_id,
                        ReviewStatus.LLM_OUTPUT_INVALID,
                        f"关键字段结构化输出无效：{exc}",
                        step_name="llm_output_invalid",
                        tool_name="extract_key_fields",
                        analysis_results=[],
                        risk_findings=[],
                        logs=[log.to_dict() for log in logs],
                    )
                    return

                state = self.event_store.update_task(
                    task_id,
                    ReviewStatus.CLAUSES_STRUCTURED,
                    "合同已解析并完成条款结构化，开始检索 NDA Playbook 规则。",
                    step_name="clauses_structured",
                    tool_name="extract_key_fields",
                    clauses=[clause.to_dict() for clause in clauses_with_fields],
                    logs=[log.to_dict() for log in logs],
                )
            else:
                clauses_with_fields = [clause_from_dict(item) for item in (state.clauses or [])]

            if _before(state.status, ReviewStatus.PLAYBOOK_RETRIEVED):
                try:
                    rule_matches = []
                    playbook_total = len(clauses_with_fields)
                    state = self._publish_progress(
                        state,
                        logs,
                        stage="playbook_retrieval",
                        stage_label="检索 Playbook 规则",
                        completed=0,
                        total=playbook_total,
                        current_item=(
                            clauses_with_fields[0].clause_id
                            if clauses_with_fields
                            else ""
                        ),
                        message=(
                            f"正在检索 {clauses_with_fields[0].clause_id} 的规则（0/{playbook_total}）。"
                            if clauses_with_fields
                            else "没有条款需要检索 Playbook。"
                        ),
                        tool_name="retrieve_playbook_rules",
                        matched_rules=[],
                    )
                    for clause_index, clause in enumerate(clauses_with_fields):
                        matched_rules = invoke_tool(
                            task_id,
                            tool_registry,
                            "retrieve_playbook_rules",
                            {
                                "contract_type": contract_type,
                                "clause_type": clause.clause_type,
                                "key_fields": clause.key_fields,
                                "review_position": state.review_position.value,
                            },
                            logs,
                            step_name="playbook_retrieval",
                        )
                        rule_matches.append(
                            {
                                "clause_id": clause.clause_id,
                                "clause_type": clause.clause_type,
                                "clause_title": clause.title,
                                "matched_rules": matched_rules,
                            }
                        )
                        completed = clause_index + 1
                        next_item = (
                            clauses_with_fields[completed].clause_id
                            if completed < playbook_total
                            else ""
                        )
                        state = self._publish_progress(
                            state,
                            logs,
                            stage="playbook_retrieval",
                            stage_label="检索 Playbook 规则",
                            completed=completed,
                            total=playbook_total,
                            current_item=next_item,
                            message=(
                                f"正在检索 {next_item} 的规则（{completed}/{playbook_total}）。"
                                if next_item
                                else f"Playbook 规则检索完成（{completed}/{playbook_total}）。"
                            ),
                            tool_name="retrieve_playbook_rules",
                            matched_rules=rule_matches,
                        )
                except EXECUTION_INTERRUPTS:
                    raise
                except Exception as exc:
                    self.event_store.update_task(
                        task_id,
                        ReviewStatus.RETRIEVAL_FAILED,
                        f"Playbook 规则检索失败：{exc}",
                        step_name="playbook_failed",
                        tool_name="retrieve_playbook_rules",
                        logs=[log.to_dict() for log in logs],
                    )
                    return

                state = self.event_store.update_task(
                    task_id,
                    ReviewStatus.PLAYBOOK_RETRIEVED,
                    "NDA Playbook 规则检索完成，开始构建风险分析上下文。",
                    step_name="playbook_retrieved",
                    tool_name="retrieve_playbook_rules",
                    matched_rules=rule_matches,
                    logs=[log.to_dict() for log in logs],
                )
            else:
                rule_matches = list(state.matched_rules or [])

            clauses_payload = [clause.to_dict() for clause in clauses_with_fields]
            clauses_by_id = {clause["clause_id"]: clause for clause in clauses_payload}
            embedding_cache = {}
            if _before(state.status, ReviewStatus.CONTEXT_BUILT):
                try:
                    review_contexts = []
                    context_work_items = []
                    for rule_group in rule_matches:
                        current_clause = clauses_by_id.get(rule_group["clause_id"])
                        if current_clause is None:
                            continue
                        for matched_rule in rule_group["matched_rules"]:
                            context_work_items.append((current_clause, matched_rule))
                    context_total = len(context_work_items)
                    first_context_item = (
                        _context_progress_item(*context_work_items[0])
                        if context_work_items
                        else ""
                    )
                    state = self._publish_progress(
                        state,
                        logs,
                        stage="context_build",
                        stage_label="构建风险分析上下文",
                        completed=0,
                        total=context_total,
                        current_item=first_context_item,
                        message=(
                            f"正在构建 {first_context_item} 的上下文（0/{context_total}）。"
                            if first_context_item
                            else "没有命中规则需要构建上下文。"
                        ),
                        review_contexts=[],
                    )
                    for context_index, (current_clause, matched_rule) in enumerate(
                        context_work_items
                    ):
                        related_clauses = invoke_tool(
                            task_id,
                            tool_registry,
                            "retrieve_related_clauses",
                            {
                                "contract_type": contract_type,
                                "current_clause": current_clause,
                                "clauses": clauses_payload,
                                "risk_type": matched_rule["risk_type"],
                                "playbook_check_point": matched_rule["check_point"],
                                "limit": 3,
                                "embedding_cache": embedding_cache,
                            },
                            logs,
                            step_name="related_clause_retrieval",
                        )
                        memory_input = {
                            "contract_type": contract_type,
                            "clause": current_clause,
                            "risk_type": matched_rule["risk_type"],
                            "review_position": state.review_position.value,
                            "memory_items": [],
                            "limit": 3,
                        }
                        if self.event_store.db_path:
                            memory_input["db_path"] = self.event_store.db_path
                        related_memory = invoke_tool(
                            task_id,
                            tool_registry,
                            "retrieve_memory",
                            memory_input,
                            logs,
                            step_name="memory_retrieval",
                        )
                        review_contexts.append(
                            build_review_context(
                                contract_type=contract_type,
                                review_position=state.review_position.value,
                                current_clause=current_clause,
                                matched_rule=matched_rule,
                                related_clauses=related_clauses,
                                related_memory=related_memory,
                            )
                        )
                        completed = context_index + 1
                        next_item = (
                            _context_progress_item(*context_work_items[completed])
                            if completed < context_total
                            else ""
                        )
                        state = self._publish_progress(
                            state,
                            logs,
                            stage="context_build",
                            stage_label="构建风险分析上下文",
                            completed=completed,
                            total=context_total,
                            current_item=next_item,
                            message=(
                                f"正在构建 {next_item} 的上下文（{completed}/{context_total}）。"
                                if next_item
                                else f"风险分析上下文构建完成（{completed}/{context_total}）。"
                            ),
                            review_contexts=review_contexts,
                        )
                except EXECUTION_INTERRUPTS:
                    raise
                except Exception as exc:
                    self.event_store.update_task(
                        task_id,
                        ReviewStatus.RETRIEVAL_FAILED,
                        f"上下文构建失败：{exc}",
                        step_name="context_failed",
                        logs=[log.to_dict() for log in logs],
                    )
                    return

                state = self.event_store.update_task(
                    task_id,
                    ReviewStatus.CONTEXT_BUILT,
                    "风险分析上下文已构建，开始执行结构化风险分析。",
                    step_name="context_built",
                    review_contexts=review_contexts,
                    logs=[log.to_dict() for log in logs],
                )
            else:
                review_contexts = list(state.review_contexts or [])

            retrieval_repair_count = _planner_retry_count(review_contexts)
            for context_index, review_context in enumerate(list(review_contexts)):
                if not _retrieval_is_insufficient(review_context, clauses_payload):
                    continue
                try:
                    decision = _invoke_planner(
                        task_id,
                        logs,
                        PlannerReasonCode.RETRIEVAL_INSUFFICIENT,
                        ReviewStatus.CONTEXT_BUILT,
                        str(review_context["current_clause"]["clause_id"]),
                        clauses_payload,
                        retrieval_repair_count,
                        "related clause retrieval returned no candidates",
                    )
                    _append_planner_trace(
                        review_context,
                        decision,
                        retrieval_repair_count,
                    )
                    if decision["action"] != PlannerAction.RETRIEVE_AGAIN.value:
                        self.event_store.update_task(
                            task_id,
                            ReviewStatus.RETRIEVAL_FAILED,
                            "相关条款召回不足，Planner 未授权再次检索。",
                            step_name="planner_retrieval_stopped",
                            tool_name="plan_review_action",
                            review_contexts=review_contexts,
                            analysis_results=[],
                            risk_findings=[],
                            logs=[log.to_dict() for log in logs],
                        )
                        return
                    retrieval_repair_count += 1
                    review_contexts[context_index] = _rebuild_review_context(
                        task_id,
                        logs,
                        review_context,
                        clauses_payload,
                        decision["query_adjustments"],
                        embedding_cache,
                        retrieval_repair_count,
                    )
                except EXECUTION_INTERRUPTS:
                    raise
                except Exception as exc:
                    self.event_store.update_task(
                        task_id,
                        ReviewStatus.RETRIEVAL_FAILED,
                        f"相关条款修复被拒绝：{exc}",
                        step_name="planner_retrieval_rejected",
                        tool_name="plan_review_action",
                        review_contexts=review_contexts,
                        analysis_results=[],
                        risk_findings=[],
                        logs=[log.to_dict() for log in logs],
                    )
                    return

            if retrieval_repair_count != _planner_retry_count(state.review_contexts or []):
                state = self.event_store.update_task(
                    task_id,
                    ReviewStatus.CONTEXT_BUILT,
                    "Planner 已完成一次受控相关条款检索修复，开始风险分析。",
                    step_name="planner_retrieval_repaired",
                    tool_name="plan_review_action",
                    review_contexts=review_contexts,
                    logs=[log.to_dict() for log in logs],
                )

            if _before(state.status, ReviewStatus.RISK_ANALYZED):
                try:
                    analysis_results = []
                    analysis_total = len(review_contexts)
                    first_analysis_item = (
                        _review_context_progress_item(review_contexts[0])
                        if review_contexts
                        else ""
                    )
                    state = self._publish_progress(
                        state,
                        logs,
                        stage="risk_analysis",
                        stage_label="分析合同风险",
                        completed=0,
                        total=analysis_total,
                        current_item=first_analysis_item,
                        message=(
                            f"正在分析 {first_analysis_item}（0/{analysis_total}）。"
                            if first_analysis_item
                            else "没有风险上下文需要分析。"
                        ),
                        tool_name="analyze_risk",
                        analysis_results=[],
                    )
                    for analysis_index, review_context in enumerate(review_contexts):
                        finding = invoke_tool(
                            task_id,
                            tool_registry,
                            "analyze_risk",
                            {"review_context": review_context},
                            logs,
                            step_name="risk_analysis",
                        )
                        analysis_results.append(finding)
                        completed = analysis_index + 1
                        next_item = (
                            _review_context_progress_item(review_contexts[completed])
                            if completed < analysis_total
                            else ""
                        )
                        state = self._publish_progress(
                            state,
                            logs,
                            stage="risk_analysis",
                            stage_label="分析合同风险",
                            completed=completed,
                            total=analysis_total,
                            current_item=next_item,
                            message=(
                                f"正在分析 {next_item}（{completed}/{analysis_total}）。"
                                if next_item
                                else f"风险分析完成（{completed}/{analysis_total}）。"
                            ),
                            tool_name="analyze_risk",
                            analysis_results=analysis_results,
                        )
                except EXECUTION_INTERRUPTS:
                    raise
                except Exception as exc:
                    target_context = review_context if "review_context" in locals() else None
                    if isinstance(target_context, dict):
                        try:
                            decision = _invoke_planner(
                                task_id,
                                logs,
                                PlannerReasonCode.STRUCTURED_OUTPUT_INVALID,
                                ReviewStatus.CONTEXT_BUILT,
                                str(target_context["current_clause"]["clause_id"]),
                                clauses_payload,
                                _planner_retry_count(review_contexts),
                                str(exc),
                            )
                            _append_planner_trace(
                                target_context,
                                decision,
                                _planner_retry_count(review_contexts),
                            )
                        except EXECUTION_INTERRUPTS:
                            raise
                        except Exception:
                            pass
                    self.event_store.update_task(
                        task_id,
                        ReviewStatus.LLM_OUTPUT_INVALID,
                        f"风险分析结构化输出无效：{exc}",
                        step_name="llm_output_invalid",
                        tool_name="analyze_risk",
                        review_contexts=review_contexts,
                        analysis_results=[],
                        risk_findings=[],
                        logs=[log.to_dict() for log in logs],
                    )
                    return

                state = self.event_store.update_task(
                    task_id,
                    ReviewStatus.RISK_ANALYZED,
                    "结构化风险分析完成，开始执行证据验证。",
                    step_name="risk_analyzed",
                    tool_name="analyze_risk",
                    analysis_results=analysis_results,
                    logs=[log.to_dict() for log in logs],
                )
            else:
                analysis_results = list(state.analysis_results or [])

            context_by_id = {context["context_id"]: context for context in review_contexts}
            context_index_by_id = {
                context["context_id"]: index
                for index, context in enumerate(review_contexts)
            }
            evidence_results = []
            risk_findings = []
            active_context = None
            evidence_candidates = [
                finding
                for finding in analysis_results
                if finding.get("review_status") != "NO_RISK"
            ]
            evidence_total = len(evidence_candidates)
            evidence_completed = 0
            first_evidence_item = (
                _finding_progress_item(evidence_candidates[0])
                if evidence_candidates
                else ""
            )
            state = self._publish_progress(
                state,
                logs,
                stage="evidence_verification",
                stage_label="校验风险与证据",
                completed=0,
                total=evidence_total,
                current_item=first_evidence_item,
                message=(
                    f"正在校验 {first_evidence_item}（0/{evidence_total}）。"
                    if first_evidence_item
                    else "没有风险候选需要证据校验。"
                ),
                tool_name="verify_evidence",
                evidence_results=[],
                risk_findings=[],
            )
            try:
                for finding_index in range(len(analysis_results)):
                    finding = analysis_results[finding_index]
                    if finding["review_status"] == "NO_RISK":
                        continue
                    finding = dict(finding)
                    context_key = f"{finding['clause_id']}:{finding['matched_rule_ids'][0]}"
                    review_context = context_by_id.get(context_key)
                    if review_context is None:
                        raise ValueError("risk finding cannot be mapped back to review context")
                    active_context = review_context
                    planner_requested_human = False
                    critic_requested_human = False

                    if _is_low_confidence(finding):
                        decision = _invoke_planner(
                            task_id,
                            logs,
                            PlannerReasonCode.LOW_CONFIDENCE,
                            ReviewStatus.RISK_ANALYZED,
                            finding["clause_id"],
                            clauses_payload,
                            retrieval_repair_count,
                            "risk confidence is below the deterministic threshold",
                        )
                        _append_planner_trace(
                            review_context,
                            decision,
                            retrieval_repair_count,
                        )
                        if decision["action"] == PlannerAction.ANALYZE_AGAIN.value:
                            retrieval_repair_count += 1
                            finding = invoke_tool(
                                task_id,
                                tool_registry,
                                "analyze_risk",
                                {"review_context": review_context},
                                logs,
                                step_name="planner_risk_reanalysis",
                            )
                            analysis_results[finding_index] = finding
                        elif decision["action"] == PlannerAction.REQUEST_HUMAN_REVIEW.value:
                            planner_requested_human = True
                        else:
                            self.event_store.update_task(
                                task_id,
                                ReviewStatus.NEED_MANUAL_REVIEW,
                                "低置信度风险被 Planner 终止，需要人工确认。",
                                step_name="planner_low_confidence_stopped",
                                tool_name="plan_review_action",
                                review_contexts=review_contexts,
                                analysis_results=analysis_results,
                                evidence_results=evidence_results,
                                risk_findings=[],
                                logs=[log.to_dict() for log in logs],
                            )
                            return

                    while finding["review_status"] != "NO_RISK":
                        critic_result = invoke_tool(
                            task_id,
                            tool_registry,
                            "criticize_risk",
                            {
                                "finding": finding,
                                "current_clause": review_context["current_clause"],
                                "matched_rule": review_context["matched_rule"],
                            },
                            logs,
                            step_name="risk_critique",
                        )
                        _append_critic_trace(review_context, finding, critic_result)
                        if (
                            critic_result["reason_code"]
                            == CriticReasonCode.PROMPT_INJECTION_DETECTED.value
                        ):
                            self.event_store.update_task(
                                task_id,
                                ReviewStatus.NEED_MANUAL_REVIEW,
                                "Critic 检测到不可信指令文本，已停止该候选进入正式风险流程。",
                                step_name="critic_injection_blocked",
                                tool_name="criticize_risk",
                                review_contexts=review_contexts,
                                analysis_results=analysis_results,
                                evidence_results=evidence_results,
                                risk_findings=[],
                                logs=[log.to_dict() for log in logs],
                            )
                            return
                        critic_requested_human = (
                            critic_result["decision"] != CriticDecision.PASS.value
                        )
                        finding = dict(finding)
                        if not critic_requested_human:
                            finding["related_memory"] = list(
                                review_context.get("related_memory") or []
                            )
                            revision = invoke_tool(
                                task_id,
                                tool_registry,
                                "generate_revision",
                                {
                                    "finding": finding,
                                    "preferred_position": state.review_position.value,
                                },
                                logs,
                                step_name="revision_generation",
                            )
                            finding["revision_suggestion"] = revision["revision_suggestion"]
                            if revision.get("memory_references"):
                                finding["memory_references"] = revision["memory_references"]
                            finding.pop("related_memory", None)
                        evidence_result = invoke_tool(
                            task_id,
                            tool_registry,
                            "verify_evidence",
                            {
                                "finding": finding,
                                "clauses": clauses_payload,
                                "matched_rule": review_context["matched_rule"],
                            },
                            logs,
                            step_name="evidence_verification",
                        )
                        evidence_results.append(evidence_result)
                        if evidence_result["is_valid"]:
                            break

                        reason_code = _evidence_planner_reason(evidence_result)
                        decision = _invoke_planner(
                            task_id,
                            logs,
                            reason_code,
                            ReviewStatus.RISK_ANALYZED,
                            finding["clause_id"],
                            clauses_payload,
                            retrieval_repair_count,
                            str(evidence_result["failure_reason"]),
                        )
                        _append_planner_trace(
                            review_context,
                            decision,
                            retrieval_repair_count,
                        )
                        if decision["action"] != PlannerAction.RETRIEVE_AGAIN.value:
                            self.event_store.update_task(
                                task_id,
                                ReviewStatus.EVIDENCE_MISSING,
                                "证据验证失败且检索修复预算已停止，转入人工处理。",
                                step_name="evidence_repair_stopped",
                                tool_name="plan_review_action",
                                review_contexts=review_contexts,
                                analysis_results=analysis_results,
                                evidence_results=evidence_results,
                                risk_findings=[],
                                logs=[log.to_dict() for log in logs],
                            )
                            return

                        retrieval_repair_count += 1
                        repaired_context = _rebuild_review_context(
                            task_id,
                            logs,
                            review_context,
                            clauses_payload,
                            decision["query_adjustments"],
                            embedding_cache,
                            retrieval_repair_count,
                        )
                        review_contexts[context_index_by_id[context_key]] = repaired_context
                        review_context = repaired_context
                        active_context = repaired_context
                        context_by_id[context_key] = repaired_context
                        finding = invoke_tool(
                            task_id,
                            tool_registry,
                            "analyze_risk",
                            {"review_context": repaired_context},
                            logs,
                            step_name="planner_risk_reanalysis",
                        )
                        analysis_results[finding_index] = finding

                    if finding["review_status"] == "NO_RISK":
                        evidence_completed += 1
                        next_item = (
                            _finding_progress_item(evidence_candidates[evidence_completed])
                            if evidence_completed < evidence_total
                            else ""
                        )
                        state = self._publish_progress(
                            state,
                            logs,
                            stage="evidence_verification",
                            stage_label="校验风险与证据",
                            completed=evidence_completed,
                            total=evidence_total,
                            current_item=next_item,
                            message=(
                                f"正在校验 {next_item}（{evidence_completed}/{evidence_total}）。"
                                if next_item
                                else f"风险与证据校验完成（{evidence_completed}/{evidence_total}）。"
                            ),
                            tool_name="verify_evidence",
                            review_contexts=review_contexts,
                            analysis_results=analysis_results,
                            evidence_results=evidence_results,
                            risk_findings=risk_findings,
                        )
                        continue
                    if evidence_result.get("source_location"):
                        finding["evidence_location"] = evidence_result["source_location"]
                    if (
                        planner_requested_human
                        or critic_requested_human
                        or _requires_manual_review(finding)
                    ):
                        finding["review_status"] = "NEED_MANUAL_REVIEW"
                    risk_findings.append(finding)
                    evidence_completed += 1
                    next_item = (
                        _finding_progress_item(evidence_candidates[evidence_completed])
                        if evidence_completed < evidence_total
                        else ""
                    )
                    state = self._publish_progress(
                        state,
                        logs,
                        stage="evidence_verification",
                        stage_label="校验风险与证据",
                        completed=evidence_completed,
                        total=evidence_total,
                        current_item=next_item,
                        message=(
                            f"正在校验 {next_item}（{evidence_completed}/{evidence_total}）。"
                            if next_item
                            else f"风险与证据校验完成（{evidence_completed}/{evidence_total}）。"
                        ),
                        tool_name="verify_evidence",
                        review_contexts=review_contexts,
                        analysis_results=analysis_results,
                        evidence_results=evidence_results,
                        risk_findings=risk_findings,
                    )
            except CriticOutputInvalidError as exc:
                if isinstance(active_context, dict):
                    try:
                        decision = _invoke_planner(
                            task_id,
                            logs,
                            PlannerReasonCode.STRUCTURED_OUTPUT_INVALID,
                            ReviewStatus.RISK_ANALYZED,
                            str(active_context["current_clause"]["clause_id"]),
                            clauses_payload,
                            retrieval_repair_count,
                            str(exc),
                        )
                        _append_planner_trace(
                            active_context,
                            decision,
                            retrieval_repair_count,
                        )
                    except EXECUTION_INTERRUPTS:
                        raise
                    except Exception:
                        pass
                self.event_store.update_task(
                    task_id,
                    ReviewStatus.LLM_OUTPUT_INVALID,
                    f"Critic 结构化输出无效：{exc}",
                    step_name="critic_output_invalid",
                    tool_name="criticize_risk",
                    review_contexts=review_contexts,
                    analysis_results=analysis_results,
                    evidence_results=evidence_results,
                    risk_findings=[],
                    logs=[log.to_dict() for log in logs],
                )
                return
            except PlannerOutputInvalidError as exc:
                self.event_store.update_task(
                    task_id,
                    ReviewStatus.EVIDENCE_MISSING,
                    f"Planner 决策被拒绝，未执行任何修复动作：{exc}",
                    step_name="planner_decision_rejected",
                    tool_name="plan_review_action",
                    review_contexts=review_contexts,
                    analysis_results=analysis_results,
                    evidence_results=evidence_results,
                    risk_findings=[],
                    logs=[log.to_dict() for log in logs],
                )
                return
            except LLMOutputInvalidError as exc:
                if isinstance(active_context, dict):
                    try:
                        decision = _invoke_planner(
                            task_id,
                            logs,
                            PlannerReasonCode.STRUCTURED_OUTPUT_INVALID,
                            ReviewStatus.RISK_ANALYZED,
                            str(active_context["current_clause"]["clause_id"]),
                            clauses_payload,
                            retrieval_repair_count,
                            str(exc),
                        )
                        _append_planner_trace(
                            active_context,
                            decision,
                            retrieval_repair_count,
                        )
                    except EXECUTION_INTERRUPTS:
                        raise
                    except Exception:
                        pass
                self.event_store.update_task(
                    task_id,
                    ReviewStatus.LLM_OUTPUT_INVALID,
                    f"修改建议结构化输出无效：{exc}",
                    step_name="llm_output_invalid",
                    tool_name="generate_revision",
                    review_contexts=review_contexts,
                    analysis_results=analysis_results,
                    evidence_results=evidence_results,
                    risk_findings=[],
                    logs=[log.to_dict() for log in logs],
                )
                return
            except EXECUTION_INTERRUPTS:
                raise
            except Exception as exc:
                self.event_store.update_task(
                    task_id,
                    ReviewStatus.EVIDENCE_MISSING,
                    f"证据验证失败：{exc}",
                    step_name="evidence_missing",
                    tool_name="verify_evidence",
                    review_contexts=review_contexts,
                    analysis_results=analysis_results,
                    evidence_results=evidence_results,
                    risk_findings=[],
                    logs=[log.to_dict() for log in logs],
                )
                return

            if any(finding["review_status"] == "NEED_MANUAL_REVIEW" for finding in risk_findings):
                self.event_store.update_task(
                    task_id,
                    ReviewStatus.HUMAN_REVIEW_PENDING,
                    "证据验证完成，存在需要人工复核的风险。",
                    step_name="human_review_pending",
                    tool_name="verify_evidence",
                    review_contexts=review_contexts,
                    analysis_results=analysis_results,
                    evidence_results=evidence_results,
                    risk_findings=risk_findings,
                    logs=[log.to_dict() for log in logs],
                )
                return

            self.event_store.update_task(
                task_id,
                ReviewStatus.EVIDENCE_VERIFIED,
                "证据验证完成，正式风险列表已生成。",
                step_name="evidence_verified",
                tool_name="verify_evidence",
                review_contexts=review_contexts,
                analysis_results=analysis_results,
                evidence_results=evidence_results,
                risk_findings=risk_findings,
                logs=[log.to_dict() for log in logs],
            )
        except TaskCancelledError:
            self.event_store.finalize_cancel(
                task_id,
                logs=[log.to_dict() for log in logs],
            )
        except NodeExecutionTimeoutError as exc:
            current = self.event_store.get_task(task_id)
            resume_from_status = (
                current.status.value
                if current is not None and current.status in RECOVERABLE_STATUS_ORDER
                else ReviewStatus.UPLOAD_RECEIVED.value
            )
            self._update_terminal_or_cancel(
                task_id,
                ReviewStatus.NODE_TIMEOUT,
                f"Node timeout at {exc.step_name}; manual recovery is required.",
                logs,
                step_name="node_timeout",
                last_timeout={
                    "type": "node",
                    "step_name": exc.step_name,
                    "elapsed_seconds": round(exc.elapsed_seconds, 4),
                    "limit_seconds": exc.limit_seconds,
                    "resume_from_status": resume_from_status,
                },
            )
        except TaskExecutionTimeoutError as exc:
            current = self.event_store.get_task(task_id)
            resume_from_status = (
                current.status.value
                if current is not None and current.status in RECOVERABLE_STATUS_ORDER
                else ReviewStatus.UPLOAD_RECEIVED.value
            )
            self._update_terminal_or_cancel(
                task_id,
                ReviewStatus.TASK_TIMEOUT,
                "Task execution timeout; completed results were retained for manual recovery.",
                logs,
                step_name="task_timeout",
                last_timeout={
                    "type": "task",
                    "limit_seconds": self.task_timeout_seconds,
                    "reason": str(exc),
                    "resume_from_status": resume_from_status,
                },
            )
        except RecoveryError as exc:
            self._update_terminal_or_cancel(
                task_id,
                ReviewStatus.NEED_MANUAL_REVIEW,
                str(exc),
                logs,
                step_name="recovery_blocked",
            )
        except PdfParseError as exc:
            self._update_terminal_or_cancel(
                task_id,
                ReviewStatus.PARSE_FAILED,
                f"PDF 解析失败：{exc}",
                logs,
                step_name="parse_failed",
                tool_name="parse_document",
            )
        except ValueError:
            self._record_unexpected_failure(task_id, logs)
        except Exception:
            self._record_unexpected_failure(task_id, logs)
        finally:
            final_state = self.event_store.get_task(task_id)
            write_runtime_log(
                "task_finished",
                task_id=task_id,
                trace_id=state.trace_id,
                status=final_state.status.value if final_state is not None else "TASK_MISSING",
                elapsed_ms=int((perf_counter() - execution_control.task_started_at) * 1000),
                log_count=len(final_state.logs or []) if final_state is not None else len(logs),
            )
            reset_execution_control(control_token)
            reset_llm_mode(llm_mode_token)
            self.event_store.release_execution(task_id, resolved_owner)

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


review_orchestrator_agent = ReviewOrchestratorAgent()


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


def _before(current_status: ReviewStatus, target_status: ReviewStatus) -> bool:
    if current_status not in RECOVERABLE_STATUS_ORDER:
        raise RecoveryError(f"任务状态 {current_status.value} 不能作为恢复起点。")
    return RECOVERABLE_STATUS_ORDER[current_status] < RECOVERABLE_STATUS_ORDER[target_status]


def _validate_recovery_checkpoint(state: AgentState) -> None:
    if state.status not in RECOVERABLE_STATUS_ORDER:
        raise RecoveryError(f"任务状态 {state.status.value} 无法安全恢复。")

    required_payloads = [
        (ReviewStatus.DOCUMENT_PARSED, state.document, "文档解析结果"),
        (ReviewStatus.CONTRACT_TYPE_CLASSIFIED, state.contract_classification, "合同类型识别结果"),
        (ReviewStatus.CLAUSES_STRUCTURED, state.clauses, "条款结构化结果"),
        (ReviewStatus.PLAYBOOK_RETRIEVED, state.matched_rules, "Playbook 检索结果"),
        (ReviewStatus.CONTEXT_BUILT, state.review_contexts, "风险上下文"),
        (ReviewStatus.RISK_ANALYZED, state.analysis_results, "风险分析结果"),
    ]
    current_order = RECOVERABLE_STATUS_ORDER[state.status]
    for checkpoint, payload, label in required_payloads:
        if current_order >= RECOVERABLE_STATUS_ORDER[checkpoint] and payload is None:
            raise RecoveryError(f"{label}缺失，无法从 {state.status.value} 节点安全恢复。")


def _manual_recovery_checkpoint(state: AgentState) -> ReviewStatus:
    if state.status in {ReviewStatus.NODE_TIMEOUT, ReviewStatus.TASK_TIMEOUT}:
        resume_from = str((state.last_timeout or {}).get("resume_from_status", ""))
        try:
            checkpoint = ReviewStatus(resume_from)
        except ValueError as exc:
            raise RecoveryError("timeout recovery checkpoint is missing or invalid") from exc
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
        raise RecoveryError(f"task status cannot be manually recovered: {state.status.value}")
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
