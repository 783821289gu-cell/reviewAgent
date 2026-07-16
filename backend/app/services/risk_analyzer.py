from models.risk import validate_risk_finding
from providers.llm_provider import (
    LLMOutputInvalidError,
    LLMProviderError,
    llm_call_records_from_tool_input,
    mark_latest_llm_call_schema_error,
)
from services.llm_service import generate_structured_revision, generate_structured_risk


def analyze_risk(tool_input: dict) -> dict:
    review_context = tool_input.get("review_context")
    if not isinstance(review_context, dict):
        raise ValueError("review_context must be a dict")
    llm_calls = llm_call_records_from_tool_input(tool_input)

    last_error = None
    for attempt in (1, 2):
        try:
            candidate = generate_structured_risk(
                review_context,
                attempt=attempt,
                llm_calls=llm_calls,
            )
            finding = validate_risk_finding(candidate)
            _validate_position_basis(finding.to_dict(), review_context)
            return finding.to_dict()
        except LLMProviderError as exc:
            last_error = exc
            if not exc.retryable:
                break
        except Exception as exc:
            last_error = exc
            mark_latest_llm_call_schema_error(llm_calls)
    raise LLMOutputInvalidError(f"LLM output invalid after retry: {last_error}")


def _validate_position_basis(finding: dict, review_context: dict) -> None:
    review_position = str(review_context.get("review_position", "")).strip()
    matched_rule = review_context.get("matched_rule")
    if not isinstance(matched_rule, dict):
        raise ValueError("review_context matched_rule must be a dict")
    position_config = matched_rule.get("position_config")
    if not isinstance(position_config, dict):
        raise ValueError("matched rule position_config is required")

    if str(finding.get("review_position", "")).strip() != review_position:
        raise ValueError("risk finding review_position does not match review context")
    if str(finding.get("risk_focus", "")).strip() != str(
        position_config.get("risk_focus", "")
    ).strip():
        raise ValueError("risk finding risk_focus does not match position_config")
    if str(finding.get("risk_type", "")) != str(matched_rule.get("risk_type", "")):
        raise ValueError("risk finding risk_type does not match matched rule")
    if str(matched_rule.get("rule_id", "")) not in [
        str(rule_id) for rule_id in finding.get("matched_rule_ids") or []
    ]:
        raise ValueError("risk finding does not reference matched rule")


def generate_revision(tool_input: dict) -> dict:
    finding = tool_input.get("finding")
    preferred_position = str(tool_input.get("preferred_position", "")).strip()
    if not isinstance(finding, dict):
        raise ValueError("finding must be a dict")
    if not preferred_position:
        raise ValueError("preferred_position is required")
    finding_position = str(finding.get("review_position", "")).strip()
    if finding_position != preferred_position:
        raise ValueError("preferred_position does not match finding review_position")
    llm_calls = llm_call_records_from_tool_input(tool_input)

    local_revision = str(finding.get("revision_suggestion", "")).strip()
    if not local_revision:
        local_revision = (
            f"建议围绕“{finding.get('risk_type', '')}”和“{finding.get('risk_focus', '')}”"
            f"补充明确、可执行且符合{preferred_position}审查立场的条款表述。"
        )

    last_error = None
    revision_text = ""
    for _attempt in (1, 2):
        try:
            candidate = generate_structured_revision(
                finding,
                preferred_position,
                local_revision,
                llm_calls=llm_calls,
            )
            revision_text = _validate_revision_candidate(candidate)
            break
        except LLMProviderError as exc:
            last_error = exc
            if not exc.retryable:
                break
        except Exception as exc:
            last_error = exc
            mark_latest_llm_call_schema_error(llm_calls)
    if not revision_text:
        raise LLMOutputInvalidError(f"revision output invalid after retry: {last_error}")

    memory_references = _memory_references(finding.get("related_memory") or [])
    if memory_references:
        reference = memory_references[0]
        suggested_text = reference.get("final_suggestion", "")
        if suggested_text:
            revision_text = f"{revision_text} 历史反馈参考：{suggested_text}"

    return {
        "revision_suggestion": revision_text,
        "preferred_position": preferred_position,
        "risk_focus": str(finding.get("risk_focus", "")),
        "basis_rule_ids": list(finding.get("matched_rule_ids") or []),
        "memory_references": memory_references,
    }


def _validate_revision_candidate(candidate: dict) -> str:
    if not isinstance(candidate, dict):
        raise ValueError("revision output must be a dict")
    revision_text = candidate.get("revision_suggestion")
    if not isinstance(revision_text, str) or not revision_text.strip():
        raise ValueError("revision_suggestion must be a non-empty string")
    return revision_text.strip()


def _memory_references(related_memory: list) -> list[dict]:
    references = []
    for item in related_memory[:3]:
        if not isinstance(item, dict):
            continue
        references.append(
            {
                "memory_id": str(item.get("memory_id", "")),
                "user_action": str(item.get("user_action", "")),
                "final_severity": str(item.get("final_severity", "")),
                "final_suggestion": str(item.get("final_suggestion", "")),
                "source_finding_id": str(item.get("source_finding_id", "")),
                "created_at": str(item.get("created_at", "")),
            }
        )
    return references
