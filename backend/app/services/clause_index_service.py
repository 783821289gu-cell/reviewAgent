from models.retrieval import RelatedClause
from providers.embedding_provider import embedding_call_records_from_tool_input
from services.embedding_service import cosine_similarity, embed_texts
from services.rerank_service import (
    build_keyword_query,
    keyword_candidate_factors,
    rerank_candidates,
)


DEFAULT_RELATED_CLAUSE_LIMIT = 3
MAX_DYNAMIC_TOP_K = 5
VECTOR_RECALL_MULTIPLIER = 4
KEYWORD_RECALL_MULTIPLIER = 4
MIN_RERANK_SCORE = 0.12
DYNAMIC_SCORE_BAND = 0.2


def retrieve_related_clauses(tool_input: dict) -> list[dict]:
    contract_type = str(tool_input.get("contract_type", "")).strip()
    current_clause = tool_input.get("current_clause")
    clauses = tool_input.get("clauses")
    risk_type = str(tool_input.get("risk_type", "")).strip()
    playbook_check_point = str(tool_input.get("playbook_check_point", "")).strip()
    limit = int(tool_input.get("limit", DEFAULT_RELATED_CLAUSE_LIMIT))

    if contract_type != "NDA":
        return []
    if not isinstance(current_clause, dict):
        raise ValueError("current_clause must be a dict")
    if not isinstance(clauses, list):
        raise ValueError("clauses must be a list")
    if not risk_type:
        raise ValueError("risk_type is required")
    if not playbook_check_point:
        raise ValueError("playbook_check_point is required")
    if limit <= 0:
        return []

    embedding_cache = tool_input.get("embedding_cache")
    if embedding_cache is None:
        embedding_cache = {}
    if not isinstance(embedding_cache, dict):
        raise ValueError("embedding_cache must be a dict")

    current_clause_id = str(current_clause.get("clause_id", ""))
    candidate_clauses = _current_contract_candidates(clauses, current_clause_id)
    if not candidate_clauses:
        return []

    keyword_query = build_keyword_query(
        current_clause,
        risk_type,
        playbook_check_point,
    )
    query_context = {
        "query_version": "hybrid-retrieval-v1",
        "current_clause_id": current_clause_id,
        "current_clause_type": current_clause.get("clause_type", ""),
        "risk_type": risk_type,
        "playbook_check_point": playbook_check_point,
        "key_fields": current_clause.get("key_fields") or {},
        "keyword_terms": keyword_query["query_terms"],
        "playbook_keywords": keyword_query["playbook_terms"],
        "playbook_check_point_terms": keyword_query["check_point_terms"],
        "embedding_query_components": [
            "current_clause_text",
            "current_clause_type",
            "risk_type",
            "playbook_check_point",
            "current_clause_key_fields",
        ],
    }
    texts = [
        build_retrieval_query(current_clause, risk_type, playbook_check_point),
        *[build_clause_embedding_text(clause) for clause in candidate_clauses],
    ]
    embedding_batch = embed_texts(
        texts,
        cache=embedding_cache,
        call_records=embedding_call_records_from_tool_input(tool_input),
    )
    query_vector = embedding_batch.vectors[0]

    candidates = []
    for clause, candidate_vector in zip(candidate_clauses, embedding_batch.vectors[1:]):
        candidates.append(
            {
                "clause": clause,
                "vector_similarity": cosine_similarity(query_vector, candidate_vector),
                "keyword_factors": keyword_candidate_factors(keyword_query, clause),
            }
        )

    requested_top_k = min(limit, MAX_DYNAMIC_TOP_K)
    recalled = _merge_recall_candidates(candidates, requested_top_k)
    reranked = rerank_candidates(
        recalled,
        current_clause=current_clause,
        risk_type=risk_type,
    )
    selected, score_cutoff = _dynamic_top_k(reranked, requested_top_k)
    query_context.update(
        {
            "candidate_count": len(candidate_clauses),
            "merged_candidate_count": len(recalled),
            "requested_top_k": limit,
            "max_top_k": MAX_DYNAMIC_TOP_K,
            "effective_top_k": len(selected),
            "score_cutoff": score_cutoff,
        }
    )

    related_clauses = []
    for item in selected:
        clause = item["clause"]
        related_clauses.append(
            RelatedClause(
                clause_id=str(clause.get("clause_id", "")),
                title=str(clause.get("title", "")),
                clause_type=str(clause.get("clause_type", "")),
                text=str(clause.get("text", "")),
                key_fields=clause.get("key_fields") or {},
                source_location=clause.get("source_location") or {},
                vector_similarity=round(float(item["vector_similarity"]), 4),
                keyword_score=float(item["keyword_factors"]["keyword_score"]),
                keyword_matches=list(item["keyword_factors"]["keyword_matches"]),
                retrieval_sources=list(item["retrieval_sources"]),
                vector_rank=item.get("vector_rank"),
                keyword_rank=item.get("keyword_rank"),
                embedding_mode=embedding_batch.mode,
                embedding_model=embedding_batch.model,
                vector_dimension=embedding_batch.vector_dimension,
                rerank_score=float(item["rerank_score"]),
                rerank_factors=item["rerank_factors"],
                retrieval_scope="current_contract",
                query_context=query_context,
            ).to_dict()
        )
    return related_clauses


