import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from config import Settings
from providers.embedding_provider import (
    EmbeddingCallMetadata,
    EmbeddingRequest,
    EmbeddingResponse,
    OpenAICompatibleEmbeddingProvider,
)
from services.log_service import invoke_tool
from services.memory_service import retrieve_memory, write_memory
from tools.registry import tool_registry


class MemoryVectorRetrievalTest(unittest.TestCase):
    def test_semantic_memory_is_ranked_cached_and_refreshed_after_content_change(self):
        provider = SemanticEmbeddingProvider()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            write_memory(
                {
                    "db_path": db_path,
                    "human_feedback": feedback(
                        "MEM-USE-001",
                        clause_type="use_restriction",
                        risk_type="unclear_use_restriction",
                        suggestion="Limit use to evaluation of the proposed transaction.",
                    ),
                }
            )
            write_memory(
                {
                    "db_path": db_path,
                    "human_feedback": feedback(
                        "MEM-LIABILITY-001",
                        clause_type="liability",
                        risk_type="unlimited_liability",
                        suggestion="Add a reasonable aggregate liability cap.",
                    ),
                }
            )

            with patch(
                "services.memory_service.create_embedding_provider",
                return_value=provider,
            ):
                first = retrieve_memory(retrieval_input(db_path))
                second = retrieve_memory(retrieval_input(db_path))
                write_memory(
                    {
                        "db_path": db_path,
                        "human_feedback": feedback(
                            "MEM-USE-002",
                            clause_type="use_restriction",
                            risk_type="unclear_use_restriction",
                            suggestion="Use the approved evaluation-only wording.",
                        ),
                    }
                )
                third = retrieve_memory(retrieval_input(db_path))

            connection = sqlite3.connect(db_path)
            try:
                persisted = connection.execute(
                    """
                    SELECT preference_id, embedding_mode, embedding_model,
                           vector_dimension, content_hash
                    FROM semantic_preference_embeddings
                    ORDER BY preference_id
                    """
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(first[0]["risk_type"], "unclear_use_restriction")
        self.assertFalse(first[0]["retrieval_factors"]["exact_clause_type"])
        self.assertFalse(first[0]["retrieval_factors"]["exact_risk_type"])
        self.assertTrue(first[0]["retrieval_eligible"])
        self.assertGreaterEqual(first[0]["vector_similarity"], 0.55)
        self.assertEqual(second[0]["memory_id"], first[0]["memory_id"])
        self.assertEqual(third[0]["source_memory_ids"], ["MEM-USE-001", "MEM-USE-002"])
        self.assertEqual([len(batch) for batch in provider.requested_texts], [3, 1, 2])
        self.assertEqual(len(persisted), 2)
        self.assertTrue(all(row[1] == provider.mode for row in persisted))
        self.assertTrue(all(row[2] == provider.model for row in persisted))
        self.assertTrue(all(row[3] == 3 for row in persisted))
        self.assertTrue(all(len(row[4]) == 64 for row in persisted))

    def test_memory_candidates_never_cross_contract_type_or_review_position(self):
        provider = SemanticEmbeddingProvider()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            write_memory(
                {
                    "db_path": db_path,
                    "human_feedback": feedback(
                        "MEM-PARTY-B",
                        clause_type="use_restriction",
                        risk_type="unclear_use_restriction",
                        suggestion="Party B wording.",
                        position="party_b",
                    ),
                }
            )
            write_memory(
                {
                    "db_path": db_path,
                    "human_feedback": feedback(
                        "MEM-PARTY-A",
                        clause_type="use_restriction",
                        risk_type="unclear_use_restriction",
                        suggestion="Party A wording.",
                        position="party_a",
                    ),
                }
            )
            write_memory(
                {
                    "db_path": db_path,
                    "human_feedback": feedback(
                        "MEM-MSA",
                        clause_type="use_restriction",
                        risk_type="unclear_use_restriction",
                        suggestion="MSA wording.",
                        position="party_b",
                        contract_type="MSA",
                    ),
                }
            )

            with patch(
                "services.memory_service.create_embedding_provider",
                return_value=provider,
            ):
                results = retrieve_memory(retrieval_input(db_path))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_memory_ids"], ["MEM-PARTY-B"])
        self.assertEqual(results[0]["contract_type"], "NDA")
        self.assertEqual(results[0]["review_position"], "party_b")

    def test_external_memory_embedding_failure_is_logged_without_local_fallback(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                request=request,
                headers={"x-request-id": "req-memory-rate-limit"},
            )

        provider = OpenAICompatibleEmbeddingProvider(
            Settings(
                embedding_mode="openai_compatible",
                embedding_base_url="https://embedding.example.test/v1",
                embedding_api_key="memory-secret",
                embedding_model="test-memory-model",
                embedding_timeout_seconds=2,
            ),
            transport=httpx.MockTransport(handler),
        )
        logs = []
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            write_memory(
                {
                    "db_path": db_path,
                    "human_feedback": feedback(
                        "MEM-EXTERNAL-001",
                        clause_type="use_restriction",
                        risk_type="unclear_use_restriction",
                        suggestion="Evaluation-only wording.",
                    ),
                }
            )
            with patch(
                "services.memory_service.create_embedding_provider",
                return_value=provider,
            ):
                with self.assertRaisesRegex(Exception, "rate limit"):
                    invoke_tool(
                        "task-memory-embedding-failure",
                        tool_registry,
                        "retrieve_memory",
                        retrieval_input(db_path),
                        logs,
                    )

        self.assertEqual(len(logs), 1)
        summary = json.loads(logs[0].token_cost_summary)
        self.assertEqual(summary["mode"], "openai_compatible")
        self.assertEqual(summary["embedding_calls"][0]["error_type"], "rate_limit")
        self.assertEqual(
            summary["embedding_calls"][0]["provider_request_id"],
            "req-memory-rate-limit",
        )
        serialized = json.dumps(logs[0].to_dict(), ensure_ascii=False)
        self.assertNotIn("memory-secret", serialized)
        self.assertNotIn("local_sparse", serialized)


class SemanticEmbeddingProvider:
    mode = "model_test_stub"
    model = "semantic-memory-stub-v1"

    def __init__(self):
        self.requested_texts = []

    def embed(self, request: EmbeddingRequest, call_records=None) -> EmbeddingResponse:
        self.requested_texts.append(list(request.texts))
        vectors = [semantic_vector(text) for text in request.texts]
        metadata = EmbeddingCallMetadata(
            mode=self.mode,
            model=self.model,
            provider_request_id="test-memory-request",
            input_count=len(request.texts),
            vector_dimension=3,
            latency_ms=0,
            error_type="",
        )
        if call_records is not None:
            call_records.append(metadata)
        return EmbeddingResponse(vectors=vectors, metadata=metadata)


def semantic_vector(text: str) -> list[float]:
    lowered = text.lower()
    return [
        float(sum(term in lowered for term in ("evaluation", "purpose", "use_restriction"))),
        float(sum(term in lowered for term in ("liability", "cap", "unlimited"))),
        0.1,
    ]


def retrieval_input(db_path: str) -> dict:
    return {
        "db_path": db_path,
        "contract_type": "NDA",
        "clause": {
            "clause_id": "CL-QUERY",
            "clause_type": "purpose_limitation",
            "title": "Permitted purpose",
            "text": "The information may be used only to evaluate the proposed transaction.",
            "key_fields": {"purpose": ["transaction evaluation"]},
        },
        "risk_type": "ambiguous_purpose",
        "review_position": "party_b",
        "memory_items": [],
        "embedding_cache": {},
        "limit": 3,
        "now": "2026-07-22T00:00:00+00:00",
    }


def feedback(
    memory_id: str,
    *,
    clause_type: str,
    risk_type: str,
    suggestion: str,
    position: str = "party_b",
    contract_type: str = "NDA",
) -> dict:
    return {
        "memory_id": memory_id,
        "contract_type": contract_type,
        "clause_type": clause_type,
        "risk_type": risk_type,
        "review_position": position,
        "user_action": "update_suggestion",
        "original_severity": "medium",
        "final_severity": "medium",
        "original_suggestion": "Original suggestion.",
        "final_suggestion": suggestion,
        "ignore_reason": "",
        "source_finding_id": f"RISK-{memory_id}",
        "source_clause_id": "CL-001",
        "include_in_report": True,
        "created_at": (
            "2026-07-20T00:00:00+00:00"
            if memory_id.endswith("001") or memory_id.endswith("B")
            else "2026-07-21T00:00:00+00:00"
        ),
    }


if __name__ == "__main__":
    unittest.main()
