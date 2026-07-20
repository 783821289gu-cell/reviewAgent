from models.planner import PlannerAction, PlannerDecision, PlannerReasonCode
from models.review import ReviewStatus
from providers.llm_provider import (
    LLMOutputInvalidError,
    LLMProviderError,
    llm_call_records_from_tool_input,
    mark_latest_llm_call_schema_error,
)
from services.llm_service import PLANNER_OUTPUT_SCHEMA, generate_structured_planner


MAX_PLANNER_RETRIES = 1
MAX_QUERY_KEYWORDS = 5
MAX_QUERY_KEYWORD_LENGTH = 40
ALLOWED_QUERY_ADJUSTMENTS = {"additional_keywords", "top_k"}

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

_RETRIEVAL_KEYWORDS = {
    PlannerReasonCode.EVIDENCE_MISSING: ["原文证据", "相关条款", "同义表达"],
    PlannerReasonCode.RETRIEVAL_INSUFFICIENT: ["相关条款", "同义表达", "交叉引用"],
    PlannerReasonCode.ANALYZER_VERIFIER_CONFLICT: ["规则一致性", "风险类型", "原文证据"],
}


class PlannerOutputInvalidError(LLMOutputInvalidError):
    pass


def plan_review_action(tool_input: dict) -> dict:
    planner_input = _validated_planner_input(tool_input)
    llm_calls = llm_call_records_from_tool_input(tool_input)
    local_output = _local_decision(planner_input)

    last_error = None
    for _attempt in (1, 2):
        try:
            candidate = generate_structured_planner(
                planner_input,
                local_output,
                llm_calls=llm_calls,
            )
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

    query_adjustments = _validated_query_adjustments(
        candidate.get("query_adjustments"),
        require_adjustment=action == PlannerAction.RETRIEVE_AGAIN,
    )
    if action != PlannerAction.RETRIEVE_AGAIN and query_adjustments:
        raise ValueError("planner query adjustments require RETRIEVE_AGAIN")

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
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"planner {field_name} must be a non-negative integer")
    return value
