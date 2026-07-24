from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from config import Settings
from db.postgres_models import Base
from providers.bge_provider import (
    BGE_EMBEDDING_DIMENSION,
    BGEEmbeddingProvider,
    BGEReranker,
)
from providers.embedding_provider import EmbeddingRequest
from providers.embedding_provider import EmbeddingProviderError
from services.clause_index_service import (
    _validated_request,
    retrieve_related_clauses,
)
from services.postgres_clause_retrieval import (
    HNSW_EF_SEARCH,
    KEYWORD_RECALL_LIMIT,
    RERANK_LIMIT,
    RRF_K,
    VECTOR_RECALL_LIMIT,
    PostgresHybridClauseRetriever,
    _rrf_merge,
    _validated_cached_vector,
)
from services.retrieval_runtime import (
    bind_related_clause_retriever,
    reset_related_clause_retriever,
)


class BGEProviderTest(unittest.TestCase):
    def test_embedding_is_normalized_and_fixed_to_1024_dimensions(self):
        model = Mock()
        model.encode.return_value = [
            [2.0, 0.0, *([0.0] * (BGE_EMBEDDING_DIMENSION - 2))]
        ]
        factory = Mock(return_value=model)
        records = []

        with TemporaryDirectory() as temp_dir:
            provider = BGEEmbeddingProvider(
                Settings(bge_cache_dir=temp_dir),
                model_factory=factory,
            )
            response = provider.embed(
                EmbeddingRequest(["contract clause"]),
                call_records=records,
            )

        self.assertEqual(len(response.vectors[0]), 1024)
        self.assertEqual(response.vectors[0][0], 1.0)
        self.assertEqual(records[0].mode, "bge_local")
        self.assertEqual(records[0].vector_dimension, 1024)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["max_length"], 1024)

    def test_reranker_uses_fixed_512_token_limit(self):
        model = Mock()
        model.predict.return_value = [0.1, 0.9]
        factory = Mock(return_value=model)

        with TemporaryDirectory() as temp_dir:
            reranker = BGEReranker(
                Settings(bge_cache_dir=temp_dir),
                model_factory=factory,
            )
            scores = reranker.score("query", ["first", "second"])

        self.assertEqual(scores, [0.1, 0.9])
        self.assertEqual(factory.call_args.kwargs["max_length"], 512)
        self.assertEqual(
            factory.call_args.kwargs["revision"],
            "2cfc18c9415c912f9d8155881c133215df768a70",
        )

    def test_embedding_rejects_result_count_mismatch(self):
        model = Mock()
        model.encode.return_value = []

        with TemporaryDirectory() as temp_dir:
            provider = BGEEmbeddingProvider(
                Settings(bge_cache_dir=temp_dir),
                model_factory=Mock(return_value=model),
            )
            with self.assertRaises(EmbeddingProviderError):
                provider.embed(EmbeddingRequest(["contract clause"]))


class RetrievalRuntimeTest(unittest.TestCase):
    def test_tool_dispatches_task_id_to_bound_postgres_retriever(self):
        retriever = Mock()
        retriever.retrieve.return_value = [{"clause_id": "CL-002"}]
        tokens = bind_related_clause_retriever(retriever, "task_pgvector")
        try:
            result = retrieve_related_clauses(_tool_input())
        finally:
            reset_related_clause_retriever(tokens)

        self.assertEqual(result, [{"clause_id": "CL-002"}])
        self.assertEqual(
            retriever.retrieve.call_args.args[0],
            "task_pgvector",
        )


