from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from models.review import ReviewPosition, ReviewStatus
from services.event_service import ReviewEventStore
from services.log_service import invoke_tool
from services.review_service import ReviewOrchestratorAgent
from test_document_pipeline import build_docx_bytes, build_high_risk_docx_bytes
from tools.contracts import runtime_calls_llm, runtime_llm_mode, tool_contracts
from tools.registry import tool_contracts_payload, tool_registry


EXPECTED_TOOL_NAMES = {
    "parse_document",
    "classify_contract_type",
    "extract_clauses",
    "extract_key_fields",
    "retrieve_playbook_rules",
    "retrieve_related_clauses",
    "retrieve_memory",
    "analyze_risk",
    "criticize_risk",
    "plan_review_action",
    "verify_evidence",
    "generate_revision",
    "write_memory",
    "generate_report",
}


class ReviewOrchestratorTest(unittest.TestCase):
    def test_all_task_three_statuses_are_defined(self):
        statuses = {status.value for status in ReviewStatus}

        self.assertIn("START", statuses)
        self.assertIn("UPLOAD_RECEIVED", statuses)
        self.assertIn("DOCUMENT_PARSED", statuses)
        self.assertIn("CONTRACT_TYPE_CLASSIFIED", statuses)
        self.assertIn("CLAUSES_STRUCTURED", statuses)
        self.assertIn("PLAYBOOK_RETRIEVED", statuses)
        self.assertIn("CONTEXT_BUILT", statuses)
        self.assertIn("RISK_ANALYZED", statuses)
        self.assertIn("EVIDENCE_VERIFIED", statuses)
        self.assertIn("HUMAN_REVIEW_PENDING", statuses)
        self.assertIn("MEMORY_UPDATED", statuses)
        self.assertIn("REPORT_READY", statuses)
        self.assertIn("PARSE_FAILED", statuses)
        self.assertIn("RETRIEVAL_FAILED", statuses)
        self.assertIn("LLM_OUTPUT_INVALID", statuses)
        self.assertIn("EVIDENCE_MISSING", statuses)
        self.assertIn("NEED_MANUAL_REVIEW", statuses)
        self.assertIn("UNSUPPORTED_CONTRACT_TYPE", statuses)

    def test_tool_registry_and_contracts_are_complete(self):
        self.assertEqual(EXPECTED_TOOL_NAMES, set(tool_registry))
        self.assertEqual(EXPECTED_TOOL_NAMES, set(tool_contracts))

        for tool_name in EXPECTED_TOOL_NAMES:
            contract = tool_contracts[tool_name]
            self.assertEqual(contract.name, tool_name)
            self.assertTrue(contract.input_schema)
            self.assertTrue(contract.output_schema)
        self.assertTrue(tool_contracts["extract_key_fields"].calls_llm)
        with patch(
            "providers.llm_provider.settings",
            SimpleNamespace(llm_mode="local_structured"),
        ):
            self.assertFalse(runtime_calls_llm("extract_key_fields"))
            self.assertFalse(runtime_calls_llm("analyze_risk"))
            self.assertFalse(runtime_calls_llm("criticize_risk"))
            self.assertFalse(runtime_calls_llm("plan_review_action"))
            self.assertEqual(runtime_llm_mode("analyze_risk"), "local_structured_no_external_llm")
            self.assertEqual(
                runtime_llm_mode("criticize_risk"),
                "deterministic_support_check_no_external_llm",
            )
            self.assertEqual(
                runtime_llm_mode("plan_review_action"),
                "deterministic_policy_no_external_llm",
            )
            contract_payload = {item["name"]: item for item in tool_contracts_payload()}
            self.assertFalse(contract_payload["analyze_risk"]["runtime_calls_llm"])
            self.assertEqual(
                contract_payload["analyze_risk"]["runtime_llm_mode"],
                "local_structured_no_external_llm",
            )

    def test_orchestrator_records_events_and_logs(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)

        state = agent.run_sync(
            file_name="sample.docx",
            file_type="docx",
            content=build_docx_bytes(),
            review_position=ReviewPosition.PARTY_A,
        )

        payload = state.to_dict()
        self.assertEqual(payload["status"], "EVIDENCE_VERIFIED")
        self.assertGreaterEqual(len(payload["events"]), 8)
        self.assertEqual(
            [log["tool_name"] for log in payload["logs"][:3]],
            ["parse_document", "classify_contract_type", "extract_clauses"],
        )
        self.assertTrue(all(log["status"] == "success" for log in payload["logs"]))
        self.assertEqual(payload["events"][-1]["status"], "EVIDENCE_VERIFIED")
        business_events = [
            event
            for event in payload["events"]
            if not event["step_name"].endswith("_progress")
        ]
        self.assertEqual(
            [event["status"] for event in business_events],
            [
                "START",
                "UPLOAD_RECEIVED",
                "DOCUMENT_PARSED",
                "CONTRACT_TYPE_CLASSIFIED",
                "CLAUSES_STRUCTURED",
                "PLAYBOOK_RETRIEVED",
                "CONTEXT_BUILT",
                "RISK_ANALYZED",
                "EVIDENCE_VERIFIED",
            ],
        )
        progress_events = [
            event
            for event in payload["events"]
            if event["step_name"].endswith("_progress")
        ]
        self.assertTrue(progress_events)
        self.assertEqual(payload["progress"]["state"], "completed")
        self.assertEqual(payload["progress"]["completed"], payload["progress"]["total"])
        self.assertEqual(payload["matched_rules"][0]["matched_rules"][0]["rule_id"], "NDA-R001")
        log_tools = [log["tool_name"] for log in payload["logs"]]
        self.assertIn("retrieve_playbook_rules", log_tools)
        self.assertIn("retrieve_related_clauses", log_tools)
        self.assertIn("retrieve_memory", log_tools)
        self.assertIn("analyze_risk", log_tools)
        self.assertIn("criticize_risk", log_tools)
        self.assertIn("generate_revision", log_tools)
        self.assertIn("verify_evidence", log_tools)
        self.assertNotIn("plan_review_action", log_tools)
        self.assertLess(log_tools.index("analyze_risk"), log_tools.index("criticize_risk"))
        self.assertLess(log_tools.index("criticize_risk"), log_tools.index("verify_evidence"))
        self.assertEqual(payload["review_contexts"][0]["matched_rule"]["rule_id"], "NDA-R001")
        self.assertTrue(payload["review_contexts"][0]["related_clauses"])
        self.assertEqual(payload["risk_findings"][0]["matched_rule_ids"], ["NDA-R001"])
        self.assertTrue(payload["evidence_results"][0]["is_valid"])
        token_summaries = [log["token_cost_summary"] for log in payload["logs"]]
        self.assertIn("local_structured_no_external_llm", token_summaries)
        self.assertNotIn("pending_llm_metering", token_summaries)

    def test_high_risk_task_enters_human_review_pending(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)

        state = agent.run_sync(
            file_name="high-risk.docx",
            file_type="docx",
            content=build_high_risk_docx_bytes(),
            review_position=ReviewPosition.PARTY_B,
        )

        payload = state.to_dict()
        self.assertEqual(payload["status"], "HUMAN_REVIEW_PENDING")
        self.assertEqual(payload["events"][-1]["status"], "HUMAN_REVIEW_PENDING")
        self.assertEqual(payload["risk_findings"][0]["review_status"], "NEED_MANUAL_REVIEW")
        self.assertEqual(payload["risk_findings"][0]["severity"], "高")

    def test_invalid_risk_schema_never_enters_formal_risk_list(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)

        with patch(
            "services.risk_analyzer.generate_structured_risk",
            return_value={"risk_type": "保密信息范围过宽"},
        ) as provider_call:
            state = agent.run_sync(
                file_name="invalid-llm-output.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        provider_call.assert_called_once()
        self.assertEqual(payload["status"], "LLM_OUTPUT_INVALID")
        self.assertEqual(payload["analysis_results"], [])
        self.assertEqual(payload["risk_findings"], [])
        planner_log = next(
            log for log in payload["logs"] if log["tool_name"] == "plan_review_action"
        )
        self.assertEqual(planner_log["status"], "success")
        self.assertIn("action=REQUEST_HUMAN_REVIEW", planner_log["output_summary"])
        failed_log = next(
            log for log in payload["logs"] if log["tool_name"] == "analyze_risk"
        )
        self.assertEqual(failed_log["status"], "failed")
        self.assertIn("missing fields", failed_log["error_message"])

    def test_invalid_revision_schema_never_enters_formal_risk_list(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)

        with patch(
            "services.risk_analyzer.generate_structured_revision",
            return_value={"revision_suggestion": 123},
        ) as provider_call:
            state = agent.run_sync(
                file_name="invalid-revision-output.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        provider_call.assert_called_once()
        self.assertEqual(payload["status"], "LLM_OUTPUT_INVALID")
        self.assertTrue(payload["analysis_results"])
        self.assertEqual(payload["risk_findings"], [])
        planner_log = next(
            log for log in payload["logs"] if log["tool_name"] == "plan_review_action"
        )
        self.assertEqual(planner_log["status"], "success")
        failed_log = next(
            log for log in payload["logs"] if log["tool_name"] == "generate_revision"
        )
        self.assertEqual(failed_log["status"], "failed")
        self.assertIn("non-empty string", failed_log["error_message"])

    def test_first_evidence_failure_retrieves_and_reanalyzes_once(self):
        original_verifier = tool_registry["verify_evidence"]
        verifier_calls = 0

        def fail_once(tool_input):
            nonlocal verifier_calls
            verifier_calls += 1
            if verifier_calls == 1:
                return invalid_evidence_result(tool_input["finding"])
            return original_verifier(tool_input)

        with patch.dict(tool_registry, {"verify_evidence": fail_once}):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="repair-once.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        tools = [log["tool_name"] for log in payload["logs"]]
        self.assertEqual(payload["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(tools.count("plan_review_action"), 1)
        self.assertEqual(tools.count("retrieve_related_clauses"), 2)
        self.assertEqual(tools.count("analyze_risk"), 2)
        self.assertEqual(tools.count("verify_evidence"), 2)
        self.assertEqual(payload["review_contexts"][0]["planner_retry_count"], 1)
        self.assertEqual(
            payload["review_contexts"][0]["planner_trace"][0]["action"],
            "RETRIEVE_AGAIN",
        )
        self.assertEqual(len(payload["risk_findings"]), 1)

    def test_second_evidence_failure_stops_without_a_second_retrieval(self):
        def always_fail(tool_input):
            return invalid_evidence_result(tool_input["finding"])

        with patch.dict(tool_registry, {"verify_evidence": always_fail}):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="repair-exhausted.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        tools = [log["tool_name"] for log in payload["logs"]]
        planner_logs = [
            log for log in payload["logs"] if log["tool_name"] == "plan_review_action"
        ]
        self.assertEqual(payload["status"], "EVIDENCE_MISSING")
        self.assertEqual(tools.count("retrieve_related_clauses"), 2)
        self.assertEqual(tools.count("analyze_risk"), 2)
        self.assertEqual(tools.count("verify_evidence"), 2)
        self.assertEqual(len(planner_logs), 2)
        self.assertIn("action=RETRIEVE_AGAIN", planner_logs[0]["output_summary"])
        self.assertIn("action=REQUEST_HUMAN_REVIEW", planner_logs[1]["output_summary"])
        self.assertEqual(payload["risk_findings"], [])
        self.assertEqual(len(payload["evidence_results"]), 2)

    def test_invalid_planner_action_executes_no_repair_tool(self):
        invalid_decision = {
            "action": "CALL_ANY_TOOL",
            "reason_code": "EVIDENCE_MISSING",
            "target_clause_id": "CL-001",
            "query_adjustments": {},
            "confidence": 1.0,
        }

        with (
            patch.dict(
                tool_registry,
                {"verify_evidence": lambda tool_input: invalid_evidence_result(tool_input["finding"])},
            ),
            patch(
                "services.planner_service.generate_structured_planner",
                return_value=invalid_decision,
            ),
        ):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="invalid-planner.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        tools = [log["tool_name"] for log in payload["logs"]]
        planner_log = next(
            log for log in payload["logs"] if log["tool_name"] == "plan_review_action"
        )
        self.assertEqual(payload["status"], "EVIDENCE_MISSING")
        self.assertEqual(tools.count("retrieve_related_clauses"), 1)
        self.assertEqual(tools.count("analyze_risk"), 1)
        self.assertEqual(planner_log["status"], "failed")
        self.assertIn("planner action is not allowed", planner_log["error_message"])
        self.assertIn("Planner 决策被拒绝", payload["message"])

    def test_empty_related_recall_is_repaired_before_analysis(self):
        original_retriever = tool_registry["retrieve_related_clauses"]
        retriever_calls = 0

        def empty_once(tool_input):
            nonlocal retriever_calls
            retriever_calls += 1
            if retriever_calls == 1:
                return []
            return original_retriever(tool_input)

        with patch.dict(tool_registry, {"retrieve_related_clauses": empty_once}):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="repair-empty-recall.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        planner_log = next(
            log for log in payload["logs"] if log["tool_name"] == "plan_review_action"
        )
        self.assertEqual(payload["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(retriever_calls, 2)
        self.assertIn("reason=RETRIEVAL_INSUFFICIENT", planner_log["output_summary"])
        self.assertEqual(payload["review_contexts"][0]["planner_retry_count"], 1)

    def test_low_confidence_uses_planner_and_keeps_manual_review(self):
        original_analyzer = tool_registry["analyze_risk"]

        def low_confidence(tool_input):
            finding = original_analyzer(tool_input)
            finding["confidence"] = 0.5
            return finding

        with patch.dict(tool_registry, {"analyze_risk": low_confidence}):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="low-confidence.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        planner_logs = [
            log for log in payload["logs"] if log["tool_name"] == "plan_review_action"
        ]
        self.assertEqual(payload["status"], "HUMAN_REVIEW_PENDING")
        self.assertEqual(len(planner_logs), 1)
        self.assertIn("reason=LOW_CONFIDENCE", planner_logs[0]["output_summary"])
        self.assertEqual(payload["risk_findings"][0]["review_status"], "NEED_MANUAL_REVIEW")

    def test_critic_rejection_requires_human_but_evidence_remains_final(self):
        critic_result = {
            "decision": "REJECT",
            "reason_code": "EVIDENCE_UNSUPPORTED",
        }

        with patch.dict(tool_registry, {"criticize_risk": lambda _tool_input: critic_result}):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="critic-conflict.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        tools = [log["tool_name"] for log in payload["logs"]]
        self.assertEqual(payload["status"], "HUMAN_REVIEW_PENDING")
        self.assertEqual(payload["risk_findings"][0]["review_status"], "NEED_MANUAL_REVIEW")
        self.assertIn("criticize_risk", tools)
        self.assertIn("verify_evidence", tools)
        self.assertNotIn("generate_revision", tools)
        self.assertTrue(payload["evidence_results"][0]["is_valid"])
        self.assertEqual(
            payload["review_contexts"][0]["critic_trace"][0]["decision"],
            "REJECT",
        )

    def test_cross_clause_evidence_is_never_admitted_after_critic(self):
        original_analyzer = tool_registry["analyze_risk"]

        def forged_analyzer(tool_input):
            finding = original_analyzer(tool_input)
            finding["evidence_text"] = "接收方仅可为评估合作目的使用保密信息，不得用于其他目的。"
            finding["risk_reason"] = f"证据文本“{finding['evidence_text']}”支持该风险。"
            return finding

        with patch.dict(tool_registry, {"analyze_risk": forged_analyzer}):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="cross-clause-evidence.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        tools = [log["tool_name"] for log in payload["logs"]]
        self.assertEqual(payload["status"], "EVIDENCE_MISSING")
        self.assertEqual(payload["risk_findings"], [])
        self.assertEqual(tools.count("criticize_risk"), 2)
        self.assertEqual(tools.count("verify_evidence"), 2)
        self.assertEqual(tools.count("generate_revision"), 0)
        self.assertTrue(all(not item["is_valid"] for item in payload["evidence_results"]))

    def test_invalid_critic_output_never_reaches_evidence_or_formal_risks(self):
        invalid_output = {
            "decision": "PASS",
            "reason_code": "SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK",
            "evidence_text": "模型创造的证据",
        }

        with patch(
            "services.risk_critic.generate_structured_critic",
            return_value=invalid_output,
        ):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="invalid-critic-output.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        tools = [log["tool_name"] for log in payload["logs"]]
        critic_log = next(
            log for log in payload["logs"] if log["tool_name"] == "criticize_risk"
        )
        self.assertEqual(payload["status"], "LLM_OUTPUT_INVALID")
        self.assertEqual(payload["risk_findings"], [])
        self.assertNotIn("verify_evidence", tools)
        self.assertEqual(critic_log["status"], "failed")
        self.assertIn("output whitelist", critic_log["error_message"])
        self.assertIn("Critic 结构化输出无效", payload["message"])

    def test_critic_injection_signal_stops_before_formal_risk_admission(self):
        critic_result = {
            "decision": "REQUEST_HUMAN_REVIEW",
            "reason_code": "PROMPT_INJECTION_DETECTED",
        }

        with patch.dict(tool_registry, {"criticize_risk": lambda _tool_input: critic_result}):
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="critic-injection.docx",
                file_type="docx",
                content=build_docx_bytes(),
                review_position=ReviewPosition.PARTY_A,
            )

        payload = state.to_dict()
        tools = [log["tool_name"] for log in payload["logs"]]
        self.assertEqual(payload["status"], "NEED_MANUAL_REVIEW")
        self.assertEqual(payload["risk_findings"], [])
        self.assertNotIn("generate_revision", tools)
        self.assertNotIn("verify_evidence", tools)
        self.assertIn("不可信指令文本", payload["message"])

    def test_event_stream_payload_keeps_event_time_task_status(self):
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)

        state = agent.run_sync(
            file_name="sample.docx",
            file_type="docx",
            content=build_docx_bytes(),
            review_position=ReviewPosition.PARTY_A,
        )

        events = event_store.wait_for_events(state.task_id, 0, timeout_seconds=0)
        self.assertEqual(
            [event["status"] for event in events],
            [event["task"]["status"] for event in events],
        )
        expected_task_keys = set(state.to_runtime_dict()) | {"retry_limits"}
        self.assertTrue(events)
        self.assertTrue(
            all(set(event["task"]) == expected_task_keys for event in events),
            "SSE task snapshots must expose every AgentState runtime field",
        )
        playbook_event = next(
            event for event in events if event["status"] == "PLAYBOOK_RETRIEVED"
        )
        self.assertEqual(playbook_event["task"]["matched_rules"], state.matched_rules)
        public_payload = state.to_dict()
        query_payload = event_store.get_task_payload(state.task_id)
        self.assertIsNotNone(query_payload)
        self.assertEqual(
            {key: events[-1]["task"][key] for key in public_payload},
            public_payload,
        )
        self.assertEqual(
            {key: query_payload[key] for key in public_payload},
            public_payload,
        )

    def test_write_memory_validation_failure_is_logged_as_failed(self):
        logs = []

        with self.assertRaises(ValueError):
            invoke_tool(
                "task_test",
                tool_registry,
                "write_memory",
                {"human_feedback": {}},
                logs,
                step_name="memory_write",
            )

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].tool_name, "write_memory")
        self.assertEqual(logs[0].status, "failed")
        self.assertIn("feedback action", logs[0].error_message)


def invalid_evidence_result(finding: dict) -> dict:
    return {
        "risk_id": str(finding.get("risk_id", "")),
        "clause_id": str(finding.get("clause_id", "")),
        "evidence_text": str(finding.get("evidence_text", "")),
        "is_valid": False,
        "failure_reason": "evidence_text not found in clause text",
        "verified_clause_id": "",
        "source_location": {},
    }


if __name__ == "__main__":
    unittest.main()
