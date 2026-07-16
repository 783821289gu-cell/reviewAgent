from models.retrieval import RelatedClause
from providers.embedding_provider import embedding_call_records_from_tool_input
from services.embedding_service import cosine_similarity, embed_texts
from services.rerank_service import rerank_candidates


DEFAULT_RELATED_CLAUSE_LIMIT = 3
VECTOR_RECALL_MULTIPLIER = 4


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
    query_context = {
        "current_clause_id": current_clause_id,
        "current_clause_type": current_clause.get("clause_type", ""),
        "risk_type": risk_type,
        "playbook_check_point": playbook_check_point,
        "embedding_query_components": [
            "current_clause_text",
            "current_clause_type",
            "risk_type",
            "playbook_check_point",
            "current_clause_key_fields",
        ],
    }
    candidate_clauses = [
        clause
        for clause in clauses
        if isinstance(clause, dict)
        and str(clause.get("clause_id", "")) != current_clause_id
    ]
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
            }
        )

    recall_limit = max(limit, limit * VECTOR_RECALL_MULTIPLIER)
    recalled = sorted(
        candidates,
        key=lambda item: (item["vector_similarity"], item["clause"].get("clause_id", "")),
        reverse=True,
    )[:recall_limit]
    reranked = rerank_candidates(recalled, current_clause=current_clause, risk_type=risk_type)

    related_clauses = []
    for item in reranked[:limit]:
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
