import json

from models.retrieval import ReviewContext


DEFAULT_CONTEXT_MAX_CHARS = 6000

OUTPUT_CONSTRAINTS = {
    "output_format": "risk_finding_json",
    "no_formal_risk_without_playbook_rule": True,
    "risk_analysis_not_executed_in_task_5": True,
}

EVIDENCE_CONSTRAINTS = {
    "must_bind_to_original_clause": True,
    "must_quote_current_contract_text": True,
    "related_clauses_are_supporting_context_only": True,
    "memory_cannot_override_playbook_or_original_text": True,
}


def build_review_context(
    contract_type: str,
    review_position: str,
    current_clause: dict,
    matched_rule: dict,
    related_clauses: list[dict],
    related_memory: list[dict],
    max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
) -> dict:
    retained_related_clauses = list(related_clauses)
    retained_memory = list(related_memory)
    reduction_trace: list[str] = []

    context = _make_context(
        contract_type,
        review_position,
        current_clause,
        matched_rule,
        retained_related_clauses,
        retained_memory,
        reduction_trace,
    )

    while _context_size(context) > max_chars and retained_memory:
        retained_memory.pop()
        reduction_trace.append("reduced_memory")
        context = _make_context(
            contract_type,
            review_position,
            current_clause,
            matched_rule,
            retained_related_clauses,
            retained_memory,
            reduction_trace,
        )

    while _context_size(context) > max_chars and retained_related_clauses:
        retained_related_clauses.pop()
        reduction_trace.append("reduced_low_rank_related_clause")
        context = _make_context(
            contract_type,
            review_position,
            current_clause,
            matched_rule,
            retained_related_clauses,
            retained_memory,
            reduction_trace,
        )

    return context.to_dict()


def _make_context(
    contract_type: str,
    review_position: str,
    current_clause: dict,
    matched_rule: dict,
    related_clauses: list[dict],
    related_memory: list[dict],
    reduction_trace: list[str],
) -> ReviewContext:
    clause_id = str(current_clause.get("clause_id", ""))
    rule_id = str(matched_rule.get("rule_id", ""))
    resolved_rule = _validated_position_rule(matched_rule, review_position)
    return ReviewContext(
        context_id=f"{clause_id}:{rule_id}",
        contract_type=contract_type,
        review_position=review_position,
        current_clause=current_clause,
        clause_type=str(current_clause.get("clause_type", "")),
        key_fields=current_clause.get("key_fields") or {},
        matched_rule=resolved_rule,
        related_clauses=related_clauses,
        related_memory=related_memory,
        output_constraints=dict(OUTPUT_CONSTRAINTS),
        evidence_constraints=dict(EVIDENCE_CONSTRAINTS),
        reduction_trace=list(reduction_trace),
        formal_risk_generated=False,
    )


def _context_size(context: ReviewContext) -> int:
    return len(json.dumps(context.to_dict(), ensure_ascii=False))


def _validated_position_rule(matched_rule: dict, review_position: str) -> dict:
    resolved_rule = dict(matched_rule)
    if str(resolved_rule.get("review_position", "")) != review_position:
        raise ValueError("matched rule review_position does not match review context")

    positions = resolved_rule.get("positions")
    if not isinstance(positions, dict):
        raise ValueError("matched rule positions are required")
    configured_position = positions.get(review_position)
    if not isinstance(configured_position, dict):
        raise ValueError(f"matched rule missing position config: {review_position}")

    position_config = resolved_rule.get("position_config")
    if not isinstance(position_config, dict):
        raise ValueError("matched rule position_config is required")
    for field_name in ("severity_default", "risk_focus", "revision_template"):
        value = str(position_config.get(field_name, "")).strip()
        if not value:
            raise ValueError(f"matched rule position_config.{field_name} is required")
        if str(configured_position.get(field_name, "")).strip() != value:
            raise ValueError(
                f"matched rule position_config.{field_name} does not match positions"
            )
        if str(resolved_rule.get(field_name, "")).strip() != value:
            raise ValueError(f"matched rule resolved {field_name} does not match position_config")
    return resolved_rule
