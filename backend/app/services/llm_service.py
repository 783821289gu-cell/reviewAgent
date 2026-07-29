"""把各角色的业务输入交给统一 LLM Provider，并规定结构化输出。

本文件回答“是不是给了不同提示词”这个问题：

* Analyzer 使用 ``operation="analyze_risk"``。
* Critic 使用 ``operation="criticize_risk"``。
* Planner 使用 ``operation="plan_review_action"``。
* ``operation`` 传到 PromptService 后选择不同角色指令。
* ``output_schema`` 决定该角色允许返回哪些 JSON 字段。
* 三者最后都经过 ``_generate_structured``，共用同一个 Provider 配置。

所以角色差异不是三个模型，而是“专属 operation + 专属输入 + 专属 schema +
各服务自己的校验”。``openai_compatible`` 模式会请求 DeepSeek；
``local_structured`` 模式则由 Provider 直接返回 ``local_output``。
"""

from config import settings
from models.risk import (
    CriticDecision,
    CriticReasonCode,
    VALID_REVIEW_POSITIONS,
    VALID_REVIEW_STATUSES,
    VALID_RISK_TYPES,
    VALID_SEVERITIES,
)
from providers.llm_provider import (
    LLMCallMetadata,
    LLMOutputInvalidError,
    LLMRequest,
    create_llm_provider,
    effective_llm_mode,
)


BROAD_DEFINITION_TERMS = ("任何", "全部", "所有", "一切", "商业信息", "技术资料", "合作资料", "business information")
EXCEPTION_TERMS = ("公开", "已知", "第三方", "独立开发", "依法", "法律", "regulation", "required by law")
TERM_RISK_TERMS = ("永久", "无限期", "perpetual", "indefinite")
PURPOSE_TERMS = ("目的", "仅用于", "仅可", "purpose", "use only")
DISCLOSURE_RISK_TERMS = ("任何", "所有", "关联方", "affiliate", "representatives")
DISCLOSURE_SAFETY_TERMS = ("必要", "保密义务", "need to know", "confidentiality obligation")
RETURN_TERMS = ("返还", "销毁", "删除", "return", "destroy", "delete")
LIABILITY_RISK_TERMS = ("不限", "无限", "全部损失", "间接损失", "unlimited", "indirect damages")
PENALTY_TERMS = ("违约金", "penalty", "liquidated damages")

# Analyzer 的输出合同：必须返回这些字段，而且不能额外增加字段。
RISK_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "risk_type",
        "severity",
        "confidence",
        "risk_reason",
        "clause_id",
        "evidence_text",
        "matched_rule_ids",
        "review_position",
        "risk_focus",
        "revision_suggestion",
        "review_status",
    ],
    "properties": {
        "risk_type": {"enum": sorted(VALID_RISK_TYPES)},
        "severity": {"enum": sorted(VALID_SEVERITIES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "risk_reason": {"type": "string", "minLength": 1},
        "clause_id": {"type": "string", "minLength": 1},
        "evidence_text": {"type": "string"},
        "matched_rule_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "review_position": {"enum": sorted(VALID_REVIEW_POSITIONS)},
        "risk_focus": {"type": "string", "minLength": 1},
        "revision_suggestion": {"type": "string", "minLength": 1},
        "review_status": {"enum": sorted(VALID_REVIEW_STATUSES)},
    },
}
REVISION_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["revision_suggestion"],
    "properties": {"revision_suggestion": {"type": "string"}},
}
KEY_FIELD_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        field_name: {"type": "array", "items": {"type": "string"}}
        for field_name in (
            "obligation_subject",
            "right_holder",
            "confidentiality_period",
            "permitted_disclosure_targets",
            "use_purpose",
            "liability_scope",
            "breach_liability",
        )
    },
}
CONTRACT_TYPE_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contract_type", "confidence", "evidence", "decision"],
    "properties": {
        "contract_type": {"enum": ["NDA", "PROCUREMENT", "SERVICE", "EMPLOYMENT", "UNKNOWN"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "decision": {
            "enum": ["SUPPORTED", "UNSUPPORTED_CONTRACT_TYPE", "NEED_MANUAL_REVIEW"]
        },
    },
}
# Planner 只允许返回“下一步动作”，不能返回风险结论或工具调用。
PLANNER_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "action",
        "reason_code",
        "target_clause_id",
        "query_adjustments",
        "confidence",
    ],
    "properties": {
        "action": {
            "enum": [
                "RETRIEVE_AGAIN",
                "ANALYZE_AGAIN",
                "REQUEST_HUMAN_REVIEW",
                "TERMINATE",
            ]
        },
        "reason_code": {
            "enum": [
                "LOW_CONFIDENCE",
                "EVIDENCE_MISSING",
                "RETRIEVAL_INSUFFICIENT",
                "ANALYZER_VERIFIER_CONFLICT",
                "STRUCTURED_OUTPUT_INVALID",
            ]
        },
        "target_clause_id": {"type": "string", "minLength": 1},
        "query_adjustments": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "additional_keywords": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {"type": "string", "minLength": 1, "maxLength": 40},
                },
                "top_k": {"type": "integer", "minimum": 1, "maximum": 5},
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}
# Critic 的权限最小，只能输出是否通过以及固定原因码。
CRITIC_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "reason_code"],
    "properties": {
        "decision": {"enum": [decision.value for decision in CriticDecision]},
        "reason_code": {"enum": [reason.value for reason in CriticReasonCode]},
    },
}


