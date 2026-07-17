from config import settings
from models.retrieval import ReviewContext
from services.prompt_service import (
    PROMPT_VERSION,
    build_prompt_package,
    detect_prompt_injection,
    prompt_token_report,
)


DEFAULT_CONTEXT_MAX_TOKENS = settings.llm_context_budget_tokens

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


class ContextBudgetExceededError(ValueError):
    pass


def build_review_context(
    contract_type: str,
    review_position: str,
    current_clause: dict,
    matched_rule: dict,
    related_clauses: list[dict],
    related_memory: list[dict],
    max_tokens: int = DEFAULT_CONTEXT_MAX_TOKENS,
) -> dict:
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")

    retained_clause = dict(current_clause)
    retained_rule = dict(matched_rule)
    retained_related_clauses = list(related_clauses)
    retained_memory = list(related_memory)
    reduction_trace: list[str] = []
    protected_context_compacted = False
    prompt_security = detect_prompt_injection(
        current_clause,
        related_clauses,
        related_memory,
    )

    while True:
        context = _make_context(
            contract_type,
            review_position,
            retained_clause,
            retained_rule,
            retained_related_clauses,
            retained_memory,
            prompt_security,
            {},
            reduction_trace,
        )
        report = _review_context_token_report(context.to_dict())
        if report["prompt_tokens"] <= max_tokens:
            token_budget = {
                "tokenizer_model": report["tokenizer_model"],
                "tokenizer_revision": report["tokenizer_revision"],
                "tokenizer_sha256": report["tokenizer_sha256"],
                "category_tokens": report["category_tokens"],
                "final_prompt_tokens": report["prompt_tokens"],
                "max_prompt_tokens": max_tokens,
                "reduction_trace": list(reduction_trace),
                "status": "within_budget",
            }
            return _make_context(
                contract_type,
                review_position,
                retained_clause,
                retained_rule,
                retained_related_clauses,
                retained_memory,
                prompt_security,
                token_budget,
                reduction_trace,
            ).to_dict()

        if retained_memory:
            retained_memory.pop()
            reduction_trace.append("reduced_low_relevance_memory")
            continue
        if retained_related_clauses:
            retained_related_clauses.pop()
            reduction_trace.append("reduced_low_rank_related_clause")
            continue
        if not protected_context_compacted:
            retained_clause = _compact_current_clause(retained_clause)
            retained_rule = _compact_matched_rule(retained_rule, review_position)
            reduction_trace.append("compressed_protected_context_metadata")
            protected_context_compacted = True
            continue

        raise ContextBudgetExceededError(
            "CONTEXT_BUDGET_EXCEEDED: protected current clause, Playbook, Schema, and "
            "evidence constraint exceed the configured token budget"
        )


def _make_context(
    contract_type: str,
    review_position: str,
    current_clause: dict,
    matched_rule: dict,
    related_clauses: list[dict],
    related_memory: list[dict],
    prompt_security: dict,
    token_budget: dict,
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
        prompt_version=PROMPT_VERSION,
        prompt_security=dict(prompt_security),
        token_budget=dict(token_budget),
        reduction_trace=list(reduction_trace),
        formal_risk_generated=False,
    )


def _review_context_token_report(context: dict) -> dict:
    from services.llm_service import RISK_OUTPUT_SCHEMA

    prompt = build_prompt_package("analyze_risk", context, RISK_OUTPUT_SCHEMA)
    return prompt_token_report(prompt)


def _compact_current_clause(current_clause: dict) -> dict:
    return {
        field_name: current_clause[field_name]
        for field_name in (
            "clause_id",
            "title",
            "text",
            "clause_type",
            "key_fields",
            "source_location",
        )
        if field_name in current_clause
    }


def _compact_matched_rule(matched_rule: dict, review_position: str) -> dict:
    compacted = {
        field_name: matched_rule[field_name]
        for field_name in (
            "rule_id",
            "contract_type",
            "clause_type",
            "risk_type",
            "check_point",
            "review_position",
            "severity_default",
            "risk_focus",
            "revision_template",
            "position_config",
        )
        if field_name in matched_rule
    }
    positions = matched_rule.get("positions")
    if isinstance(positions, dict) and isinstance(positions.get(review_position), dict):
        compacted["positions"] = {review_position: dict(positions[review_position])}
    return compacted


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