def build_retrieval_query(
    current_clause: dict,
    risk_type: str,
    playbook_check_point: str,
) -> str:
    return " ".join(
        [
            str(current_clause.get("text", "")),
            str(current_clause.get("clause_type", "")),
            str(risk_type),
            str(playbook_check_point),
            _key_fields_text(current_clause.get("key_fields") or {}),
        ]
    )


def build_clause_embedding_text(clause: dict) -> str:
    return " ".join(
        [
            str(clause.get("text", "")),
            str(clause.get("clause_type", "")),
            _key_fields_text(clause.get("key_fields") or {}),
        ]
    )


def _current_contract_candidates(clauses: list[dict], current_clause_id: str) -> list[dict]:
    candidates = []
    seen_ids = set()
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        clause_id = str(clause.get("clause_id", "")).strip()
        if not clause_id or clause_id == current_clause_id or clause_id in seen_ids:
            continue
        seen_ids.add(clause_id)
        candidates.append(clause)
    return candidates


def _merge_recall_candidates(candidates: list[dict], requested_top_k: int) -> list[dict]:
    vector_limit = min(
        len(candidates),
        max(requested_top_k, requested_top_k * VECTOR_RECALL_MULTIPLIER),
    )
    keyword_limit = min(
        len(candidates),
        max(requested_top_k, requested_top_k * KEYWORD_RECALL_MULTIPLIER),
    )
    vector_ranked = sorted(
        candidates,
        key=lambda item: (
            -item["vector_similarity"],
            str(item["clause"].get("clause_id", "")),
        ),
    )[:vector_limit]
    keyword_ranked = sorted(
        [item for item in candidates if item["keyword_factors"]["keyword_score"] > 0],
        key=lambda item: (
            -item["keyword_factors"]["keyword_score"],
            str(item["clause"].get("clause_id", "")),
        ),
    )[:keyword_limit]

    merged = {}
    for source, ranked in (("embedding", vector_ranked), ("keyword", keyword_ranked)):
        for rank, item in enumerate(ranked, start=1):
            clause_id = str(item["clause"].get("clause_id", ""))
            if clause_id not in merged:
                merged[clause_id] = {
                    **item,
                    "retrieval_sources": [],
                    "vector_rank": None,
                    "keyword_rank": None,
                }
            merged_item = merged[clause_id]
            merged_item["retrieval_sources"].append(source)
            rank_field = "vector_rank" if source == "embedding" else "keyword_rank"
            merged_item[rank_field] = rank
    return list(merged.values())


def _dynamic_top_k(candidates: list[dict], requested_top_k: int) -> tuple[list[dict], float]:
    if not candidates or requested_top_k <= 0:
        return [], MIN_RERANK_SCORE
    top_score = float(candidates[0]["rerank_score"])
    score_cutoff = round(max(MIN_RERANK_SCORE, top_score - DYNAMIC_SCORE_BAND), 4)
    qualified = [
        candidate
        for candidate in candidates
        if float(candidate["rerank_score"]) >= score_cutoff
    ]
    if not qualified:
        qualified = candidates[:1]
    return qualified[: min(requested_top_k, MAX_DYNAMIC_TOP_K)], score_cutoff


def _key_fields_text(key_fields: dict) -> str:
    parts = []
    for key, value in key_fields.items():
        values = []
        if isinstance(value, list):
            values = [str(item) for item in value if str(item).strip()]
        elif str(value or "").strip():
            values = [str(value)]
        if not values:
            continue
        parts.extend(values)
        parts.append(str(key))
    return " ".join(parts)
