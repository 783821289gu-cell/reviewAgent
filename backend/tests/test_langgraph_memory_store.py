import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, text


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from config import Settings
from providers.embedding_provider import (
    EmbeddingCallMetadata,
    EmbeddingResponse,
)
from services.langgraph_memory_store import LangGraphPostgresMemoryStore
from services.memory_runtime import bind_memory_store, reset_memory_store
from services.memory_service import retrieve_memory, write_memory


class LangGraphMemoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = _FakeStore()
        self.pool = _FakePool()
        self.provider = _FakeEmbeddingProvider()
        self.manager = LangGraphPostgresMemoryStore(
            self.store,
            self.pool,
            self.provider,
        )

    def test_bound_store_preserves_idempotency_conflict_and_lifecycle_rules(self):
        token = bind_memory_store(self.manager)
        try:
            first = write_memory(
                {
                    "human_feedback": _feedback(
                        "Suggestion A",
                        created_at="2026-01-01T00:00:00+00:00",
                    ),
                    "idempotency_key": "feedback:stable-a",
                }
            )
            repeated = write_memory(
                {
                    "human_feedback": _feedback(
                        "Changed payload must not overwrite",
                        created_at="2026-01-02T00:00:00+00:00",
                    ),
                    "idempotency_key": "feedback:stable-a",
                }
            )
            explicit_repeat = write_memory(
                {
                    "human_feedback": {
                        **_feedback(
                            "A second explicit ID must not bypass idempotency",
                            created_at="2026-01-02T00:00:00+00:00",
                        ),
                        "memory_id": "MEM-DIFFERENT",
                    },
                    "idempotency_key": "feedback:stable-a",
                }
            )
            second = write_memory(
                {
                    "human_feedback": _feedback(
                        "Suggestion B",
                        created_at="2026-01-03T00:00:00+00:00",
                    ),
                    "idempotency_key": "feedback:stable-b",
                }
            )
            memories = retrieve_memory(
                _retrieval_input(now="2026-01-04T00:00:00+00:00")
            )
        finally:
            reset_memory_store(token)

        self.assertEqual(repeated, first)
        self.assertEqual(explicit_repeat, first)
        self.assertNotEqual(second["memory_id"], first["memory_id"])
        self.assertEqual(self.store.count_prefix(("review_memory", "episodes")), 2)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["conflict_status"], "conflicted")
        self.assertEqual(memories[0]["support_count"], 2)
        self.assertEqual(len(memories[0]["source_memory_ids"]), 2)
        self.assertFalse(memories[0]["can_influence_suggestion"])
        self.assertEqual(
            memories[0]["memory_source"],
            "langgraph_postgres_vector_memory",
        )
        self.assertEqual(memories[0]["embedding_mode"], "bge_local")

    def test_store_hard_filters_contract_and_review_position(self):
        token = bind_memory_store(self.manager)
        try:
            write_memory(
                {
                    "human_feedback": _feedback(
                        "Party A only",
                        review_position="party_a",
                    ),
                    "idempotency_key": "feedback:party-a",
                }
            )
            result = retrieve_memory(
                _retrieval_input(review_position="party_b")
            )
        finally:
            reset_memory_store(token)

        self.assertEqual(result, [])

    def test_legacy_migration_is_idempotent_and_keeps_stored_lifecycle(self):
        manager = _LegacyRowsMemoryStore(
            self.store,
            self.pool,
            self.provider,
        )

        first = manager.migrate_legacy_app_memory()
        second = manager.migrate_legacy_app_memory()

        self.assertEqual(first, second)
        self.assertEqual(first["migrated_episode_count"], 1)
        self.assertEqual(first["migrated_preference_count"], 1)
        preference = next(
            item.value
            for item in self.store.search(
                ("review_memory", "preferences"),
                limit=10,
            )
        )
        self.assertEqual(preference["lifecycle_status"], "expired")
        self.assertEqual(preference["confidence"], 0.275)


