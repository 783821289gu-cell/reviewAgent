"""Planner 角色：流程出现指定异常时，从白名单中选择下一步。

Planner 不是风险分析者，也不是整个 LangGraph 的总指挥。正常路径
``Analyzer -> Critic -> Evidence Verifier`` 不需要它；只有低置信度、证据缺失、
检索不足等代码已经识别出的异常，节点才会调用 ``plan_review_action``。

它的权限可以概括为“提建议，不执行”：

* 可以选择 RETRIEVE_AGAIN、ANALYZE_AGAIN、REQUEST_HUMAN_REVIEW、TERMINATE。
* 不能直接调用检索或 Analyzer，也不能跳转到任意 LangGraph 节点。
* 返回后仍由节点代码检查白名单、重试预算和目标条款，再决定是否执行。

Planner 专属提示词原文位于 ``prompt_service._OPERATION_INSTRUCTIONS``：

    Choose exactly one allowed planner action for the supplied trigger. Do not
    call tools, create findings, change Playbook rules, or target a clause
    outside the whitelist.

Analyzer、Critic、Planner 共用 DeepSeek Provider 和系统安全规则，但通过不同
``operation`` 获得不同角色提示词、不同输入数据和不同 JSON 输出结构。
"""

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
    """让 Planner 对一次已识别异常给出受控决定。

    ``tool_input`` 来自发生异常的 LangGraph 节点，而不是完整合同上下文：

    * ``trigger_reason``：为什么现在需要 Planner。
    * ``current_status``：异常发生时任务处于哪个业务状态。
    * ``target_clause_id``：本次只能处理哪条条款。
    * ``contract_clause_ids``：当前合同真实存在的条款 ID 白名单。
    * ``retry_count``：这条风险已经修复过几次。
    * ``failure_reason``：给 Planner 看的简短失败摘要，最多保留 160 字。
    * ``_llm_call_records``：工具执行器附带的调用审计列表。

    示例输入 A::

        {
            "trigger_reason": "EVIDENCE_MISSING",
            "current_status": "RISK_ANALYZED",
            "target_clause_id": "CL-002",
            "contract_clause_ids": ["CL-001", "CL-002"],
            "retry_count": 0,
            "failure_reason": "证据文本不在当前条款中"
        }

    允许输出 B::

        {
            "action": "RETRIEVE_AGAIN",
            "reason_code": "EVIDENCE_MISSING",
            "target_clause_id": "CL-002",
            "query_adjustments": {
                "additional_keywords": ["原文证据", "相关条款"],
                "top_k": 5
            },
            "confidence": 0.9
        }

    B 不会自己执行。调用它的 LangGraph 节点看到 ``RETRIEVE_AGAIN`` 后，才把
    ``query_adjustments`` 写入分支状态并路由到 ``repair_retrieval``。
    """

    # 第 1 步：先在调用模型前验证触发原因、业务状态、条款范围和重试次数。
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

            # 模型只负责提出候选决定；代码再次执行完整权限校验。
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
    try:
        reason_code = PlannerReasonCode(str(tool_input.get("trigger_reason", "")))
    except ValueError as exc:
        raise ValueError("planner trigger_reason is not allowed") from exc

    current_status = str(tool_input.get("current_status", "")).strip()
    if current_status not in ALLOWED_STATUSES_BY_REASON[reason_code]:
        raise ValueError("planner trigger is not allowed in current_status")

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

    retry_count = _non_negative_int(tool_input.get("retry_count"), "retry_count")
    if retry_count > MAX_PLANNER_RETRIES:
        raise ValueError("planner retry_count exceeds retry budget")

    # allowed_actions 会作为提示词中的白名单发给 DeepSeek。
    # 模型看不到白名单外动作，返回后仍会再检查一次。
    allowed_actions = sorted(
        action.value for action in ALLOWED_ACTIONS_BY_REASON[reason_code]
    )
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
    """本地模式的保守策略：有预算就重检索，否则转人工。

    它不会因为“看起来可以”就无限循环。一次预算用完后，不再批准自动修复。
    """

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
    """验证模型没有越权，并转换成不可变的 ``PlannerDecision``。

    这里是 Planner 真正的权限边界：提示词只是告诉模型应该怎么做，本函数负责
    保证模型即使不听话也无法越过动作、原因、条款和重试预算白名单。
    """

    if not isinstance(candidate, dict):
        raise ValueError("planner decision must be a dict")
    expected_fields = set(PLANNER_OUTPUT_SCHEMA["properties"])
    if set(candidate) != expected_fields:
        raise ValueError("planner decision fields do not match the output whitelist")

    try:
        action = PlannerAction(str(candidate.get("action", "")))
    except ValueError as exc:
        raise ValueError("planner action is not allowed") from exc
    try:
        reason_code = PlannerReasonCode(str(candidate.get("reason_code", "")))
    except ValueError as exc:
        raise ValueError("planner reason_code is not allowed") from exc

    expected_reason = PlannerReasonCode(planner_input["trigger_reason"])
    if reason_code != expected_reason:
        raise ValueError("planner reason_code does not match trigger_reason")
    if action not in ALLOWED_ACTIONS_BY_REASON[reason_code]:
        raise ValueError("planner action is not allowed for reason_code")

    target_clause_id = str(candidate.get("target_clause_id", "")).strip()
    if target_clause_id != planner_input["target_clause_id"]:
        raise ValueError("planner target_clause_id changed the requested clause")
    if target_clause_id not in planner_input["contract_clause_ids"]:
        raise ValueError("planner target_clause_id is outside the current contract")

    retry_count = planner_input["retry_count"]
    if action in {PlannerAction.RETRIEVE_AGAIN, PlannerAction.ANALYZE_AGAIN}:
        if retry_count >= planner_input["retry_budget"]:
            raise ValueError("planner retry budget is exhausted")

    raw_query_adjustments = candidate.get("query_adjustments")
    if not isinstance(raw_query_adjustments, dict):
        raise ValueError("planner query_adjustments must be a dict")
    query_adjustments = (
        _validated_query_adjustments(raw_query_adjustments, require_adjustment=True)
        if action == PlannerAction.RETRIEVE_AGAIN
        else {}
    )

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
    unexpected = set(value) - ALLOWED_QUERY_ADJUSTMENTS
    if unexpected:
        raise ValueError("planner query_adjustments contains unsupported fields")

    normalized = {}
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
