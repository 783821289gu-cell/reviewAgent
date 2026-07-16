from pathlib import Path
import sys
import unittest


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
        self.assertFalse(runtime_calls_llm("extract_key_fields"))
        self.assertFalse(runtime_calls_llm("analyze_risk"))
        self.assertEqual(runtime_llm_mode("analyze_risk"), "local_structured_no_external_llm")
        contract_payload = {item["name"]: item for item in tool_contracts_payload()}
        self.assertFalse(contract_payload["analyze_risk"]["runtime_calls_llm"])
        self.assertEqual(contract_payload["analyze_risk"]["runtime_llm_mode"], "local_structured_no_external_llm")

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
        self.assertEqual(
            [event["status"] for event in payload["events"]],
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
        self.assertEqual(payload["matched_rules"][0]["matched_rules"][0]["rule_id"], "NDA-R001")
        log_tools = [log["tool_name"] for log in payload["logs"]]
        self.assertIn("retrieve_playbook_rules", log_tools)
        self.assertIn("retrieve_related_clauses", log_tools)
        self.assertIn("retrieve_memory", log_tools)
        self.assertIn("analyze_risk", log_tools)
        self.assertIn("generate_revision", log_tools)
        self.assertIn("verify_evidence", log_tools)
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


if __name__ == "__main__":
    unittest.main()
