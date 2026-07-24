from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
import sys
import time
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from db.repositories import ReviewPersistence
from models.log import StepLog
from models.review import LLMMode, NodeExecutionTimeoutError, ReviewPosition, ReviewStatus
from models.review import TaskExecutionTimeoutError
from providers.llm_provider import bind_llm_mode, reset_llm_mode
from services.event_service import ReviewEventStore
from services.log_service import ToolExecutionControl, invoke_tool
from services.review_service import ReviewOrchestratorAgent
from test_document_pipeline import build_docx_bytes
from test_risk_analysis import build_review_context
from tools.registry import tool_registry


class LongTaskExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.persistence = ReviewPersistence(
            str(self.root / "review.sqlite3"),
            str(self.root / "uploads"),
        )
        self.content = build_docx_bytes()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_risk_analysis_resumes_missing_items_with_bounded_concurrency(self):
        contexts = _review_contexts(5)
        first_result = _finding(contexts[0], review_status="NO_RISK")
        task_id = self._persist_checkpoint(
            ReviewStatus.CONTEXT_BUILT,
            contexts,
            analysis_results=[first_result],
            progress={
                "stage": "risk_analysis",
                "stage_label": "分析合同风险",
                "state": "failed",
                "completed": 1,
                "total": 5,
                "current_item": contexts[1]["context_id"],
                "message": "timeout fixture",
                "updated_at": "2026-07-22T00:00:00+00:00",
            },
        )
        store = ReviewEventStore(self.persistence)
        store.load_persisted()
        active = 0
        maximum_active = 0
        called_context_ids = []
        lock = Lock()

        def delayed_analyzer(tool_input):
            nonlocal active, maximum_active
            context = tool_input["review_context"]
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                called_context_ids.append(context["context_id"])
            try:
                time.sleep(0.03)
                return _finding(context, review_status="NO_RISK")
            finally:
                with lock:
                    active -= 1

        with patch.dict(tool_registry, {"analyze_risk": delayed_analyzer}):
            ReviewOrchestratorAgent(
                store,
                llm_max_concurrency=2,
            ).run(task_id, self.content)

        final = store.get_task(task_id)
        self.assertEqual(final.status, ReviewStatus.EVIDENCE_VERIFIED)
        self.assertEqual(maximum_active, 2)
        self.assertCountEqual(
            called_context_ids,
            [context["context_id"] for context in contexts[1:]],
        )
        self.assertNotIn(contexts[0]["context_id"], called_context_ids)
        self.assertEqual(
            [
                f"{result['clause_id']}:{result['matched_rule_ids'][0]}"
                for result in final.analysis_results
            ],
            [context["context_id"] for context in contexts],
        )
        self.assertEqual(final.progress["completed"], final.progress["total"])

    def test_evidence_recovery_skips_completed_candidate_chain(self):
        contexts = _review_contexts(3)
        findings = [_finding(context) for context in contexts]
        first_evidence = _evidence(findings[0])
        task_id = self._persist_checkpoint(
            ReviewStatus.RISK_ANALYZED,
            contexts,
            analysis_results=findings,
            evidence_results=[first_evidence],
            risk_findings=[findings[0]],
            progress={
                "stage": "evidence_verification",
                "stage_label": "校验风险与证据",
                "state": "failed",
                "completed": 1,
                "total": 3,
                "completed_item_ids": [contexts[0]["context_id"]],
                "current_item": contexts[1]["context_id"],
                "message": "timeout fixture",
                "updated_at": "2026-07-22T00:00:00+00:00",
            },
        )
        store = ReviewEventStore(self.persistence)
        store.load_persisted()
        critic_calls = []
        revision_calls = []
        evidence_calls = []

        def critic(tool_input):
            critic_calls.append(tool_input["finding"]["risk_id"])
            return {
                "decision": "PASS",
                "reason_code": "SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK",
            }

        def revision(tool_input):
            revision_calls.append(tool_input["finding"]["risk_id"])
            return {"revision_suggestion": "使用已验证的修改建议。"}

        def verifier(tool_input):
            finding = tool_input["finding"]
            evidence_calls.append(finding["risk_id"])
            return _evidence(finding)

        with patch.dict(
            tool_registry,
            {
                "criticize_risk": critic,
                "generate_revision": revision,
                "verify_evidence": verifier,
            },
        ):
            ReviewOrchestratorAgent(store).run(task_id, self.content)

        final = store.get_task(task_id)
        expected_pending_ids = [finding["risk_id"] for finding in findings[1:]]
        self.assertEqual(final.status, ReviewStatus.EVIDENCE_VERIFIED)
        self.assertEqual(critic_calls, expected_pending_ids)
        self.assertEqual(revision_calls, expected_pending_ids)
        self.assertEqual(evidence_calls, expected_pending_ids)
        self.assertEqual(
            [finding["risk_id"] for finding in final.risk_findings],
            [finding["risk_id"] for finding in findings],
        )
        self.assertEqual(len(final.evidence_results), 3)
        self.assertEqual(
            final.progress["completed_item_ids"],
            [context["context_id"] for context in contexts],
        )
        evidence_progress_messages = [
            event["message"]
            for event in final.events
            if event["step_name"] == "evidence_verification_progress"
        ]
        self.assertTrue(evidence_progress_messages[0].endswith("（1/3）。"))

    def test_timed_out_external_call_is_recorded_as_in_flight_not_adopted(self):
        release = Event()
        logs: list[StepLog] = []

        def blocked_analyzer(_tool_input):
            release.wait(timeout=1)
            return {}

        control = ToolExecutionControl(
            cancel_check=lambda: None,
            task_started_at=time.perf_counter(),
            task_timeout_seconds=1,
            node_timeout_seconds=0.01,
        )
        token = bind_llm_mode("openai_compatible")
        try:
            with patch.dict(tool_registry, {"analyze_risk": blocked_analyzer}):
                with self.assertRaises(NodeExecutionTimeoutError):
                    invoke_tool(
                        "task_inflight",
                        tool_registry,
                        "analyze_risk",
                        {"review_context": {}},
                        logs,
                        step_name="risk_analysis",
                        execution_control=control,
                    )
        finally:
            release.set()
            reset_llm_mode(token)

        self.assertEqual(logs[-1].status, "timeout")
        self.assertEqual(
            logs[-1].token_cost_summary,
            "openai_compatible_in_flight_result_not_adopted",
        )

    def test_external_call_rejected_before_start_is_recorded_as_not_invoked(self):
        invoked = False
        logs: list[StepLog] = []

        def analyzer(_tool_input):
            nonlocal invoked
            invoked = True
            return {}

        control = ToolExecutionControl(
            cancel_check=lambda: None,
            task_started_at=time.perf_counter() - 1,
            task_timeout_seconds=0.01,
            node_timeout_seconds=1,
        )
        token = bind_llm_mode("openai_compatible")
        try:
            with patch.dict(tool_registry, {"analyze_risk": analyzer}):
                with self.assertRaises(TaskExecutionTimeoutError):
                    invoke_tool(
                        "task_not_started",
                        tool_registry,
                        "analyze_risk",
                        {"review_context": {}},
                        logs,
                        step_name="risk_analysis",
                        execution_control=control,
                    )
        finally:
            reset_llm_mode(token)

        self.assertFalse(invoked)
        self.assertEqual(logs[-1].status, "timeout")
        self.assertEqual(
            logs[-1].token_cost_summary,
            "openai_compatible_not_invoked",
        )

    def test_evidence_business_failure_recovery_restarts_cleared_results(self):
        contexts = _review_contexts(2)
        findings = [_finding(context) for context in contexts]
        task_id = self._persist_checkpoint(
            ReviewStatus.RISK_ANALYZED,
            contexts,
            analysis_results=findings,
            evidence_results=[],
            risk_findings=[],
            recovery_from_status=ReviewStatus.EVIDENCE_MISSING.value,
            progress={
                "stage": "evidence_verification",
                "stage_label": "校验风险与证据",
                "state": "failed",
                "completed": 1,
                "total": 2,
                "completed_item_ids": [contexts[0]["context_id"]],
                "current_item": contexts[1]["context_id"],
                "message": "business failure fixture",
                "updated_at": "2026-07-22T00:00:00+00:00",
            },
        )
        store = ReviewEventStore(self.persistence)
        store.load_persisted()
        evidence_calls = []

        with patch.dict(
            tool_registry,
            {
                "criticize_risk": lambda _input: {
                    "decision": "PASS",
                    "reason_code": "SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK",
                },
                "generate_revision": lambda _input: {
                    "revision_suggestion": "使用已验证的修改建议。"
                },
                "verify_evidence": lambda tool_input: (
                    evidence_calls.append(tool_input["finding"]["risk_id"])
                    or _evidence(tool_input["finding"])
                ),
            },
        ):
            ReviewOrchestratorAgent(store).run(task_id, self.content)

        final = store.get_task(task_id)
        self.assertEqual(final.status, ReviewStatus.EVIDENCE_VERIFIED)
        self.assertCountEqual(
            evidence_calls,
            [finding["risk_id"] for finding in findings],
        )
        self.assertEqual(len(final.risk_findings), 2)

    def _persist_checkpoint(
        self,
        status,
        contexts,
        *,
        analysis_results,
        progress,
        evidence_results=None,
        risk_findings=None,
        recovery_from_status=None,
    ):
        store = ReviewEventStore(self.persistence)
        state = store.create_task(
            "long-task.docx",
            "docx",
            ReviewPosition.PARTY_A,
            content=self.content,
            llm_mode=LLMMode.DEEPSEEK,
        )
        clauses = [context["current_clause"] for context in contexts]
        store.update_task(
            state.task_id,
            status,
            "persisted checkpoint",
            step_name="checkpoint_fixture",
            contract_classification={"contract_type": "NDA"},
            clauses=clauses,
            matched_rules=[],
            review_contexts=contexts,
            analysis_results=analysis_results,
            evidence_results=evidence_results,
            risk_findings=risk_findings,
            logs=[],
            recovery_from_status=recovery_from_status,
            progress=progress,
        )
        return state.task_id


