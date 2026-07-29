"""Planner 角色：流程出现指定异常时，从白名单中建议下一步。"""

from models.planner import PlannerAction, PlannerDecision, PlannerReasonCode
from models.review import ReviewStatus
from providers.llm_provider import (
    LLMOutputInvalidError,
    LLMProviderError,
    llm_call_records_from_tool_input,
    mark_latest_llm_call_schema_error,
)
from services.llm_service import PLANNER_OUTPUT_SCHEMA, generate_structured_planner


# Planner 最多批准同一分支进行一次“重新检索/重新分析”。
MAX_PLANNER_RETRIES = 1
# 重新检索时，Planner 最多补充 5 个关键词，每个不超过 40 个字符。
MAX_QUERY_KEYWORDS = 5
MAX_QUERY_KEYWORD_LENGTH = 40
# 即使模型输出其他检索参数，也会被下面的校验拒绝。
ALLOWED_QUERY_ADJUSTMENTS = {"additional_keywords", "top_k"}

# 不同异常原因拥有不同动作白名单。
# 例如证据缺失可以重新检索，但不能直接要求“重新分析”来绕过证据问题。
ALLOWED_ACTIONS_BY_REASON = {
    PlannerReasonCode.LOW_CONFIDENCE: {
        PlannerAction.ANALYZE_AGAIN,
        PlannerAction.REQUEST_HUMAN_REVIEW,
        PlannerAction.TERMINATE,
    },
    PlannerReasonCode.EVIDENCE_MISSING: {
        PlannerAction.RETRIEVE_AGAIN,
        PlannerAction.REQUEST_HUMAN_REVIEW,
        PlannerAction.TERMINATE,
    },
    PlannerReasonCode.RETRIEVAL_INSUFFICIENT: {
        PlannerAction.RETRIEVE_AGAIN,
        PlannerAction.REQUEST_HUMAN_REVIEW,
        PlannerAction.TERMINATE,
    },
    PlannerReasonCode.ANALYZER_VERIFIER_CONFLICT: {
        PlannerAction.RETRIEVE_AGAIN,
        PlannerAction.REQUEST_HUMAN_REVIEW,
        PlannerAction.TERMINATE,
    },
    PlannerReasonCode.STRUCTURED_OUTPUT_INVALID: {
        PlannerAction.REQUEST_HUMAN_REVIEW,
        PlannerAction.TERMINATE,
    },
}

# Planner 只能在这些业务状态下处理对应异常，不能在任意阶段介入。
ALLOWED_STATUSES_BY_REASON = {
    PlannerReasonCode.LOW_CONFIDENCE: {ReviewStatus.RISK_ANALYZED.value},
    PlannerReasonCode.EVIDENCE_MISSING: {ReviewStatus.RISK_ANALYZED.value},
    PlannerReasonCode.RETRIEVAL_INSUFFICIENT: {
        ReviewStatus.CONTEXT_BUILT.value,
        ReviewStatus.RISK_ANALYZED.value,
    },
    PlannerReasonCode.ANALYZER_VERIFIER_CONFLICT: {
        ReviewStatus.RISK_ANALYZED.value
    },
    PlannerReasonCode.STRUCTURED_OUTPUT_INVALID: {
        ReviewStatus.CONTEXT_BUILT.value,
        ReviewStatus.RISK_ANALYZED.value,
    },
}

# 本地模式在重新检索时使用的固定补充词；DeepSeek 模式也会收到允许调整范围，
# 但输出仍必须通过相同的关键词数量和 top_k 校验。
_RETRIEVAL_KEYWORDS = {
    PlannerReasonCode.EVIDENCE_MISSING: ["原文证据", "相关条款", "同义表达"],
    PlannerReasonCode.RETRIEVAL_INSUFFICIENT: ["相关条款", "同义表达", "交叉引用"],
    PlannerReasonCode.ANALYZER_VERIFIER_CONFLICT: ["规则一致性", "风险类型", "原文证据"],
}


class PlannerOutputInvalidError(LLMOutputInvalidError):
    """Planner 返回了白名单外或结构不合法的决定。"""

    pass


