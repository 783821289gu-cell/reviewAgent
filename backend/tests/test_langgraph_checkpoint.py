import os
from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import sleep
import unittest
from unittest.mock import patch

from sqlalchemy import text


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from config import Settings
from db.postgres_persistence import PostgresReviewPersistence
from db.repositories import ReviewPersistence
from models.review import ReviewPosition, ReviewStatus
from services.checkpoint_service import (
    CHECKPOINT_SCHEMA,
    ReviewCheckpointManager,
)
from services.event_service import ReviewEventStore
from services.feedback_service import apply_feedback_to_task
from services.review_service import ReviewOrchestratorAgent
from services.review_workflow import CONTROL_STATE_KEYS
from test_document_pipeline import build_high_risk_docx_bytes


class LangGraphCheckpointTest(unittest.TestCase):
    def test_close_waits_for_compatibility_execution_to_finish(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)
        execution_started = Event()
        release_execution = Event()

        def blocked_run(*args, **kwargs):
            execution_started.set()
            release_execution.wait(timeout=5)

        with patch.object(agent, "run", side_effect=blocked_run):
            agent.start(
                file_name="checkpoint-close.docx",
                file_type="docx",
                content=build_high_risk_docx_bytes(),
                review_position=ReviewPosition.PARTY_B,
            )
            self.assertTrue(execution_started.wait(timeout=2))
            close_thread = Thread(target=agent.close)
            close_thread.start()
            sleep(0.05)
            self.assertTrue(close_thread.is_alive())
            release_execution.set()
            close_thread.join(timeout=2)

        self.assertFalse(close_thread.is_alive())

    def test_human_review_interrupt_resumes_with_compact_control_state(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)
        state = agent.run_sync(
            file_name="checkpoint.docx",
            file_type="docx",
            content=build_high_risk_docx_bytes(),
            review_position=ReviewPosition.PARTY_B,
        )

        config = {"configurable": {"thread_id": state.task_id}}
        snapshot = agent.control_graph.get_state(config)
        self.assertEqual(state.status, ReviewStatus.HUMAN_REVIEW_PENDING)
        self.assertEqual(snapshot.next, ("await_human_review",))
        self.assertLessEqual(set(snapshot.values), CONTROL_STATE_KEYS)
        self.assertNotIn("clauses", snapshot.values)
        self.assertNotIn("risk_findings", snapshot.values)

        with _runtime_temp_dir() as temp_dir:
            risk_id = state.risk_findings[0]["risk_id"]
            apply_feedback_to_task(
                state.task_id,
                {
                    "risk_id": risk_id,
                    "action": "accept",
                    "include_in_report": True,
                },
                event_store=event_store,
                db_path=str(Path(temp_dir) / "memory.sqlite3"),
            )
            resumed = agent.resume_human_review(
                state.task_id,
                {"action": "accept", "risk_id": risk_id},
            )

        self.assertEqual(resumed.status, ReviewStatus.MEMORY_UPDATED)
        self.assertEqual(agent.control_graph.get_state(config).next, ())
        agent.close()

    def test_human_review_resume_does_not_require_the_original_upload(self):
        with _runtime_temp_dir() as temp_dir:
            persistence = ReviewPersistence(
                str(Path(temp_dir) / "tasks.sqlite3"),
                str(Path(temp_dir) / "uploads"),
            )
            event_store = ReviewEventStore(persistence)
            agent = ReviewOrchestratorAgent(event_store)
            state = agent.run_sync(
                file_name="checkpoint-missing-upload.docx",
                file_type="docx",
                content=build_high_risk_docx_bytes(),
                review_position=ReviewPosition.PARTY_B,
            )
            stored_upload = persistence.document_repository.get_upload(
                state.task_id
            )
            self.assertIsNotNone(stored_upload)
            (
                persistence.upload_dir / stored_upload["stored_path"]
            ).unlink()
            risk_id = state.risk_findings[0]["risk_id"]
            apply_feedback_to_task(
                state.task_id,
                {
                    "risk_id": risk_id,
                    "action": "accept",
                    "include_in_report": True,
                },
                event_store=event_store,
                db_path=str(Path(temp_dir) / "memory.sqlite3"),
            )

            resumed = agent.resume_human_review(
                state.task_id,
                {"action": "accept", "risk_id": risk_id},
            )

            self.assertEqual(resumed.status, ReviewStatus.MEMORY_UPDATED)
            agent.close()

    def test_checkpoint_resume_failure_does_not_replace_persisted_feedback(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)
        state = agent.run_sync(
            file_name="checkpoint-resume-failure.docx",
            file_type="docx",
            content=build_high_risk_docx_bytes(),
            review_position=ReviewPosition.PARTY_B,
        )
        with _runtime_temp_dir() as temp_dir:
            risk_id = state.risk_findings[0]["risk_id"]
            apply_feedback_to_task(
                state.task_id,
                {
                    "risk_id": risk_id,
                    "action": "accept",
                    "include_in_report": True,
                },
                event_store=event_store,
                db_path=str(Path(temp_dir) / "memory.sqlite3"),
            )
            with patch.object(
                agent,
                "_stream_control",
                side_effect=RuntimeError("checkpoint unavailable"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "checkpoint unavailable",
                ):
                    agent.resume_human_review(
                        state.task_id,
                        {"action": "accept", "risk_id": risk_id},
                    )

        persisted = event_store.get_task(state.task_id)
        self.assertEqual(persisted.status, ReviewStatus.MEMORY_UPDATED)
        self.assertFalse(event_store.is_execution_active(state.task_id))
        agent.close()

    def test_partial_feedback_reinterrupts_until_manual_evidence_closes_review(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)
        state = agent.run_sync(
            file_name="checkpoint-partial.docx",
            file_type="docx",
            content=build_high_risk_docx_bytes(),
            review_position=ReviewPosition.PARTY_B,
        )
        first_risk = state.risk_findings[0]
        second_risk = deepcopy(first_risk)
        second_risk["risk_id"] = f"{first_risk['risk_id']}-SECOND"
        event_store.update_task(
            state.task_id,
            ReviewStatus.HUMAN_REVIEW_PENDING,
            "Two risks require human review.",
            step_name="human_review_pending",
            risk_findings=[first_risk, second_risk],
        )

        with _runtime_temp_dir() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            apply_feedback_to_task(
                state.task_id,
                {
                    "risk_id": first_risk["risk_id"],
                    "action": "ignore",
                    "ignore_reason": "Accepted by the reviewer.",
                    "include_in_report": False,
                },
                event_store=event_store,
                db_path=db_path,
            )
            pending = agent.resume_human_review(
                state.task_id,
                {"action": "ignore", "risk_id": first_risk["risk_id"]},
            )
            snapshot = agent.control_graph.get_state(
                {"configurable": {"thread_id": state.task_id}}
            )
            self.assertEqual(pending.status, ReviewStatus.HUMAN_REVIEW_PENDING)
            self.assertEqual(snapshot.next, ("await_human_review",))
            self.assertEqual(
                snapshot.values["pending_risk_ids"],
                [second_risk["risk_id"]],
            )

            clause = next(
                item
                for item in pending.clauses
                if item["clause_id"] == second_risk["clause_id"]
            )
            evidence_text = clause["text"][: min(30, len(clause["text"]))]
            apply_feedback_to_task(
                state.task_id,
                {
                    "risk_id": second_risk["risk_id"],
                    "action": "update_evidence",
                    "final_evidence_text": evidence_text,
                    "include_in_report": True,
                },
                event_store=event_store,
                db_path=db_path,
            )
            completed = agent.resume_human_review(
                state.task_id,
                {
                    "action": "update_evidence",
                    "risk_id": second_risk["risk_id"],
                },
            )

        self.assertEqual(completed.status, ReviewStatus.MEMORY_UPDATED)
        self.assertEqual(
            agent.control_graph.get_state(
                {"configurable": {"thread_id": state.task_id}}
            ).next,
            (),
        )
        agent.close()


@unittest.skipUnless(
    os.getenv("REVIEW_AGENT_RUN_EXTERNAL_TESTS") == "1",
    "external PostgreSQL checkpoint integration is opt-in",
)
class PostgresCheckpointIntegrationTest(unittest.TestCase):
    def test_checkpoint_survives_agent_restart_without_business_payload_copy(self):
        settings = Settings()
        persistence = PostgresReviewPersistence(
            settings.database_url,
            settings.upload_dir,
        )
        first_store = ReviewEventStore(persistence)
        first_store.load_persisted(clear_execution_leases=False)
        first_manager = ReviewCheckpointManager.postgres(settings.database_url)
        first_agent = ReviewOrchestratorAgent(
            first_store,
            checkpoint_manager=first_manager,
        )
        task_id = ""
        upload_path = None
        try:
            state = first_agent.run_sync(
                file_name="checkpoint-postgres.docx",
                file_type="docx",
                content=build_high_risk_docx_bytes(),
                review_position=ReviewPosition.PARTY_B,
            )
            task_id = state.task_id
            upload_path = persistence.upload_dir / f"{task_id}.docx"
            self.assertEqual(state.status, ReviewStatus.HUMAN_REVIEW_PENDING)
            first_agent.close()

            restarted_store = ReviewEventStore(persistence)
            restarted_store.load_persisted(clear_execution_leases=False)
            restarted_agent = ReviewOrchestratorAgent(
                restarted_store,
                checkpoint_manager=ReviewCheckpointManager.postgres(
                    settings.database_url
                ),
            )
            try:
                with _runtime_temp_dir() as temp_dir:
                    risk_id = state.risk_findings[0]["risk_id"]
                    apply_feedback_to_task(
                        task_id,
                        {
                            "risk_id": risk_id,
                            "action": "accept",
                            "include_in_report": True,
                        },
                        event_store=restarted_store,
                        db_path=str(Path(temp_dir) / "memory.sqlite3"),
                    )
                    resumed = restarted_agent.resume_human_review(
                        task_id,
                        {"action": "accept", "risk_id": risk_id},
                    )
                self.assertEqual(resumed.status, ReviewStatus.MEMORY_UPDATED)
                snapshot = restarted_agent.control_graph.get_state(
                    {"configurable": {"thread_id": task_id}}
                )
                self.assertEqual(snapshot.next, ())
            finally:
                restarted_agent.close()

            with persistence.engine.connect() as connection:
                channels = set(
                    connection.execute(
                        text(
                            f'SELECT DISTINCT channel FROM "{CHECKPOINT_SCHEMA}".checkpoint_blobs '
                            "WHERE thread_id = :task_id"
                        ),
                        {"task_id": task_id},
                    ).scalars()
                )
            self.assertFalse(
                channels
                & {
                    "content",
                    "document",
                    "clauses",
                    "risk_items",
                    "risk_results",
                    "risk_findings",
                }
            )
        finally:
            first_agent.close()
            if task_id:
                with persistence.engine.begin() as connection:
                    connection.execute(
                        text(
                            f'DELETE FROM "{CHECKPOINT_SCHEMA}".checkpoint_writes '
                            "WHERE thread_id = :task_id"
                        ),
                        {"task_id": task_id},
                    )
                    connection.execute(
                        text(
                            f'DELETE FROM "{CHECKPOINT_SCHEMA}".checkpoint_blobs '
                            "WHERE thread_id = :task_id"
                        ),
                        {"task_id": task_id},
                    )
                    connection.execute(
                        text(
                            f'DELETE FROM "{CHECKPOINT_SCHEMA}".checkpoints '
                            "WHERE thread_id = :task_id"
                        ),
                        {"task_id": task_id},
                    )
                    connection.execute(
                        text(
                            "DELETE FROM app.review_tasks WHERE task_id = :task_id"
                        ),
                        {"task_id": task_id},
                    )
            if upload_path is not None:
                upload_path.unlink(missing_ok=True)
            persistence.dispose()


def _runtime_temp_dir() -> TemporaryDirectory:
    runtime_root = Path(
        os.getenv("REVIEW_AGENT_RUNTIME_ROOT", r"D:\demo-runtime")
    )
    temp_root = runtime_root / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(dir=temp_root)


if __name__ == "__main__":
    unittest.main()
