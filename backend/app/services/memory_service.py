from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isfinite
from uuid import uuid4

from config import settings
from db.repositories.memory_repository import MemoryRepository
from models.feedback import build_human_feedback
from models.memory import (
    MemoryItem,
    build_semantic_preference,
    semantic_preference_from_row,
)
from providers.embedding_provider import (
    create_embedding_provider,
    embedding_call_records_from_tool_input,
)
from services.embedding_service import cosine_similarity, embed_texts


DEFAULT_MEMORY_STALE_AFTER_DAYS = 365
EXPIRED_CONFIDENCE_FACTOR = 0.5
MIN_MEMORY_VECTOR_SIMILARITY = 0.55


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
        clause=clause,
        risk_type=risk_type,
        review_position=review_position,
        limit=limit,
        db_path=tool_input.get("db_path"),
        now=now,
        stale_after_days=stale_after_days,
        embedding_cache=tool_input.get("embedding_cache"),
        embedding_call_records=embedding_call_records_from_tool_input(tool_input),
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
    clause: dict,
    risk_type: str,
    review_position: str,
    limit: int,
    db_path: str | None,
    now: datetime,
    stale_after_days: int,
    embedding_cache: dict | None,
    embedding_call_records: list | None,
) -> list[dict]:
    if embedding_cache is None:
        embedding_cache = {}
    if not isinstance(embedding_cache, dict):
        raise ValueError("embedding_cache must be a dict")

    repository = MemoryRepository(str(db_path or settings.memory_db_path))
    rows = repository.find_candidate_preferences(
        contract_type=contract_type,
        review_position=review_position,
    )
    if not rows:
        return []

    preferences = [semantic_preference_from_row(row).to_dict() for row in rows]
    ranked_preferences = _rank_preferences_by_embedding(
        repository=repository,
        preferences=preferences,
        contract_type=contract_type,
        clause=clause,
        risk_type=risk_type,
        review_position=review_position,
        embedding_cache=embedding_cache,
        embedding_call_records=embedding_call_records,
        now=now,
    )
    payloads = []
    now_text = now.isoformat()
    for ranked in ranked_preferences[:limit]:
        preference = ranked["preference"]
        payload = _preference_payload(
            preference,
            source="sqlite_vector_memory",
            match_score=ranked["match_score"],
            now=now,
            stale_after_days=stale_after_days,
        )
        exact_semantic_match = bool(
            ranked["retrieval_factors"]["exact_clause_type"]
            and ranked["retrieval_factors"]["exact_risk_type"]
        )
        retrieval_eligible = bool(
            exact_semantic_match
            or ranked["vector_similarity"] >= MIN_MEMORY_VECTOR_SIMILARITY
        )
        if not retrieval_eligible:
            payload["can_influence_suggestion"] = False
            payload["influence_reason"] = "blocked_low_vector_relevance"
        payload.update(
            {
                "vector_similarity": ranked["vector_similarity"],
                "embedding_mode": ranked["embedding_mode"],
                "embedding_model": ranked["embedding_model"],
                "vector_dimension": ranked["vector_dimension"],
                "embedding_content_hash": ranked["content_hash"],
                "retrieval_strategy": "vector_with_structured_safety_filters",
                "retrieval_eligible": retrieval_eligible,
                "retrieval_factors": ranked["retrieval_factors"],
            }
        )
        payload["memory_injection"].update(
            {
                "vector_similarity": ranked["vector_similarity"],
                "retrieval_eligible": retrieval_eligible,
                "retrieval_strategy": "vector_with_structured_safety_filters",
            }
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


def _rank_preferences_by_embedding(
    *,
    repository: MemoryRepository,
    preferences: list[dict],
    contract_type: str,
    clause: dict,
    risk_type: str,
    review_position: str,
    embedding_cache: dict,
    embedding_call_records: list | None,
    now: datetime,
) -> list[dict]:
    provider = create_embedding_provider()
    documents = {
        preference["preference_id"]: build_preference_embedding_text(preference)
        for preference in preferences
    }
    content_hashes = {
        preference_id: sha256(text.encode("utf-8")).hexdigest()
        for preference_id, text in documents.items()
    }
    stored = repository.find_preference_embeddings(
        [preference["preference_id"] for preference in preferences],
        embedding_mode=provider.mode,
        embedding_model=provider.model,
    )
    candidate_vectors: dict[str, list[float]] = {}
    missing_preferences = []
    for preference in preferences:
        preference_id = preference["preference_id"]
        row = stored.get(preference_id)
        vector = _stored_vector(row, content_hashes[preference_id])
        if vector is None:
            missing_preferences.append(preference)
        else:
            candidate_vectors[preference_id] = vector

    query_text = build_memory_query_text(
        contract_type,
        clause,
        risk_type,
        review_position,
    )
    embedding_batch = embed_texts(
        [query_text, *[documents[item["preference_id"]] for item in missing_preferences]],
        cache=embedding_cache,
        call_records=embedding_call_records,
        provider=provider,
    )
    query_vector = embedding_batch.vectors[0]
    new_vectors = embedding_batch.vectors[1:]
    upserts = []
    for preference, vector in zip(missing_preferences, new_vectors):
        preference_id = preference["preference_id"]
        candidate_vectors[preference_id] = vector
        upserts.append(
            {
                "preference_id": preference_id,
                "embedding_mode": embedding_batch.mode,
                "embedding_model": embedding_batch.model,
                "content_hash": content_hashes[preference_id],
                "vector_dimension": embedding_batch.vector_dimension,
                "vector": vector,
                "updated_at": now.isoformat(),
            }
        )

    mismatched = [
        preference
        for preference in preferences
        if len(candidate_vectors[preference["preference_id"]])
        != embedding_batch.vector_dimension
    ]
    if mismatched:
        refreshed = embed_texts(
            [documents[item["preference_id"]] for item in mismatched],
            cache=embedding_cache,
            call_records=embedding_call_records,
            provider=provider,
        )
        for preference, vector in zip(mismatched, refreshed.vectors):
            preference_id = preference["preference_id"]
            candidate_vectors[preference_id] = vector
            upserts.append(
                {
                    "preference_id": preference_id,
                    "embedding_mode": refreshed.mode,
                    "embedding_model": refreshed.model,
                    "content_hash": content_hashes[preference_id],
                    "vector_dimension": refreshed.vector_dimension,
                    "vector": vector,
                    "updated_at": now.isoformat(),
                }
            )
    repository.upsert_preference_embeddings(upserts)

    ranked = []
    clause_type = str(clause.get("clause_type", ""))
    for preference in preferences:
        preference_id = preference["preference_id"]
        similarity = round(
            cosine_similarity(query_vector, candidate_vectors[preference_id]),
            4,
        )
        exact_clause_type = preference["clause_type"] == clause_type
        exact_risk_type = preference["risk_type"] == risk_type
        exact_position = preference["review_position"] == review_position
        score = round(
            max(0.0, similarity) * 0.7
            + (0.15 if exact_clause_type else 0.0)
            + (0.15 if exact_risk_type else 0.0),
            4,
        )
        ranked.append(
            {
                "preference": preference,
                "match_score": round(score * 10, 4),
                "vector_similarity": similarity,
                "embedding_mode": embedding_batch.mode,
                "embedding_model": embedding_batch.model,
                "vector_dimension": len(candidate_vectors[preference_id]),
                "content_hash": content_hashes[preference_id],
                "retrieval_factors": {
                    "vector_weight": 0.7,
                    "exact_clause_type": exact_clause_type,
                    "exact_clause_type_weight": 0.15,
                    "exact_risk_type": exact_risk_type,
                    "exact_risk_type_weight": 0.15,
                    "exact_review_position": exact_position,
                    "hard_filters": ["contract_type", "review_position"],
                },
            }
        )
    return sorted(
        ranked,
        key=lambda item: (
            item["match_score"],
            item["preference"]["confidence"],
            item["preference"]["last_feedback_at"],
            item["preference"]["preference_id"],
        ),
        reverse=True,
    )


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


def _stored_vector(row, expected_content_hash: str) -> list[float] | None:
    if row is None or str(row["content_hash"]) != expected_content_hash:
        return None
    try:
        vector = json.loads(str(row["vector_json"]))
    except json.JSONDecodeError:
        return None
    if not isinstance(vector, list) or not vector:
        return None
    if len(vector) != int(row["vector_dimension"]):
        return None
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        for value in vector
    ):
        return None
    return [float(value) for value in vector]


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
