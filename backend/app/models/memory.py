from dataclasses import asdict, dataclass
from hashlib import sha256
import json


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    memory_type: str
    contract_type: str
    clause_type: str
    risk_type: str
    review_position: str
    user_action: str
    original_severity: str
    final_severity: str
    original_suggestion: str
    final_suggestion: str
    ignore_reason: str
    source_finding_id: str
    source_clause_id: str
    include_in_report: bool
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SemanticPreference:
    preference_id: str
    memory_type: str
    contract_type: str
    clause_type: str
    risk_type: str
    review_position: str
    support_count: int
    opposition_count: int
    source_memory_ids: list[str]
    variants: list[dict]
    conflict_status: str
    base_confidence: float
    confidence: float
    lifecycle_status: str
    last_feedback_at: str
    last_used_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def memory_item_from_row(row) -> MemoryItem:
    return MemoryItem(
        memory_id=str(row["memory_id"]),
        memory_type=str(row["memory_type"]),
        contract_type=str(row["contract_type"]),
        clause_type=str(row["clause_type"]),
        risk_type=str(row["risk_type"]),
        review_position=str(row["review_position"]),
        user_action=str(row["user_action"]),
        original_severity=str(row["original_severity"]),
        final_severity=str(row["final_severity"]),
        original_suggestion=str(row["original_suggestion"]),
        final_suggestion=str(row["final_suggestion"]),
        ignore_reason=str(row["ignore_reason"]),
        source_finding_id=str(row["source_finding_id"]),
        source_clause_id=str(row["source_clause_id"]),
        include_in_report=bool(row["include_in_report"]),
        created_at=str(row["created_at"]),
    )


def semantic_preference_from_row(row) -> SemanticPreference:
    return SemanticPreference(
        preference_id=str(row["preference_id"]),
        memory_type="semantic_preference",
        contract_type=str(row["contract_type"]),
        clause_type=str(row["clause_type"]),
        risk_type=str(row["risk_type"]),
        review_position=str(row["review_position"]),
        support_count=int(row["support_count"]),
        opposition_count=int(row["opposition_count"]),
        source_memory_ids=list(json.loads(str(row["source_memory_ids_json"]))),
        variants=list(json.loads(str(row["variants_json"]))),
        conflict_status=str(row["conflict_status"]),
        base_confidence=float(row["base_confidence"]),
        confidence=float(row["confidence"]),
        lifecycle_status=str(row["lifecycle_status"]),
        last_feedback_at=str(row["last_feedback_at"]),
        last_used_at=str(row["last_used_at"]),
        updated_at=str(row["updated_at"]),
    )


def build_semantic_preference(
    memory_items: list[dict],
    *,
    last_used_at: str = "",
) -> SemanticPreference:
    if not memory_items:
        raise ValueError("memory_items must not be empty")

    ordered_items = sorted(
        (dict(item) for item in memory_items),
        key=lambda item: (str(item.get("created_at", "")), str(item.get("memory_id", ""))),
    )
    first = ordered_items[0]
    group_fields = ("contract_type", "clause_type", "risk_type", "review_position")
    group = tuple(str(first.get(field, "")) for field in group_fields)
    if any(tuple(str(item.get(field, "")) for field in group_fields) != group for item in ordered_items):
        raise ValueError("all memory items must belong to the same semantic preference group")

    source_memory_ids = [str(item.get("memory_id", "")) for item in ordered_items]
    if any(not memory_id for memory_id in source_memory_ids):
        raise ValueError("memory_id is required for semantic preference aggregation")
    if len(source_memory_ids) != len(set(source_memory_ids)):
        raise ValueError("memory_id values must be unique within a semantic preference")

    variant_index: dict[tuple[str, str, str], dict] = {}
    support_count = 0
    opposition_count = 0
    for item in ordered_items:
        action = str(item.get("user_action", ""))
        is_opposition = action == "ignore"
        if is_opposition:
            opposition_count += 1
            key = ("opposition", "", "")
        else:
            support_count += 1
            key = (
                "support",
                str(item.get("final_severity", "")),
                str(item.get("final_suggestion", "")),
            )
        variant = variant_index.setdefault(
            key,
            {
                "stance": key[0],
                "final_severity": key[1],
                "final_suggestion": key[2],
                "count": 0,
                "source_memory_ids": [],
            },
        )
        variant["count"] += 1
        variant["source_memory_ids"].append(str(item["memory_id"]))

    variants = sorted(
        variant_index.values(),
        key=lambda item: (
            item["stance"] != "support",
            -int(item["count"]),
            str(item["final_severity"]),
            str(item["final_suggestion"]),
        ),
    )
    support_variants = [item for item in variants if item["stance"] == "support"]
    conflicted = (support_count > 0 and opposition_count > 0) or len(support_variants) > 1
    base_confidence = _preference_confidence(
        support_count,
        opposition_count,
        support_variants,
    )
    latest_feedback_at = str(ordered_items[-1].get("created_at", ""))
    digest = sha256("\x1f".join(group).encode("utf-8")).hexdigest()[:16]
    return SemanticPreference(
        preference_id=f"PREF-{digest}",
        memory_type="semantic_preference",
        contract_type=group[0],
        clause_type=group[1],
        risk_type=group[2],
        review_position=group[3],
        support_count=support_count,
        opposition_count=opposition_count,
        source_memory_ids=source_memory_ids,
        variants=variants,
        conflict_status="conflicted" if conflicted else "aligned",
        base_confidence=base_confidence,
        confidence=base_confidence,
        lifecycle_status="active",
        last_feedback_at=latest_feedback_at,
        last_used_at=last_used_at,
        updated_at=latest_feedback_at,
    )


def _preference_confidence(
    support_count: int,
    opposition_count: int,
    support_variants: list[dict],
) -> float:
    if support_count <= 0:
        return 0.0
    repeated_support = min(0.4, 0.1 * max(0, support_count - 1))
    confidence = 0.55 + repeated_support
    confidence *= support_count / (support_count + opposition_count)
    if len(support_variants) > 1:
        confidence *= max(int(item["count"]) for item in support_variants) / support_count
    return round(min(0.95, confidence), 4)
