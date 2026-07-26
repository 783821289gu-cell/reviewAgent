from contextvars import ContextVar
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
import json
import re

from langchain_core.embeddings import Embeddings
from langgraph.store.base import PutOp, SearchOp
from langgraph.store.postgres import PostgresStore
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url

from config import Settings
from db.postgres_models import APP_SCHEMA
from models.memory import build_semantic_preference
from providers.bge_provider import BGE_EMBEDDING_DIMENSION, BGEEmbeddingProvider
from providers.embedding_provider import EmbeddingRequest
from services.memory_text import (
    build_memory_query_text,
    build_preference_embedding_text,
)


MEMORY_NAMESPACE_ROOT = "review_memory"
MEMORY_MIGRATION_KEY = "app-memory-v1"
MEMORY_SEARCH_LIMIT = 20
MEMORY_STORE_HNSW_EF_SEARCH = 80
_SCHEMA_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EMBEDDING_CALL_RECORDS: ContextVar[list | None] = ContextVar(
    "review_agent_memory_embedding_call_records",
    default=None,
)


class LangGraphPostgresMemoryStore:
    source = "langgraph_postgres_vector_memory"

    def __init__(
        self,
        store: PostgresStore,
        pool: ConnectionPool,
        embedding_provider: BGEEmbeddingProvider,
        *,
        schema: str = APP_SCHEMA,
    ):
        self.store = store
        self.pool = pool
        self.embedding_provider = embedding_provider
        self.schema = _validated_schema(schema)

    @classmethod
    def postgres(
        cls,
        database_url: str,
        app_settings: Settings,
        *,
        embedding_provider: BGEEmbeddingProvider | None = None,
        schema: str = APP_SCHEMA,
        migrate_legacy: bool = True,
    ) -> "LangGraphPostgresMemoryStore":
        normalized_url = str(database_url).strip()
        if not normalized_url:
            raise ValueError("PostgreSQL database URL is required for Memory Store")
        resolved_schema = _validated_schema(schema)
        url = make_url(normalized_url).set(drivername="postgresql")
        conninfo = url.render_as_string(hide_password=False)
        with Connection.connect(conninfo, autocommit=True) as connection:
            connection.execute(
                f'CREATE SCHEMA IF NOT EXISTS "{resolved_schema}"'
            )

        provider = embedding_provider or BGEEmbeddingProvider(app_settings)
        pool = ConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=2,
            open=True,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            configure=partial(
                _configure_connection,
                schema=resolved_schema,
            ),
            check=ConnectionPool.check_connection,
        )
        try:
            pool.wait()
            store = PostgresStore(
                pool,
                index={
                    "dims": BGE_EMBEDDING_DIMENSION,
                    "embed": _BGEStoreEmbeddings(provider),
                    "fields": ["embedding_text"],
                    "distance_type": "cosine",
                    "ann_index_config": {
                        "kind": "hnsw",
                        "m": 16,
                        "ef_construction": 64,
                    },
                },
            )
            store.setup()
            manager = cls(
                store,
                pool,
                provider,
                schema=resolved_schema,
            )
            if migrate_legacy:
                manager.migrate_legacy_app_memory()
            return manager
        except Exception:
            pool.close()
            raise

    def close(self) -> None:
        self.pool.close()

    def stable_memory_id(self, idempotency_key: str) -> str:
        digest = sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]
        return f"MEM-{digest}"

    def save(self, item: dict, idempotency_key: str | None = None) -> dict:
        episode = dict(item)
        namespace = self._episodes_namespace()
        if idempotency_key:
            marker = self.store.get(
                self._idempotency_namespace(),
                _idempotency_store_key(idempotency_key),
            )
            if marker is not None:
                stored_episode = self.store.get(
                    namespace,
                    str(marker.value["memory_id"]),
                )
                if stored_episode is None:
                    stored_episode_value = dict(marker.value["episode"])
                    self.store.put(
                        namespace,
                        str(stored_episode_value["memory_id"]),
                        stored_episode_value,
                        index=False,
                    )
                else:
                    stored_episode_value = dict(stored_episode.value)
                self._rebuild_preference(stored_episode_value)
                return _episode_payload(stored_episode_value)

        existing = self.store.get(namespace, str(episode["memory_id"]))
        if existing is not None:
            if idempotency_key:
                self._save_idempotency_marker(idempotency_key, existing.value)
            self._rebuild_preference(existing.value)
            return _episode_payload(existing.value)

        self.store.put(
            namespace,
            str(episode["memory_id"]),
            episode,
            index=False,
        )
        if idempotency_key:
            self._save_idempotency_marker(idempotency_key, episode)
        self._rebuild_preference(episode)
        return _episode_payload(episode)

    def find_ranked_preferences(
        self,
        *,
        contract_type: str,
        clause: dict,
        risk_type: str,
        review_position: str,
        limit: int,
        embedding_call_records: list | None,
    ) -> list[dict]:
        namespaces = self._preference_namespaces(
            contract_type,
            review_position,
        )
        token = _EMBEDDING_CALL_RECORDS.set(embedding_call_records)
        try:
            self._ensure_preference_indexes(namespaces)
            query = build_memory_query_text(
                contract_type,
                clause,
                risk_type,
                review_position,
            )
            semantic_limit = max(MEMORY_SEARCH_LIMIT, limit * 4)
            semantic_results = self.store.batch(
                [
                    SearchOp(
                        namespace,
                        limit=semantic_limit,
                        query=query,
                        refresh_ttl=False,
                    )
                    for namespace in namespaces
                ]
            )
        finally:
            _EMBEDDING_CALL_RECORDS.reset(token)

        candidates = {}
        for results in semantic_results:
            for result in results:
                preference = _preference_payload(result.value)
                candidates[preference["preference_id"]] = {
                    "preference": preference,
                    "vector_similarity": _bounded_score(result.score),
                }

        exact_filter = {
            "clause_type": str(clause.get("clause_type", "")),
            "risk_type": risk_type,
        }
        for namespace in namespaces:
            for result in self.store.search(
                namespace,
                filter=exact_filter,
                limit=semantic_limit,
                refresh_ttl=False,
            ):
                preference = _preference_payload(result.value)
                candidates.setdefault(
                    preference["preference_id"],
                    {
                        "preference": preference,
                        "vector_similarity": 0.0,
                    },
                )

        ranked = []
        clause_type = str(clause.get("clause_type", ""))
        for candidate in candidates.values():
            preference = candidate["preference"]
            similarity = candidate["vector_similarity"]
            exact_clause_type = preference["clause_type"] == clause_type
            exact_risk_type = preference["risk_type"] == risk_type
            exact_position = preference["review_position"] == review_position
            score = round(
                max(0.0, similarity) * 0.7
                + (0.15 if exact_clause_type else 0.0)
                + (0.15 if exact_risk_type else 0.0),
                4,
            )
            content_hash = _preference_content_hash(preference)
            ranked.append(
                {
                    "preference": preference,
                    "match_score": round(score * 10, 4),
                    "vector_similarity": round(similarity, 4),
                    "embedding_mode": self.embedding_provider.mode,
                    "embedding_model": self.embedding_provider.model,
                    "vector_dimension": BGE_EMBEDDING_DIMENSION,
                    "content_hash": content_hash,
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

    def update_preference_lifecycle(
        self,
        preference_id: str,
        *,
        contract_type: str,
        review_position: str,
        lifecycle_status: str,
        confidence: float,
        updated_at: str,
        last_used_at: str | None = None,
    ) -> None:
        namespace = self._preference_namespace(
            contract_type,
            review_position,
        )
        existing = self.store.get(namespace, preference_id)
        if existing is None:
            return
        value = dict(existing.value)
        value["lifecycle_status"] = lifecycle_status
        value["confidence"] = confidence
        value["updated_at"] = updated_at
        if last_used_at is not None:
            value["last_used_at"] = last_used_at
        self.store.put(
            namespace,
            preference_id,
            value,
            index=False,
        )

    def migrate_legacy_app_memory(self) -> dict:
        marker_namespace = self._system_namespace()
        marker = self.store.get(marker_namespace, MEMORY_MIGRATION_KEY)
        if marker is not None:
            return dict(marker.value)

        memory_rows, preference_rows = self._legacy_rows()
        migrated_episodes = 0
        migrated_preferences = 0
        episode_namespace = self._episodes_namespace()
        for row in memory_rows:
            episode = _legacy_episode(row)
            if self.store.get(episode_namespace, episode["memory_id"]) is None:
                self.store.put(
                    episode_namespace,
                    episode["memory_id"],
                    episode,
                    index=False,
                )
                migrated_episodes += 1
            idempotency_key = str(row.get("idempotency_key") or "")
            if (
                idempotency_key
                and self.store.get(
                    self._idempotency_namespace(),
                    _idempotency_store_key(idempotency_key),
                )
                is None
            ):
                self._save_idempotency_marker(idempotency_key, episode)

        for row in preference_rows:
            preference = _legacy_preference(row)
            namespace = self._preference_namespace(
                preference["contract_type"],
                preference["review_position"],
            )
            if self.store.get(namespace, preference["preference_id"]) is None:
                self.store.put(
                    namespace,
                    preference["preference_id"],
                    _stored_preference(preference, indexed=False),
                    index=False,
                )
                migrated_preferences += 1

        result = {
            "migration": MEMORY_MIGRATION_KEY,
            "source_schema": self.schema,
            "source_episode_count": len(memory_rows),
            "source_preference_count": len(preference_rows),
            "migrated_episode_count": migrated_episodes,
            "migrated_preference_count": migrated_preferences,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.put(
            marker_namespace,
            MEMORY_MIGRATION_KEY,
            result,
            index=False,
        )
        return result

    def _legacy_rows(self) -> tuple[list[dict], list[dict]]:
        schema = self.schema
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                for table_name in ("memory_items", "semantic_preferences"):
                    cursor.execute(
                        "SELECT to_regclass(%s) AS table_name",
                        (f"{schema}.{table_name}",),
                    )
                    if cursor.fetchone()["table_name"] is None:
                        raise RuntimeError(
                            f"{schema}.{table_name} is required for Memory migration"
                        )
                cursor.execute(
                    f"""
                    SELECT memory_id, memory_type, contract_type, clause_type,
                           risk_type, review_position, user_action,
                           original_severity, final_severity,
                           original_suggestion, final_suggestion, ignore_reason,
                           source_finding_id, source_clause_id, include_in_report,
                           created_at, idempotency_key
                    FROM "{schema}".memory_items
                    ORDER BY created_at, memory_id
                    """
                )
                memory_rows = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    f"""
                    SELECT preference_id, contract_type, clause_type, risk_type,
                           review_position, support_count, opposition_count,
                           source_memory_ids_json, variants_json, conflict_status,
                           base_confidence, confidence, lifecycle_status,
                           last_feedback_at, last_used_at, updated_at
                    FROM "{schema}".semantic_preferences
                    ORDER BY preference_id
                    """
                )
                preference_rows = [dict(row) for row in cursor.fetchall()]
        return memory_rows, preference_rows

    def _rebuild_preference(self, episode: dict) -> None:
        group_filter = {
            "contract_type": str(episode["contract_type"]),
            "clause_type": str(episode["clause_type"]),
            "risk_type": str(episode["risk_type"]),
            "review_position": str(episode["review_position"]),
        }
        episodes = [
            _episode_payload(item.value)
            for item in self._search_all(
                self._episodes_namespace(),
                filter=group_filter,
            )
        ]
        if not episodes:
            return
        probe = build_semantic_preference(episodes)
        namespace = self._preference_namespace(
            probe.contract_type,
            probe.review_position,
        )
        existing = self.store.get(namespace, probe.preference_id)
        last_used_at = (
            str(existing.value.get("last_used_at", ""))
            if existing is not None
            else ""
        )
        preference = build_semantic_preference(
            episodes,
            last_used_at=last_used_at,
        ).to_dict()
        self.store.put(
            namespace,
            preference["preference_id"],
            _stored_preference(preference, indexed=False),
            index=False,
        )

    def _ensure_preference_indexes(
        self,
        namespaces: tuple[tuple[str, ...], ...],
    ) -> None:
        operations = []
        for namespace in namespaces:
            for item in self._search_all(namespace):
                preference = _preference_payload(item.value)
                content_hash = _preference_content_hash(preference)
                if item.value.get("indexed_content_hash") == content_hash:
                    continue
                value = _stored_preference(preference, indexed=True)
                operations.append(
                    PutOp(
                        namespace,
                        preference["preference_id"],
                        value,
                        index=["embedding_text"],
                    )
                )
        if operations:
            self.store.batch(operations)

    def _search_all(
        self,
        namespace: tuple[str, ...],
        *,
        filter: dict | None = None,
    ) -> list:
        results = []
        offset = 0
        page_size = 100
        while True:
            page = self.store.search(
                namespace,
                filter=filter,
                limit=page_size,
                offset=offset,
                refresh_ttl=False,
            )
            results.extend(page)
            if len(page) < page_size:
                return results
            offset += page_size

    def _preference_namespaces(
        self,
        contract_type: str,
        review_position: str,
    ) -> tuple[tuple[str, ...], ...]:
        positions = [review_position]
        if review_position:
            positions.append("")
        return tuple(
            self._preference_namespace(contract_type, position)
            for position in positions
        )

    @staticmethod
    def _episodes_namespace() -> tuple[str, ...]:
        return (MEMORY_NAMESPACE_ROOT, "episodes")

    @staticmethod
    def _system_namespace() -> tuple[str, ...]:
        return (MEMORY_NAMESPACE_ROOT, "system")

    @staticmethod
    def _idempotency_namespace() -> tuple[str, ...]:
        return (MEMORY_NAMESPACE_ROOT, "idempotency")

    def _save_idempotency_marker(
        self,
        idempotency_key: str,
        episode: dict,
    ) -> None:
        self.store.put(
            self._idempotency_namespace(),
            _idempotency_store_key(idempotency_key),
            {
                "memory_id": str(episode["memory_id"]),
                "episode": _episode_payload(episode),
            },
            index=False,
        )

    @staticmethod
    def _preference_namespace(
        contract_type: str,
        review_position: str,
    ) -> tuple[str, ...]:
        return (
            MEMORY_NAMESPACE_ROOT,
            "preferences",
            _scope_label(contract_type),
            _scope_label(review_position),
        )


class _BGEStoreEmbeddings(Embeddings):
    def __init__(self, provider: BGEEmbeddingProvider):
        self.provider = provider

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        response = self.provider.embed(
            EmbeddingRequest([str(text) for text in texts]),
            call_records=_EMBEDDING_CALL_RECORDS.get(),
        )
        return response.vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _configure_connection(connection: Connection, *, schema: str) -> None:
    connection.execute(f'SET search_path TO "{schema}", public')
    connection.execute(
        f"SET hnsw.ef_search = {MEMORY_STORE_HNSW_EF_SEARCH}"
    )
    connection.execute("SET hnsw.iterative_scan = strict_order")


def _validated_schema(schema: str) -> str:
    value = str(schema).strip()
    if not _SCHEMA_PATTERN.fullmatch(value):
        raise ValueError("Memory Store schema name is invalid")
    return value


def _scope_label(value: str) -> str:
    text = str(value)
    if not text:
        return "scope_empty"
    return f"scope_{sha256(text.encode('utf-8')).hexdigest()[:24]}"


def _idempotency_store_key(idempotency_key: str) -> str:
    return sha256(idempotency_key.encode("utf-8")).hexdigest()


def _preference_content_hash(preference: dict) -> str:
    text = build_preference_embedding_text(preference)
    return sha256(text.encode("utf-8")).hexdigest()


def _stored_preference(preference: dict, *, indexed: bool) -> dict:
    value = dict(preference)
    value["embedding_text"] = build_preference_embedding_text(preference)
    content_hash = _preference_content_hash(preference)
    value["embedding_content_hash"] = content_hash
    value["indexed_content_hash"] = content_hash if indexed else ""
    return value


def _preference_payload(value: dict) -> dict:
    return {
        key: item
        for key, item in value.items()
        if key
        not in {
            "embedding_text",
            "embedding_content_hash",
            "indexed_content_hash",
        }
    }


def _episode_payload(value: dict) -> dict:
    return {
        key: item
        for key, item in value.items()
        if key != "idempotency_key"
    }


def _bounded_score(value) -> float:
    if value is None:
        return 0.0
    score = float(value)
    return min(1.0, max(-1.0, score))


def _legacy_episode(row: dict) -> dict:
    return {
        "memory_id": str(row["memory_id"]),
        "memory_type": str(row["memory_type"]),
        "contract_type": str(row["contract_type"]),
        "clause_type": str(row["clause_type"]),
        "risk_type": str(row["risk_type"]),
        "review_position": str(row["review_position"]),
        "user_action": str(row["user_action"]),
        "original_severity": str(row["original_severity"]),
        "final_severity": str(row["final_severity"]),
        "original_suggestion": str(row["original_suggestion"]),
        "final_suggestion": str(row["final_suggestion"]),
        "ignore_reason": str(row["ignore_reason"]),
        "source_finding_id": str(row["source_finding_id"]),
        "source_clause_id": str(row["source_clause_id"]),
        "include_in_report": bool(row["include_in_report"]),
        "created_at": str(row["created_at"]),
    }


def _legacy_preference(row: dict) -> dict:
    return {
        "preference_id": str(row["preference_id"]),
        "memory_type": "semantic_preference",
        "contract_type": str(row["contract_type"]),
        "clause_type": str(row["clause_type"]),
        "risk_type": str(row["risk_type"]),
        "review_position": str(row["review_position"]),
        "support_count": int(row["support_count"]),
        "opposition_count": int(row["opposition_count"]),
        "source_memory_ids": _json_list(row["source_memory_ids_json"]),
        "variants": _json_list(row["variants_json"]),
        "conflict_status": str(row["conflict_status"]),
        "base_confidence": float(row["base_confidence"]),
        "confidence": float(row["confidence"]),
        "lifecycle_status": str(row["lifecycle_status"]),
        "last_feedback_at": str(row["last_feedback_at"]),
        "last_used_at": str(row["last_used_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _json_list(value) -> list:
    if isinstance(value, list):
        return list(value)
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ValueError("Memory migration expected a JSON list")
    return parsed