@unittest.skipUnless(
    os.getenv("REVIEW_AGENT_RUN_EXTERNAL_TESTS") == "1",
    "external PostgreSQL integration is opt-in",
)
class LangGraphPostgresMemoryIntegrationTest(unittest.TestCase):
    def test_real_store_persists_vectors_and_retrieves_preference(self):
        settings = Settings()
        schema = f"test_memory_{uuid4().hex[:12]}"
        manager = None
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        try:
            manager = LangGraphPostgresMemoryStore.postgres(
                settings.database_url,
                settings,
                embedding_provider=_FakeEmbeddingProvider(),
                schema=schema,
                migrate_legacy=False,
            )
            token = bind_memory_store(manager)
            try:
                first = write_memory(
                    {
                        "human_feedback": _feedback("Use approved wording."),
                        "idempotency_key": "feedback:postgres-real",
                    }
                )
                repeated = write_memory(
                    {
                        "human_feedback": _feedback("Use approved wording."),
                        "idempotency_key": "feedback:postgres-real",
                    }
                )
                memories = retrieve_memory(_retrieval_input())
            finally:
                reset_memory_store(token)

            with engine.connect() as connection:
                vector_count = connection.execute(
                    text(f'SELECT count(*) FROM "{schema}".store_vectors')
                ).scalar_one()
            self.assertEqual(first, repeated)
            self.assertGreater(vector_count, 0)
            self.assertEqual(len(memories), 1)
            self.assertEqual(
                memories[0]["memory_source"],
                "langgraph_postgres_vector_memory",
            )
            self.assertTrue(memories[0]["can_influence_suggestion"])
        finally:
            if manager is not None:
                manager.close()
            with engine.begin() as connection:
                connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
            engine.dispose()

    def test_real_store_migrates_legacy_rows_once(self):
        settings = Settings()
        schema = f"test_memory_migration_{uuid4().hex[:8]}"
        manager = None
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                connection.execute(
                    text(
                        f"""
                        CREATE TABLE "{schema}".memory_items (
                            memory_id text PRIMARY KEY,
                            memory_type text NOT NULL,
                            contract_type text NOT NULL,
                            clause_type text NOT NULL,
                            risk_type text NOT NULL,
                            review_position text NOT NULL,
                            user_action text NOT NULL,
                            original_severity text NOT NULL,
                            final_severity text NOT NULL,
                            original_suggestion text NOT NULL,
                            final_suggestion text NOT NULL,
                            ignore_reason text NOT NULL,
                            source_finding_id text NOT NULL,
                            source_clause_id text NOT NULL,
                            include_in_report boolean NOT NULL,
                            created_at text NOT NULL,
                            idempotency_key text
                        );
                        CREATE TABLE "{schema}".semantic_preferences (
                            preference_id text PRIMARY KEY,
                            contract_type text NOT NULL,
                            clause_type text NOT NULL,
                            risk_type text NOT NULL,
                            review_position text NOT NULL,
                            support_count integer NOT NULL,
                            opposition_count integer NOT NULL,
                            source_memory_ids_json jsonb NOT NULL,
                            variants_json jsonb NOT NULL,
                            conflict_status text NOT NULL,
                            base_confidence double precision NOT NULL,
                            confidence double precision NOT NULL,
                            lifecycle_status text NOT NULL,
                            last_feedback_at text NOT NULL,
                            last_used_at text NOT NULL,
                            updated_at text NOT NULL
                        )
                        """
                    )
                )
                connection.exec_driver_sql(
                    f"""
                        INSERT INTO "{schema}".memory_items VALUES (
                            'MEM-REAL-LEGACY', 'human_feedback', 'NDA',
                            'definition', 'scope', 'party_a',
                            'update_suggestion', '中', '中',
                            'Original.', 'Approved wording.', '',
                            'RISK-REAL', 'CL-REAL', true,
                            '2026-01-01T00:00:00+00:00',
                            'feedback:real-legacy'
                        );
                        INSERT INTO "{schema}".semantic_preferences VALUES (
                            'PREF-REAL-LEGACY', 'NDA', 'definition', 'scope',
                            'party_a', 1, 0, '["MEM-REAL-LEGACY"]'::jsonb,
                            '[{{"stance":"support","final_severity":"中",
                               "final_suggestion":"Approved wording.","count":1,
                               "source_memory_ids":["MEM-REAL-LEGACY"]}}]'::jsonb,
                            'aligned', 0.55, 0.55, 'active',
                            '2026-01-01T00:00:00+00:00', '',
                            '2026-01-01T00:00:00+00:00'
                        )
                        """
                )

            manager = LangGraphPostgresMemoryStore.postgres(
                settings.database_url,
                settings,
                embedding_provider=_FakeEmbeddingProvider(),
                schema=schema,
                migrate_legacy=True,
            )
            first = manager.migrate_legacy_app_memory()
            second = manager.migrate_legacy_app_memory()

            self.assertEqual(first, second)
            self.assertEqual(first["source_episode_count"], 1)
            self.assertEqual(first["source_preference_count"], 1)
            episode = manager.store.get(
                ("review_memory", "episodes"),
                "MEM-REAL-LEGACY",
            )
            self.assertIsNotNone(episode)
            self.assertEqual(
                episode.value["final_suggestion"],
                "Approved wording.",
            )
        finally:
            if manager is not None:
                manager.close()
            with engine.begin() as connection:
                connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
            engine.dispose()


