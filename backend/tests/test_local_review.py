from pathlib import Path
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from main import ReviewAgentHandler
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
        server = ThreadingHTTPServer(("127.0.0.1", 0), ReviewAgentHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"

        try:
            created = _upload_docx(base_url)
            task = _wait_for_terminal_task(base_url, created["task_id"])

            error = _post_json(
                base_url,
                "/api/local-review",
                {
                    "task_id": task["task_id"],
                    "clause_id": "CL-001",
                    "selected_text": "不属于该条款的文字",
                },
                expect_error=True,
            )
            self.assertEqual(error["status"], "TASK_ERROR")
            self.assertIn("框选文本不属于当前条款", error["message"])

            result = _post_json(
                base_url,
                "/api/local-review",
                {
                    "task_id": task["task_id"],
                    "clause_id": "CL-001",
                    "selected_text": "保密信息是指披露方提供的商业信息。",
                },
            )
            self.assertEqual(result["scope"], "local_selection")
            self.assertFalse(result["formal_risk_generated"])
            self.assertTrue(result["local_findings"])
        finally:
            server.shutdown()
            server.server_close()


def _upload_docx(base_url: str) -> dict:
    boundary = "codexBoundary"
    body = b"".join(
        [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"review_position\"\r\n\r\n甲方\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"contract_file\"; filename=\"sample.docx\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode("utf-8"),
            build_docx_bytes(),
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib.request.Request(
        f"{base_url}/api/tasks",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))


def _wait_for_terminal_task(base_url: str, task_id: str) -> dict:
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
        task = json.loads(urllib.request.urlopen(f"{base_url}/api/tasks/{task_id}", timeout=5).read().decode("utf-8"))
        if task["status"] in terminal_statuses:
            return task
        time.sleep(0.05)
    raise AssertionError("review task did not reach terminal status")


def _post_json(base_url: str, path: str, payload: dict, expect_error: bool = False) -> dict:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        response_body = urllib.request.urlopen(request, timeout=5).read().decode("utf-8")
        return json.loads(response_body)
    except urllib.error.HTTPError as exc:
        if not expect_error:
            raise
        return json.loads(exc.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
