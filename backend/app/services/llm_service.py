from config import settings


BROAD_DEFINITION_TERMS = ("任何", "全部", "所有", "一切", "商业信息", "技术资料", "合作资料", "business information")
EXCEPTION_TERMS = ("公开", "已知", "第三方", "独立开发", "依法", "法律", "regulation", "required by law")
TERM_RISK_TERMS = ("永久", "无限期", "perpetual", "indefinite")
PURPOSE_TERMS = ("目的", "仅用于", "仅可", "purpose", "use only")
DISCLOSURE_RISK_TERMS = ("任何", "所有", "关联方", "affiliate", "representatives")
DISCLOSURE_SAFETY_TERMS = ("必要", "保密义务", "need to know", "confidentiality obligation")
RETURN_TERMS = ("返还", "销毁", "删除", "return", "destroy", "delete")
LIABILITY_RISK_TERMS = ("不限", "无限", "全部损失", "间接损失", "unlimited", "indirect damages")
PENALTY_TERMS = ("违约金", "penalty", "liquidated damages")


def generate_structured_risk(review_context: dict, attempt: int = 1) -> dict:
    if settings.llm_mode != "local_structured":
        raise ValueError(f"unsupported REVIEW_AGENT_LLM_MODE: {settings.llm_mode}")

    debug_outputs = review_context.get("debug_llm_outputs")
    if isinstance(debug_outputs, list) and attempt <= len(debug_outputs):
        return dict(debug_outputs[attempt - 1])

    clause = review_context.get("current_clause") or {}
    rule = review_context.get("matched_rule") or {}
    risk_type = str(rule.get("risk_type", ""))
    clause_text = str(clause.get("text", ""))
    evidence_text = _detect_evidence(risk_type, clause_text)
    review_status = _review_status(rule, evidence_text)

    return {
        "risk_type": risk_type,
        "severity": str(rule.get("severity_default", "中")),
        "confidence": _confidence(review_status, evidence_text),
        "risk_reason": _risk_reason(rule, evidence_text),
        "clause_id": str(clause.get("clause_id", "")),
        "evidence_text": evidence_text,
        "matched_rule_ids": [str(rule.get("rule_id", ""))],
        "revision_suggestion": str(rule.get("revision_template", "")),
        "review_status": review_status,
    }


def _detect_evidence(risk_type: str, clause_text: str) -> str:
    text = clause_text.lower()
    if risk_type == "保密信息范围过宽" and _contains_any(text, BROAD_DEFINITION_TERMS):
        return _sentence_with_term(clause_text, BROAD_DEFINITION_TERMS)
    if risk_type == "缺少保密信息例外" and not _contains_any(text, EXCEPTION_TERMS):
        return _short_text(clause_text)
    if risk_type == "保密期限不合理" and _contains_any(text, TERM_RISK_TERMS):
        return _sentence_with_term(clause_text, TERM_RISK_TERMS)
    if risk_type == "使用目的或使用限制不清" and not _contains_any(text, PURPOSE_TERMS):
        return _short_text(clause_text)
    if risk_type == "允许披露对象过宽" and _contains_any(text, DISCLOSURE_RISK_TERMS):
        if not _contains_any(text, DISCLOSURE_SAFETY_TERMS):
            return _sentence_with_term(clause_text, DISCLOSURE_RISK_TERMS)
    if risk_type == "返还或销毁义务不明确" and not _contains_any(text, RETURN_TERMS):
        return _short_text(clause_text)
    if risk_type == "责任无限或责任边界不清" and _contains_any(text, LIABILITY_RISK_TERMS):
        return _sentence_with_term(clause_text, LIABILITY_RISK_TERMS)
    if risk_type == "违约责任或违约金明显不合理" and _contains_any(text, PENALTY_TERMS):
        return _sentence_with_term(clause_text, PENALTY_TERMS)
    return ""


def _review_status(rule: dict, evidence_text: str) -> str:
    if not evidence_text:
        return "NO_RISK"
    if rule.get("severity_default") == "高":
        return "NEED_MANUAL_REVIEW"
    return "CONFIRMED_RISK"


def _confidence(review_status: str, evidence_text: str) -> float:
    if review_status == "NO_RISK":
        return 0.72
    return 0.86 if len(evidence_text) >= 6 else 0.62


def _risk_reason(rule: dict, evidence_text: str) -> str:
    if not evidence_text:
        return f"未在当前条款中发现触发“{rule.get('risk_type', '')}”的明确证据。"
    return f"证据文本“{evidence_text}”命中 Playbook 检查点：{rule.get('check_point', '')}"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)


def _sentence_with_term(clause_text: str, terms: tuple[str, ...]) -> str:
    separators = ("。", "；", ";", ".")
    sentences = [clause_text]
    for separator in separators:
        if separator in clause_text:
            sentences = [item.strip() for item in clause_text.split(separator) if item.strip()]
            break
    lower_terms = tuple(term.lower() for term in terms)
    for sentence in sentences:
        if _contains_any(sentence.lower(), lower_terms):
            return _short_text(sentence)
    return _short_text(clause_text)


def _short_text(text: str) -> str:
    return str(text or "").strip()[:180]