def _review_contexts(count):
    template = build_review_context()
    contexts = []
    for index in range(1, count + 1):
        context = deepcopy(template)
        clause_id = f"CL-{index:03d}"
        rule_id = f"NDA-R{index:03d}"
        context["context_id"] = f"{clause_id}:{rule_id}"
        context["current_clause"]["clause_id"] = clause_id
        context["matched_rule"]["rule_id"] = rule_id
        context["output_constraints"]["allowed_clause_ids"] = [clause_id]
        context["output_constraints"]["allowed_rule_ids"] = [rule_id]
        context["related_clauses"] = [
            {
                "clause_id": "CL-RELATED",
                "clause_type": "定义",
                "text": "相关条款测试文本。",
                "key_fields": {},
            }
        ]
        contexts.append(context)
    return contexts


def _finding(context, review_status="CONFIRMED_RISK"):
    clause = context["current_clause"]
    rule = context["matched_rule"]
    position_config = rule["position_config"]
    return {
        "risk_id": f"RISK-{clause['clause_id']}-{rule['rule_id']}",
        "risk_type": rule["risk_type"],
        "severity": "低",
        "confidence": 0.95,
        "risk_reason": "测试风险理由。",
        "clause_id": clause["clause_id"],
        "evidence_text": "测试证据。" if review_status != "NO_RISK" else "",
        "matched_rule_ids": [rule["rule_id"]],
        "review_position": context["review_position"],
        "risk_focus": position_config["risk_focus"],
        "revision_suggestion": position_config["revision_template"],
        "review_status": review_status,
    }


def _evidence(finding):
    return {
        "risk_id": finding["risk_id"],
        "clause_id": finding["clause_id"],
        "evidence_text": finding["evidence_text"],
        "is_valid": True,
        "failure_reason": "",
        "verified_clause_id": finding["clause_id"],
        "source_location": {},
    }


if __name__ == "__main__":
    unittest.main()
