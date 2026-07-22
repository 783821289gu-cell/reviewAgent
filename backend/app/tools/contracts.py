from models.tool import ToolContract
from providers.llm_provider import effective_llm_mode


LOCAL_NO_EXTERNAL_LLM_MODES = {
    "classify_contract_type": "deterministic_features_no_external_llm",
    "extract_key_fields": "rule_based_no_external_llm",
    "analyze_risk": "local_structured_no_external_llm",
    "criticize_risk": "deterministic_support_check_no_external_llm",
    "plan_review_action": "deterministic_policy_no_external_llm",
    "generate_revision": "local_template_no_external_llm",
}


def runtime_calls_llm(tool_name: str) -> bool:
    contract = tool_contracts.get(tool_name)
    if contract is None or not contract.calls_llm:
        return False
    return effective_llm_mode() == "openai_compatible"


def runtime_llm_mode(tool_name: str) -> str:
    contract = tool_contracts.get(tool_name)
    if contract is None or not contract.calls_llm:
        return "not_applicable"
    llm_mode = effective_llm_mode()
    if llm_mode == "local_structured":
        return LOCAL_NO_EXTERNAL_LLM_MODES.get(tool_name, "no_external_llm")
    if llm_mode == "openai_compatible":
        return "openai_compatible"
    return f"invalid_llm_mode:{llm_mode}"


tool_contracts = {
    "parse_document": ToolContract(
        name="parse_document",
        input_schema={"file_id": "str", "file_name": "str", "file_type": "docx|pdf", "content": "bytes"},
        output_schema={"paragraphs": "list", "tables": "list", "page_map": "list"},
        calls_llm=False,
        description="解析 DOCX/PDF 合同正文，输出可回溯文档结构。",
    ),
    "classify_contract_type": ToolContract(
        name="classify_contract_type",
        input_schema={"document": "ContractDocument"},
        output_schema={
            "contract_type": "NDA|PROCUREMENT|SERVICE|EMPLOYMENT|UNKNOWN",
            "confidence": "float",
            "evidence": "list[str]",
            "decision": "SUPPORTED|UNSUPPORTED_CONTRACT_TYPE|NEED_MANUAL_REVIEW",
        },
        calls_llm=True,
        description="使用确定性标题、角色和保密义务特征识别 NDA；低置信度时保留人工复核入口。",
    ),
    "extract_clauses": ToolContract(
        name="extract_clauses",
        input_schema={"document": "ContractDocument"},
        output_schema={"clauses": "list[Clause]"},
        calls_llm=False,
        description="把文档结构拆分为条款结构。",
    ),
    "extract_key_fields": ToolContract(
        name="extract_key_fields",
        input_schema={"clause": "Clause"},
        output_schema={"key_fields": "dict"},
        calls_llm=True,
        description="从条款中抽取第一版需要的关键字段。",
    ),
    "retrieve_playbook_rules": ToolContract(
        name="retrieve_playbook_rules",
        input_schema={"contract_type": "str", "clause_type": "str", "key_fields": "dict", "review_position": "str"},
        output_schema={"matched_rules": "list"},
        calls_llm=False,
        description="按合同类型、条款类型、关键字段和审查立场检索 Playbook 规则。",
    ),
    "retrieve_related_clauses": ToolContract(
        name="retrieve_related_clauses",
        input_schema={
            "contract_type": "str",
            "current_clause": "dict",
            "clauses": "list",
            "risk_type": "str",
            "playbook_check_point": "str",
            "limit": "int",
            "query_adjustments": "restricted dict optional",
            "embedding_cache": "dict optional (runtime only)",
        },
        output_schema={"related_clauses": "list"},
        calls_llm=False,
        description="在当前合同内合并关键词与 Embedding 候选，去重后执行动态 Top-K 和可解释 rerank。",
    ),
    "retrieve_memory": ToolContract(
        name="retrieve_memory",
        input_schema={
            "contract_type": "str",
            "clause": "dict",
            "risk_type": "str",
            "review_position": "str",
            "memory_items": "list optional",
            "limit": "int",
            "embedding_cache": "dict optional (runtime only)",
        },
        output_schema={"memories": "list"},
        calls_llm=False,
        description="按合同类型和审查立场硬过滤 Memory，再使用模型 Embedding、精确字段因子和生命周期规则检索语义偏好。",
    ),
    "analyze_risk": ToolContract(
        name="analyze_risk",
        input_schema={"review_context": "dict"},
        output_schema={"risk_finding": "dict"},
        calls_llm=True,
        description="基于审查上下文输出结构化风险判断。",
    ),
    "criticize_risk": ToolContract(
        name="criticize_risk",
        input_schema={
            "finding": "validated RiskFinding",
            "current_clause": "current contract clause",
            "matched_rule": "matched Playbook rule",
        },
        output_schema={
            "decision": "PASS|REJECT|REQUEST_HUMAN_REVIEW",
            "reason_code": "fixed enum",
        },
        calls_llm=True,
        description="独立检查 Analyzer 风险原因是否得到当前条款原文和 Playbook 支持；不生成或修改证据、风险和规则。",
    ),
    "plan_review_action": ToolContract(
        name="plan_review_action",
        input_schema={
            "trigger_reason": "fixed enum",
            "current_status": "str",
            "target_clause_id": "str",
            "contract_clause_ids": "list[str]",
            "retry_count": "int (0..1)",
            "failure_reason": "str summary",
        },
        output_schema={
            "action": "RETRIEVE_AGAIN|ANALYZE_AGAIN|REQUEST_HUMAN_REVIEW|TERMINATE",
            "reason_code": "fixed enum",
            "target_clause_id": "str",
            "query_adjustments": "restricted dict",
            "confidence": "float",
        },
        calls_llm=True,
        description="在固定异常节点返回受白名单、当前合同条款和一次重试预算约束的结构化决策。",
    ),
    "verify_evidence": ToolContract(
        name="verify_evidence",
        input_schema={"finding": "dict", "clauses": "list", "matched_rule": "dict"},
        output_schema={"citation_status": "dict including PDF source_location when available"},
        calls_llm=False,
        description="校验风险证据是否可回到当前合同指定条款原文；PDF 证据同时定位页码、文本块和坐标，并确认风险类型与命中规则一致。",
    ),
    "generate_revision": ToolContract(
        name="generate_revision",
        input_schema={"finding": "dict", "preferred_position": "str"},
        output_schema={"revision_suggestion": "dict"},
        calls_llm=True,
        description="生成结构化修改建议。",
    ),
    "write_memory": ToolContract(
        name="write_memory",
        input_schema={"human_feedback": "dict"},
        output_schema={"memory_item": "dict"},
        calls_llm=False,
        description="写入人工反馈 Memory。",
    ),
    "generate_report": ToolContract(
        name="generate_report",
        input_schema={"task_id": "str", "task": "dict optional", "report_dir": "str optional"},
        output_schema={"report_file": "dict"},
        calls_llm=False,
        description="生成 Markdown 审查报告。",
    ),
}
