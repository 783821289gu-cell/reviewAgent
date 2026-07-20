from providers.embedding_provider import tokenize_text


KEY_CLAUSE_TYPES = {
    "定义",
    "例外",
    "期限",
    "保密义务",
    "使用限制",
    "允许披露",
    "法定披露",
    "返还销毁",
    "数据处置",
    "违约责任",
    "责任限制",
}

RISK_TYPE_CLAUSE_TYPE_SCORES = {
    "保密信息范围过宽": {"定义": 1.0, "例外": 0.8, "保密义务": 0.6},
    "缺少保密信息例外": {"例外": 1.0, "定义": 0.7, "法定披露": 0.6},
    "保密期限不合理": {"期限": 1.0, "返还销毁": 0.6, "数据处置": 0.6},
    "使用目的或使用限制不清": {"使用限制": 1.0, "保密义务": 0.6, "定义": 0.5},
    "允许披露对象过宽": {"允许披露": 1.0, "法定披露": 1.0, "保密义务": 0.6},
    "返还或销毁义务不明确": {"返还销毁": 1.0, "数据处置": 1.0, "期限": 0.4},
    "责任无限或责任边界不清": {"责任限制": 1.0, "违约责任": 0.8, "争议解决": 0.3},
    "违约责任或违约金明显不合理": {"违约责任": 1.0, "责任限制": 0.7, "争议解决": 0.3},
}

RISK_TYPE_PLAYBOOK_KEYWORDS = {
    "保密信息范围过宽": {
        "保密信息",
        "商业信息",
        "技术资料",
        "定义",
        "范围",
        "可识别",
        "confidential information",
    },
    "缺少保密信息例外": {
        "例外",
        "公开",
        "已知",
        "第三方",
        "独立开发",
        "依法",
        "public domain",
        "independently developed",
    },
    "保密期限不合理": {
        "保密期限",
        "持续期间",
        "永久",
        "无限期",
        "终止后",
        "年",
        "perpetual",
        "indefinite",
    },
    "使用目的或使用限制不清": {
        "使用目的",
        "约定用途",
        "仅限",
        "不得挪作他用",
        "挪作他用",
        "超范围使用",
        "use only",
        "purpose",
    },
    "允许披露对象过宽": {
        "必要知悉",
        "员工",
        "顾问",
        "关联方",
        "代表",
        "法定披露",
        "行政机关",
        "监管命令",
        "指令",
        "事先通知",
        "信息所有人",
        "need to know",
    },
    "返还或销毁义务不明确": {
        "返还",
        "归还",
        "销毁",
        "删除",
        "清除",
        "数据处置",
        "全部副本",
        "备份",
        "destroy",
        "delete",
    },
    "责任无限或责任边界不清": {
        "责任上限",
        "累计责任",
        "间接损失",
        "全部损失",
        "无限责任",
        "赔偿范围",
        "liability cap",
        "indirect damages",
    },
    "违约责任或违约金明显不合理": {
        "违约金",
        "罚金",
        "每日",
        "比例",
        "上限",
        "实际损失",
        "liquidated damages",
        "penalty",
    },
}

RISK_TYPE_KEY_FIELDS = {
    "保密期限不合理": {"confidentiality_period"},
    "使用目的或使用限制不清": {"use_purpose"},
    "允许披露对象过宽": {"permitted_disclosure_targets"},
    "责任无限或责任边界不清": {"liability_scope"},
    "违约责任或违约金明显不合理": {"breach_liability"},
}

_QUERY_STOP_TERMS = {
    "协议",
    "条款",
    "双方",
    "相关",
    "是否",
    "检查",
    "应当",
    "可以",
    "不得",
    "要求",
    "the",
    "and",
    "for",
    "with",
}


def build_keyword_query(
    current_clause: dict,
    risk_type: str,
    playbook_check_point: str,
) -> dict:
    query_text = " ".join(
        [
            str(current_clause.get("text", "")),
            str(current_clause.get("clause_type", "")),
            str(risk_type),
            str(playbook_check_point),
            _key_fields_text(current_clause.get("key_fields") or {}),
        ]
    )
    return {
        "query_terms": sorted(_keyword_terms(query_text)),
        "playbook_terms": sorted(RISK_TYPE_PLAYBOOK_KEYWORDS.get(risk_type, set())),
        "check_point_terms": sorted(_keyword_terms(playbook_check_point)),
    }