def generate_structured_risk(
    review_context: dict,
    attempt: int = 1,
    llm_calls: list[LLMCallMetadata] | None = None,
) -> dict:
    """准备 Analyzer 请求。

    ``review_context`` 是当前条款、规则、关联条款、Memory 和证据约束；
    ``attempt`` 表示当前第几次结构化生成，测试时可用它选择模拟输出；
    ``llm_calls`` 是 Provider 追加记录的审计列表，不会写进提示词。

    ``operation="analyze_risk"`` 是选择 Analyzer 专属提示词的关键。
    """

    prompt_security = review_context.get("prompt_security")
    if isinstance(prompt_security, dict) and prompt_security.get("detected") is True:
        signal_codes = prompt_security.get("signal_codes") or []
        raise LLMOutputInvalidError(
            "PROMPT_INJECTION_DETECTED: untrusted context contains instruction-like input "
            f"({','.join(str(item) for item in signal_codes)})"
        )

    debug_outputs = review_context.get("debug_llm_outputs")
    if isinstance(debug_outputs, list) and attempt <= len(debug_outputs):
        local_output = dict(debug_outputs[attempt - 1])
    else:
        local_output = _local_structured_risk(review_context)

    return _generate_structured(
        operation="analyze_risk",
        input_payload=review_context,
        output_schema=RISK_OUTPUT_SCHEMA,
        local_output=local_output,
        llm_calls=llm_calls,
    )


def generate_structured_revision(
    finding: dict,
    preferred_position: str,
    local_revision: str,
    llm_calls: list[LLMCallMetadata] | None = None,
) -> dict:
    return _generate_structured(
        operation="generate_revision",
        input_payload={
            "finding": finding,
            "preferred_position": preferred_position,
        },
        output_schema=REVISION_OUTPUT_SCHEMA,
        local_output={"revision_suggestion": local_revision},
        llm_calls=llm_calls,
    )


def generate_structured_key_fields(
    clause: dict,
    local_output: dict,
    llm_calls: list[LLMCallMetadata] | None = None,
) -> dict:
    return _generate_structured(
        operation="extract_key_fields",
        input_payload={"clause": clause},
        output_schema=KEY_FIELD_OUTPUT_SCHEMA,
        local_output=local_output,
        llm_calls=llm_calls,
    )


def generate_structured_contract_type(
    document_texts: list[str],
    local_output: dict,
    llm_calls: list[LLMCallMetadata] | None = None,
) -> dict:
    return _generate_structured(
        operation="classify_contract_type",
        input_payload={"document_texts": document_texts},
        output_schema=CONTRACT_TYPE_OUTPUT_SCHEMA,
        local_output=local_output,
        llm_calls=llm_calls,
    )


def generate_structured_planner(
    planner_input: dict,
    local_output: dict,
    llm_calls: list[LLMCallMetadata] | None = None,
) -> dict:
    """准备 Planner 请求。

    ``planner_input`` 只含异常原因、条款白名单和重试预算；``local_output`` 是
    本地模式直接采用的保守决定。专属 operation 和 schema 限制它不能输出风险。
    """

    return _generate_structured(
        operation="plan_review_action",
        input_payload=planner_input,
        output_schema=PLANNER_OUTPUT_SCHEMA,
        local_output=local_output,
        llm_calls=llm_calls,
    )


def generate_structured_critic(
    critic_input: dict,
    local_output: dict,
    llm_calls: list[LLMCallMetadata] | None = None,
) -> dict:
    """准备 Critic 请求。

    ``critic_input`` 只含 finding、当前条款和规则；``local_output`` 是确定性
    复核基线。专属 operation 和 schema 把输出限制为决定与原因码。
    """

    return _generate_structured(
        operation="criticize_risk",
        input_payload=critic_input,
        output_schema=CRITIC_OUTPUT_SCHEMA,
        local_output=local_output,
        llm_calls=llm_calls,
    )


def _generate_structured(
    operation: str,
    input_payload: dict,
    output_schema: dict,
    local_output: dict,
    llm_calls: list[LLMCallMetadata] | None,
) -> dict:
    """三个角色共用的 Provider 出口。

    * ``operation``：选择角色提示词，例如 ``criticize_risk``。
    * ``input_payload``：这个角色本次真正能看到的数据。
    * ``output_schema``：这个角色允许返回的 JSON 形状。
    * ``local_output``：不开 DeepSeek 时返回的确定性结果。
    * ``llm_calls``：只用于追加 token、耗时和错误等审计信息。

    DeepSeek 模式的数据流：

    A ``operation + input_payload + output_schema``
    -> B ``PromptService.build_prompt_package``
    -> C ``system/user 两条消息``
    -> D ``DeepSeek HTTP 请求``
    -> E ``response.output``。
    """

    # 模型模式由任务运行上下文决定，不允许某个角色自行选择模型。
    provider = create_llm_provider(settings, llm_mode=effective_llm_mode())
    response = provider.generate_structured(
        LLMRequest(
            operation=operation,
            input_payload=input_payload,
            output_schema=output_schema,
            local_output=local_output,
        ),
        call_records=llm_calls,
    )
    return response.output


