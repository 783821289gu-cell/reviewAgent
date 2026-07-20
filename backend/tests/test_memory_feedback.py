from pathlib import Path
import sys
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from models.review import ReviewPosition
from services.feedback_service import apply_feedback_to_task
from services.memory_service import retrieve_memory, write_memory
from services.review_service import ReviewOrchestratorAgent
from services.event_service import ReviewEventStore
from test_document_pipeline import build_high_risk_docx_bytes
from test_retrieval_context import build_clause_payloads


class MemoryFeedbackTest(unittest.TestCase):
    def test_write_memory_and_retrieve_from_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")

            memory_item = write_memory(
                {
                    "db_path": db_path,
                    "human_feedback": sample_feedback(),
                }
            )
            memories = retrieve_memory(
                {
                    "db_path": db_path,
                    "contract_type": "NDA",
                    "clause": build_clause_payloads()[1],
                    "risk_type": "使用目的或使用限制不清",
                    "review_position": "乙方",
                    "memory_items": [],
                    "limit": 3,
                }
            )

        self.assertEqual(memory_item["memory_type"], "human_feedback")
        self.assertEqual(memory_item["user_action"], "update_suggestion")
        self.assertEqual(memory_item["source_finding_id"], "RISK-001")
        self.assertEqual(memories[0]["memory_type"], "semantic_preference")
        self.assertEqual(memories[0]["source_memory_ids"], [memory_item["memory_id"]])
        self.assertEqual(memories[0]["memory_source"], "sqlite_semantic_preference")
        self.assertEqual(memories[0]["final_suggestion"], "限定使用目的为评估合作。")
        self.assertTrue(memories[0]["can_influence_suggestion"])

    def test_feedback_updates_task_and_writes_memory_through_registry(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)
        state = agent.run_sync(
            file_name="high-risk.docx",
            file_type="docx",
            content=build_high_risk_docx_bytes(),
            review_position=ReviewPosition.PARTY_B,
        )
        risk_id = state.risk_findings[0]["risk_id"]
        risk_clause = next(
            clause
            for clause in state.clauses
            if clause["clause_id"] == state.risk_findings[0]["clause_id"]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            result = apply_feedback_to_task(
                state.task_id,
                {
                    "risk_id": risk_id,
                    "action": "ignore",
                    "ignore_reason": "客户已接受该责任安排。",
                    "include_in_report": False,
                },
                event_store=event_store,
                db_path=db_path,
            )
            memories = retrieve_memory(
                {
                    "db_path": db_path,
                    "contract_type": "NDA",
                    "clause": risk_clause,
                    "risk_type": state.risk_findings[0]["risk_type"],
                    "review_position": "乙方",
                    "memory_items": [],
                    "limit": 3,
                }
            )

        updated_task = result["task"]
        updated_risk = result["risk"]
        self.assertEqual(result["status"], "MEMORY_UPDATED")
        self.assertEqual(updated_task["status"], "MEMORY_UPDATED")
        self.assertEqual(updated_risk["review_status"], "IGNORED_RISK")
        self.assertFalse(updated_risk["include_in_report"])
        self.assertEqual(updated_risk["feedback"]["memory_id"], result["memory_item"]["memory_id"])
        self.assertEqual(memories[0]["opposition_count"], 1)
        self.assertEqual(memories[0]["support_count"], 0)
        self.assertFalse(memories[0]["can_influence_suggestion"])
        self.assertEqual(updated_task["logs"][-1]["tool_name"], "write_memory")
        self.assertEqual(updated_task["logs"][-1]["status"], "success")


def sample_feedback() -> dict:
    return {
        "contract_type": "NDA",
        "clause_type": "使用限制",
        "risk_type": "使用目的或使用限制不清",
        "review_position": "乙方",
        "user_action": "update_suggestion",
        "original_severity": "中",
        "final_severity": "中",
        "original_suggestion": "补充使用限制。",
        "final_suggestion": "限定使用目的为评估合作。",
        "ignore_reason": "",
        "source_finding_id": "RISK-001",
        "source_clause_id": "CL-002",
        "include_in_report": True,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


if __name__ == "__main__":
    unittest.main()
