from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from db.repositories import ReviewPersistence
from models.review import ReviewPosition, ReviewStatus
from providers.embedding_provider import EmbeddingRequest, LocalSparseEmbeddingProvider
from services.event_service import ReviewEventStore
from services.log_service import invoke_tool
from services.review_service import ReviewOrchestratorAgent
from test_document_pipeline import build_docx_bytes


class ObservabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = str(self.root / "review.sqlite3")
        self.persistence = ReviewPersistence(self.db_path, str(self.root / "uploads"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_trace_and_step_ids_survive_restart(self):
        event_store = ReviewEventStore(self.persistence)
        state = ReviewOrchestratorAgent(event_store).run_sync(
            file_name="observability.docx",
            file_type="docx",
            content=build_docx_bytes(),
            review_position=ReviewPosition.PARTY_A,
        )

        self.assertTrue(state.trace_id.startswith("trace_"))
        step_ids = [log["step_id"] for log in state.logs]
        self.assertTrue(step_ids)
        self.assertEqual(len(step_ids), len(set(step_ids)))
        self.assertTrue(all(step_id.startswith("step_") for step_id in step_ids))
        self.assertTrue(all(log["trace_id"] == state.trace_id for log in state.logs))

        restarted_store = ReviewEventStore(self.persistence)
        restarted_store.load_persisted()
        restored = restarted_store.get_task(state.task_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.trace_id, state.trace_id)
        self.assertEqual(
            [log["step_id"] for log in restored.logs],
            step_ids,
        )

    def test_recovery_count_and_origin_are_exposed_and_persisted(self):
        event_store = ReviewEventStore(self.persistence)
        state = event_store.create_task(
            "recover.docx",
            "docx",
            ReviewPosition.PARTY_A,
            content=build_docx_bytes(),
        )
        event_store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "上传已接收。",
            step_name="upload_received",
        )

        recovered = event_store.record_recovery(state.task_id)
        self.assertEqual(recovered.recovery_count, 1)
        self.assertEqual(recovered.recovery_from_status, "UPLOAD_RECEIVED")
        self.assertEqual(recovered.events[-1]["step_name"], "recovery_started")

        restarted_store = ReviewEventStore(self.persistence)
        restarted_store.load_persisted()
        restored = restarted_store.get_task(state.task_id)
        self.assertEqual(restored.recovery_count, 1)
        self.assertEqual(restored.recovery_from_status, "UPLOAD_RECEIVED")

    def test_tool_log_redacts_sensitive_inputs_and_errors(self):
        logs = []
        secret = "task10-test-credential-123456"
        output = invoke_tool(
            "task_sensitive",
            {"safe_tool": lambda payload: {"ok": bool(payload)}},
            "safe_tool",
            {
                "authorization": f"Bearer {secret}",
                "api_key": secret,
                "prompt": "full sensitive contract prompt",
                "task": {
                    "task_id": "task_sensitive",
                    "status": "START",
                    "document": "full sensitive contract",
                },
            },
            logs,
        )

        self.assertEqual(output, {"ok": True})
        self.assertNotIn(secret, logs[0].input_summary)
        self.assertNotIn("full sensitive contract", logs[0].input_summary)
        self.assertIn("[redacted]", logs[0].input_summary)
        self.assertEqual(logs[0].trace_id, "trace_sensitive")
        self.assertTrue(logs[0].step_id.startswith("step_"))

        with self.assertRaisesRegex(RuntimeError, "Authorization"):
            invoke_tool(
                "task_sensitive",
                {"failing_tool": lambda _payload: self._raise_secret(secret)},
                "failing_tool",
                {},
                logs,
            )
        self.assertNotIn(secret, logs[-1].error_message)
        self.assertNotIn("Traceback", logs[-1].error_message)

        sensitive_prompt = "完整敏感合同提示词：不得向日志输出本段合同内容。"
        with self.assertRaisesRegex(RuntimeError, "provider rejected prompt"):
            invoke_tool(
                "task_sensitive",
                {
                    "failing_tool": lambda _payload: self._raise_prompt(
                        sensitive_prompt
                    )
                },
                "failing_tool",
                {"prompt": sensitive_prompt},
                logs,
            )
        self.assertNotIn(sensitive_prompt, logs[-1].error_message)
        self.assertNotIn("Traceback", logs[-1].error_message)
        self.assertEqual(logs[-1].error_message, "provider rejected prompt: [redacted]")

        with self.assertRaisesRegex(RuntimeError, "validation failed"):
            invoke_tool(
                "task_sensitive",
                {"failing_tool": lambda _payload: self._raise_empty_sensitive_value()},
                "failing_tool",
                {"prompt": ""},
                logs,
            )
        self.assertEqual(logs[-1].error_message, "validation failed")

    def test_embedding_metadata_has_explicit_local_cost_status(self):
        response = LocalSparseEmbeddingProvider().embed(EmbeddingRequest(["sample"]))
        self.assertEqual(response.metadata.cost_status, "no_external_embedding")
        serialized = json.dumps(response.metadata.to_dict(), ensure_ascii=False)
        self.assertNotIn("sample", serialized)

    @staticmethod
    def _raise_secret(secret: str):
        raise RuntimeError(f"Authorization: Bearer {secret}")

    @staticmethod
    def _raise_prompt(prompt: str):
        raise RuntimeError(f"provider rejected prompt: {prompt}\nTraceback: internal stack")

    @staticmethod
    def _raise_empty_sensitive_value():
        raise RuntimeError("validation failed")


if __name__ == "__main__":
    unittest.main()
