from pathlib import Path
import sys
import time
import unittest

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from main import create_app
from services.event_service import ReviewEventStore
from services.local_review_service import run_local_review
from services.task_service import create_review_task
from test_document_pipeline import build_docx_bytes


class LocalReviewTest(unittest.TestCase):
    def test_local_review_returns_separate_result_without_mutating_formal_risks(self):
        task = create_review_task(
            file_name="sample.docx",
            content=build_docx_bytes(),
            review_position_value="甲方",
        )
        payload = task.to_dict()
        formal_risk_count = len(payload["risk_findings"])

        result = run_local_review(
            payload,
            clause_id="CL-001",
            selected_text="保密信息是指披露方提供的商业信息。",
        )

        self.assertEqual(result["scope"], "local_selection")
        self.assertFalse(result["formal_risk_generated"])
        self.assertEqual(len(payload["risk_findings"]), formal_risk_count)
        self.assertEqual(result["local_findings"][0]["scope"], "local_selection")
        self.assertFalse(result["local_findings"][0]["formal_risk"])
        self.assertEqual(result["local_findings"][0]["source_clause_id"], "CL-001")
        self.assertTrue(result["related_formal_risks"])

    def test_local_review_rejects_text_outside_clause(self):
        task = create_review_task(
            file_name="sample.docx",
            content=build_docx_bytes(),
            review_position_value="甲方",
        )

        with self.assertRaisesRegex(ValueError, "框选文本不属于当前条款"):
            run_local_review(
                task.to_dict(),
                clause_id="CL-001",
                selected_text="接收方应仅用于评估合作目的",
            )

    def test_http_local_review_endpoint_returns_friendly_error_and_result(self):
        app = create_app(event_store=ReviewEventStore())
        with TestClient(app) as client:
            created = _upload_docx(client)
            task = _wait_for_terminal_task(client, created["task_id"])

            error_response = client.post(
                "/api/local-review",
                json={
                    "task_id": task["task_id"],
                    "clause_id": "CL-001",
                    "selected_text": "不属于该条款的文字",
                },
            )
            self.assertEqual(error_response.status_code, 400)
            error = error_response.json()
            self.assertEqual(error["status"], "TASK_ERROR")
            self.assertIn("框选文本不属于当前条款", error["message"])

            result_response = client.post(
                "/api/local-review",
                json={
                    "task_id": task["task_id"],
                    "clause_id": "CL-001",
                    "selected_text": "保密信息是指披露方提供的商业信息。",
                },
            )
            self.assertEqual(result_response.status_code, 200)
            result = result_response.json()
            self.assertEqual(result["scope"], "local_selection")
            self.assertFalse(result["formal_risk_generated"])
            self.assertTrue(result["local_findings"])


def _upload_docx(client: TestClient) -> dict:
    response = client.post(
        "/api/tasks",
        data={"review_position": "甲方"},
        files={
            "contract_file": (
                "sample.docx",
                build_docx_bytes(),
                "application/octet-stream",
            )
        },
    )
    if response.status_code != 201:
        raise AssertionError(response.text)
    return response.json()


def _wait_for_terminal_task(client: TestClient, task_id: str) -> dict:
    terminal_statuses = {
        "EVIDENCE_VERIFIED",
        "HUMAN_REVIEW_PENDING",
        "PARSE_FAILED",
        "RETRIEVAL_FAILED",
        "LLM_OUTPUT_INVALID",
        "EVIDENCE_MISSING",
        "NEED_MANUAL_REVIEW",
        "REPORT_READY",
    }
    deadline = time.time() + 5
    while time.time() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        if response.status_code != 200:
            raise AssertionError(response.text)
        task = response.json()
        if task["status"] in terminal_statuses:
            return task
        time.sleep(0.05)
    raise AssertionError("review task did not reach terminal status")


if __name__ == "__main__":
    unittest.main()
