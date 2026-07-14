import re

from models.risk import EvidenceResult


def verify_evidence(tool_input: dict) -> dict:
    finding = tool_input.get("finding")
    clauses = tool_input.get("clauses")
    matched_rule = tool_input.get("matched_rule")

    if not isinstance(finding, dict):
        raise ValueError("finding must be a dict")
    if not isinstance(clauses, list):
        raise ValueError("clauses must be a list")
    if not isinstance(matched_rule, dict):
        raise ValueError("matched_rule must be a dict")

    risk_id = str(finding.get("risk_id", ""))
    clause_id = str(finding.get("clause_id", ""))
    evidence_text = str(finding.get("evidence_text", "")).strip()
    risk_reason = str(finding.get("risk_reason", "")).strip()
    clauses_by_id = {str(clause.get("clause_id", "")): clause for clause in clauses if isinstance(clause, dict)}

    failure_reason = ""
    if clause_id not in clauses_by_id:
        failure_reason = "clause_id not found"
    elif not evidence_text:
        failure_reason = "evidence_text is empty"
    elif evidence_text not in str(clauses_by_id[clause_id].get("text", "")):
        failure_reason = "evidence_text not found in clause text"
    elif not risk_reason:
        failure_reason = "risk_reason is empty"
    elif not _reason_related_to_evidence(risk_reason, evidence_text):
        failure_reason = "risk_reason is not related to evidence_text"
    elif str(finding.get("risk_type", "")) != str(matched_rule.get("risk_type", "")):
        failure_reason = "risk_type does not match matched rule"
    elif str(matched_rule.get("rule_id", "")) not in [str(item) for item in finding.get("matched_rule_ids", [])]:
        failure_reason = "matched rule id missing from finding"

    return EvidenceResult(
        risk_id=risk_id,
        clause_id=clause_id,
        evidence_text=evidence_text,
        is_valid=not failure_reason,
        failure_reason=failure_reason,
        verified_clause_id=clause_id if not failure_reason else "",
    ).to_dict()


def _reason_related_to_evidence(risk_reason: str, evidence_text: str) -> bool:
    reason_tokens = _tokens(risk_reason)
    evidence_tokens = _tokens(evidence_text)
    if not reason_tokens or not evidence_tokens:
        return False
    return len(reason_tokens & evidence_tokens) >= 2


def _tokens(text: str) -> set[str]:
    normalized = str(text or "").lower()
    tokens = set(re.findall(r"[a-z0-9]{3,}", normalized))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        tokens.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return tokens
