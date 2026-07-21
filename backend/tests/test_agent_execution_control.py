from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
import json
import sys
import time
import unittest

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from db.repositories import ReviewPersistence
from config import Settings
from main import create_app
from models.log import StepLog
from models.review import ReviewPosition, ReviewStatus
from services.event_service import ReviewEventStore
from services.log_service import invoke_tool
from services.review_service import ReviewOrchestratorAgent
from services.task_service import cancel_review_task
from test_document_pipeline import build_docx_bytes
from tools.registry import tool_registry


class AgentExecutionControlTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.persistence = ReviewPersistence(
            str(self.root / "review.sqlite3"),
            str(self.root / "uploads"),
        )
        self.store = ReviewEventStore(self.persistence)
        self.content = build_docx_bytes()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cancel_stops_new_nodes_and_retains_completed_results(self):
        agent = ReviewOrchestratorAgent(self.store)
        original_classifier = tool_registry["classify_contract_type"]
        classifier_started = Event()
        release_classifier = Event()

        def blocking_classifier(tool_input):
            classifier_started.set()
            release_classifier.wait(timeout=3)
            return original_classifier(tool_input)

        with patch.dict(
            tool_registry,
            {"classify_contract_type": blocking_classifier},
        ):
            started = agent.start(
                "cancel.docx",
                "docx",
                self.content,
                ReviewPosition.PARTY_A,
            )
            self.assertTrue(classifier_started.wait(timeout=3))
            try:
                requested = cancel_review_task(
                    started.task_id,
                    "User stopped the review.",
                    self.store,
                )
                self.assertIn(requested["status"], {"CANCEL_REQUESTED", "CANCELLED"})
                duplicate = cancel_review_task(
                    started.task_id,
                    "Duplicate cancellation must be idempotent.",
                    self.store,
                )
                self.assertIn(duplicate["status"], {"CANCEL_REQUESTED", "CANCELLED"})
                self.assertEqual(
                    duplicate["task"]["cancel_reason"],
                    "User stopped the review.",
                )
            finally:
                release_classifier.set()
            final = wait_for_status(self.store, started.task_id, {ReviewStatus.CANCELLED})

        self.assertIsNotNone(final.document)
        self.assertIsNone(final.clauses)
        self.assertEqual(
            [log["tool_name"] for log in final.logs],
            ["parse_document", "classify_contract_type"],
        )
        self.assertEqual(final.logs[0]["status"], "success")
        self.assertEqual(final.logs[1]["status"], "cancelled")
        self.assertEqual(final.events[-2]["step_name"], "cancel_requested")
        self.assertEqual(final.events[-1]["step_name"], "task_cancelled")
        self.assertEqual(final.cancel_reason, "User stopped the review.")
        self.assertEqual(
            [event["step_name"] for event in final.events].count("cancel_requested"),
            1,
        )
        self.assertFalse(
            self.store.wait_for_events(
                started.task_id,
                len(final.events) - 1,
                timeout_seconds=0,
            )[0]["task"]["execution_active"]
        )

    def test_node_and_task_timeout_are_not_recorded_as_success(self):
        original_parser = tool_registry["parse_document"]

        def slow_parser(tool_input):
            time.sleep(0.03)
            return original_parser(tool_input)

        with patch.dict(tool_registry, {"parse_document": slow_parser}):
            node_state = ReviewOrchestratorAgent(
                self.store,
                node_timeout_seconds=0.01,
                task_timeout_seconds=1,
            ).run_sync(
                "node-timeout.docx",
                "docx",
                self.content,
                ReviewPosition.PARTY_A,
            )

        self.assertEqual(node_state.status, ReviewStatus.NODE_TIMEOUT)
        self.assertEqual(node_state.logs[-1]["status"], "timeout")
        self.assertIsNone(node_state.document)
        self.assertEqual(node_state.retry_counts, {"node_timeout": 1})

        second_store = ReviewEventStore(
            ReviewPersistence(
                str(self.root / "task-timeout.sqlite3"),
                str(self.root / "task-timeout-uploads"),
            )
        )
        with patch.dict(tool_registry, {"parse_document": slow_parser}):
            task_state = ReviewOrchestratorAgent(
                second_store,
                node_timeout_seconds=1,
                task_timeout_seconds=0.01,
            ).run_sync(
                "task-timeout.docx",
                "docx",
                self.content,
                ReviewPosition.PARTY_A,
            )

        self.assertEqual(task_state.status, ReviewStatus.TASK_TIMEOUT)
        self.assertEqual(task_state.logs[-1]["status"], "timeout")
        self.assertEqual(task_state.retry_counts, {"task_timeout": 1})

        classification_store = ReviewEventStore(
            ReviewPersistence(
                str(self.root / "classification-timeout.sqlite3"),
                str(self.root / "classification-timeout-uploads"),
            )
        )
        original_classifier = tool_registry["classify_contract_type"]

        def slow_classifier(tool_input):
            time.sleep(0.03)
            return original_classifier(tool_input)

        with patch.dict(tool_registry, {"classify_contract_type": slow_classifier}):
            classification_state = ReviewOrchestratorAgent(
                classification_store,
                node_timeout_seconds=0.01,
                task_timeout_seconds=1,
            ).run_sync(
                "classification-timeout.docx",
                "docx",
                self.content,
                ReviewPosition.PARTY_A,
            )

        self.assertEqual(classification_state.status, ReviewStatus.NODE_TIMEOUT)
        self.assertEqual(
            classification_state.last_timeout["step_name"],
            "contract_type_classification",
        )
        self.assertEqual(classification_state.logs[-1]["status"], "timeout")

        blocked_store = ReviewEventStore(
            ReviewPersistence(
                str(self.root / "blocked-timeout.sqlite3"),
                str(self.root / "blocked-timeout-uploads"),
            )
        )
        release_parser = Event()

        def blocked_parser(tool_input):
            release_parser.wait(timeout=2)
            return original_parser(tool_input)

        started_at = time.perf_counter()
        try:
            with patch.dict(tool_registry, {"parse_document": blocked_parser}):
                blocked_state = ReviewOrchestratorAgent(
                    blocked_store,
                    node_timeout_seconds=0.02,
                    task_timeout_seconds=1,
                ).run_sync(
                    "blocked-timeout.docx",
                    "docx",
                    self.content,
                    ReviewPosition.PARTY_A,
                )
        finally:
            release_parser.set()

        self.assertLess(time.perf_counter() - started_at, 0.5)
        self.assertEqual(blocked_state.status, ReviewStatus.NODE_TIMEOUT)
        self.assertEqual(blocked_state.logs[-1]["status"], "timeout")

    def test_non_parse_execution_error_uses_task_error_status(self):
        def failing_clause_extractor(tool_input):
            raise RuntimeError("forced clause extraction failure")

        with patch.dict(
            tool_registry,
            {"extract_clauses": failing_clause_extractor},
        ):
            state = ReviewOrchestratorAgent(self.store).run_sync(
                "task-error.docx",
                "docx",
                self.content,
                ReviewPosition.PARTY_A,
            )

        self.assertEqual(state.status, ReviewStatus.TASK_ERROR)
        self.assertEqual(state.events[-1]["step_name"], "task_error")
        self.assertEqual(state.logs[-1]["tool_name"], "extract_clauses")
        self.assertEqual(state.logs[-1]["status"], "failed")
        self.assertEqual(state.retry_counts, {"task": 1})

    def test_cancel_http_endpoint_exposes_real_terminal_state(self):
        settings = Settings(
            host="127.0.0.1",
            port=8000,
            max_upload_bytes=10 * 1024 * 1024,
            allowed_origins=(),
            llm_mode="local_structured",
            memory_db_path=str(self.root / "http.sqlite3"),
            upload_dir=str(self.root / "http-uploads"),
            service_name="contract-review-agent",
        )
        store = ReviewEventStore(
            ReviewPersistence(settings.memory_db_path, settings.upload_dir)
        )
        app = create_app(settings, store)
        original_classifier = tool_registry["classify_contract_type"]
        classifier_started = Event()
        release_classifier = Event()

        def blocking_classifier(tool_input):
            classifier_started.set()
            release_classifier.wait(timeout=3)
            return original_classifier(tool_input)

        with TestClient(app) as client, patch.dict(
            tool_registry,
            {"classify_contract_type": blocking_classifier},
        ):
            response = client.post(
                "/api/tasks",
                files={
                    "contract_file": (
                        "cancel-http.docx",
                        self.content,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
                data={"review_position": ReviewPosition.PARTY_A.value},
            )
            self.assertEqual(response.status_code, 201, response.text)
            task_id = response.json()["task_id"]
            self.assertTrue(classifier_started.wait(timeout=3))

            cancelled = client.post(
                f"/api/tasks/{task_id}/cancel",
                json={"reason": "HTTP cancellation test"},
            )
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertEqual(
                cancelled.json()["status"],
                cancelled.json()["task"]["status"],
            )
            self.assertEqual(
                cancelled.json()["task"]["cancel_reason"],
                "HTTP cancellation test",
            )
            self.assertIn(
                cancelled.json()["status"],
                {"CANCEL_REQUESTED", "CANCELLED"},
            )
            release_classifier.set()

            deadline = time.time() + 5
            while time.time() < deadline:
                final = client.get(f"/api/tasks/{task_id}").json()
                if final["status"] == "CANCELLED":
                    break
                time.sleep(0.01)
            else:
                self.fail("HTTP cancellation did not reach CANCELLED")

        self.assertEqual(final["events"][-1]["step_name"], "task_cancelled")
        self.assertEqual(final["logs"][-1]["status"], "cancelled")
        self.assertFalse(final["execution_active"])

    def test_manual_recovery_persists_budget_and_exhaustion_across_restart(self):
        original_parser = tool_registry["parse_document"]

        def slow_parser(tool_input):
            time.sleep(0.03)
            return original_parser(tool_input)

        with patch.dict(tool_registry, {"parse_document": slow_parser}):
            first = ReviewOrchestratorAgent(
                self.store,
                node_timeout_seconds=0.01,
                task_timeout_seconds=1,
            ).run_sync(
                "recover-timeout.docx",
                "docx",
                self.content,
                ReviewPosition.PARTY_A,
            )

        restarted_store = ReviewEventStore(self.persistence)
        restarted_store.load_persisted()
        restarted_agent = ReviewOrchestratorAgent(
            restarted_store,
            node_timeout_seconds=0.01,
            task_timeout_seconds=1,
        )
        with patch.dict(tool_registry, {"parse_document": slow_parser}):
            restarted_agent.recover_task(
                first.task_id,
                resume_from=None,
                operator_action="qa_manual_retry",
                reason="Retry the timed out parser once.",
            )
            second = wait_for_status(
                restarted_store,
                first.task_id,
                {ReviewStatus.NODE_TIMEOUT},
                minimum_event_count=len(first.events) + 2,
            )

        self.assertEqual(second.retry_counts, {"node_timeout": 2})
        self.assertEqual(second.recovery_count, 1)
        self.assertEqual(len(second.recovery_history), 1)
        self.assertEqual(second.logs[-1]["retry_index"], 1)
        self.assertEqual(second.recovery_history[0]["operator_action"], "qa_manual_retry")
        self.assertEqual(second.recovery_history[0]["reason"], "Retry the timed out parser once.")

        with self.assertRaisesRegex(ValueError, "retry budget exhausted"):
            restarted_agent.recover_task(
                first.task_id,
                resume_from=None,
                operator_action="qa_manual_retry",
                reason="A second retry must be rejected.",
            )
        persisted = self.persistence.task_repository.get(first.task_id)
        self.assertEqual(persisted["retry_counts"], {"node_timeout": 2})
        self.assertEqual(len(persisted["recovery_history"]), 1)

    def test_concurrent_run_is_rejected_without_duplicate_tool_logs(self):
        agent = ReviewOrchestratorAgent(self.store)
        original_classifier = tool_registry["classify_contract_type"]
        classifier_started = Event()
        release_classifier = Event()

        def blocking_classifier(tool_input):
            classifier_started.set()
            release_classifier.wait(timeout=3)
            return original_classifier(tool_input)

        with patch.dict(tool_registry, {"classify_contract_type": blocking_classifier}):
            started = agent.start(
                "concurrent.docx",
                "docx",
                self.content,
                ReviewPosition.PARTY_A,
            )
            self.assertTrue(classifier_started.wait(timeout=3))
            competing_store = ReviewEventStore(self.persistence)
            competing_store.load_persisted()
            ReviewOrchestratorAgent(competing_store).run(started.task_id, self.content)
            release_classifier.set()
            final = wait_for_status(
                self.store,
                started.task_id,
                {ReviewStatus.EVIDENCE_VERIFIED, ReviewStatus.HUMAN_REVIEW_PENDING},
            )

        tool_names = [log["tool_name"] for log in final.logs]
        self.assertEqual(tool_names.count("parse_document"), 1)
        self.assertEqual(tool_names.count("classify_contract_type"), 1)
        self.assertFalse(final.execution_active)
        self.assertFalse(competing_store.is_execution_active(started.task_id))

    def test_trace_records_decisions_versions_tokens_and_stable_keys(self):
        state = ReviewOrchestratorAgent(self.store).run_sync(
            "trace.docx",
            "docx",
            self.content,
            ReviewPosition.PARTY_A,
        )

        for index, log in enumerate(state.logs):
            self.assertTrue(log["trace_id"])
            self.assertTrue(log["step_id"])
            self.assertEqual(log["retry_index"], 0)
            if index == 0:
                self.assertEqual(log["parent_step_id"], "")
            else:
                self.assertEqual(log["parent_step_id"], state.logs[index - 1]["step_id"])
            trace = log["trace_summary"]
            self.assertIn("versions", trace)
            serialized = json.dumps(trace, ensure_ascii=False)
            self.assertNotIn("Authorization", serialized)
            self.assertNotIn("api_key", serialized.lower())
            self.assertNotIn("保密信息是指披露方提供的商业信息", serialized)

        decision_logs = [
            log
            for log in state.logs
            if log["tool_name"] in {
                "plan_review_action",
                "retrieve_related_clauses",
                "criticize_risk",
            }
        ]
        self.assertTrue(decision_logs)
        self.assertTrue(all(log["idempotency_key"] for log in decision_logs))
        self.assertTrue(
            any(
                log["trace_summary"].get("decision", {}).get("type") == "retrieval"
                for log in decision_logs
            )
        )
        retrieval_trace = next(
            log["trace_summary"]["decision"]
            for log in decision_logs
            if log["trace_summary"].get("decision", {}).get("type") == "retrieval"
        )
        self.assertTrue(retrieval_trace["query"]["embedding_query_components"])
        self.assertIsNotNone(retrieval_trace["top_k"]["effective"])
        self.assertTrue(retrieval_trace["candidates"][0]["rerank_factors"])
        self.assertTrue(
            any(log["trace_summary"].get("token_allocation") for log in state.logs)
        )
        self.assertTrue(
            any(
                log["trace_summary"].get("decision", {}).get("type")
                == "evidence_verifier"
                for log in state.logs
            )
        )

        planner_input = {
            "trigger_reason": "LOW_CONFIDENCE",
            "current_status": "RISK_ANALYZED",
            "target_clause_id": "CL-001",
            "contract_clause_ids": ["CL-001"],
            "retry_count": 0,
            "failure_reason": "low confidence",
        }
        first_logs: list[StepLog] = []
        second_logs: list[StepLog] = []
        invoke_tool(
            state.task_id,
            tool_registry,
            "plan_review_action",
            planner_input,
            first_logs,
            step_name="planner_route",
        )
        invoke_tool(
            state.task_id,
            tool_registry,
            "plan_review_action",
            planner_input,
            second_logs,
            step_name="planner_route",
        )
        self.assertEqual(first_logs[0].idempotency_key, second_logs[0].idempotency_key)

        changed_planner_logs: list[StepLog] = []
        invoke_tool(
            state.task_id,
            tool_registry,
            "plan_review_action",
            {**planner_input, "failure_reason": "evidence missing"},
            changed_planner_logs,
            step_name="planner_route",
        )
        self.assertNotEqual(
            first_logs[0].idempotency_key,
            changed_planner_logs[0].idempotency_key,
        )

        changed_retrieval_logs: list[StepLog] = []
        base_retrieval_input = {
            "contract_type": "NDA",
            "current_clause": {"clause_id": "CL-001"},
            "clauses": [],
            "risk_type": "CONFIDENTIALITY_SCOPE",
            "playbook_check_point": "scope",
            "query_adjustments": {"required_terms": ["confidential"]},
            "limit": 3,
        }
        invoke_tool(
            state.task_id,
            {"retrieve_related_clauses": lambda _: []},
            "retrieve_related_clauses",
            base_retrieval_input,
            changed_retrieval_logs,
        )
        invoke_tool(
            state.task_id,
            {"retrieve_related_clauses": lambda _: []},
            "retrieve_related_clauses",
            {**base_retrieval_input, "risk_type": "TERM"},
            changed_retrieval_logs,
        )
        self.assertNotEqual(
            changed_retrieval_logs[0].idempotency_key,
            changed_retrieval_logs[1].idempotency_key,
        )

        critic_logs: list[StepLog] = []
        critic_tool = {
            "criticize_risk": lambda _: {
                "decision": "PASS",
                "reason_code": "SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK",
            }
        }
        critic_input = {
            "finding": {
                "risk_id": "RISK-001",
                "clause_id": "CL-001",
                "risk_type": "TERM",
                "risk_reason": "first analysis",
                "evidence_text": "first evidence",
            },
            "current_clause": {"clause_id": "CL-001"},
            "matched_rule": {"rule_id": "NDA-R001"},
        }
        invoke_tool(
            state.task_id,
            critic_tool,
            "criticize_risk",
            critic_input,
            critic_logs,
        )
        invoke_tool(
            state.task_id,
            critic_tool,
            "criticize_risk",
            {
                **critic_input,
                "finding": {**critic_input["finding"], "risk_reason": "reanalysis"},
            },
            critic_logs,
        )
        self.assertNotEqual(critic_logs[0].idempotency_key, critic_logs[1].idempotency_key)


def wait_for_status(
    store: ReviewEventStore,
    task_id: str,
    statuses: set[ReviewStatus],
    *,
    minimum_event_count: int = 0,
):
    deadline = time.time() + 8
    while time.time() < deadline:
        state = store.get_task(task_id)
        if (
            state is not None
            and state.status in statuses
            and len(state.events or []) >= minimum_event_count
            and not state.execution_active
        ):
            return state
        time.sleep(0.01)
    raise AssertionError(f"task did not reach expected status: {statuses}")


if __name__ == "__main__":
    unittest.main()
