from models.risk import validate_risk_finding
from services.llm_service import generate_structured_risk


def analyze_risk(tool_input: dict) -> dict:
    review_context = tool_input.get("review_context")
    if not isinstance(review_context, dict):
        raise ValueError("review_context must be a dict")

    last_error = None
    for attempt in (1, 2):
        try:
            candidate = generate_structured_risk(review_context, attempt=attempt)
            finding = validate_risk_finding(candidate)
            return finding.to_dict()
        except Exception as exc:
            last_error = exc
    raise ValueError(f"LLM output invalid after retry: {last_error}")


def generate_revision(tool_input: dict) -> dict:
    finding = tool_input.get("finding")
    preferred_position = str(tool_input.get("preferred_position", "")).strip()
    if not isinstance(finding, dict):
        raise ValueError("finding must be a dict")
    if not preferred_position:
        raise ValueError("preferred_position is required")

    revision_text = str(finding.get("revision_suggestion", "")).strip()
    if not revision_text:
        revision_text = f"建议围绕“{finding.get('risk_type', '')}”补充明确、可执行且符合{preferred_position}审查立场的条款表述。"

    memory_references = _memory_references(finding.get("related_memory") or [])
    if memory_references:
        reference = memory_references[0]
        suggested_text = reference.get("final_suggestion", "")
        if suggested_text:
            revision_text = f"{revision_text} 历史反馈参考：{suggested_text}"

    return {
        "revision_suggestion": revision_text,
        "preferred_position": preferred_position,
        "basis_rule_ids": list(finding.get("matched_rule_ids") or []),
        "memory_references": memory_references,
    }


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