def plan_review_action(tool_input: dict) -> dict:
    """让 Planner 对一次已识别异常给出受控决定。"""

    # 第 1 步：先在调用模型前验证触发原因、业务状态、条款范围和重试次数。
    # 节点传入的不是完整合同，而是类似：
    # EVIDENCE_MISSING + RISK_ANALYZED + CL-002 + retry_count=0。
    planner_input = _validated_planner_input(tool_input)

    # LLM 调用审计列表不进入 Planner 业务判断。
    llm_calls = llm_call_records_from_tool_input(tool_input)

    # 本地确定性决策供 local_structured 模式使用。DeepSeek 模式不会拿它替代
    # 模型答案，只是两种 Provider 共用同一个 LLMRequest 接口。
    local_output = _local_decision(planner_input)

    last_error = None
    for _attempt in (1, 2):
        try:
            # operation="plan_review_action" 会选择 Planner 专属提示词和
            # PLANNER_OUTPUT_SCHEMA，而不是 Analyzer/Critic 的提示词。
            candidate = generate_structured_planner(
                planner_input,
                local_output,
                llm_calls=llm_calls,
            )

            # 模型只负责提出候选决定，例如：
            # RETRIEVE_AGAIN + {"additional_keywords": ["原文证据"], "top_k": 5}
            # 它不会在这里执行检索；调用方通过校验后才决定是否路由。
            return _validate_decision(candidate, planner_input).to_dict()
        except LLMProviderError as exc:
            last_error = exc
            if not exc.retryable:
                break
        except Exception as exc:
            last_error = exc
            mark_latest_llm_call_schema_error(llm_calls)
            break
    raise PlannerOutputInvalidError(f"planner output invalid: {last_error}")


def _validated_planner_input(tool_input: dict) -> dict:
    """把节点传入的数据整理成 Planner 可见且可相信的最小输入。"""

    if not isinstance(tool_input, dict):
        raise ValueError("planner input must be a dict")
    # 触发原因先转成枚举。任意新字符串都不能临时创造一种 Planner 用法。
    try:
        reason_code = PlannerReasonCode(str(tool_input.get("trigger_reason", "")))
    except ValueError as exc:
        raise ValueError("planner trigger_reason is not allowed") from exc

    # 同一个原因只允许出现在指定业务阶段，例如证据缺失发生在风险分析后。
    current_status = str(tool_input.get("current_status", "")).strip()
    if current_status not in ALLOWED_STATUSES_BY_REASON[reason_code]:
        raise ValueError("planner trigger is not allowed in current_status")

    # target_clause_id 是本次目标；contract_clause_ids 是当前合同的真实白名单。
    target_clause_id = str(tool_input.get("target_clause_id", "")).strip()
    contract_clause_ids = tool_input.get("contract_clause_ids")
    if not isinstance(contract_clause_ids, list) or not contract_clause_ids:
        raise ValueError("contract_clause_ids must be a non-empty list")
    normalized_clause_ids = [str(item).strip() for item in contract_clause_ids]
    if any(not item for item in normalized_clause_ids):
        raise ValueError("contract_clause_ids must not contain empty values")
    if len(normalized_clause_ids) != len(set(normalized_clause_ids)):
        raise ValueError("contract_clause_ids must be unique")
    if target_clause_id not in normalized_clause_ids:
        raise ValueError("planner target_clause_id is outside the current contract")

    # 自动修复最多一次。输入已经超出预算时，甚至不会询问模型。
    retry_count = _non_negative_int(tool_input.get("retry_count"), "retry_count")
    if retry_count > MAX_PLANNER_RETRIES:
        raise ValueError("planner retry_count exceeds retry budget")

    # allowed_actions 会作为提示词中的白名单发给 DeepSeek。
    # 模型看不到白名单外动作，返回后仍会再检查一次。
    allowed_actions = sorted(
        action.value for action in ALLOWED_ACTIONS_BY_REASON[reason_code]
    )
    # A: 节点给出原始异常数据。
    # B: 返回给模型的是裁剪后的输入，并额外带上允许动作和允许调整字段。
    return {
        "trigger_reason": reason_code.value,
        "current_status": current_status,
        "target_clause_id": target_clause_id,
        "contract_clause_ids": normalized_clause_ids,
        "retry_count": retry_count,
        "retry_budget": MAX_PLANNER_RETRIES,
        "allowed_actions": allowed_actions,
        "allowed_query_adjustments": sorted(ALLOWED_QUERY_ADJUSTMENTS),
        "failure_reason": str(tool_input.get("failure_reason", "")).strip()[:160],
    }


def _local_decision(planner_input: dict) -> dict:
    """本地模式的保守策略：有预算就重检索，否则转人工。"""

    reason_code = PlannerReasonCode(planner_input["trigger_reason"])
    retry_count = planner_input["retry_count"]
    if (
        reason_code in _RETRIEVAL_KEYWORDS
        and retry_count < planner_input["retry_budget"]
    ):
        action = PlannerAction.RETRIEVE_AGAIN
        query_adjustments = {
            "additional_keywords": list(_RETRIEVAL_KEYWORDS[reason_code]),
            "top_k": 5,
        }
        confidence = 0.9
    else:
        action = PlannerAction.REQUEST_HUMAN_REVIEW
        query_adjustments = {}
        confidence = 0.95
    return {
        "action": action.value,
        "reason_code": reason_code.value,
        "target_clause_id": planner_input["target_clause_id"],
        "query_adjustments": query_adjustments,
        "confidence": confidence,
    }


