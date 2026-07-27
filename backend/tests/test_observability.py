from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import unittest

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from config import Settings
from db.repositories import ReviewPersistence
from models.review import ReviewPosition, ReviewStatus
from providers.embedding_provider import EmbeddingRequest, LocalSparseEmbeddingProvider
from services.event_service import ReviewEventStore
from services.log_service import invoke_tool
from services.observability_service import (
    _parse_otlp_headers,
    configure_observability,
    shutdown_observability,
    trace_tool_call,
)
from services.review_service import ReviewOrchestratorAgent
from services.runtime_log_service import (
    close_runtime_logging,
    configure_runtime_logging,
    write_runtime_log,
)
from test_document_pipeline import build_docx_bytes


class ObservabilityTest(unittest.TestCase):
    def setUp(self):
        shutdown_observability()
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = str(self.root / "review.sqlite3")
        self.persistence = ReviewPersistence(self.db_path, str(self.root / "uploads"))

    def tearDown(self):
        shutdown_observability()
        close_runtime_logging()
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

    def test_runtime_file_log_records_redacted_tool_lifecycle(self):
        log_file = self.root / "logs" / "runtime.log"
        secret = "runtime-secret-token"
        close_runtime_logging()
        configure_runtime_logging(str(log_file), "INFO")
        try:
            write_runtime_log(
                "redaction_probe",
                error_message=f"Authorization: Bearer {secret}",
            )
            logs = []
            with self.assertRaisesRegex(RuntimeError, "Authorization"):
                invoke_tool(
                    "task_runtime_log",
                    {"failing_tool": lambda _payload: self._raise_secret(secret)},
                    "failing_tool",
                    {},
                    logs,
                    step_name="runtime_log_test",
                )
        finally:
            close_runtime_logging()

        content = log_file.read_text(encoding="utf-8")
        self.assertIn('"event": "tool_started"', content)
        self.assertIn('"event": "tool_finished"', content)
        self.assertIn('"status": "failed"', content)
        self.assertNotIn(secret, content)
        self.assertNotIn("Bearer", content)

    def test_observability_is_disabled_by_default(self):
        runtime = configure_observability(Settings(observability_enabled=False))
        self.assertFalse(runtime.enabled)
        with trace_tool_call(
            task_id="task-disabled",
            trace_id="trace-disabled",
            step_id="step-disabled",
            step_name="parse",
            tool_name="parse_document",
            retry_index=0,
        ):
            pass

    def test_enabled_observability_requires_an_otlp_endpoint(self):
        with self.assertRaisesRegex(
            ValueError,
            "REVIEW_AGENT_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        ):
            configure_observability(Settings(observability_enabled=True))

    def test_tool_span_contains_metadata_but_not_business_content(self):
        exporter = InMemorySpanExporter()
        configure_observability(
            Settings(
                observability_enabled=True,
                otel_service_name="review-test",
            ),
            span_exporter=exporter,
        )
        with trace_tool_call(
            task_id="task-1",
            trace_id="trace-1",
            step_id="step-1",
            step_name="risk_analyzed",
            tool_name="analyze_risk",
            retry_index=1,
        ):
            contract_text = "TOP SECRET CONTRACT BODY"
            api_key = "secret-api-key"
            self.assertTrue(contract_text and api_key)

        spans = exporter.get_finished_spans()
        self.assertEqual(1, len(spans))
        span = spans[0]
        self.assertEqual("tool.analyze_risk", span.name)
        self.assertEqual("task-1", span.attributes["review.task_id"])
        self.assertEqual("success", span.attributes["review.status"])
        self.assertEqual(1, span.attributes["review.retry_index"])
        serialized = repr(dict(span.attributes))
        self.assertNotIn("TOP SECRET CONTRACT BODY", serialized)
        self.assertNotIn("secret-api-key", serialized)

    def test_failure_records_only_exception_type(self):
        exporter = InMemorySpanExporter()
        configure_observability(
            Settings(observability_enabled=True),
            span_exporter=exporter,
        )
        with self.assertRaisesRegex(RuntimeError, "sensitive contract sentence"):
            with trace_tool_call(
                task_id="task-2",
                trace_id="trace-2",
                step_id="step-2",
                step_name="evidence",
                tool_name="verify_evidence",
                retry_index=0,
            ):
                raise RuntimeError("sensitive contract sentence")

        span = exporter.get_finished_spans()[0]
        self.assertEqual("failed", span.attributes["review.status"])
        self.assertEqual("RuntimeError", span.attributes["error.type"])
        self.assertNotIn("sensitive contract sentence", repr(span.to_json()))
        self.assertEqual([], list(span.events))

    def test_invoke_tool_creates_a_redacted_span(self):
        exporter = InMemorySpanExporter()
        configure_observability(
            Settings(observability_enabled=True),
            span_exporter=exporter,
        )
        logs = []
        result = invoke_tool(
            "task-integrated",
            {"echo": lambda tool_input: {"count": len(tool_input)}},
            "echo",
            {"contract_text": "private clause", "authorization": "Bearer secret"},
            logs,
        )
        self.assertEqual({"count": 2}, result)
        self.assertEqual("success", logs[0].status)
        span = exporter.get_finished_spans()[0]
        self.assertEqual("tool.echo", span.name)
        self.assertEqual("task-integrated", span.attributes["review.task_id"])
        self.assertNotIn("private clause", repr(span.to_json()))
        self.assertNotIn("Bearer secret", repr(span.to_json()))

    def test_otlp_headers_are_url_decoded_and_require_key_value_pairs(self):
        self.assertEqual(
            {
                "Authorization": "Basic abc==",
                "x-langfuse-ingestion-version": "4",
            },
            _parse_otlp_headers(
                "Authorization=Basic%20abc%3D%3D,"
                "x-langfuse-ingestion-version=4"
            ),
        )
        with self.assertRaisesRegex(ValueError, "key=value"):
            _parse_otlp_headers("invalid-header")

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
