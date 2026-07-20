from datetime import datetime, timedelta, timezone
from uuid import uuid4

from config import settings
from db.repositories.memory_repository import MemoryRepository
from models.feedback import build_human_feedback
from models.memory import (
    MemoryItem,
    build_semantic_preference,
    semantic_preference_from_row,
)


DEFAULT_MEMORY_STALE_AFTER_DAYS = 365
EXPIRED_CONFIDENCE_FACTOR = 0.5


def retrieve_memory(tool_input: dict) -> list[dict]:
    contract_type = str(tool_input.get("contract_type", "")).strip()
    clause = tool_input.get("clause")
    risk_type = str(tool_input.get("risk_type", "")).strip()
    review_position = str(tool_input.get("review_position", "")).strip()
    memory_items = tool_input.get("memory_items")
    limit = int(tool_input.get("limit", 3))
    stale_after_days = int(
        tool_input.get("stale_after_days", DEFAULT_MEMORY_STALE_AFTER_DAYS)
    )
    now = _parse_timestamp(
        str(tool_input.get("now") or datetime.now(timezone.utc).isoformat())
    )

    if not contract_type:
        raise ValueError("contract_type is required")
    if not isinstance(clause, dict):
        raise ValueError("clause must be a dict")
    if not risk_type:
        raise ValueError("risk_type is required")
    if not review_position:
        raise ValueError("review_position is required")
    if stale_after_days <= 0:
        raise ValueError("stale_after_days must be a positive integer")
    if limit <= 0:
        return []

    clause_type = str(clause.get("clause_type", ""))
    if isinstance(memory_items, list) and memory_items:
        return _aggregate_provided_memory_items(
            contract_type,
            clause_type,
            risk_type,
            review_position,
            memory_items,
            limit,
            now,
            stale_after_days,
        )
    if memory_items not in (None, []):
        raise ValueError("memory_items must be a list")

    return _query_sqlite_preferences(
        contract_type=contract_type,
        clause_type=clause_type,
        risk_type=risk_type,
        review_position=review_position,
        limit=limit,
        db_path=tool_input.get("db_path"),
        now=now,
        stale_after_days=stale_after_days,
    )


def write_memory(tool_input: dict) -> dict:
    raw_feedback = tool_input.get("human_feedback")
    if not isinstance(raw_feedback, dict):
        raise ValueError("human_feedback must be a dict")

    feedback = build_human_feedback(raw_feedback)
    item = MemoryItem(
        memory_id=str(raw_feedback.get("memory_id") or f"MEM-{uuid4().hex[:12]}"),
        memory_type="human_feedback",
        contract_type=feedback.contract_type,
        clause_type=feedback.clause_type,
        risk_type=feedback.risk_type,
        review_position=feedback.review_position,
        user_action=feedback.user_action,
        original_severity=feedback.original_severity,
        final_severity=feedback.final_severity,
        original_suggestion=feedback.original_suggestion,
        final_suggestion=feedback.final_suggestion,
        ignore_reason=feedback.ignore_reason,
        source_finding_id=feedback.source_finding_id,
        source_clause_id=feedback.source_clause_id,
        include_in_report=feedback.include_in_report,
        created_at=_parse_timestamp(feedback.created_at).isoformat(),
    )

    repository = MemoryRepository(str(tool_input.get("db_path") or settings.memory_db_path))
    return repository.save(
        item.to_dict(),
        idempotency_key=str(tool_input.get("idempotency_key") or "") or None,
    )


def evaluate_memory_comparison(cases: list) -> dict:
    results = []
    for raw_case in cases:
        case = raw_case.model_dump(mode="json") if hasattr(raw_case, "model_dump") else dict(raw_case)
        episodes = []
        for episode in case["episodes"]:
            payload = dict(episode)
            payload.update(
                {
                    "contract_type": case["contract_type"],
                    "clause_type": case["clause_type"],
                    "risk_type": case["risk_type"],
                    "review_position": case["review_position"],
                }
            )
            episodes.append(payload)
        preference = build_semantic_preference(episodes).to_dict()
        memory = _preference_payload(
            preference,
            source="annotation_memory",
            match_score=8,
            now=_parse_timestamp(case["evaluation_time"]),
            stale_after_days=int(case["stale_after_days"]),
        )
        baseline_suggestion = str(case["baseline_suggestion"])
        expected_suggestion = str(case["expected_suggestion"])
        with_memory_suggestion = (
            str(memory["final_suggestion"])
            if memory["can_influence_suggestion"]
            else baseline_suggestion
        )
        results.append(
            {
                "case_id": str(case["case_id"]),
                "without_memory_consistent": baseline_suggestion == expected_suggestion,
                "with_memory_consistent": with_memory_suggestion == expected_suggestion,
                "suggestion_affected": with_memory_suggestion != baseline_suggestion,
                "preference_id": memory["memory_id"],
                "preference_status": memory["conflict_status"],
                "lifecycle_status": memory["lifecycle_status"],
            }
        )

    without_count = sum(item["without_memory_consistent"] for item in results)
    with_count = sum(item["with_memory_consistent"] for item in results)
    delta = with_count - without_count
    return {
        "sample_count": len(results),
        "without_memory_consistent_count": without_count,
        "with_memory_consistent_count": with_count,
        "consistency_delta": delta,
        "improved": delta > 0,
        "conclusion": "improved" if delta > 0 else "not_improved",
        "results": results,
    }


