from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite, sqrt
from threading import Lock

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import Engine

from config import Settings
from db.postgres_models import ClauseEmbeddingRow, ClauseRow
from models.retrieval import RelatedClause
from providers.bge_provider import (
    BGE_EMBEDDING_DIMENSION,
    BGEEmbeddingProvider,
    BGEReranker,
)
from providers.embedding_provider import EmbeddingCallMetadata, EmbeddingRequest
from services.clause_index_service import (
    MAX_DYNAMIC_TOP_K,
    RelatedClauseRequest,
    build_clause_embedding_text,
    build_retrieval_query,
)
from services.rerank_service import build_keyword_query, keyword_candidate_factors
from services.runtime_log_service import write_runtime_log


VECTOR_RECALL_LIMIT = 20
KEYWORD_RECALL_LIMIT = 20
RERANK_LIMIT = 20
RRF_K = 60
HNSW_EF_SEARCH = 80
TRGM_SIMILARITY_THRESHOLD = 0.05
RETRIEVAL_VERSION = "postgres-bge-hybrid-v1"


class PostgresHybridClauseRetriever:
    def __init__(
        self,
        engine: Engine,
        redis_client: Redis,
        app_settings: Settings,
        *,
        embedding_provider: BGEEmbeddingProvider | None = None,
        reranker: BGEReranker | None = None,
    ):
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresHybridClauseRetriever requires PostgreSQL")
        self.engine = engine
        self.redis = redis_client
        self.settings = app_settings
        self.embedding_provider = embedding_provider or BGEEmbeddingProvider(
            app_settings
        )
        self.reranker = reranker or BGEReranker(app_settings)
        self._index_lock = Lock()

    def retrieve(
        self,
        task_id: str,
        request: RelatedClauseRequest,
        call_records: list[EmbeddingCallMetadata] | None = None,
    ) -> list[dict]:
        current_clause_id = str(request.current_clause.get("clause_id", "")).strip()
        if not current_clause_id:
            raise ValueError("current_clause requires clause_id")

        corpus_hash, candidate_count = self._ensure_clause_embeddings(
            task_id,
            call_records,
        )
        if candidate_count <= 1:
            return []

        adjusted_check_point = " ".join(
            [
                request.playbook_check_point,
                *(request.query_adjustments.get("additional_keywords") or []),
            ]
        ).strip()
        query = build_retrieval_query(
            request.current_clause,
            request.risk_type,
            adjusted_check_point,
        )
        keyword_query = build_keyword_query(
            request.current_clause,
            request.risk_type,
            adjusted_check_point,
        )
        cache_key = self._retrieval_cache_key(
            task_id,
            corpus_hash,
            request,
            query,
        )
        cached = self._cache_get_json(cache_key)
        if self._valid_retrieval_cache(cached):
            if call_records is not None:
                call_records.append(self.embedding_provider.cache_metadata(0))
            return cached

        query_vector = self._embeddings_for_texts([query], call_records)[0]
        vector_recall = self._vector_recall(
            task_id,
            current_clause_id,
            query_vector,
        )
        keyword_text = " ".join(
            [
                *keyword_query["query_terms"],
                *keyword_query["playbook_terms"],
                *keyword_query["check_point_terms"],
            ]
        ).strip()
        keyword_recall = self._keyword_recall(
            task_id,
            current_clause_id,
            keyword_text,
        )
        merged = _rrf_merge(vector_recall, keyword_recall)
        rerank_candidates = merged[:RERANK_LIMIT]
        rerank_scores = self.reranker.score(
            query,
            [
                build_clause_embedding_text(item["clause"])
                for item in rerank_candidates
            ],
        )
        for item, score in zip(rerank_candidates, rerank_scores):
            item["rerank_score"] = round(score, 6)
        reranked = sorted(
            rerank_candidates,
            key=lambda item: (
                -item["rerank_score"],
                -item["rrf_score"],
                str(item["clause"].get("clause_id", "")),
            ),
        )
        selected = reranked[: min(request.limit, MAX_DYNAMIC_TOP_K)]
        result = self._result_payload(
            request,
            selected,
            keyword_query,
            candidate_count=candidate_count - 1,
            vector_recall_count=len(vector_recall),
            keyword_recall_count=len(keyword_recall),
        )
        self._cache_set_json(
            cache_key,
            result,
            self.settings.retrieval_cache_ttl_seconds,
        )
        return result

    def close(self) -> None:
        self.redis.close()

    def warmup(self) -> None:
        self.embedding_provider.warmup()
        self.reranker.warmup()

    def _ensure_clause_embeddings(
        self,
        task_id: str,
        call_records: list[EmbeddingCallMetadata] | None,
    ) -> tuple[str, int]:
        with self._index_lock:
            join_condition = and_(
                ClauseEmbeddingRow.task_id == ClauseRow.task_id,
                ClauseEmbeddingRow.clause_id == ClauseRow.clause_id,
                ClauseEmbeddingRow.embedding_model
                == self.settings.bge_embedding_model,
                ClauseEmbeddingRow.model_revision
                == self.settings.bge_embedding_revision,
            )
            with self.engine.connect() as connection:
                rows = connection.execute(
                    select(
                        ClauseRow.clause_id,
                        ClauseRow.payload_json,
                        ClauseEmbeddingRow.content_hash,
                    )
                    .outerjoin(ClauseEmbeddingRow, join_condition)
                    .where(ClauseRow.task_id == task_id)
                    .order_by(ClauseRow.clause_id)
                ).all()
            if not rows:
                raise ValueError(f"no persisted clauses found for task {task_id}")

            corpus_parts = []
            missing = []
            for clause_id, clause_payload, persisted_hash in rows:
                embedding_text = build_clause_embedding_text(
                    clause_payload
                )
                content_hash = sha256(embedding_text.encode("utf-8")).hexdigest()
                corpus_parts.append(f"{clause_id}:{content_hash}")
                if persisted_hash != content_hash:
                    missing.append((clause_id, content_hash, embedding_text))

            if missing:
                vectors = self._embeddings_for_texts(
                    [item[2] for item in missing],
                    call_records,
                )
                now = _utc_now()
                with self.engine.begin() as connection:
                    for (clause_id, content_hash, _), vector in zip(
                        missing,
                        vectors,
                    ):
                        statement = postgres_insert(ClauseEmbeddingRow).values(
                            task_id=task_id,
                            clause_id=clause_id,
                            embedding_model=self.settings.bge_embedding_model,
                            model_revision=self.settings.bge_embedding_revision,
                            content_hash=content_hash,
                            embedding=vector,
                            updated_at=now,
                        )
                        connection.execute(
                            statement.on_conflict_do_update(
                                index_elements=[
                                    ClauseEmbeddingRow.task_id,
                                    ClauseEmbeddingRow.clause_id,
                                    ClauseEmbeddingRow.embedding_model,
                                    ClauseEmbeddingRow.model_revision,
                                ],
                                set_={
                                    "content_hash": statement.excluded.content_hash,
                                    "embedding": statement.excluded.embedding,
                                    "updated_at": statement.excluded.updated_at,
                                },
                            )
                        )
            corpus_hash = sha256(
                "\n".join(corpus_parts).encode("utf-8")
            ).hexdigest()
            return corpus_hash, len(rows)

    def _embeddings_for_texts(
        self,
        texts: list[str],
        call_records: list[EmbeddingCallMetadata] | None,
    ) -> list[list[float]]:
        keys = [self._embedding_cache_key(text) for text in texts]
        cached_values = self._cache_mget_json(keys)
        vectors: list[list[float] | None] = [
            _validated_cached_vector(value)
            for value in cached_values
        ]
        missing_indices = [
            index for index, vector in enumerate(vectors) if vector is None
        ]
        if missing_indices:
            response = self.embedding_provider.embed(
                EmbeddingRequest([texts[index] for index in missing_indices]),
                call_records=call_records,
            )
            if len(response.vectors) != len(missing_indices):
                raise ValueError(
                    "embedding result count does not match requested text count"
                )
            for index, vector in zip(missing_indices, response.vectors):
                vectors[index] = vector
                self._cache_set_json(
                    keys[index],
                    vector,
                    self.settings.embedding_cache_ttl_seconds,
                )
        elif call_records is not None:
            call_records.append(
                self.embedding_provider.cache_metadata(len(texts))
            )
        if any(vector is None for vector in vectors):
            raise RuntimeError("embedding batch contains an unresolved vector")
        return list(vectors)

    def _vector_recall(
        self,
        task_id: str,
        current_clause_id: str,
        query_vector: list[float],
    ) -> list[dict]:
        distance = ClauseEmbeddingRow.embedding.cosine_distance(
            query_vector
        ).label("distance")
        statement = (
            select(ClauseRow.payload_json, distance)
            .join(
                ClauseEmbeddingRow,
                and_(
                    ClauseEmbeddingRow.task_id == ClauseRow.task_id,
                    ClauseEmbeddingRow.clause_id == ClauseRow.clause_id,
                ),
            )
            .where(
                ClauseRow.task_id == task_id,
                ClauseRow.clause_id != current_clause_id,
                ClauseEmbeddingRow.embedding_model
                == self.settings.bge_embedding_model,
                ClauseEmbeddingRow.model_revision
                == self.settings.bge_embedding_revision,
            )
            .order_by(distance, ClauseRow.clause_id)
            .limit(VECTOR_RECALL_LIMIT)
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "SET LOCAL hnsw.iterative_scan = strict_order"
            )
            connection.exec_driver_sql(
                f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}"
            )
            rows = connection.execute(statement).all()
        return [
            {
                "clause": payload,
                "vector_similarity": round(1.0 - float(distance_value), 6),
            }
            for payload, distance_value in rows
        ]

    def _keyword_recall(
        self,
        task_id: str,
        current_clause_id: str,
        query: str,
    ) -> list[dict]:
        if not query:
            return []
        similarity = func.similarity(ClauseRow.search_text, query).label(
            "keyword_score"
        )
        statement = (
            select(ClauseRow.payload_json, similarity)
            .where(
                ClauseRow.task_id == task_id,
                ClauseRow.clause_id != current_clause_id,
                ClauseRow.search_text.op("%")(query),
            )
            .order_by(similarity.desc(), ClauseRow.clause_id)
            .limit(KEYWORD_RECALL_LIMIT)
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "SET LOCAL pg_trgm.similarity_threshold = "
                f"{TRGM_SIMILARITY_THRESHOLD}"
            )
            rows = connection.execute(statement).all()
        return [
            {
                "clause": payload,
                "keyword_score": round(float(score), 6),
            }
            for payload, score in rows
        ]

    def _result_payload(
        self,
        request: RelatedClauseRequest,
        selected: list[dict],
        keyword_query: dict,
        *,
        candidate_count: int,
        vector_recall_count: int,
        keyword_recall_count: int,
    ) -> list[dict]:
        query_context = {
            "query_version": RETRIEVAL_VERSION,
            "current_clause_id": request.current_clause.get("clause_id", ""),
            "current_clause_type": request.current_clause.get("clause_type", ""),
            "risk_type": request.risk_type,
            "playbook_check_point": request.playbook_check_point,
            "query_adjustments": request.query_adjustments,
            "key_fields": request.current_clause.get("key_fields") or {},
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
            "candidate_count": candidate_count,
            "vector_recall_count": vector_recall_count,
            "keyword_recall_count": keyword_recall_count,
            "vector_top_k": VECTOR_RECALL_LIMIT,
            "keyword_top_k": KEYWORD_RECALL_LIMIT,
            "rrf_k": RRF_K,
            "rerank_top_k": RERANK_LIMIT,
            "requested_top_k": request.limit,
            "max_top_k": MAX_DYNAMIC_TOP_K,
            "effective_top_k": len(selected),
            "score_cutoff": (
                min(float(item["rerank_score"]) for item in selected)
                if selected
                else None
            ),
            "hnsw": {
                "m": 16,
                "ef_construction": 64,
                "ef_search": HNSW_EF_SEARCH,
                "iterative_scan": "strict_order",
            },
            "embedding_revision": self.settings.bge_embedding_revision,
            "embedding_max_length": self.settings.bge_embedding_max_length,
            "reranker_model": self.settings.bge_reranker_model,
            "reranker_revision": self.settings.bge_reranker_revision,
            "reranker_max_length": self.settings.bge_reranker_max_length,
        }
        payloads = []
        for item in selected:
            clause = item["clause"]
            keyword_factors = keyword_candidate_factors(
                keyword_query,
                clause,
            )
            rerank_factors = {
                "bge_score": item["rerank_score"],
                "rrf_score": round(item["rrf_score"], 8),
                "vector_similarity": item.get("vector_similarity", 0.0),
                "keyword_score": item.get("keyword_score", 0.0),
                "keyword_matches": keyword_factors["keyword_matches"],
                "playbook_check_point_matches": keyword_factors[
                    "playbook_check_point_matches"
                ],
                "playbook_keyword_matches": keyword_factors[
                    "playbook_keyword_matches"
                ],
                "final_score": item["rerank_score"],
            }
            payloads.append(
                RelatedClause(
                    clause_id=str(clause.get("clause_id", "")),
                    title=str(clause.get("title", "")),
                    clause_type=str(clause.get("clause_type", "")),
                    text=str(clause.get("text", "")),
                    key_fields=clause.get("key_fields") or {},
                    source_location=clause.get("source_location") or {},
                    vector_similarity=float(item.get("vector_similarity", 0.0)),
                    keyword_score=float(item.get("keyword_score", 0.0)),
                    keyword_matches=list(keyword_factors["keyword_matches"]),
                    retrieval_sources=list(item["retrieval_sources"]),
                    vector_rank=item.get("vector_rank"),
                    keyword_rank=item.get("keyword_rank"),
                    embedding_mode=self.embedding_provider.mode,
                    embedding_model=self.settings.bge_embedding_model,
                    vector_dimension=BGE_EMBEDDING_DIMENSION,
                    rerank_score=float(item["rerank_score"]),
                    rerank_factors=rerank_factors,
                    retrieval_scope="current_contract",
                    query_context=query_context,
                ).to_dict()
            )
        return payloads

    def _embedding_cache_key(self, text: str) -> str:
        content_hash = sha256(text.encode("utf-8")).hexdigest()
        identity = ":".join(
            [
                self.settings.bge_embedding_model,
                self.settings.bge_embedding_revision,
                content_hash,
            ]
        )
        return f"review-agent:embedding:{sha256(identity.encode()).hexdigest()}"

    def _retrieval_cache_key(
        self,
        task_id: str,
        corpus_hash: str,
        request: RelatedClauseRequest,
        query: str,
    ) -> str:
        identity = {
            "version": RETRIEVAL_VERSION,
            "task_id": task_id,
            "corpus_hash": corpus_hash,
            "query_hash": sha256(query.encode("utf-8")).hexdigest(),
            "embedding_model": self.settings.bge_embedding_model,
            "embedding_revision": self.settings.bge_embedding_revision,
            "reranker_model": self.settings.bge_reranker_model,
            "reranker_revision": self.settings.bge_reranker_revision,
            "limit": request.limit,
            "query_adjustments": request.query_adjustments,
        }
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"review-agent:retrieval:{sha256(canonical.encode()).hexdigest()}"

    def _valid_retrieval_cache(self, value) -> bool:
        if not isinstance(value, list) or len(value) > MAX_DYNAMIC_TOP_K:
            return False
        for item in value:
            if not isinstance(item, dict) or not str(item.get("clause_id", "")):
                return False
            if item.get("embedding_mode") != self.embedding_provider.mode:
                return False
            if item.get("embedding_model") != self.settings.bge_embedding_model:
                return False
            if item.get("vector_dimension") != BGE_EMBEDDING_DIMENSION:
                return False
            query_context = item.get("query_context")
            if not isinstance(query_context, dict):
                return False
            if (
                query_context.get("embedding_revision")
                != self.settings.bge_embedding_revision
            ):
                return False
            if (
                query_context.get("reranker_revision")
                != self.settings.bge_reranker_revision
            ):
                return False
        return True

    def _cache_mget_json(self, keys: list[str]) -> list:
        try:
            values = self.redis.mget(keys)
        except RedisError as exc:
            _record_cache_failure("mget", exc)
            return [None] * len(keys)
        return [_decode_json(value) for value in values]

    def _cache_get_json(self, key: str):
        try:
            return _decode_json(self.redis.get(key))
        except RedisError as exc:
            _record_cache_failure("get", exc)
            return None

    def _cache_set_json(self, key: str, value, ttl_seconds: int) -> None:
        try:
            self.redis.setex(
                key,
                ttl_seconds,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            )
        except RedisError as exc:
            _record_cache_failure("setex", exc)