class PostgresHybridRetrievalTest(unittest.TestCase):
    def test_embedding_index_handles_core_connection_row_shape(self):
        redis = _FakeRedis()
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        read_connection = engine.connect.return_value.__enter__.return_value
        read_connection.execute.return_value.all.return_value = [
            ("CL-001", _clause("CL-001"), None),
        ]
        write_connection = engine.begin.return_value.__enter__.return_value
        provider = Mock()
        provider.mode = "bge_local"
        retriever = PostgresHybridClauseRetriever(
            engine,
            redis,
            Settings(),
            embedding_provider=provider,
            reranker=Mock(),
        )
        retriever._embeddings_for_texts = Mock(
            return_value=[
                [1.0, *([0.0] * (BGE_EMBEDDING_DIMENSION - 1))]
            ]
        )

        corpus_hash, clause_count = retriever._ensure_clause_embeddings(
            "task_pgvector",
            [],
        )

        self.assertEqual(clause_count, 1)
        self.assertEqual(len(corpus_hash), 64)
        write_connection.execute.assert_called_once()

    def test_rrf_uses_fixed_k_and_keeps_both_recall_ranks(self):
        vector = [
            {"clause": _clause("CL-002"), "vector_similarity": 0.8},
            {"clause": _clause("CL-003"), "vector_similarity": 0.7},
        ]
        keyword = [
            {"clause": _clause("CL-003"), "keyword_score": 0.9},
            {"clause": _clause("CL-004"), "keyword_score": 0.8},
        ]

        merged = _rrf_merge(vector, keyword)

        self.assertEqual(RRF_K, 60)
        self.assertEqual(merged[0]["clause"]["clause_id"], "CL-003")
        self.assertEqual(merged[0]["vector_rank"], 2)
        self.assertEqual(merged[0]["keyword_rank"], 1)
        self.assertEqual(
            merged[0]["retrieval_sources"],
            ["vector", "pg_trgm"],
        )

    def test_pipeline_reranks_top_20_and_returns_at_most_five(self):
        redis = _FakeRedis()
        engine = Mock()
        engine.dialect.name = "postgresql"
        settings = Settings()
        reranker = Mock()
        reranker.score.side_effect = lambda query, documents: [
            float(index) for index in range(len(documents))
        ]
        retriever = _StubPostgresRetriever(
            engine,
            redis,
            settings,
            reranker=reranker,
        )

        result = retriever.retrieve(
            "task_pgvector",
            _validated_request(_tool_input(limit=99)),
            call_records=[],
        )

        self.assertEqual(VECTOR_RECALL_LIMIT, 20)
        self.assertEqual(KEYWORD_RECALL_LIMIT, 20)
        self.assertEqual(RERANK_LIMIT, 20)
        self.assertEqual(HNSW_EF_SEARCH, 80)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["clause_id"], "CL-021")
        self.assertEqual(result[0]["embedding_model"], "BAAI/bge-m3")
        self.assertEqual(result[0]["vector_dimension"], 1024)
        context = result[0]["query_context"]
        self.assertEqual(context["rrf_k"], 60)
        self.assertEqual(context["hnsw"]["m"], 16)
        self.assertEqual(context["hnsw"]["ef_construction"], 64)
        self.assertEqual(context["hnsw"]["ef_search"], 80)
        self.assertEqual(context["hnsw"]["iterative_scan"], "strict_order")
        self.assertEqual(context["rerank_top_k"], 20)
        self.assertEqual(
            context["embedding_revision"],
            settings.bge_embedding_revision,
        )
        self.assertEqual(redis.last_ttl, 3600)

    def test_malformed_retrieval_cache_is_ignored(self):
        retriever, reranker = _stub_retriever()
        retriever._cache_get_json = Mock(
            return_value=[{"clause_id": "CL-002"}],
        )

        result = retriever.retrieve(
            "task_pgvector",
            _validated_request(_tool_input()),
            call_records=[],
        )

        self.assertTrue(result)
        reranker.score.assert_called_once()

    def test_vector_recall_enables_filtered_hnsw_iteration(self):
        redis = _FakeRedis()
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        connection = engine.begin.return_value.__enter__.return_value
        connection.execute.return_value.all.return_value = []
        retriever = PostgresHybridClauseRetriever(
            engine,
            redis,
            Settings(),
            embedding_provider=Mock(),
            reranker=Mock(),
        )

        result = retriever._vector_recall(
            "task_pgvector",
            "CL-001",
            [1.0, *([0.0] * (BGE_EMBEDDING_DIMENSION - 1))],
        )

        self.assertEqual(result, [])
        self.assertEqual(
            [
                call.args[0]
                for call in connection.exec_driver_sql.call_args_list
            ],
            [
                "SET LOCAL hnsw.iterative_scan = strict_order",
                "SET LOCAL hnsw.ef_search = 80",
            ],
        )

    def test_cached_vectors_must_be_finite_and_are_normalized(self):
        vector = _validated_cached_vector(
            [2.0, *([0.0] * (BGE_EMBEDDING_DIMENSION - 1))]
        )

        self.assertEqual(vector[0], 1.0)
        self.assertIsNone(
            _validated_cached_vector(
                [float("nan"), *([0.0] * (BGE_EMBEDDING_DIMENSION - 1))]
            )
        )

    def test_metadata_declares_trigram_and_hnsw_indexes(self):
        clauses = Base.metadata.tables["app.clauses"]
        embeddings = Base.metadata.tables["app.clause_embeddings"]
        clause_index = next(
            index for index in clauses.indexes
            if index.name == "idx_clauses_search_trgm"
        )
        vector_index = next(
            index for index in embeddings.indexes
            if index.name == "idx_clause_embeddings_hnsw"
        )

        self.assertEqual(clause_index.dialect_options["postgresql"]["using"], "gin")
        self.assertEqual(
            vector_index.dialect_options["postgresql"]["using"],
            "hnsw",
        )
        self.assertEqual(
            vector_index.dialect_options["postgresql"]["with"],
            {"m": 16, "ef_construction": 64},
        )


