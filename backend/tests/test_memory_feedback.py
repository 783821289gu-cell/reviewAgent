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
from services.evaluation_service import _docx_bytes_from_text
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

    def test_feedback_keeps_task_pending_until_every_risk_is_handled(self):
        event_store = ReviewEventStore()
        state = ReviewOrchestratorAgent(event_store).run_sync(
            file_name="multi-risk.docx",
            file_type="docx",
            content=_docx_bytes_from_text(MULTI_RISK_NDA_TEXT),
            review_position=ReviewPosition.PARTY_A,
        )
        self.assertGreater(len(state.risk_findings), 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = apply_feedback_to_task(
                state.task_id,
                {
                    "risk_id": state.risk_findings[0]["risk_id"],
                    "action": "accept",
                    "include_in_report": True,
                },
                event_store=event_store,
                db_path=str(Path(temp_dir) / "memory.sqlite3"),
            )

        self.assertEqual(result["status"], "HUMAN_REVIEW_PENDING")
        self.assertEqual(result["task"]["progress"]["completed"], 1)
        self.assertEqual(
            result["task"]["progress"]["total"],
            len(state.risk_findings),
        )
        self.assertIn("还剩", result["message"])

    def test_manual_evidence_must_come_from_the_risk_clause(self):
        event_store = ReviewEventStore()
        state = ReviewOrchestratorAgent(event_store).run_sync(
            file_name="manual-evidence.docx",
            file_type="docx",
            content=build_high_risk_docx_bytes(),
            review_position=ReviewPosition.PARTY_A,
        )
        risk = state.risk_findings[0]
        clause = next(item for item in state.clauses if item["clause_id"] == risk["clause_id"])
        other_clause = next(item for item in state.clauses if item["clause_id"] != risk["clause_id"])
        selected_text = clause["text"][: min(30, len(clause["text"]))]

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            with self.assertRaisesRegex(ValueError, "对应条款原文"):
                apply_feedback_to_task(
                    state.task_id,
                    {
                        "risk_id": risk["risk_id"],
                        "action": "update_evidence",
                        "final_evidence_text": other_clause["text"],
                    },
                    event_store=event_store,
                    db_path=db_path,
                )
            result = apply_feedback_to_task(
                state.task_id,
                {
                    "risk_id": risk["risk_id"],
                    "action": "update_evidence",
                    "final_evidence_text": selected_text,
                    "include_in_report": True,
                },
                event_store=event_store,
                db_path=db_path,
            )

        self.assertEqual(result["risk"]["evidence_text"], selected_text)
        self.assertEqual(
            result["risk"]["evidence_verification"]["status"],
            "MANUALLY_VERIFIED",
        )
        self.assertEqual(result["risk"]["feedback"]["user_action"], "update_evidence")


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


MULTI_RISK_NDA_TEXT = """1. 定义
保密信息包括披露方提供的任何商业信息、技术资料和合作资料。

2. 使用限制
接收方可以为内部商业安排使用保密信息。

3. 允许披露
接收方可以向所有关联方、顾问和代表披露保密信息。

4. 违约责任
违约方应赔偿守约方全部损失和间接损失。
"""


if __name__ == "__main__":
    unittest.main()