def _validate_decision(candidate: dict, planner_input: dict) -> PlannerDecision:
    """验证模型没有越权，并转换成不可变的 ``PlannerDecision``。"""

    # 第 1 关：只能返回 Schema 声明的五个字段，多一个 tool_call 也会失败。
    if not isinstance(candidate, dict):
        raise ValueError("planner decision must be a dict")
    expected_fields = set(PLANNER_OUTPUT_SCHEMA["properties"])
    if set(candidate) != expected_fields:
        raise ValueError("planner decision fields do not match the output whitelist")

    # 第 2 关：动作和原因都必须属于代码枚举。
    try:
        action = PlannerAction(str(candidate.get("action", "")))
    except ValueError as exc:
        raise ValueError("planner action is not allowed") from exc
    try:
        reason_code = PlannerReasonCode(str(candidate.get("reason_code", "")))
    except ValueError as exc:
        raise ValueError("planner reason_code is not allowed") from exc

    # 第 3 关：模型不能改写触发原因，也不能为该原因选择未授权动作。
    expected_reason = PlannerReasonCode(planner_input["trigger_reason"])
    if reason_code != expected_reason:
        raise ValueError("planner reason_code does not match trigger_reason")
    if action not in ALLOWED_ACTIONS_BY_REASON[reason_code]:
        raise ValueError("planner action is not allowed for reason_code")

    # 第 4 关：模型不能把 CL-002 改成另一条款，更不能指向合同外 ID。
    target_clause_id = str(candidate.get("target_clause_id", "")).strip()
    if target_clause_id != planner_input["target_clause_id"]:
        raise ValueError("planner target_clause_id changed the requested clause")
    if target_clause_id not in planner_input["contract_clause_ids"]:
        raise ValueError("planner target_clause_id is outside the current contract")

    # 第 5 关：自动动作必须还有重试预算。
    retry_count = planner_input["retry_count"]
    if action in {PlannerAction.RETRIEVE_AGAIN, PlannerAction.ANALYZE_AGAIN}:
        if retry_count >= planner_input["retry_budget"]:
            raise ValueError("planner retry budget is exhausted")

    # 第 6 关：只有 RETRIEVE_AGAIN 可以保留检索参数，其余动作强制变成空字典。
    raw_query_adjustments = candidate.get("query_adjustments")
    if not isinstance(raw_query_adjustments, dict):
        raise ValueError("planner query_adjustments must be a dict")
    query_adjustments = (
        _validated_query_adjustments(raw_query_adjustments, require_adjustment=True)
        if action == PlannerAction.RETRIEVE_AGAIN
        else {}
    )

    # 第 7 关：Planner 对自己决定的置信度也必须是 0..1 数字。
    confidence = candidate.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("planner confidence must be a number")
    if not 0 <= float(confidence) <= 1:
        raise ValueError("planner confidence must be between 0 and 1")
    return PlannerDecision(
        action=action,
        reason_code=reason_code,
        target_clause_id=target_clause_id,
        query_adjustments=query_adjustments,
        confidence=round(float(confidence), 4),
    )


def _validated_query_adjustments(value, require_adjustment: bool) -> dict:
    """只接受关键词和 top_k 两种检索调整，拒绝模型添加的任意参数。"""

    if not isinstance(value, dict):
        raise ValueError("planner query_adjustments must be a dict")
    # 先拒绝额外字段，例如 index_name 或任意 SQL。
    unexpected = set(value) - ALLOWED_QUERY_ADJUSTMENTS
    if unexpected:
        raise ValueError("planner query_adjustments contains unsupported fields")

    normalized = {}
    # 再逐项限制关键词数量、长度并去重。
    if "additional_keywords" in value:
        keywords = value["additional_keywords"]
        if (
            not isinstance(keywords, list)
            or not keywords
            or len(keywords) > MAX_QUERY_KEYWORDS
        ):
            raise ValueError("planner additional_keywords exceeds its limit")
        normalized_keywords = [str(item).strip() for item in keywords]
        if any(
            not item or len(item) > MAX_QUERY_KEYWORD_LENGTH
            for item in normalized_keywords
        ):
            raise ValueError("planner additional_keywords contains an invalid value")
        normalized["additional_keywords"] = list(dict.fromkeys(normalized_keywords))
    # top_k 只能取 1..5，防止模型把一次修复扩大成无界检索。
    if "top_k" in value:
        top_k = value["top_k"]
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 5:
            raise ValueError("planner top_k must be between 1 and 5")
        normalized["top_k"] = top_k
    if require_adjustment and not normalized:
        raise ValueError("RETRIEVE_AGAIN requires query adjustments")
    return normalized


def _non_negative_int(value, field_name: str) -> int:
    """校验重试计数必须是非负整数，避免布尔值被当作 0/1 接受。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"planner {field_name} must be a non-negative integer")
    return value
