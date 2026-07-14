from pathlib import Path
import sys
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from services.clause_index_service import _key_fields_text, retrieve_related_clauses
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

        self.assertEqual(len(related_clauses), 2)
        self.assertNotIn("CL-002", [item["clause_id"] for item in related_clauses])
        self.assertTrue(all(item["retrieval_scope"] == "current_contract" for item in related_clauses))
        self.assertEqual(related_clauses[0]["query_context"]["current_clause_id"], "CL-002")
        self.assertIn("playbook_check_point", related_clauses[0]["query_context"])
        for factor_name in [
            "vector_similarity",
            "clause_type_relatedness",
            "key_clause_weight",
            "risk_type_relation",
            "key_field_hit",
        ]:
            self.assertIn(factor_name, related_clauses[0]["rerank_factors"])

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
            },
            related_clauses=build_clause_payloads(),
            related_memory=[{"memory_id": "MEM-001", "note": "x" * 2000}],
            max_chars=1300,
        )

        self.assertEqual(context["current_clause"]["clause_id"], "CL-002")
        self.assertEqual(context["matched_rule"]["rule_id"], "NDA-R004")
        self.assertTrue(context["output_constraints"]["no_formal_risk_without_playbook_rule"])
        self.assertTrue(context["evidence_constraints"]["must_bind_to_original_clause"])
        self.assertFalse(context["formal_risk_generated"])
        self.assertIn("reduced_memory", context["reduction_trace"])


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
