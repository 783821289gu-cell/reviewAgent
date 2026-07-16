from dataclasses import replace
from threading import Thread

from db.repositories import RecoveryError
from models.contract import clause_from_dict, contract_document_from_dict
from models.log import StepLog
from models.review import AgentState, ReviewPosition, ReviewStatus
from parsers.pdf_parser import PdfParseError
from providers.llm_provider import LLMOutputInvalidError
from services.context_builder import build_review_context
from services.event_service import ReviewEventStore, review_event_store
from services.log_service import invoke_tool
from tools.registry import tool_registry


class ReviewOrchestratorAgent:
    def __init__(self, event_store: ReviewEventStore = review_event_store):
        self.event_store = event_store

    def start(self, file_name: str, file_type: str, content: bytes, review_position: ReviewPosition) -> AgentState:
        state = self.event_store.create_task(file_name, file_type, review_position, content=content)
        state = self.event_store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "上传已接收，开始执行文档解析。",
            step_name="upload_received",
        )
        Thread(
            target=self.run,
            args=(state.task_id, content),
            daemon=True,
        ).start()
        return state

    def run_sync(self, file_name: str, file_type: str, content: bytes, review_position: ReviewPosition) -> AgentState:
        state = self.event_store.create_task(file_name, file_type, review_position, content=content)
        state = self.event_store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "上传已接收，开始执行文档解析。",
            step_name="upload_received",
        )
        self.run(state.task_id, content)
        final_state = self.event_store.get_task(state.task_id)
        if final_state is None:
            raise ValueError("审查任务状态丢失。")
        return final_state

    def recover_pending_tasks(self, async_mode: bool = True) -> list[str]:
        if self.event_store.persistence is None:
            return []
        recovered_task_ids = []
        for state in self.event_store.pending_tasks():
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
                continue

            recovered_task_ids.append(state.task_id)
            if async_mode:
                Thread(target=self.run, args=(state.task_id, content), daemon=True).start()
            else:
                self.run(state.task_id, content)
        return recovered_task_ids

    def run(self, task_id: str, content: bytes) -> None:
        state = self.event_store.get_task(task_id)
        if state is None:
            return

        logs = [StepLog(**item) for item in (state.logs or [])]
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
                    contract_classification = invoke_tool(
                        task_id,
                        tool_registry,
                        "classify_contract_type",
                        {"document": document},
                        logs,
                        step_name="contract_type_classification",
                    )
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
                    for clause in clauses:
                        key_fields = invoke_tool(
                            task_id,
                            tool_registry,
                            "extract_key_fields",
                            {"clause": clause},
                            logs,
                            step_name="key_field_extract",
                        )
                        clauses_with_fields.append(replace(clause, key_fields=key_fields))
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
                    for clause in clauses_with_fields:
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
            if _before(state.status, ReviewStatus.CONTEXT_BUILT):
                try:
                    review_contexts = []
                    embedding_cache = {}
                    for rule_group in rule_matches:
                        current_clause = clauses_by_id.get(rule_group["clause_id"])
                        if current_clause is None:
                            continue
                        for matched_rule in rule_group["matched_rules"]:
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

            if _before(state.status, ReviewStatus.RISK_ANALYZED):
                try:
                    analysis_results = []
                    for review_context in review_contexts:
                        finding = invoke_tool(
                            task_id,
                            tool_registry,
                            "analyze_risk",
                            {"review_context": review_context},
                            logs,
                            step_name="risk_analysis",
                        )
                        analysis_results.append(finding)
                except Exception as exc:
                    self.event_store.update_task(
                        task_id,
                        ReviewStatus.LLM_OUTPUT_INVALID,
                        f"风险分析结构化输出无效：{exc}",
                        step_name="llm_output_invalid",
                        tool_name="analyze_risk",
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
            evidence_results = []
            risk_findings = []
            try:
                for finding in analysis_results:
                    if finding["review_status"] == "NO_RISK":
                        continue
                    finding = dict(finding)
                    context_key = f"{finding['clause_id']}:{finding['matched_rule_ids'][0]}"
                    review_context = context_by_id.get(context_key)
                    if review_context is None:
                        raise ValueError("risk finding cannot be mapped back to review context")
                    finding["related_memory"] = list(review_context.get("related_memory") or [])
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
                    if not evidence_result["is_valid"]:
                        self.event_store.update_task(
                            task_id,
                            ReviewStatus.EVIDENCE_MISSING,
                            f"证据验证失败：{evidence_result['failure_reason']}",
                            step_name="evidence_missing",
                            tool_name="verify_evidence",
                            analysis_results=analysis_results,
                            evidence_results=evidence_results,
                            risk_findings=[],
                            logs=[log.to_dict() for log in logs],
                        )
                        return
                    if evidence_result.get("source_location"):
                        finding["evidence_location"] = evidence_result["source_location"]
                    if _requires_manual_review(finding):
                        finding["review_status"] = "NEED_MANUAL_REVIEW"
                    risk_findings.append(finding)
            except LLMOutputInvalidError as exc:
                self.event_store.update_task(
                    task_id,
                    ReviewStatus.LLM_OUTPUT_INVALID,
                    f"修改建议结构化输出无效：{exc}",
                    step_name="llm_output_invalid",
                    tool_name="generate_revision",
                    analysis_results=analysis_results,
                    evidence_results=evidence_results,
                    risk_findings=[],
                    logs=[log.to_dict() for log in logs],
                )
                return
            except Exception as exc:
                self.event_store.update_task(
                    task_id,
                    ReviewStatus.EVIDENCE_MISSING,
                    f"证据验证失败：{exc}",
                    step_name="evidence_missing",
                    tool_name="verify_evidence",
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
                analysis_results=analysis_results,
                evidence_results=evidence_results,
                risk_findings=risk_findings,
                logs=[log.to_dict() for log in logs],
            )
        except RecoveryError as exc:
            self.event_store.update_task(
                task_id,
                ReviewStatus.NEED_MANUAL_REVIEW,
                str(exc),
                step_name="recovery_blocked",
                logs=[log.to_dict() for log in logs],
            )
        except PdfParseError as exc:
            self.event_store.update_task(
                task_id,
                ReviewStatus.PARSE_FAILED,
                f"PDF 解析失败：{exc}",
                step_name="parse_failed",
                tool_name="parse_document",
                logs=[log.to_dict() for log in logs],
            )
        except ValueError:
            self.event_store.update_task(
                task_id,
                ReviewStatus.PARSE_FAILED,
                "合同解析失败，请确认文件为可读取的 DOCX 或 PDF。",
                step_name="parse_failed",
                logs=[log.to_dict() for log in logs],
            )
        except Exception as exc:
            self.event_store.update_task(
                task_id,
                ReviewStatus.PARSE_FAILED,
                f"合同处理失败：{exc}",
                step_name="parse_failed",
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


def _requires_manual_review(finding: dict) -> bool:
    if finding.get("review_status") == "NEED_MANUAL_REVIEW":
        return True
    if finding.get("severity") == "高":
        return True
    try:
        return float(finding.get("confidence", 1)) < 0.7
    except (TypeError, ValueError):
        return True
