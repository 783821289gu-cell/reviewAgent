from config import settings
from models.tool import ToolContract


LOCAL_NO_EXTERNAL_LLM_MODES = {
    "classify_contract_type": "deterministic_features_no_external_llm",
    "extract_key_fields": "rule_based_no_external_llm",
    "analyze_risk": "local_structured_no_external_llm",
    "generate_revision": "local_template_no_external_llm",
}


def runtime_calls_llm(tool_name: str) -> bool:
    contract = tool_contracts.get(tool_name)
    if contract is None or not contract.calls_llm:
        return False
    return settings.llm_mode == "openai_compatible"


def runtime_llm_mode(tool_name: str) -> str:
    contract = tool_contracts.get(tool_name)
    if contract is None or not contract.calls_llm:
        return "not_applicable"
    if settings.llm_mode == "local_structured":
        return LOCAL_NO_EXTERNAL_LLM_MODES.get(tool_name, "no_external_llm")
    if settings.llm_mode == "openai_compatible":
        return "openai_compatible"
    return f"invalid_llm_mode:{settings.llm_mode}"


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
            "embedding_cache": "dict optional (runtime only)",
        },
        output_schema={"related_clauses": "list"},
        calls_llm=False,
        description="按配置使用定长 Embedding 在当前合同条款内执行余弦召回和规则 rerank。",
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
        },
        output_schema={"memories": "list"},
        calls_llm=False,
        description="按合同类型、条款类型、风险类型和审查立场检索相关历史反馈；优先过滤调用方提供的 Memory，否则查询 SQLite Memory。",
    ),
    "analyze_risk": ToolContract(
        name="analyze_risk",
        input_schema={"review_context": "dict"},
        output_schema={"risk_finding": "dict"},
        calls_llm=True,
        description="基于审查上下文输出结构化风险判断。",
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
