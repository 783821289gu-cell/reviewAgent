import json
from functools import lru_cache
from pathlib import Path

from models.playbook import PlaybookRule


PLAYBOOK_VERSION = "nda-v1"
PLAYBOOK_PATH = Path(__file__).resolve().parents[1] / "playbooks" / "nda.json"


def retrieve_playbook_rules(tool_input: dict) -> list[dict]:
    contract_type = str(tool_input.get("contract_type", "")).strip()
    clause_type = str(tool_input.get("clause_type", "")).strip()
    review_position = str(tool_input.get("review_position", "")).strip()
    key_fields = tool_input.get("key_fields") or {}

    if not contract_type:
        raise ValueError("contract_type is required")
    if not clause_type:
        raise ValueError("clause_type is required")
    if not review_position:
        raise ValueError("review_position is required")
    if not isinstance(key_fields, dict):
        raise ValueError("key_fields must be a dict")

    matched_rules = []
    for rule in load_playbook_rules():
        if not rule.matches(contract_type, clause_type, review_position):
            continue
        match_score, matched_key_fields = _score_rule(rule, key_fields)
        payload = rule.to_dict()
        payload["playbook_version"] = PLAYBOOK_VERSION
        payload["match_score"] = match_score
        payload["matched_key_fields"] = matched_key_fields
        matched_rules.append(payload)

    return sorted(
        matched_rules,
        key=lambda item: (-item["match_score"], item["rule_id"]),
    )


@lru_cache(maxsize=1)
def load_playbook_rules() -> tuple[PlaybookRule, ...]:
    with PLAYBOOK_PATH.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    rules = tuple(PlaybookRule.from_dict(item) for item in payload.get("rules", []))
    if payload.get("playbook_version") != PLAYBOOK_VERSION:
        raise ValueError("Playbook version mismatch")
    if len(rules) != 8:
        raise ValueError("NDA Playbook must contain 8 first-version rules")
    return rules


def _score_rule(rule: PlaybookRule, key_fields: dict) -> tuple[int, list[str]]:
    matched_key_fields = []
    for field_name in rule.key_field_triggers:
        if _normalize_values(key_fields.get(field_name)):
            matched_key_fields.append(field_name)

    return 1 + len(matched_key_fields), matched_key_fields


def _normalize_values(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
