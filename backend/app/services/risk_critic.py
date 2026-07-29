"""Critic 角色：复核 Analyzer 的候选风险是否同时有原文和规则支持。"""

import re

from models.risk import (
    CriticDecision,
    CriticReasonCode,
    validate_critic_result,
    validate_risk_finding,
)
from providers.llm_provider import (
    LLMOutputInvalidError,
    LLMProviderError,
    llm_call_records_from_tool_input,
    mark_latest_llm_call_schema_error,
)
from services.llm_service import generate_structured_critic
from services.prompt_service import detect_prompt_injection


class CriticOutputInvalidError(LLMOutputInvalidError):
    """DeepSeek 返回内容无法满足 Critic 固定输出结构。"""

    pass


def criticize_risk(tool_input: dict) -> dict:
    """执行一次 Critic 复核，只返回决定和原因码。"""

    # 第 1 步：验证输入并主动裁掉 Critic 不需要看的字段。
    # 角色隔离不仅靠提示词，也靠代码控制它能看到的数据。
    # 输入示例：finding.evidence_text="保密义务持续1年"，
    # current_clause.text="保密义务持续1年"，matched_rule.rule_id="NDA-R01"。
    critic_input = _validated_critic_input(tool_input)

    # 第 2 步：先用确定性规则算一份本地结果。
    # local_structured 模式直接使用它；DeepSeek 模式不会拿它替代模型答案，
    # 只是因为两种 Provider 共用 LLMRequest，所以这里统一准备该字段。
    local_output = _local_critic_result(critic_input)

    # 第 3 步：合同原文是不可信数据。若发现“忽略系统规则”等注入特征，
    # 不再把内容发给模型，直接转人工复核。
    prompt_security = detect_prompt_injection(
        critic_input["finding"],
        critic_input["current_clause"],
    )
    if prompt_security["detected"]:
        return validate_critic_result(
            {
                "decision": CriticDecision.REQUEST_HUMAN_REVIEW.value,
                "reason_code": CriticReasonCode.PROMPT_INJECTION_DETECTED.value,
            }
        ).to_dict()

    # 第 4 步：取得审计列表。它只记录 LLM 调用，不改变 Critic 的判断。
    llm_calls = llm_call_records_from_tool_input(tool_input)
    last_error = None
    for _attempt in (1, 2):
        try:
            # operation="criticize_risk" 会选中本文件顶部列出的专属提示词。
            candidate = generate_structured_critic(
                critic_input,
                local_output,
                llm_calls=llm_calls,
            )

            # 即使 DeepSeek 给出更多解释文字也不会接收；最终只能变成：
            # {"decision": "PASS",
            #  "reason_code": "SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK"}
            return validate_critic_result(candidate).to_dict()
        except LLMProviderError as exc:
            last_error = exc
            if not exc.retryable:
                break
        except Exception as exc:
            last_error = exc
            mark_latest_llm_call_schema_error(llm_calls)
            break
    raise CriticOutputInvalidError(f"critic output invalid: {last_error}")


def _validated_critic_input(tool_input: dict) -> dict:
    """验证输入，并缩小 Critic 能看到的数据范围。"""

    if not isinstance(tool_input, dict):
        raise ValueError("critic input must be a dict")
    finding = tool_input.get("finding")
    current_clause = tool_input.get("current_clause")
    matched_rule = tool_input.get("matched_rule")
    if not isinstance(finding, dict):
        raise ValueError("critic finding must be a dict")
    if not isinstance(current_clause, dict):
        raise ValueError("critic current_clause must be a dict")
    if not isinstance(matched_rule, dict):
        raise ValueError("critic matched_rule must be a dict")

    # 先确保 Analyzer finding 本身完整合法，再从中裁字段。
    validated_finding = validate_risk_finding(finding).to_dict()
    clause_id = _required_string(current_clause, "clause_id", "current_clause")
    clause_text = _required_string(current_clause, "text", "current_clause")
    rule_id = _required_string(matched_rule, "rule_id", "matched_rule")
    risk_type = _required_string(matched_rule, "risk_type", "matched_rule")
    check_point = _required_string(matched_rule, "check_point", "matched_rule")
    # Analyzer finding 原本还有 severity、confidence、revision_suggestion、Memory。
    # 返回的新 dict 不含这些数据，Critic 只能判断“结论是否有依据”。
    return {
        "finding": {
            field_name: validated_finding[field_name]
            for field_name in (
                "risk_id",
                "risk_type",
                "risk_reason",
                "clause_id",
                "evidence_text",
                "matched_rule_ids",
            )
        },
        "current_clause": {"clause_id": clause_id, "text": clause_text},
        "matched_rule": {
            "rule_id": rule_id,
            "risk_type": risk_type,
            "check_point": check_point,
        },
    }


