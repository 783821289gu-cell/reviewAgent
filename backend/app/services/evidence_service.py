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
    clause = clauses_by_id.get(clause_id)
    source_location = {}

    failure_reason = ""
    if clause_id not in clauses_by_id:
        failure_reason = "clause_id not found"
    elif not evidence_text:
        failure_reason = "evidence_text is empty"
    elif evidence_text not in str(clause.get("text", "")):
        failure_reason = "evidence_text not found in clause text"
    elif not risk_reason:
        failure_reason = "risk_reason is empty"
    elif not _reason_related_to_evidence(risk_reason, evidence_text):
        failure_reason = "risk_reason is not related to evidence_text"
    elif str(finding.get("risk_type", "")) != str(matched_rule.get("risk_type", "")):
        failure_reason = "risk_type does not match matched rule"
    elif str(matched_rule.get("rule_id", "")) not in [str(item) for item in finding.get("matched_rule_ids", [])]:
        failure_reason = "matched rule id missing from finding"

    if not failure_reason:
        source_location = evidence_location_for_text(clause, evidence_text)
        clause_location = clause.get("source_location") or {}
        if clause_location.get("pdf_blocks") and not source_location:
            failure_reason = "evidence location not found in PDF blocks"

    return EvidenceResult(
        risk_id=risk_id,
        clause_id=clause_id,
        evidence_text=evidence_text,
        is_valid=not failure_reason,
        failure_reason=failure_reason,
        verified_clause_id=clause_id if not failure_reason else "",
        source_location=source_location if not failure_reason else {},
    ).to_dict()


def evidence_location_for_text(clause: dict, evidence_text: str) -> dict:
    clause_location = clause.get("source_location") or {}
    pdf_blocks = clause_location.get("pdf_blocks") or []
    if not pdf_blocks:
        return {}

    block_texts = [str(block.get("text", "")) for block in pdf_blocks]
    combined_text = "\n".join(block_texts)
    clause_text = str(clause.get("text", ""))
    evidence_offset = clause_text.find(evidence_text)
    clause_offset = combined_text.find(clause_text)
    if evidence_offset >= 0 and clause_offset >= 0:
        evidence_start = clause_offset + evidence_offset
    else:
        evidence_start = combined_text.find(evidence_text)
    if evidence_start < 0:
        return {}
    evidence_end = evidence_start + len(evidence_text)

    located_blocks = []
    cursor = 0
    for block in pdf_blocks:
        block_text = str(block.get("text", ""))
        block_start = cursor
        block_end = block_start + len(block_text)
        if evidence_start < block_end and evidence_end > block_start:
            located_blocks.append(
                {
                    "block_id": str(block.get("block_id", "")),
                    "page_number": block.get("page_number"),
                    "bbox": list(block.get("bbox") or []),
                    "evidence_start": max(evidence_start, block_start) - block_start,
                    "evidence_end": min(evidence_end, block_end) - block_start,
                }
            )
        cursor = block_end + 1

    if not located_blocks:
        return {}
    return {
        "start_page": located_blocks[0]["page_number"],
        "end_page": located_blocks[-1]["page_number"],
        "pdf_blocks": located_blocks,
    }


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
