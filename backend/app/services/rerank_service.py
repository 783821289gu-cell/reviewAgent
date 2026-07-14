KEY_CLAUSE_TYPES = {
    "定义",
    "例外",
    "期限",
    "保密义务",
    "使用限制",
    "允许披露",
    "返还销毁",
    "违约责任",
}

RISK_TYPE_RELATED_CLAUSE_TYPES = {
    "保密信息范围过宽": {"定义", "例外", "保密义务"},
    "缺少保密信息例外": {"例外", "定义", "保密义务"},
    "保密期限不合理": {"期限", "定义", "返还销毁"},
    "使用目的或使用限制不清": {"使用限制", "定义", "允许披露"},
    "允许披露对象过宽": {"允许披露", "保密义务", "定义"},
    "返还或销毁义务不明确": {"返还销毁", "期限", "定义"},
    "责任无限或责任边界不清": {"违约责任", "争议解决"},
    "违约责任或违约金明显不合理": {"违约责任", "争议解决"},
}

RISK_TYPE_KEY_FIELDS = {
    "保密期限不合理": {"confidentiality_period"},
    "使用目的或使用限制不清": {"use_purpose"},
    "允许披露对象过宽": {"permitted_disclosure_targets"},
    "责任无限或责任边界不清": {"liability_scope"},
    "违约责任或违约金明显不合理": {"breach_liability"},
}


def rerank_candidates(candidates: list[dict], current_clause: dict, risk_type: str) -> list[dict]:
    current_clause_type = str(current_clause.get("clause_type", ""))
    for candidate in candidates:
        clause = candidate["clause"]
        clause_type = str(clause.get("clause_type", ""))
        factors = {
            "vector_similarity": round(float(candidate["vector_similarity"]), 4),
            "clause_type_relatedness": _clause_type_relatedness(
                current_clause_type,
                clause_type,
                risk_type,
            ),
            "key_clause_weight": 1.0 if clause_type in KEY_CLAUSE_TYPES else 0.0,
            "risk_type_relation": 1.0 if clause_type in RISK_TYPE_RELATED_CLAUSE_TYPES.get(risk_type, set()) else 0.0,
            "key_field_hit": _key_field_hit(current_clause, clause, risk_type),
        }
        candidate["rerank_factors"] = factors
        candidate["rerank_score"] = round(
            factors["vector_similarity"] * 0.45
            + factors["clause_type_relatedness"] * 0.2
            + factors["key_clause_weight"] * 0.15
            + factors["risk_type_relation"] * 0.15
            + factors["key_field_hit"] * 0.05,
            4,
        )

    return sorted(
        candidates,
        key=lambda item: (item["rerank_score"], item["vector_similarity"], item["clause"].get("clause_id", "")),
        reverse=True,
    )


def _clause_type_relatedness(current_clause_type: str, candidate_clause_type: str, risk_type: str) -> float:
    if candidate_clause_type == current_clause_type:
        return 0.7
    if candidate_clause_type in RISK_TYPE_RELATED_CLAUSE_TYPES.get(risk_type, set()):
        return 1.0
    if candidate_clause_type in KEY_CLAUSE_TYPES:
        return 0.4
    return 0.0


def _key_field_hit(current_clause: dict, candidate_clause: dict, risk_type: str) -> float:
    current_fields = current_clause.get("key_fields") or {}
    candidate_fields = candidate_clause.get("key_fields") or {}

    for field_name, value in current_fields.items():
        if _has_value(value) and _has_value(candidate_fields.get(field_name)):
            return 1.0

    for field_name in RISK_TYPE_KEY_FIELDS.get(risk_type, set()):
        if _has_value(candidate_fields.get(field_name)):
            return 1.0
    return 0.0


def _has_value(value) -> bool:
    if isinstance(value, list):
        return any(str(item).strip() for item in value)
    return bool(str(value or "").strip())
