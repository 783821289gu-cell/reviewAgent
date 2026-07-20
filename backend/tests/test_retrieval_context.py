from pathlib import Path
import sys
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from services.clause_index_service import (
    MAX_DYNAMIC_TOP_K,
    _dynamic_top_k,
    _key_fields_text,
    build_retrieval_query,
    retrieve_related_clauses,
)
from services.context_builder import build_review_context
from services.memory_service import retrieve_memory
from services.log_service import invoke_tool
from tools.registry import tool_registry


class RetrievalContextTest(unittest.TestCase):
    def test_key_fields_text_only_includes_fields_with_values(self):
        text = _key_fields_text(
            {
                "obligation_subject": [],
                "use_purpose": ["仅用于"],
                "liability_scope": [],
                "breach_liability": [],
            }
        )

        self.assertIn("仅用于", text)
        self.assertIn("use_purpose", text)
        self.assertNotIn("liability_scope", text)
        self.assertNotIn("breach_liability", text)

    def test_related_clause_retrieval_uses_current_contract_and_rerank_factors(self):
        clauses = build_clause_payloads()
        related_clauses = retrieve_related_clauses(
            {
                "contract_type": "NDA",
                "current_clause": clauses[1],
                "clauses": clauses,
                "risk_type": "使用目的或使用限制不清",
                "playbook_check_point": "检查是否明确保密信息只能用于约定合作目的。",
                "limit": 2,
            }
        )

        self.assertGreaterEqual(len(related_clauses), 1)
        self.assertLessEqual(len(related_clauses), 2)
        self.assertNotIn("CL-002", [item["clause_id"] for item in related_clauses])
        self.assertTrue(all(item["retrieval_scope"] == "current_contract" for item in related_clauses))
        query_context = related_clauses[0]["query_context"]
        self.assertEqual(query_context["current_clause_id"], "CL-002")
        self.assertEqual(query_context["current_clause_type"], "使用限制")
        self.assertEqual(query_context["risk_type"], "使用目的或使用限制不清")
        self.assertEqual(
            query_context["playbook_check_point"],
            "检查是否明确保密信息只能用于约定合作目的。",
        )
        self.assertEqual(query_context["key_fields"], clauses[1]["key_fields"])
        self.assertTrue(query_context["keyword_terms"])
        self.assertTrue(query_context["playbook_keywords"])
        self.assertTrue(query_context["playbook_check_point_terms"])
        self.assertEqual(query_context["effective_top_k"], len(related_clauses))
        query_text = build_retrieval_query(
            clauses[1],
            "使用目的或使用限制不清",
            "检查是否明确保密信息只能用于约定合作目的。",
        )
        for expected in [
            clauses[1]["text"],
            clauses[1]["clause_type"],
            "使用目的或使用限制不清",
            "检查是否明确保密信息只能用于约定合作目的。",
            "use_purpose",
        ]:
            self.assertIn(expected, query_text)
        self.assertEqual(related_clauses[0]["embedding_mode"], "local_sparse")
        self.assertEqual(related_clauses[0]["embedding_model"], "local_sparse_hash_v1")
        self.assertEqual(related_clauses[0]["vector_dimension"], 256)
        self.assertEqual(
            related_clauses[0]["query_context"]["embedding_query_components"],
            [
                "current_clause_text",
                "current_clause_type",
                "risk_type",
                "playbook_check_point",
                "current_clause_key_fields",
            ],
        )
        for factor_name in [
            "vector_similarity",
            "clause_type_relatedness",
            "keyword_overlap",
            "keyword_matches",
            "field_match",
            "field_matches",
            "playbook_check_point_overlap",
            "playbook_check_point_matches",
            "playbook_applicability",
            "playbook_keyword_matches",
            "final_score",
        ]:
            self.assertIn(factor_name, related_clauses[0]["rerank_factors"])
        self.assertIn("embedding", related_clauses[0]["retrieval_sources"])
        self.assertIn("keyword", related_clauses[0]["retrieval_sources"])
        self.assertIsInstance(related_clauses[0]["vector_rank"], int)
        self.assertIsInstance(related_clauses[0]["keyword_rank"], int)

    def test_hybrid_recall_deduplicates_current_contract_and_caps_dynamic_top_k(self):
        current_clause = {
            "clause_id": "CL-CURRENT",
            "clause_type": "允许披露",
            "text": "接收方可以向员工和顾问披露保密信息。",
            "key_fields": {"permitted_disclosure_targets": ["员工", "顾问"]},
        }
        candidates = [
            {
                "clause_id": f"CL-{index:03d}",
                "clause_type": "允许披露",
                "text": f"仅可向必要知悉且承担保密义务的员工或顾问披露，序号 {index}。",
                "key_fields": {"permitted_disclosure_targets": ["必要知悉人员"]},
            }
            for index in range(1, 9)
        ]
        clauses = [current_clause, *candidates, dict(candidates[0])]

        related = retrieve_related_clauses(
            {
                "contract_type": "NDA",
                "current_clause": current_clause,
                "clauses": clauses,
                "risk_type": "允许披露对象过宽",
                "playbook_check_point": "检查披露对象是否限于必要知悉人员。",
                "limit": 99,
            }
        )

        clause_ids = [item["clause_id"] for item in related]
        self.assertEqual(len(clause_ids), len(set(clause_ids)))
        self.assertLessEqual(len(related), MAX_DYNAMIC_TOP_K)
        self.assertTrue(set(clause_ids).issubset({item["clause_id"] for item in candidates}))
        self.assertTrue(all(item["retrieval_scope"] == "current_contract" for item in related))
        context = related[0]["query_context"]
        self.assertEqual(context["candidate_count"], 8)
        self.assertEqual(context["requested_top_k"], 99)
        self.assertEqual(context["max_top_k"], MAX_DYNAMIC_TOP_K)
        self.assertEqual(context["effective_top_k"], len(related))
        self.assertTrue(all(item["rerank_score"] >= context["score_cutoff"] for item in related))
        self.assertTrue(all(len(item["retrieval_sources"]) == len(set(item["retrieval_sources"])) for item in related))

    def test_dynamic_top_k_uses_score_band_and_hard_cap(self):
        candidates = [
            {"rerank_score": score}
            for score in [0.9, 0.85, 0.8, 0.75, 0.72, 0.71, 0.69, 0.2]
        ]

        selected, cutoff = _dynamic_top_k(candidates, requested_top_k=20)

        self.assertEqual(cutoff, 0.7)
        self.assertEqual(len(selected), MAX_DYNAMIC_TOP_K)
        self.assertTrue(all(item["rerank_score"] >= cutoff for item in selected))

    def test_related_clause_tool_logs_rerank_summary(self):
        logs = []

        related_clauses = invoke_tool(
            "task_test",
            tool_registry,
            "retrieve_related_clauses",
            {
                "contract_type": "NDA",
                "current_clause": build_clause_payloads()[1],
                "clauses": build_clause_payloads(),
                "risk_type": "使用目的或使用限制不清",
                "playbook_check_point": "检查是否明确保密信息只能用于约定合作目的。",
                "limit": 2,
            },
            logs,
            step_name="related_clause_retrieval",
        )

        self.assertTrue(related_clauses)
        self.assertEqual(logs[0].status, "success")
        self.assertIn("rerank_score", logs[0].output_summary)
        self.assertIn("sources=embedding+keyword", logs[0].output_summary)
        self.assertIn("effective_top_k=", logs[0].output_summary)
        self.assertEqual(logs[0].token_cost_summary, "local_sparse_no_external_embedding")

    def test_memory_retrieval_filters_provided_items(self):
        memories = retrieve_memory(
            {
                "contract_type": "NDA",
                "clause": build_clause_payloads()[1],
                "risk_type": "使用目的或使用限制不清",
                "review_position": "乙方",
                "memory_items": [
                    {
                        "memory_id": "MEM-001",
                        "contract_type": "NDA",
                        "clause_type": "使用限制",
                        "risk_type": "使用目的或使用限制不清",
                        "review_position": "乙方",
                        "user_action": "修改",
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "memory_id": "MEM-002",
                        "contract_type": "MSA",
                        "clause_type": "使用限制",
                        "risk_type": "使用目的或使用限制不清",
                        "review_position": "乙方",
                    },
                ],
                "limit": 3,
            }
        )

        self.assertEqual([item["memory_id"] for item in memories], ["MEM-001"])
        self.assertEqual(memories[0]["memory_source"], "provided_memory_items")

    def test_memory_retrieval_without_provided_items_returns_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memories = retrieve_memory(
                {
                    "db_path": str(Path(temp_dir) / "memory.sqlite3"),
                    "contract_type": "NDA",
                    "clause": build_clause_payloads()[1],
                    "risk_type": "使用目的或使用限制不清",
                    "review_position": "乙方",
                    "memory_items": [],
                    "limit": 3,
                }
            )

        self.assertEqual(memories, [])

    def test_context_builder_reduces_memory_before_related_clauses(self):
        context = build_review_context(
            contract_type="NDA",
            review_position="甲方",
            current_clause=build_clause_payloads()[1],
            matched_rule={
                "rule_id": "NDA-R004",
                "risk_type": "使用目的或使用限制不清",
                "check_point": "检查是否明确使用目的。",
                "review_position": "甲方",
                "severity_default": "高",
                "risk_focus": "限制乙方超范围使用甲方保密信息。",
                "revision_template": "限定乙方使用目的。",
                "positions": {
                    "甲方": {
                        "severity_default": "高",
                        "risk_focus": "限制乙方超范围使用甲方保密信息。",
                        "revision_template": "限定乙方使用目的。",
                    }
                },
                "position_config": {
                    "severity_default": "高",
                    "risk_focus": "限制乙方超范围使用甲方保密信息。",
                    "revision_template": "限定乙方使用目的。",
                },
            },
            related_clauses=build_clause_payloads(),
            related_memory=[{"memory_id": "MEM-001", "note": "x" * 2000}],
            max_tokens=1200,
        )

        self.assertEqual(context["current_clause"]["clause_id"], "CL-002")
        self.assertEqual(context["matched_rule"]["rule_id"], "NDA-R004")
        self.assertTrue(context["output_constraints"]["no_formal_risk_without_playbook_rule"])
        self.assertTrue(context["evidence_constraints"]["must_bind_to_original_clause"])
        self.assertFalse(context["formal_risk_generated"])
        self.assertEqual(context["reduction_trace"][0], "reduced_low_relevance_memory")
        self.assertIn("reduced_low_rank_related_clause", context["reduction_trace"])
        self.assertLessEqual(context["token_budget"]["final_prompt_tokens"], 1200)
        self.assertEqual(context["token_budget"]["max_prompt_tokens"], 1200)
        self.assertEqual(
            set(context["token_budget"]["category_tokens"]),
            {
                "system_policy",
                "task",
                "playbook",
                "contract_data",
                "related_clauses",
                "memory",
                "evidence_constraint",
                "output_schema",
            },
        )
        self.assertNotIn("note", context["token_budget"])

    def test_context_builder_rejects_rule_without_resolved_position_config(self):
        with self.assertRaisesRegex(ValueError, "positions are required"):
            build_review_context(
                contract_type="NDA",
                review_position="甲方",
                current_clause=build_clause_payloads()[0],
                matched_rule={
                    "rule_id": "NDA-R001",
                    "risk_type": "保密信息范围过宽",
                    "check_point": "检查定义范围。",
                    "review_position": "甲方",
                },
                related_clauses=[],
                related_memory=[],
            )

    def test_context_builder_compresses_metadata_but_keeps_protected_prompt_sections(self):
        position_config = {
            "severity_default": "高",
            "risk_focus": "限制乙方超范围使用甲方保密信息。",
            "revision_template": "限定乙方使用目的。",
        }
        current_clause = dict(build_clause_payloads()[1])
        current_clause["non_prompt_metadata"] = "x" * 1000
        matched_rule = {
            "rule_id": "NDA-R004",
            "risk_type": "使用目的或使用限制不清",
            "check_point": "检查是否明确使用目的。",
            "review_position": "甲方",
            "severity_default": position_config["severity_default"],
            "risk_focus": position_config["risk_focus"],
            "revision_template": position_config["revision_template"],
            "positions": {"甲方": dict(position_config)},
            "position_config": dict(position_config),
            "non_prompt_metadata": "x" * 1000,
        }

        context = build_review_context(
            contract_type="NDA",
            review_position="甲方",
            current_clause=current_clause,
            matched_rule=matched_rule,
            related_clauses=[],
            related_memory=[],
            max_tokens=1100,
        )

        self.assertEqual(
            context["reduction_trace"],
            ["compressed_protected_context_metadata"],
        )
        self.assertEqual(context["current_clause"]["text"], current_clause["text"])
        self.assertEqual(context["matched_rule"]["rule_id"], "NDA-R004")
        self.assertTrue(context["output_constraints"])
        self.assertTrue(context["evidence_constraints"])
        self.assertNotIn("non_prompt_metadata", context["current_clause"])
        self.assertNotIn("non_prompt_metadata", context["matched_rule"])