def _local_critic_result(critic_input: dict) -> dict:
    """不调用外部模型时，按固定顺序复核候选风险。"""

    finding = critic_input["finding"]
    current_clause = critic_input["current_clause"]
    matched_rule = critic_input["matched_rule"]

    # 候选风险指向了别的条款。
    if finding["clause_id"] != current_clause["clause_id"]:
        return _result(CriticDecision.REJECT, CriticReasonCode.CLAUSE_MISMATCH)

    # 候选引用的证据为空，或不是当前条款的原文片段。
    evidence_text = finding["evidence_text"].strip()
    if not evidence_text or evidence_text not in current_clause["text"]:
        return _result(CriticDecision.REJECT, CriticReasonCode.EVIDENCE_UNSUPPORTED)

    # 候选风险类型或规则 ID 与当前 Playbook 命中不一致。
    if (
        finding["risk_type"] != matched_rule["risk_type"]
        or finding["matched_rule_ids"] != [matched_rule["rule_id"]]
    ):
        return _result(CriticDecision.REJECT, CriticReasonCode.PLAYBOOK_MISMATCH)

    # ID 和原文虽然能对上，但风险理由与证据/规则的文字关系仍不清楚。
    # 这种情况不武断拒绝，而是要求人工判断。
    if (
        not _reason_supports_evidence(finding["risk_reason"], evidence_text)
        or not _reason_supports_playbook(finding["risk_reason"], matched_rule)
    ):
        return _result(
            CriticDecision.REQUEST_HUMAN_REVIEW,
            CriticReasonCode.REASON_SUPPORT_AMBIGUOUS,
        )
    return _result(
        CriticDecision.PASS,
        CriticReasonCode.SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK,
    )


def _result(decision: CriticDecision, reason_code: CriticReasonCode) -> dict:
    """把内部枚举转换为 Critic 输出 Schema 要求的两个字符串字段。"""

    return {"decision": decision.value, "reason_code": reason_code.value}


def _reason_supports_evidence(risk_reason: str, evidence_text: str) -> bool:
    """本地模式检查风险理由与证据至少共享两个可比较词。"""

    # 这里只做最低关联检查，不把关键词重合冒充语义证明。
    # 不满足时上层会转人工，而不是在这里擅自补写理由。
    reason_terms = _terms(risk_reason)
    evidence_terms = _terms(evidence_text)
    return bool(reason_terms and evidence_terms and len(reason_terms & evidence_terms) >= 2)


def _reason_supports_playbook(risk_reason: str, matched_rule: dict) -> bool:
    """本地模式检查风险理由是否与规则类型/检查点存在最低文字关联。"""

    reason_terms = _terms(risk_reason)
    playbook_terms = _terms(
        f"{matched_rule.get('risk_type', '')} {matched_rule.get('check_point', '')}"
    )
    return bool(reason_terms and playbook_terms and len(reason_terms & playbook_terms) >= 2)


def _terms(text: str) -> set[str]:
    """把中英文文本变成可比较的词集合，供本地基线做最小关联检查。"""

    normalized = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9]{3,}", normalized))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        terms.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return terms


def _required_string(payload: dict, field_name: str, label: str) -> str:
    """读取必填字符串；为空时在调用模型前直接拒绝输入。"""

    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"critic {label} {field_name} must be a non-empty string")
    return value.strip()