def _aggregate_provided_memory_items(
    contract_type: str,
    clause_type: str,
    risk_type: str,
    review_position: str,
    memory_items: list,
    limit: int,
    now: datetime,
    stale_after_days: int,
) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for item in memory_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("contract_type", "")) != contract_type:
            continue
        if str(item.get("clause_type", "")) != clause_type:
            continue
        if str(item.get("risk_type", "")) != risk_type:
            continue
        position = str(item.get("review_position", ""))
        if position not in ("", review_position):
            continue
        normalized_item = dict(item)
        normalized_item["created_at"] = _parse_timestamp(
            str(item.get("created_at", ""))
        ).isoformat()
        groups.setdefault(position, []).append(normalized_item)

    payloads = []
    for position, items in groups.items():
        preference = build_semantic_preference(items).to_dict()
        payloads.append(
            _preference_payload(
                preference,
                source="provided_memory_items",
                match_score=8 if position == review_position else 7,
                now=now,
                stale_after_days=stale_after_days,
            )
        )
    return sorted(
        payloads,
        key=lambda item: (item["match_score"], item["confidence"], item["last_feedback_at"]),
        reverse=True,
    )[:limit]


def _query_sqlite_preferences(
    contract_type: str,
    clause_type: str,
    risk_type: str,
    review_position: str,
    limit: int,
    db_path: str | None,
    now: datetime,
    stale_after_days: int,
) -> list[dict]:
    repository = MemoryRepository(str(db_path or settings.memory_db_path))
    rows = repository.find_preferences(
        contract_type=contract_type,
        clause_type=clause_type,
        risk_type=risk_type,
        review_position=review_position,
    )
    payloads = []
    now_text = now.isoformat()
    for row in rows[:limit]:
        preference = semantic_preference_from_row(row).to_dict()
        match_score = 8 if preference["review_position"] == review_position else 7
        payload = _preference_payload(
            preference,
            source="sqlite_semantic_preference",
            match_score=match_score,
            now=now,
            stale_after_days=stale_after_days,
        )
        repository.update_preference_lifecycle(
            preference["preference_id"],
            lifecycle_status=payload["lifecycle_status"],
            confidence=float(payload["confidence"]),
            updated_at=now_text,
            last_used_at=now_text if payload["lifecycle_status"] == "active" else None,
        )
        if payload["lifecycle_status"] == "active":
            payload["last_used_at"] = now_text
        payloads.append(payload)
    return payloads


def _preference_payload(
    preference: dict,
    *,
    source: str,
    match_score: int,
    now: datetime,
    stale_after_days: int,
) -> dict:
    activity_times = [_parse_timestamp(str(preference["last_feedback_at"]))]
    if preference.get("last_used_at"):
        activity_times.append(_parse_timestamp(str(preference["last_used_at"])))
    last_activity = max(activity_times)
    expired = now - last_activity > timedelta(days=stale_after_days)
    support_variants = [
        item for item in preference["variants"] if item.get("stance") == "support"
    ]
    aligned = (
        preference["conflict_status"] == "aligned"
        and int(preference["support_count"]) > 0
        and int(preference["opposition_count"]) == 0
        and len(support_variants) == 1
    )
    suggested_variant = support_variants[0] if aligned else {}
    can_influence = bool(suggested_variant.get("final_suggestion")) and not expired
    if expired:
        influence_reason = "blocked_expired"
    elif preference["conflict_status"] == "conflicted":
        influence_reason = "blocked_conflict"
    elif int(preference["support_count"]) <= 0:
        influence_reason = "blocked_opposition_only"
    elif not suggested_variant.get("final_suggestion"):
        influence_reason = "blocked_missing_suggestion"
    else:
        influence_reason = "eligible_aligned_preference"

    confidence = float(preference["base_confidence"])
    if expired:
        confidence = round(confidence * EXPIRED_CONFIDENCE_FACTOR, 4)
    payload = dict(preference)
    payload.update(
        {
            "memory_id": preference["preference_id"],
            "memory_source": source,
            "match_score": match_score,
            "confidence": confidence,
            "lifecycle_status": "expired" if expired else "active",
            "expired": expired,
            "can_influence_suggestion": can_influence,
            "influence_reason": influence_reason,
            "final_severity": str(suggested_variant.get("final_severity", "")),
            "final_suggestion": str(suggested_variant.get("final_suggestion", "")),
            "memory_injection": {
                "source": source,
                "match_score": match_score,
                "suggestion_eligible": can_influence,
                "suggestion_affected": False,
                "trimmed": False,
                "trim_reason": "",
            },
        }
    )
    return payload


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