def build_clause_payloads() -> list[dict]:
    return [
        {
            "clause_id": "CL-001",
            "title": "1. 定义",
            "text": "保密信息是指披露方提供的商业信息、技术资料和合作资料。",
            "clause_type": "定义",
            "key_fields": {"right_holder": ["披露方"]},
            "source_location": {"start_order": 1},
        },
        {
            "clause_id": "CL-002",
            "title": "2. 使用限制",
            "text": "接收方仅可为评估合作目的使用保密信息，不得用于其他目的。",
            "clause_type": "使用限制",
            "key_fields": {"use_purpose": ["目的", "仅用于"]},
            "source_location": {"start_order": 2},
        },
        {
            "clause_id": "CL-003",
            "title": "3. 允许披露",
            "text": "接收方可向确有知悉必要的员工和顾问披露保密信息。",
            "clause_type": "允许披露",
            "key_fields": {"permitted_disclosure_targets": ["员工", "顾问"]},
            "source_location": {"start_order": 3},
        },
        {
            "clause_id": "CL-004",
            "title": "4. 违约责任",
            "text": "违反保密义务的一方应赔偿守约方损失。",
            "clause_type": "违约责任",
            "key_fields": {"breach_liability": ["赔偿"]},
            "source_location": {"start_order": 4},
        },
    ]


if __name__ == "__main__":
    unittest.main()