class _LegacyRowsMemoryStore(LangGraphPostgresMemoryStore):
    def _legacy_rows(self):
        episode = _feedback("Legacy suggestion.")
        episode.update(
            {
                "memory_id": "MEM-LEGACY",
                "memory_type": "human_feedback",
            }
        )
        preference = {
            "preference_id": "PREF-LEGACY",
            "contract_type": "NDA",
            "clause_type": "definition",
            "risk_type": "scope",
            "review_position": "party_a",
            "support_count": 1,
            "opposition_count": 0,
            "source_memory_ids_json": ["MEM-LEGACY"],
            "variants_json": [
                {
                    "stance": "support",
                    "final_severity": "中",
                    "final_suggestion": "Legacy suggestion.",
                    "count": 1,
                    "source_memory_ids": ["MEM-LEGACY"],
                }
            ],
            "conflict_status": "aligned",
            "base_confidence": 0.55,
            "confidence": 0.275,
            "lifecycle_status": "expired",
            "last_feedback_at": "2024-01-01T00:00:00+00:00",
            "last_used_at": "",
            "updated_at": "2025-01-01T00:00:00+00:00",
        }
        return [episode], [preference]


class _FakeEmbeddingProvider:
    mode = "bge_local"
    model = "BAAI/bge-m3"

    def embed(self, request, call_records=None):
        vectors = [
            [1.0, *([0.0] * 1023)]
            for _text in request.texts
        ]
        metadata = EmbeddingCallMetadata(
            mode=self.mode,
            model=self.model,
            provider_request_id="",
            input_count=len(request.texts),
            vector_dimension=1024,
            latency_ms=0,
            error_type="",
            cost_status="test",
        )
        if call_records is not None:
            call_records.append(metadata)
        return EmbeddingResponse(vectors=vectors, metadata=metadata)


class _FakePool:
    def close(self):
        return None


class _FakeItem:
    def __init__(self, namespace, key, value, score=None):
        self.namespace = namespace
        self.key = key
        self.value = dict(value)
        self.score = score


class _FakeStore:
    def __init__(self):
        self.values = {}

    def get(self, namespace, key, **kwargs):
        value = self.values.get((tuple(namespace), key))
        if value is None:
            return None
        return _FakeItem(tuple(namespace), key, value)

    def put(self, namespace, key, value, index=None, **kwargs):
        self.values[(tuple(namespace), key)] = dict(value)

    def search(
        self,
        namespace_prefix,
        *,
        query=None,
        filter=None,
        limit=10,
        offset=0,
        **kwargs,
    ):
        prefix = tuple(namespace_prefix)
        matches = []
        for (namespace, key), value in self.values.items():
            if namespace[: len(prefix)] != prefix:
                continue
            if filter and any(value.get(name) != expected for name, expected in filter.items()):
                continue
            score = 0.9 if query else None
            matches.append(_FakeItem(namespace, key, value, score))
        matches.sort(key=lambda item: item.key)
        return matches[offset : offset + limit]

    def batch(self, operations):
        results = []
        for operation in operations:
            if hasattr(operation, "namespace_prefix"):
                results.append(
                    self.search(
                        operation.namespace_prefix,
                        query=operation.query,
                        filter=operation.filter,
                        limit=operation.limit,
                        offset=operation.offset,
                    )
                )
            else:
                self.put(
                    operation.namespace,
                    operation.key,
                    operation.value,
                    index=operation.index,
                )
                results.append(None)
        return results

    def count_prefix(self, prefix):
        prefix = tuple(prefix)
        return sum(
            namespace[: len(prefix)] == prefix
            for namespace, _key in self.values
        )


def _feedback(
    suggestion,
    *,
    review_position="party_a",
    created_at="2026-01-01T00:00:00+00:00",
):
    return {
        "contract_type": "NDA",
        "clause_type": "definition",
        "risk_type": "scope",
        "review_position": review_position,
        "user_action": "update_suggestion",
        "original_severity": "中",
        "final_severity": "中",
        "original_suggestion": "Original suggestion.",
        "final_suggestion": suggestion,
        "ignore_reason": "",
        "source_finding_id": "RISK-001",
        "source_clause_id": "CL-001",
        "include_in_report": True,
        "created_at": created_at,
    }


def _retrieval_input(
    *,
    review_position="party_a",
    now="2026-01-02T00:00:00+00:00",
):
    return {
        "contract_type": "NDA",
        "clause": {
            "clause_id": "CL-001",
            "clause_type": "definition",
            "title": "Confidential information",
            "text": "The definition must be limited to an identifiable scope.",
            "key_fields": {"scope": ["business information"]},
        },
        "risk_type": "scope",
        "review_position": review_position,
        "memory_items": [],
        "limit": 3,
        "now": now,
        "embedding_cache": {},
    }


if __name__ == "__main__":
    unittest.main()
