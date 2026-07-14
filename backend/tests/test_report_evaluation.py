from pathlib import Path
import sys
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_DIR))

from services.evaluation_service import NO_PRODUCTION_CLAIM, run_basic_evaluation
from services.log_service import invoke_tool
from services.report_service import generate_report
from tools.registry import tool_registry


class ReportEvaluationTest(unittest.TestCase):
    def test_generate_report_filters_unapproved_and_pending_risks(self):
        task = sample_task()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = generate_report(
                {
                    "task_id": task["task_id"],
                    "task": task,
                    "report_dir": temp_dir,
                }
            )

            report_file = result["report_file"]
            markdown = report_file["markdown"]

        self.assertEqual(report_file["risk_count"], 1)
        self.assertIn("sample-nda.docx", markdown)
        self.assertIn("甲方", markdown)
        self.assertIn("nda-v1", markdown)
        self.assertIn("保密信息范围过宽", markdown)
        self.assertIn("任何商业信息", markdown)
        self.assertIn("限定保密信息范围", markdown)
        self.assertIn("采纳", markdown)
        self.assertNotIn("不应导出的忽略风险", markdown)
        self.assertNotIn("不应导出的待复核风险", markdown)
        self.assertNotIn("execution log", markdown.lower())

    def test_generate_report_is_invoked_through_tool_registry(self):
        task = sample_task()
        logs = []
        with tempfile.TemporaryDirectory() as temp_dir:
            result = invoke_tool(
                task["task_id"],
                tool_registry,
                "generate_report",
                {
                    "task_id": task["task_id"],
                    "task": task,
                    "report_dir": temp_dir,
                },
                logs,
                step_name="report_generation",
            )

        self.assertIn("report_file", result)
        self.assertEqual(logs[0].tool_name, "generate_report")
        self.assertEqual(logs[0].status, "success")

    def test_generate_report_rejects_unfinished_task(self):
        task = sample_task()
        task["status"] = "UPLOAD_RECEIVED"

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "尚未完成"):
                generate_report(
                    {
                        "task_id": task["task_id"],
                        "task": task,
                        "report_dir": temp_dir,
                    }
                )

    def test_basic_evaluation_runs_ten_synthetic_samples(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = run_basic_evaluation(
                {
                    "samples_dir": str(PROJECT_ROOT / "samples"),
                    "output_dir": str(Path(temp_dir) / "evaluation"),
                    "report_dir": str(Path(temp_dir) / "reports"),
                    "memory_db_path": str(Path(temp_dir) / "memory.sqlite3"),
                }
            )

        self.assertEqual(summary["sample_count"], 10)
        self.assertEqual(summary["passed_count"], 10)
        self.assertEqual(summary["claim"], NO_PRODUCTION_CLAIM)
        self.assertTrue(all(result["task_ran"] for result in summary["results"]))
        self.assertTrue(all(result["document_parsed"] for result in summary["results"]))
        self.assertTrue(all(result["clauses_structured"] for result in summary["results"]))
        self.assertTrue(all(result["playbook_hit"] for result in summary["results"]))
        self.assertTrue(all(result["risk_has_evidence"] for result in summary["results"]))
        self.assertTrue(all(result["feedback_memory_written"] for result in summary["results"]))
        self.assertTrue(all(result["report_exported"] for result in summary["results"]))
        self.assertTrue(all(not result["failure_reason"] for result in summary["results"]))


def sample_task() -> dict:
    return {
        "task_id": "task_report_test",
        "status": "MEMORY_UPDATED",
        "file_name": "sample-nda.docx",
        "file_type": "docx",
        "review_position": "甲方",
        "message": "ready",
        "matched_rules": [
            {
                "clause_id": "CL-001",
                "matched_rules": [
                    {
                        "rule_id": "NDA-R001",
                        "playbook_version": "nda-v1",
                    }
                ],
            }
        ],
        "risk_findings": [
            {
                "risk_id": "RISK-001",
                "risk_type": "保密信息范围过宽",
                "severity": "中",
                "risk_reason": "证据文本命中 Playbook。",
                "clause_id": "CL-001",
                "evidence_text": "任何商业信息",
                "revision_suggestion": "限定保密信息范围。",
                "review_status": "CONFIRMED_RISK",
                "include_in_report": True,
                "feedback": {"action_label": "采纳"},
            },
            {
                "risk_id": "RISK-002",
                "risk_type": "不应导出的忽略风险",
                "severity": "低",
                "risk_reason": "ignored",
                "clause_id": "CL-002",
                "evidence_text": "ignored evidence",
                "revision_suggestion": "ignored suggestion",
                "review_status": "IGNORED_RISK",
                "include_in_report": False,
            },
            {
                "risk_id": "RISK-003",
                "risk_type": "不应导出的待复核风险",
                "severity": "高",
                "risk_reason": "pending",
                "clause_id": "CL-003",
                "evidence_text": "pending evidence",
                "revision_suggestion": "pending suggestion",
                "review_status": "NEED_MANUAL_REVIEW",
                "include_in_report": True,
            },
        ],
        "logs": [
            {
                "task_id": "task_report_test",
                "step_name": "execution log should not export",
                "tool_name": "parse_document",
                "status": "success",
                "latency_ms": 1,
                "input_summary": "secret prompt",
                "output_summary": "secret output",
                "token_cost_summary": "not_applicable",
                "error_message": "",
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