def keyword_candidate_factors(
    keyword_query: dict,
    candidate_clause: dict,
) -> dict:
    candidate_text = " ".join(
        [
            str(candidate_clause.get("text", "")),
            str(candidate_clause.get("clause_type", "")),
            _key_fields_text(candidate_clause.get("key_fields") or {}),
        ]
    )
    candidate_terms = _keyword_terms(candidate_text)
    query_terms = set(keyword_query.get("query_terms") or [])
    keyword_matches = sorted(query_terms & candidate_terms)
    keyword_overlap = min(1.0, len(keyword_matches) / 4)
    check_point_matches = sorted(
        set(keyword_query.get("check_point_terms") or []) & candidate_terms
    )
    check_point_overlap = min(1.0, len(check_point_matches) / 4)

    normalized_candidate = _normalized_text(candidate_text)
    playbook_matches = sorted(
        term
        for term in keyword_query.get("playbook_terms") or []
        if _normalized_text(term) in normalized_candidate
    )
    domain_applicability = min(1.0, len(playbook_matches) / 3)
    playbook_applicability = min(
        1.0,
        domain_applicability * 0.8 + check_point_overlap * 0.2,
    )
    return {
        "keyword_overlap": round(keyword_overlap, 4),
        "keyword_matches": keyword_matches,
        "playbook_check_point_overlap": round(check_point_overlap, 4),
        "playbook_check_point_matches": check_point_matches,
        "playbook_applicability": round(playbook_applicability, 4),
        "playbook_keyword_matches": playbook_matches,
        "keyword_score": round(
            keyword_overlap * 0.4 + playbook_applicability * 0.6,
            4,
        ),
    }


def rerank_candidates(
    candidates: list[dict],
    current_clause: dict,
    risk_type: str,
) -> list[dict]:
    current_clause_type = str(current_clause.get("clause_type", ""))
    for candidate in candidates:
        clause = candidate["clause"]
        clause_type = str(clause.get("clause_type", ""))
        field_match, field_matches = _field_match(current_clause, clause, risk_type)
        factors = {
            "clause_type_relatedness": _clause_type_relatedness(
                current_clause_type,
                clause_type,
                risk_type,
            ),
            "keyword_overlap": float(candidate["keyword_factors"]["keyword_overlap"]),
            "keyword_matches": list(candidate["keyword_factors"]["keyword_matches"]),
            "vector_similarity": round(float(candidate["vector_similarity"]), 4),
            "field_match": field_match,
            "field_matches": field_matches,
            "playbook_check_point_overlap": float(
                candidate["keyword_factors"]["playbook_check_point_overlap"]
            ),
            "playbook_check_point_matches": list(
                candidate["keyword_factors"]["playbook_check_point_matches"]
            ),
            "playbook_applicability": float(
                candidate["keyword_factors"]["playbook_applicability"]
            ),
            "playbook_keyword_matches": list(
                candidate["keyword_factors"]["playbook_keyword_matches"]
            ),
        }
        candidate["rerank_score"] = round(
            factors["vector_similarity"] * 0.15
            + factors["clause_type_relatedness"] * 0.2
            + factors["keyword_overlap"] * 0.2
            + factors["field_match"] * 0.1
            + factors["playbook_applicability"] * 0.35,
            4,
        )
        factors["final_score"] = candidate["rerank_score"]
        candidate["rerank_factors"] = factors

    return sorted(
        candidates,
        key=lambda item: (
            -item["rerank_score"],
            -item["vector_similarity"],
            str(item["clause"].get("clause_id", "")),
        ),
    )


def _clause_type_relatedness(
    current_clause_type: str,
    candidate_clause_type: str,
    risk_type: str,
) -> float:
    configured_score = RISK_TYPE_CLAUSE_TYPE_SCORES.get(risk_type, {}).get(
        candidate_clause_type
    )
    if configured_score is not None:
        return configured_score
    if candidate_clause_type == current_clause_type:
        return 0.7
    if candidate_clause_type in KEY_CLAUSE_TYPES:
        return 0.3
    return 0.0


def _field_match(
    current_clause: dict,
    candidate_clause: dict,
    risk_type: str,
) -> tuple[float, list[str]]:
    current_fields = current_clause.get("key_fields") or {}
    candidate_fields = candidate_clause.get("key_fields") or {}
    matched_fields = {
        field_name
        for field_name, value in current_fields.items()
        if _has_value(value) and _has_value(candidate_fields.get(field_name))
    }
    matched_fields.update(
        field_name
        for field_name in RISK_TYPE_KEY_FIELDS.get(risk_type, set())
        if _has_value(candidate_fields.get(field_name))
    )
    return (1.0 if matched_fields else 0.0, sorted(matched_fields))


def _keyword_terms(text: str) -> set[str]:
    return {
        token
        for token in tokenize_text(text)
        if len(token) >= 2 and token not in _QUERY_STOP_TERMS
    }


def _key_fields_text(key_fields: dict) -> str:
    parts = []
    for field_name, value in key_fields.items():
        if isinstance(value, list):
            values = [str(item).strip() for item in value if str(item).strip()]
        else:
            values = [str(value).strip()] if str(value or "").strip() else []
        if values:
            parts.extend([str(field_name), *values])
    return " ".join(parts)


def _normalized_text(value: str) -> str:
    return "".join(str(value or "").lower().split())


def _has_value(value) -> bool:
    if isinstance(value, list):
        return any(str(item).strip() for item in value)
    return bool(str(value or "").strip())
