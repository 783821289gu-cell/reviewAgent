import json


def build_memory_query_text(
    contract_type: str,
    clause: dict,
    risk_type: str,
    review_position: str,
) -> str:
    key_fields = clause.get("key_fields") or {}
    return " ".join(
        part
        for part in (
            contract_type,
            review_position,
            risk_type,
            str(clause.get("clause_type", "")),
            str(clause.get("title", "")),
            str(clause.get("text", "")),
            json.dumps(key_fields, ensure_ascii=False, sort_keys=True),
        )
        if part
    )


def build_preference_embedding_text(preference: dict) -> str:
    variants = []
    for variant in preference.get("variants") or []:
        variants.extend(
            [
                str(variant.get("stance", "")),
                str(variant.get("final_severity", "")),
                str(variant.get("final_suggestion", "")),
            ]
        )
    return " ".join(
        part
        for part in (
            str(preference.get("contract_type", "")),
            str(preference.get("review_position", "")),
            str(preference.get("clause_type", "")),
            str(preference.get("risk_type", "")),
            str(preference.get("conflict_status", "")),
            *variants,
        )
        if part
    )