class _StubPostgresRetriever(PostgresHybridClauseRetriever):
    def _ensure_clause_embeddings(self, task_id, call_records):
        return "corpus-hash", 22

    def _embeddings_for_texts(self, texts, call_records):
        return [[1.0, *([0.0] * (BGE_EMBEDDING_DIMENSION - 1))]]

    def _vector_recall(self, task_id, current_clause_id, query_vector):
        return [
            {
                "clause": _clause(f"CL-{index + 2:03d}"),
                "vector_similarity": 1.0 - (index / 100),
            }
            for index in range(20)
        ]

    def _keyword_recall(self, task_id, current_clause_id, query):
        return [
            {
                "clause": _clause(f"CL-{index + 2:03d}"),
                "keyword_score": 1.0 - (index / 100),
            }
            for index in range(20)
        ]


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.last_ttl = None

    def get(self, key):
        return self.values.get(key)

    def mget(self, keys):
        return [self.values.get(key) for key in keys]

    def setex(self, key, ttl, value):
        self.last_ttl = ttl
        self.values[key] = value

    def close(self):
        return None


def _stub_retriever():
    redis = _FakeRedis()
    engine = Mock()
    engine.dialect.name = "postgresql"
    settings = Settings()
    reranker = Mock()
    reranker.score.side_effect = lambda query, documents: [
        float(index) for index in range(len(documents))
    ]
    return (
        _StubPostgresRetriever(
            engine,
            redis,
            settings,
            reranker=reranker,
        ),
        reranker,
    )


def _tool_input(limit=3):
    current = _clause("CL-001")
    return {
        "contract_type": "NDA",
        "current_clause": current,
        "clauses": [current, _clause("CL-002")],
        "risk_type": "confidentiality scope",
        "playbook_check_point": "check whether scope is identifiable",
        "limit": limit,
        "embedding_cache": {},
    }


def _clause(clause_id):
    return {
        "clause_id": clause_id,
        "title": f"Clause {clause_id}",
        "clause_type": "definition",
        "text": f"Confidential information definition {clause_id}",
        "key_fields": {"scope": ["business information"]},
        "source_location": {"start_order": int(clause_id.split("-")[-1])},
    }


if __name__ == "__main__":
    unittest.main()
