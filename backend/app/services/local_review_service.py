import re

from models.log import StepLog

from services.context_builder import build_review_context
from services.log_service import invoke_tool
from tools.registry import tool_registry


MAX_SELECTION_LENGTH = 1000


def run_local_review(task_payload: dict, clause_id: str, selected_text: str) -> dict:
    if not isinstance(task_payload, dict):
        raise ValueError("审查任务无效，无法执行局部审查。")

    normalized_clause_id = str(clause_id or "").strip()
    normalized_text = _normalize_selected_text(selected_text)
    clauses = task_payload.get("clauses") or []
    clause = _find_clause(clauses, normalized_clause_id)

    if not _contains_selected_text(str(clause.get("text", "")), normalized_text):
        raise ValueError("框选文本不属于当前条款，请重新框选合同原文。")

    matched_rules = _matched_rules_for_clause(task_payload.get("matched_rules") or [], normalized_clause_id)
    related_formal_risks = _related_formal_risks(task_payload.get("risk_findings") or [], normalized_clause_id, normalized_text)
    selected_clause = dict(clause)
    selected_clause["text"] = normalized_text

    logs: list[StepLog] = []
    local_findings = []
    skipped_rules = []
    for rule in matched_rules:
        review_context = build_review_context(
            contract_type="NDA",
            review_position=str(task_payload.get("review_position", "")),
            current_clause=selected_clause,
            matched_rule=rule,
            related_clauses=[],
            related_memory=[],
        )
        finding = invoke_tool(
            str(task_payload.get("task_id", "")),
            tool_registry,
            "analyze_risk",
            {"review_context": review_context},
            logs,
            step_name="local_risk_analysis",
        )
        if finding["review_status"] == "NO_RISK":
            skipped_rules.append(rule["rule_id"])
            continue

        revision = invoke_tool(
            str(task_payload.get("task_id", "")),
            tool_registry,
            "generate_revision",
            {
                "finding": finding,
                "preferred_position": str(task_payload.get("review_position", "")),
            },
            logs,
            step_name="local_revision_generation",
        )
        finding = dict(finding)
        finding["revision_suggestion"] = revision["revision_suggestion"]
        evidence_result = invoke_tool(
            str(task_payload.get("task_id", "")),
            tool_registry,
            "verify_evidence",
            {
                "finding": finding,
                "clauses": [selected_clause],
                "matched_rule": rule,
            },
            logs,
            step_name="local_evidence_verification",
        )
        if not evidence_result["is_valid"]:
            skipped_rules.append(rule["rule_id"])
            continue

        finding["scope"] = "local_selection"
        finding["formal_risk"] = False
        finding["source_clause_id"] = normalized_clause_id
        local_findings.append(finding)

    return {
        "review_id": f"LOCAL-{task_payload.get('task_id', 'task')}-{normalized_clause_id}",
        "task_id": str(task_payload.get("task_id", "")),
        "scope": "local_selection",
        "clause_id": normalized_clause_id,
        "clause_type": str(clause.get("clause_type", "")),
        "selected_text": normalized_text,
        "matched_rules": matched_rules,
        "related_formal_risks": related_formal_risks,
        "local_findings": local_findings,
        "skipped_rule_ids": skipped_rules,
        "formal_risk_generated": False,
        "message": _result_message(local_findings, matched_rules),
        "logs": [log.to_dict() for log in logs],
    }


def _normalize_selected_text(selected_text: str) -> str:
    text = " ".join(str(selected_text or "").split())
    if not text:
        raise ValueError("请先在合同原文中框选需要局部审查的文本。")
    if len(text) > MAX_SELECTION_LENGTH:
        raise ValueError(f"框选文本过长，请控制在 {MAX_SELECTION_LENGTH} 字以内。")
    return text


def _contains_selected_text(clause_text: str, selected_text: str) -> bool:
    return selected_text in " ".join(str(clause_text or "").split())


def _find_clause(clauses: list[dict], clause_id: str) -> dict:
    if not clause_id:
        raise ValueError("缺少条款编号，无法执行局部审查。")
    for clause in clauses:
        if isinstance(clause, dict) and str(clause.get("clause_id", "")) == clause_id:
            return clause
    raise ValueError("未找到对应条款，无法执行局部审查。")


def _matched_rules_for_clause(rule_groups: list[dict], clause_id: str) -> list[dict]:
    for group in rule_groups:
        if not isinstance(group, dict) or str(group.get("clause_id", "")) != clause_id:
            continue
        rules = group.get("matched_rules") or []
        return [rule for rule in rules if isinstance(rule, dict)]
    return []


def _related_formal_risks(risks: list[dict], clause_id: str, selected_text: str) -> list[dict]:
    related = []
    for risk in risks:
        if not isinstance(risk, dict) or str(risk.get("clause_id", "")) != clause_id:
            continue
        evidence_text = str(risk.get("evidence_text", ""))
        if evidence_text and _text_overlaps(evidence_text, selected_text):
            related.append(risk)
    return related


def _text_overlaps(left: str, right: str) -> bool:
    normalized_left = re.sub(r"\W+", "", str(left or ""), flags=re.UNICODE).lower()
    normalized_right = re.sub(r"\W+", "", str(right or ""), flags=re.UNICODE).lower()
    if not normalized_left or not normalized_right:
        return False
    return normalized_left in normalized_right or normalized_right in normalized_left


def _result_message(local_findings: list[dict], matched_rules: list[dict]) -> str:
    if local_findings:
        return "局部审查完成，结果仅用于当前框选文本复核。"
    if matched_rules:
        return "局部审查完成，当前框选文本未形成局部风险结果。"
    return "当前条款未命中 Playbook 规则，不生成局部风险结果。"