def _rrf_merge(vector_recall: list[dict], keyword_recall: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for source, ranked in (
        ("vector", vector_recall),
        ("pg_trgm", keyword_recall),
    ):
        for rank, item in enumerate(ranked, start=1):
            clause = item["clause"]
            clause_id = str(clause.get("clause_id", ""))
            if not clause_id:
                continue
            candidate = merged.setdefault(
                clause_id,
                {
                    "clause": clause,
                    "vector_similarity": 0.0,
                    "keyword_score": 0.0,
                    "vector_rank": None,
                    "keyword_rank": None,
                    "retrieval_sources": [],
                    "rrf_score": 0.0,
                },
            )
            candidate["retrieval_sources"].append(source)
            candidate["rrf_score"] += 1.0 / (RRF_K + rank)
            if source == "vector":
                candidate["vector_rank"] = rank
                candidate["vector_similarity"] = item["vector_similarity"]
            else:
                candidate["keyword_rank"] = rank
                candidate["keyword_score"] = item["keyword_score"]
    return sorted(
        merged.values(),
        key=lambda item: (
            -item["rrf_score"],
            str(item["clause"].get("clause_id", "")),
        ),
    )


def _validated_cached_vector(value) -> list[float] | None:
    if not isinstance(value, list) or len(value) != BGE_EMBEDDING_DIMENSION:
        return None
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(isfinite(item) for item in vector):
        return None
    length = sqrt(sum(item * item for item in vector))
    if not length:
        return None
    return [item / length for item in vector]


def _decode_json(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _record_cache_failure(operation: str, error: Exception) -> None:
    write_runtime_log(
        "retrieval_cache_unavailable",
        operation=operation,
        error_type=error.__class__.__name__,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
