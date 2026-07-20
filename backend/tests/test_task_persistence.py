from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sqlite3
import sys
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

import services.report_service as report_service
from db.repositories import ReviewPersistence
from db.sqlite import connect
from models.review import ReviewPosition, ReviewStatus
from services.event_service import ReviewEventStore
from services.feedback_service import apply_feedback_to_task
from services.report_task_service import generate_task_report
from services.review_service import ReviewOrchestratorAgent
from test_document_pipeline import build_docx_bytes, build_high_risk_docx_bytes


REQUIRED_TABLES = {
    "review_tasks",
    "documents",
    "clauses",
    "risk_findings",
    "step_logs",
    "task_events",
    "memory_items",
}


class TaskPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = str(self.root / "review.sqlite3")
        self.upload_dir = self.root / "uploads"
        self.persistence = ReviewPersistence(self.db_path, str(self.upload_dir))
        self.event_store = ReviewEventStore(self.persistence)
        self.agent = ReviewOrchestratorAgent(self.event_store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_completed_task_upload_and_normalized_results_survive_restart(self):
        content = build_docx_bytes()
        state = self.agent.run_sync(
            file_name="sample.docx",
            file_type="docx",
            content=content,
            review_position=ReviewPosition.PARTY_A,
        )

        connection = connect(self.db_path)
        try:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table} WHERE task_id = ?",
                        (state.task_id,),
                    ).fetchone()["count"]
                )
                for table in REQUIRED_TABLES - {"memory_items"}
            }
            task_row = connection.execute(
                """
                SELECT task_id, status, current_node, created_at, updated_at
                FROM review_tasks WHERE task_id = ?
                """,
                (state.task_id,),
            ).fetchone()
            document_row = connection.execute(
                "SELECT file_hash, stored_path FROM documents WHERE task_id = ?",
                (state.task_id,),
            ).fetchone()
        finally:
            connection.close()

        self.assertTrue(REQUIRED_TABLES.issubset(tables))
        self.assertEqual(counts["review_tasks"], 1)
        self.assertEqual(counts["documents"], 1)
        self.assertEqual(counts["clauses"], len(state.clauses))
        self.assertEqual(counts["risk_findings"], len(state.risk_findings))
        self.assertEqual(counts["step_logs"], len(state.logs))
        self.assertEqual(counts["task_events"], len(state.events))
        self.assertEqual(task_row["status"], ReviewStatus.EVIDENCE_VERIFIED.value)
        self.assertEqual(task_row["current_node"], "evidence_verified")
        self.assertTrue(task_row["created_at"])
        self.assertTrue(task_row["updated_at"])
        self.assertEqual(document_row["file_hash"], state.document["content_hash"])
        self.assertEqual(Path(document_row["stored_path"]).name, document_row["stored_path"])
        self.assertEqual((self.upload_dir / document_row["stored_path"]).read_bytes(), content)
        self.assertTrue(
            all(str(self.root) not in log["input_summary"] for log in state.logs)
        )

        restarted_store = ReviewEventStore(self.persistence)
        loaded = restarted_store.load_persisted()
        restored = restarted_store.get_task(state.task_id)

        self.assertEqual([item.task_id for item in loaded], [state.task_id])
        self.assertEqual(restored.to_dict(), state.to_dict())
        self.assertTrue(all(log["trace_summary"] for log in restored.logs))
        self.assertEqual(restored.logs[0]["parent_step_id"], "")
        self.assertTrue(all("retry_index" in log for log in restored.logs))

    def test_restart_clears_stale_execution_lease_before_recovery(self):
        state = self.event_store.create_task(
            "pending.docx",
            "docx",
            ReviewPosition.PARTY_A,
            content=build_docx_bytes(),
        )
        connection = connect(self.db_path)
        try:
            connection.execute(
                "UPDATE review_tasks SET execution_owner = ? WHERE task_id = ?",
                ("stale_process_owner", state.task_id),
            )
            connection.commit()
        finally:
            connection.close()

        restarted_store = ReviewEventStore(self.persistence)
        restarted_store.load_persisted()

        self.assertEqual(
            self.persistence.task_repository.get(state.task_id)["execution_owner"],
            "",
        )
        self.assertTrue(restarted_store.try_acquire_execution(state.task_id, "new_owner"))
        restarted_store.release_execution(state.task_id, "new_owner")

    def test_feedback_and_report_side_effects_are_idempotent_and_persisted(self):
        state = self.agent.run_sync(
            file_name="high-risk.docx",
            file_type="docx",
            content=build_high_risk_docx_bytes(),
            review_position=ReviewPosition.PARTY_B,
        )
        feedback = {
            "risk_id": state.risk_findings[0]["risk_id"],
            "action": "accept",
            "include_in_report": True,
        }

        first_feedback = apply_feedback_to_task(state.task_id, feedback, self.event_store)
        second_feedback = apply_feedback_to_task(state.task_id, feedback, self.event_store)
        self.assertEqual(
            first_feedback["memory_item"]["memory_id"],
            second_feedback["memory_item"]["memory_id"],
        )

        with patch.object(report_service, "REPORT_DIR", self.root / "reports"):
            first_report = generate_task_report(state.task_id, self.event_store)
            second_report = generate_task_report(state.task_id, self.event_store)

        self.assertEqual(first_report["report_file"], second_report["report_file"])
        self.assertEqual(len(list((self.root / "reports").glob("*.md"))), 1)

        connection = connect(self.db_path)
        try:
            memory_count = connection.execute(
                "SELECT COUNT(*) AS count FROM memory_items"
            ).fetchone()["count"]
            report_key_count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM idempotency_records
                WHERE operation = 'generate_report'
                """
            ).fetchone()["count"]
        finally:
            connection.close()

        self.assertEqual(memory_count, 1)
        self.assertEqual(report_key_count, 1)
        restarted_store = ReviewEventStore(self.persistence)
        restarted_store.load_persisted()
        restored = restarted_store.get_task(state.task_id)
        self.assertEqual(restored.status, ReviewStatus.REPORT_READY)
        self.assertEqual(restored.report_file, first_report["report_file"])
        self.assertEqual(restored.risk_findings[0]["feedback"]["memory_id"], first_feedback["memory_item"]["memory_id"])
        self.assertTrue(
            all("idempotency_key" not in log["input_summary"] for log in restored.logs)
        )
        self.assertTrue(
            all(str(self.root) not in log["input_summary"] for log in restored.logs)
        )
        self.assertEqual(ReviewOrchestratorAgent(restarted_store).recover_pending_tasks(async_mode=False), [])

        connection = connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM idempotency_records WHERE operation = 'generate_report'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_failed_state_transaction_keeps_database_and_memory_at_previous_status(self):
        state = self.event_store.create_task(
            "sample.docx",
            "docx",
            ReviewPosition.PARTY_A,
            content=build_docx_bytes(),
        )

        with patch.object(
            self.persistence.event_repository,
            "append",
            side_effect=sqlite3.OperationalError("forced event write failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "forced event write failure"):
                self.event_store.update_task(
                    state.task_id,
                    ReviewStatus.UPLOAD_RECEIVED,
                    "should not persist",
                    step_name="upload_received",
                )

        in_memory = self.event_store.get_task(state.task_id)
        self.assertEqual(in_memory.status, ReviewStatus.START)
        self.assertEqual(len(in_memory.events), 1)

        restarted_store = ReviewEventStore(self.persistence)
        restarted_store.load_persisted()
        restored = restarted_store.get_task(state.task_id)
        self.assertEqual(restored.status, ReviewStatus.START)
        self.assertEqual(len(restored.events), 1)

    def test_client_path_is_rejected_before_database_or_upload_write(self):
        with self.assertRaisesRegex(ValueError, "客户端文件路径"):
            self.event_store.create_task(
                "C:\\private\\sample.docx",
                "docx",
                ReviewPosition.PARTY_A,
                content=build_docx_bytes(),
            )

        self.assertEqual(self.persistence.task_repository.list_all(), [])
        self.assertFalse(self.upload_dir.exists())

    def test_returned_snapshots_cannot_mutate_store_or_persisted_state(self):
        state = self.agent.run_sync(
            file_name="sample.docx",
            file_type="docx",
            content=build_docx_bytes(),
            review_position=ReviewPosition.PARTY_A,
        )
        snapshot = self.event_store.get_task(state.task_id)
        original_clause_text = snapshot.clauses[0]["text"]
        original_event_message = snapshot.events[0]["message"]

        snapshot.clauses[0]["text"] = "mutated outside store"
        snapshot.events[0]["message"] = "mutated outside store"

        current = self.event_store.get_task(state.task_id)
        self.assertEqual(current.clauses[0]["text"], original_clause_text)
        self.assertEqual(current.events[0]["message"], original_event_message)

        restarted_store = ReviewEventStore(self.persistence)
        restarted_store.load_persisted()
        restored = restarted_store.get_task(state.task_id)
        self.assertEqual(restored.clauses[0]["text"], original_clause_text)
        self.assertEqual(restored.events[0]["message"], original_event_message)

    def test_update_payloads_cannot_mutate_store_after_commit(self):
        state = self.event_store.create_task(
            "sample.docx",
            "docx",
            ReviewPosition.PARTY_A,
            content=build_docx_bytes(),
        )
        clauses = [{"clause_id": "CL-001", "text": "original"}]
        self.event_store.update_task(
            state.task_id,
            ReviewStatus.CLAUSES_STRUCTURED,
            "clauses committed",
            step_name="clauses_structured",
            clauses=clauses,
        )

        clauses[0]["text"] = "mutated after commit"

        current = self.event_store.get_task(state.task_id)
        self.assertEqual(current.clauses[0]["text"], "original")
        restarted_store = ReviewEventStore(self.persistence)
        restarted_store.load_persisted()
        self.assertEqual(
            restarted_store.get_task(state.task_id).clauses[0]["text"],
            "original",
        )


if __name__ == "__main__":
    unittest.main()
