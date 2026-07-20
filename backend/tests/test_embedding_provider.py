import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_DIR))

from config import Settings
from models.review import ReviewPosition, ReviewStatus
from providers.embedding_provider import (
    EmbeddingCallMetadata,
    EmbeddingProviderError,
    EmbeddingRequest,
    EmbeddingResponse,
    LocalSparseEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    create_embedding_provider,
)
from services.clause_index_service import retrieve_related_clauses
from services.embedding_service import (
    cosine_similarity,
    embed_texts,
)
from services.evaluation_service import _docx_bytes_from_text
from services.event_service import ReviewEventStore
from services.log_service import invoke_tool
from services.review_service import ReviewOrchestratorAgent
from tools.contracts import tool_contracts
from tools.registry import tool_registry


class EmbeddingProviderTest(unittest.TestCase):
    def test_local_sparse_vectors_are_fixed_length_deterministic_and_offline(self):
        first = LocalSparseEmbeddingProvider().embed(
            EmbeddingRequest(["保密信息仅用于项目评估", ""])
        ).vectors
        second = LocalSparseEmbeddingProvider().embed(
            EmbeddingRequest(["保密信息仅用于项目评估", ""])
        ).vectors

        self.assertEqual(first, second)
        self.assertEqual(len(first[0]), 256)
        self.assertEqual(len(first[1]), 256)
        self.assertTrue(any(value != 0 for value in first[0]))

    def test_openai_compatible_provider_records_metadata_without_secret_or_text(self):
        api_key = "embedding-secret"
        sensitive_text = "未公开并购交易资料"

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], f"Bearer {api_key}")
            body = json.loads(request.content)
            self.assertEqual(body["input"], [sensitive_text, "第二条"])
            return httpx.Response(
                200,
                request=request,
                headers={"x-request-id": "req-embedding-success"},
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                        {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                    ]
                },
            )

        calls = []
        provider = OpenAICompatibleEmbeddingProvider(
            _external_settings(embedding_api_key=api_key),
            transport=httpx.MockTransport(handler),
        )
        response = provider.embed(
            EmbeddingRequest([sensitive_text, "第二条"]),
            call_records=calls,
        )

        self.assertEqual(response.vectors, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        self.assertEqual(calls[0].provider_request_id, "req-embedding-success")
        self.assertEqual(calls[0].vector_dimension, 3)
        metadata_text = json.dumps(calls[0].to_dict(), ensure_ascii=False)
        self.assertNotIn(api_key, metadata_text)
        self.assertNotIn(sensitive_text, metadata_text)

    def test_openai_compatible_provider_rejects_mixed_vector_dimensions(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": [
                        {"index": 0, "embedding": [1.0, 0.0]},
                        {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    ]
                },
            )

        calls = []
        provider = OpenAICompatibleEmbeddingProvider(
            _external_settings(),
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaises(EmbeddingProviderError) as raised:
            provider.embed(EmbeddingRequest(["第一条", "第二条"]), call_records=calls)

        self.assertEqual(raised.exception.error_type, "schema_error")
        self.assertEqual(calls[0].error_type, "schema_error")

    def test_cosine_similarity_rejects_dimension_mismatch(self):
        with self.assertRaisesRegex(ValueError, "dimensions must match"):
            cosine_similarity([1.0, 0.0], [1.0])

    def test_external_failure_enters_retrieval_failed_without_lexical_fallback(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                request=request,
                headers={"x-request-id": "req-embedding-rate-limit"},
            )

        provider = OpenAICompatibleEmbeddingProvider(
            _external_settings(),
            transport=httpx.MockTransport(handler),
        )
        with patch("services.embedding_service.create_embedding_provider", return_value=provider):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="nda.docx",
                file_type="docx",
                content=_docx_bytes_from_text(
                    "保密协议\n"
                    "1. 定义\n"
                    "保密信息包括披露方提供的商业信息和技术资料。\n"
                    "2. 保密义务\n"
                    "接收方不得向第三方披露保密信息。"
                ),
                review_position=ReviewPosition.PARTY_A,
            )

        self.assertEqual(state.status, ReviewStatus.RETRIEVAL_FAILED)
        failed_logs = [
            item
            for item in state.logs or []
            if item.get("tool_name") == "retrieve_related_clauses"
            and item.get("status") == "failed"
        ]
        self.assertEqual(len(failed_logs), 1)
        summary = json.loads(failed_logs[0]["token_cost_summary"])
        call = summary["embedding_calls"][0]
        self.assertEqual(call["model"], "test-embedding-model")
        self.assertEqual(call["provider_request_id"], "req-embedding-rate-limit")
        self.assertEqual(call["error_type"], "rate_limit")
        self.assertNotIn("lexical_fallback", failed_logs[0]["token_cost_summary"])
        failed_log_text = json.dumps(failed_logs[0], ensure_ascii=False)
        self.assertNotIn("test-embedding-key", failed_log_text)
        self.assertNotIn("商业信息和技术资料", failed_log_text)

    def test_external_success_is_recorded_by_tool_log_without_sensitive_text(self):
        sensitive_text = "仅用于未公开并购项目评估"

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            vectors = [
                {"index": index, "embedding": [1.0, float(index), 0.0]}
                for index in range(len(body["input"]))
            ]
            return httpx.Response(
                200,
                request=request,
                headers={"x-request-id": "req-tool-success"},
                json={"data": vectors},
            )

        current_clause = {
            "clause_id": "CL-001",
            "title": "使用限制",
            "clause_type": "使用限制",
            "text": sensitive_text,
            "key_fields": {"use_purpose": ["项目评估"]},
            "source_location": {"start_order": 1},
        }
        candidate = {
            "clause_id": "CL-002",
            "title": "允许披露",
            "clause_type": "允许披露",
            "text": "仅可向确有知悉必要的顾问披露。",
            "key_fields": {"permitted_disclosure_targets": ["顾问"]},
            "source_location": {"start_order": 2},
        }
        provider = OpenAICompatibleEmbeddingProvider(
            _external_settings(),
            transport=httpx.MockTransport(handler),
        )
        logs = []

        with patch("services.embedding_service.create_embedding_provider", return_value=provider):
            related = invoke_tool(
                "task-embedding-success",
                tool_registry,
                "retrieve_related_clauses",
                {
                    "contract_type": "NDA",
                    "current_clause": current_clause,
                    "clauses": [current_clause, candidate],
                    "risk_type": "使用目的或使用限制不清",
                    "playbook_check_point": "检查是否限制使用目的。",
                    "limit": 1,
                    "embedding_cache": {},
                },
                logs,
            )

        self.assertEqual(related[0]["embedding_model"], "test-embedding-model")
        summary = json.loads(logs[0].token_cost_summary)
        call = summary["embedding_calls"][0]
        self.assertEqual(call["provider_request_id"], "req-tool-success")
        self.assertEqual(call["model"], "test-embedding-model")
        self.assertEqual(call["input_count"], 2)
        self.assertEqual(call["vector_dimension"], 3)
        log_text = json.dumps(logs[0].to_dict(), ensure_ascii=False)
        self.assertNotIn("test-embedding-key", log_text)
        self.assertNotIn(sensitive_text, log_text)

    def test_embedding_cache_only_requests_missing_texts(self):
        provider = CountingEmbeddingProvider()
        cache = {}

        first = embed_texts(["query", "clause-a", "clause-b"], cache=cache, provider=provider)
        second = embed_texts(["query", "clause-a", "clause-c"], cache=cache, provider=provider)

        self.assertEqual(len(first.vectors[0]), 4)
        self.assertEqual(len(second.vectors[0]), 4)
        self.assertEqual(provider.requested_texts, [
            ["query", "clause-a", "clause-b"],
            ["clause-c"],
        ])

    def test_provider_mode_factory_is_explicit(self):
        self.assertIsInstance(
            create_embedding_provider(Settings(embedding_mode="local_sparse")),
            LocalSparseEmbeddingProvider,
        )
        self.assertIsInstance(
            create_embedding_provider(_external_settings()),
            OpenAICompatibleEmbeddingProvider,
        )
        with self.assertRaises(EmbeddingProviderError):
            create_embedding_provider(Settings(embedding_mode="unsupported"))

    def test_related_clause_tool_contract_declares_runtime_cache(self):
        contract = tool_contracts["retrieve_related_clauses"]

        self.assertEqual(
            contract.input_schema["embedding_cache"],
            "dict optional (runtime only)",
        )

    def test_hybrid_retrieval_meets_expanded_manually_annotated_recall_at_k(self):
        annotation_path = ROOT_DIR / "samples" / "annotations" / "related_clauses.json"
        benchmark = json.loads(annotation_path.read_text(encoding="utf-8"))
        k = benchmark["k"]
        recalls = []

        for case in benchmark["cases"]:
            related = retrieve_related_clauses(
                {
                    "contract_type": "NDA",
                    "current_clause": case["current_clause"],
                    "clauses": [case["current_clause"], *case["candidates"]],
                    "risk_type": case["risk_type"],
                    "playbook_check_point": case["playbook_check_point"],
                    "limit": k,
                }
            )
            actual_ids = {item["clause_id"] for item in related}
            relevant = set(case["relevant_clause_ids"])
            recalls.append(len(actual_ids & relevant) / len(relevant))
            self.assertTrue(all(item["retrieval_scope"] == "current_contract" for item in related))

        current_recall = sum(recalls[:3]) / len(recalls[:3])
        expanded_recall = sum(recalls) / len(recalls)
        self.assertGreaterEqual(len(benchmark["cases"]), 11)
        self.assertEqual(len({case["risk_type"] for case in benchmark["cases"]}), 8)
        self.assertGreaterEqual(current_recall, 0.8)
        self.assertGreaterEqual(expanded_recall, 0.8)


class CountingEmbeddingProvider:
    mode = "deterministic_test_stub"
    model = "counting-stub-v1"

    def __init__(self):
        self.requested_texts = []

    def embed(self, request, call_records=None):
        self.requested_texts.append(list(request.texts))
        vectors = [_hash_vector(text) for text in request.texts]
        metadata = EmbeddingCallMetadata(
            mode=self.mode,
            model=self.model,
            provider_request_id="",
            input_count=len(request.texts),
            vector_dimension=4,
            latency_ms=0,
            error_type="",
        )
        if call_records is not None:
            call_records.append(metadata)
        return EmbeddingResponse(vectors=vectors, metadata=metadata)


def _external_settings(**overrides) -> Settings:
    values = {
        "embedding_mode": "openai_compatible",
        "embedding_base_url": "https://embedding.example.test/v1",
        "embedding_api_key": "test-embedding-key",
        "embedding_model": "test-embedding-model",
        "embedding_timeout_seconds": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _hash_vector(text: str) -> list[float]:
    vector = [0.0] * 4
    vector[sum(text.encode("utf-8")) % 4] = 1.0
    return vector


if __name__ == "__main__":
    unittest.main()