def _local_structured_risk(review_context: dict) -> dict:

    clause = review_context.get("current_clause") or {}
    rule = review_context.get("matched_rule") or {}
    review_position, position_config = _position_config(review_context, rule)
    risk_type = str(rule.get("risk_type", ""))
    clause_text = str(clause.get("text", ""))
    evidence_text = _detect_evidence(risk_type, clause_text)
    review_status = _review_status(position_config, evidence_text)

    return {
        "risk_type": risk_type,
        "severity": str(position_config["severity_default"]),
        "confidence": _confidence(review_status, evidence_text),
        "risk_reason": _risk_reason(rule, evidence_text, review_position, position_config),
        "clause_id": str(clause.get("clause_id", "")),
        "evidence_text": evidence_text,
        "matched_rule_ids": [str(rule.get("rule_id", ""))],
        "review_position": review_position,
        "risk_focus": str(position_config["risk_focus"]),
        "revision_suggestion": str(position_config["revision_template"]),
        "review_status": review_status,
    }


def _detect_evidence(risk_type: str, clause_text: str) -> str:
    text = clause_text.lower()
    if risk_type == "保密信息范围过宽" and _contains_any(text, BROAD_DEFINITION_TERMS):
        return _sentence_with_term(clause_text, BROAD_DEFINITION_TERMS)
    if risk_type == "缺少保密信息例外" and not _contains_any(text, EXCEPTION_TERMS):
        return _short_text(clause_text)
    if risk_type == "保密期限不合理" and _contains_any(text, TERM_RISK_TERMS):
        return _sentence_with_term(clause_text, TERM_RISK_TERMS)
    if risk_type == "使用目的或使用限制不清" and not _contains_any(text, PURPOSE_TERMS):
        return _short_text(clause_text)
    if risk_type == "允许披露对象过宽" and _contains_any(text, DISCLOSURE_RISK_TERMS):
        if not _contains_any(text, DISCLOSURE_SAFETY_TERMS):
            return _sentence_with_term(clause_text, DISCLOSURE_RISK_TERMS)
    if risk_type == "返还或销毁义务不明确" and not _contains_any(text, RETURN_TERMS):
        return _short_text(clause_text)
    if risk_type == "责任无限或责任边界不清" and _contains_any(text, LIABILITY_RISK_TERMS):
        return _sentence_with_term(clause_text, LIABILITY_RISK_TERMS)
    if risk_type == "违约责任或违约金明显不合理" and _contains_any(text, PENALTY_TERMS):
        return _sentence_with_term(clause_text, PENALTY_TERMS)
    return ""


def _review_status(position_config: dict, evidence_text: str) -> str:
    if not evidence_text:
        return "NO_RISK"
    if position_config.get("severity_default") == "高":
        return "NEED_MANUAL_REVIEW"
    return "CONFIRMED_RISK"


def _confidence(review_status: str, evidence_text: str) -> float:
    if review_status == "NO_RISK":
        return 0.72
    return 0.86 if len(evidence_text) >= 6 else 0.62


def _risk_reason(
    rule: dict,
    evidence_text: str,
    review_position: str,
    position_config: dict,
) -> str:
    position_basis = f"{review_position}立场风险重点：{position_config.get('risk_focus', '')}"
    if not evidence_text:
        return f"未在当前条款中发现触发“{rule.get('risk_type', '')}”的明确证据；{position_basis}。"
    return (
        f"证据文本“{evidence_text}”命中 Playbook 检查点：{rule.get('check_point', '')}；"
        f"{position_basis}。"
    )


def _position_config(review_context: dict, rule: dict) -> tuple[str, dict]:
    review_position = str(review_context.get("review_position", "")).strip()
    if not review_position:
        raise ValueError("review_context review_position is required")
    if str(rule.get("review_position", "")).strip() != review_position:
        raise ValueError("matched rule review_position does not match review context")

    position_config = rule.get("position_config")
    if not isinstance(position_config, dict):
        raise ValueError("matched rule position_config is required")
    for field_name in ("severity_default", "risk_focus", "revision_template"):
        if not str(position_config.get(field_name, "")).strip():
            raise ValueError(f"matched rule position_config.{field_name} is required")
    return review_position, position_config


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)


def _sentence_with_term(clause_text: str, terms: tuple[str, ...]) -> str:
    separators = ("。", "；", ";", ".")
    sentences = [clause_text]
    for separator in separators:
        if separator in clause_text:
            sentences = [item.strip() for item in clause_text.split(separator) if item.strip()]
            break
    lower_terms = tuple(term.lower() for term in terms)
    for sentence in sentences:
        if _contains_any(sentence.lower(), lower_terms):
            return _short_text(sentence)
    return _short_text(clause_text)


def _short_text(text: str) -> str:
    return str(text or "").strip()[:180]
