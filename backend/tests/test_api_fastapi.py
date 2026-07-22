from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import asyncio
import json
import sys
import time
import unittest

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

import db.sqlite as sqlite_module
import services.evaluation_service as evaluation_service
import services.report_service as report_service
from api.errors import ApiError
from api.routes import evaluation as evaluation_route
from api.routes.tasks import _read_upload
from config import Settings
from main import create_app
from models.review import ReviewPosition
from services.event_service import ReviewEventStore
from services.evaluation_service import _docx_bytes_from_text
from test_document_pipeline import build_pdf_bytes


CONTRACT_PATH = Path(__file__).resolve().parent / "fixtures" / "api_contracts" / "current_http_server.json"
TERMINAL_STATUSES = {
    "EVIDENCE_VERIFIED",
    "HUMAN_REVIEW_PENDING",
    "PARSE_FAILED",
    "RETRIEVAL_FAILED",
    "LLM_OUTPUT_INVALID",
    "EVIDENCE_MISSING",
    "NEED_MANUAL_REVIEW",
    "UNSUPPORTED_CONTRACT_TYPE",
    "REPORT_READY",
    "TASK_ERROR",
}
OBSERVABILITY_TASK_KEYS = {"trace_id", "recovery_count", "recovery_from_status"}
RUNTIME_TASK_KEYS = {
    "retry_counts",
    "retry_limits",
    "recovery_history",
    "cancel_requested_at",
    "cancelled_at",
    "cancel_reason",
    "execution_active",
    "last_timeout",
}


class FastApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = TemporaryDirectory()
        cls.temp_path = Path(cls.temp_dir.name)
        cls.settings = Settings(
            host="127.0.0.1",
            port=8000,
            max_upload_bytes=10 * 1024 * 1024,
            allowed_origins=("http://allowed.test",),
            llm_mode="local_structured",
            memory_db_path=str(cls.temp_path / "memory.sqlite3"),
            service_name="contract-review-agent",
        )
        cls.event_store = ReviewEventStore()
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        sample_text = (PROJECT_ROOT / "samples" / "nda_sample_10.txt").read_text(encoding="utf-8")
        cls.docx_bytes = _docx_bytes_from_text(sample_text)

        cls.original_sqlite_settings = sqlite_module.settings
        cls.original_report_dir = report_service.REPORT_DIR
        cls.original_evaluation_output_dir = evaluation_service.DEFAULT_OUTPUT_DIR
        sqlite_module.settings = cls.settings
        report_service.REPORT_DIR = cls.temp_path / "reports"
        evaluation_service.DEFAULT_OUTPUT_DIR = cls.temp_path / "evaluation"

        cls.app = create_app(cls.settings, cls.event_store)
        cls.client_context = TestClient(cls.app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        sqlite_module.settings = cls.original_sqlite_settings
        report_service.REPORT_DIR = cls.original_report_dir
        evaluation_service.DEFAULT_OUTPUT_DIR = cls.original_evaluation_output_dir
        cls.temp_dir.cleanup()

    def test_health_statuses_tools_and_static_frontend(self):
        health_contract = self.contract["success"]["health"]
        response = self.client.get(health_contract["path"])
        self.assertEqual(response.status_code, health_contract["status"])
        self.assertEqual(response.json(), health_contract["body"])

        statuses_contract = self.contract["success"]["statuses"]
        response = self.client.get(statuses_contract["path"])
        self.assertEqual(response.status_code, statuses_contract["status"])
        self.assertEqual(response.json(), statuses_contract["body"])

        tools_contract = self.contract["success"]["tools"]
        response = self.client.get(tools_contract["path"])
        self.assertEqual(response.status_code, tools_contract["status"])
        self.assertEqual(_normalize_tools(response.json()["tools"]), tools_contract["tools"])

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn('/src/main.js', page.text)
        self.assertEqual(self.client.get("/src/main.js").status_code, 200)
        self.assertEqual(self.client.get("/src/styles.css").status_code, 200)

    def test_task_create_query_and_sse_match_baseline(self):
        created = self._upload_docx()
        create_contract = self.contract["success"]["task_create"]
        self.assertEqual(
            set(create_contract["required_keys"]) | OBSERVABILITY_TASK_KEYS,
            set(created),
        )
        self.assertTrue(created["trace_id"].startswith("trace_"))
        self.assertEqual(created["recovery_count"], 0)
        self.assertEqual(created["recovery_from_status"], "")
        for key, value in create_contract["response_body_summary"].items():
            self.assertEqual(created[key], value)
        self.assertEqual(created["llm_mode"], "local_structured")

        task = self._wait_for_terminal_task(created["task_id"])
        task_contract = self.contract["success"]["task_get"]
        self.assertEqual(
            set(task_contract["required_keys"])
            | OBSERVABILITY_TASK_KEYS
            | RUNTIME_TASK_KEYS,
            set(task),
        )
        self.assertEqual(task["trace_id"], created["trace_id"])
        summary = task_contract["response_body_summary"]
        self.assertEqual(task["status"], summary["status"])
        self.assertEqual(len(task["clauses"]), summary["clause_count"])
        self.assertEqual(len(task["risk_findings"]), summary["risk_count"])
        self.assertEqual(len(task["logs"]), summary["log_count"])
        self.assertFalse(task["execution_active"])
        self.assertEqual(task["retry_counts"], {})
        self.assertEqual(task["retry_limits"]["node_timeout"], 2)

        with self.client.stream("GET", f"/api/tasks/{created['task_id']}/events") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers["content-type"],
                self.contract["success"]["events"]["content_type"],
            )
            body = "".join(response.iter_text())

        events = _parse_sse(body)
        event_contract = self.contract["success"]["events"]
        self.assertEqual([event["data"]["status"] for event in events], event_contract["sequence"])
        self.assertEqual(events[0]["event"], event_contract["event_name"])
        self.assertEqual(events[0]["id"], str(event_contract["response_event_example"]["id"]))
        self.assertEqual(set(events[0]["data"]), set(event_contract["event_required_keys"]))
        self.assertIsInstance(events[0]["data"]["event_id"], int)
        self.assertIsInstance(events[0]["data"]["task"], dict)
        self.assertEqual(events[0]["data"]["task"]["trace_id"], created["trace_id"])

    def test_local_review_feedback_and_report_match_baseline(self):
        created = self._upload_docx()
        task = self._wait_for_terminal_task(created["task_id"])
        local_contract = self.contract["success"]["local_review"]
        selected_text = local_contract["request_example"]["body"]["selected_text"]

        response = self.client.post(
            local_contract["path"],
            json={
                "task_id": task["task_id"],
                "clause_id": "CL-001",
                "selected_text": selected_text,
            },
        )
        self.assertEqual(response.status_code, local_contract["status"])
        local_result = response.json()
        self.assertEqual(set(local_contract["required_keys"]), set(local_result))
        for key, value in local_contract["response_body_summary"].items():
            self.assertEqual(local_result[key], value)

        risk = task["risk_findings"][0]
        feedback_contract = self.contract["success"]["feedback"]
        response = self.client.post(
            f"/api/tasks/{task['task_id']}/feedback",
            json={
                "risk_id": risk["risk_id"],
                "action": "accept",
                "final_severity": risk["severity"],
                "final_suggestion": risk["revision_suggestion"],
                "ignore_reason": "",
                "include_in_report": True,
            },
        )
        self.assertEqual(response.status_code, feedback_contract["status"])
        feedback_result = response.json()
        self.assertEqual(set(feedback_contract["required_keys"]), set(feedback_result))
        self.assertEqual(feedback_result["status"], "MEMORY_UPDATED")
        self.assertEqual(feedback_result["risk"]["review_status"], "CONFIRMED_RISK")
        self.assertTrue(feedback_result["risk"]["include_in_report"])

        report_contract = self.contract["success"]["report"]
        response = self.client.post(f"/api/tasks/{task['task_id']}/report")
        self.assertEqual(response.status_code, report_contract["status"])
        report_result = response.json()
        self.assertEqual(set(report_contract["required_keys"]), set(report_result))
        self.assertEqual(set(report_contract["report_file_required_keys"]), set(report_result["report_file"]))
        self.assertEqual(report_result["status"], "REPORT_READY")
        self.assertEqual(report_result["task"]["status"], "REPORT_READY")
        self.assertEqual(report_result["report_file"]["risk_count"], 1)

    def test_event_stream_sends_terminal_event_when_subscribed_immediately(self):
        created = self._upload_docx()

        with self.client.stream(
            "GET", f"/api/tasks/{created['task_id']}/events"
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(response.iter_text())

        events = _parse_sse(body)
        statuses = [event["data"]["status"] for event in events]
        self.assertEqual(statuses, self.contract["success"]["events"]["sequence"])
        self.assertIn(statuses[-1], TERMINAL_STATUSES)

    def test_evaluation_route_matches_baseline(self):
        evaluation_contract = self.contract["success"]["evaluation"]
        response = self.client.post(evaluation_contract["path"])
        self.assertEqual(response.status_code, evaluation_contract["status"])
        result = response.json()
        self.assertEqual(set(evaluation_contract["required_keys"]), set(result))
        self.assertEqual(result["sample_count"], 10)
        self.assertEqual(result["passed_count"], 10)
        self.assertEqual(result["claim"], evaluation_contract["response_body_summary"]["claim"])
        self.assertTrue(all(set(evaluation_contract["result_required_keys"]) == set(item) for item in result["results"]))

        effect_contract = self.contract["success"]["effect_evaluation"]
        response = self.client.post(effect_contract["path"])
        self.assertEqual(response.status_code, effect_contract["status"])
        effect = response.json()
        self.assertEqual(set(effect_contract["required_keys"]), set(effect))
        self.assertEqual(
            effect["evaluation_type"],
            effect_contract["response_body_summary"]["evaluation_type"],
        )
        self.assertEqual(effect["sample_count"], effect_contract["response_body_summary"]["sample_count"])
        self.assertEqual(len(effect["metrics"]), effect_contract["response_body_summary"]["metric_count"])
        self.assertEqual(effect["claim"], effect_contract["response_body_summary"]["claim"])
        self.assertTrue(
            all(set(effect_contract["metric_required_keys"]) == set(item) for item in effect["metrics"])
        )

        subset_response = self.client.post(
            effect_contract["path"],
            json={"contract_ids": ["nda-01"], "run_label": "api-subset"},
        )
        self.assertEqual(subset_response.status_code, 200, subset_response.text)
        subset = subset_response.json()
        self.assertEqual(subset["sample_count"], 1)
        self.assertEqual(subset["runtime"]["run_label"], "api-subset")

    def test_missing_routes_and_tasks_keep_error_contracts(self):
        missing_task = self.contract["errors"]["missing_task"]
        response = self.client.get(missing_task["path"])
        self.assertEqual((response.status_code, response.json()), (missing_task["status"], missing_task["body"]))

        response = self.client.get("/api/tasks/task_missing/events")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Task not found"})

        response = self.client.post("/api/tasks/task_missing/report")
        self.assertEqual(response.status_code, 404)
        self.assertIn("未找到审查任务", response.json()["message"])

        response = self.client.post(
            "/api/local-review",
            json={"task_id": "task_missing", "clause_id": "CL-001", "selected_text": "示例"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("未找到审查任务", response.json()["message"])

        response = self.client.post(
            "/api/tasks/task_missing/feedback",
            json={"risk_id": "RISK-001", "action": "accept"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "TASK_ERROR")

        missing_route = self.contract["errors"]["missing_route"]
        response = self.client.get(missing_route["path"])
        self.assertEqual((response.status_code, response.json()), (missing_route["status"], missing_route["body"]))

    def test_pydantic_and_task_errors_return_stable_400_payloads(self):
        malformed = self.contract["errors"]["malformed_json"]
        response = self.client.post(
            malformed["path"],
            content=malformed["request_example"]["raw_body"],
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, malformed["status"])
        self.assertEqual(response.json()["status"], "TASK_ERROR")
        self.assertTrue(response.json()["message"])

        response = self.client.post(
            "/api/tasks/task_missing/feedback",
            json={"risk_id": "RISK-001", "action": "unsupported"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "TASK_ERROR")

        unfinished = self.event_store.create_task("unfinished.docx", "docx", ReviewPosition.PARTY_A)
        response = self.client.post(f"/api/tasks/{unfinished.task_id}/report")
        expected = self.contract["errors"]["unfinished_report"]
        self.assertEqual((response.status_code, response.json()), (expected["status"], expected["body"]))

        with patch.object(evaluation_route, "run_basic_evaluation", side_effect=ValueError("评测输入无效。")):
            response = self.client.post("/api/evaluation/run")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"status": "TASK_ERROR", "message": "评测输入无效。"})

    def test_upload_validation_and_safe_file_name(self):
        missing_multipart = self.contract["errors"]["missing_multipart"]
        response = self.client.post(missing_multipart["path"], json={})
        self.assertEqual((response.status_code, response.json()), (missing_multipart["status"], missing_multipart["body"]))

        unsupported = self.contract["errors"]["unsupported_upload"]
        response = self._post_upload("baseline.txt", b"synthetic text", "甲方", "text/plain")
        self.assertEqual((response.status_code, response.json()), (unsupported["status"], unsupported["body"]))

        invalid_position = self.contract["errors"]["invalid_parameter"]
        response = self._post_upload("baseline.docx", self.docx_bytes, "法务")
        self.assertEqual((response.status_code, response.json()), (invalid_position["status"], invalid_position["body"]))

        response = self._post_upload("baseline.docx", self.docx_bytes, "甲方", "text/plain")
        self.assertEqual(response.status_code, 400)
        self.assertIn("MIME", response.json()["message"])

        response = self._post_upload("baseline.docx", b"not-a-zip", "甲方")
        self.assertEqual(response.status_code, 400)
        self.assertIn("DOCX 文件签名无效", response.json()["message"])

        response = self._post_upload(
            "baseline.docx",
            self.docx_bytes,
            "甲方",
            llm_mode="unsupported",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("LLM 模式无效", response.json()["message"])

        response = self._post_upload(
            "baseline.docx",
            self.docx_bytes,
            "甲方",
            llm_mode="openai_compatible",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("DeepSeek 配置不完整", response.json()["message"])
        self.assertNotIn("API Key is not configured", response.text)

        response = self._post_upload("baseline.pdf", b"not-a-pdf", "甲方", "application/pdf")
        self.assertEqual(response.status_code, 400)
        self.assertIn("PDF 文件签名无效", response.json()["message"])

        response = self._post_upload("baseline.docx", b"", "甲方")
        self.assertEqual(response.status_code, 400)
        self.assertIn("上传文件为空", response.json()["message"])

        response = self._post_upload("..\\safe.docx", self.docx_bytes, "甲方")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["file_name"], "safe.docx")
        self._wait_for_terminal_task(response.json()["task_id"])

        response = self._post_upload("baseline.pdf", build_pdf_bytes(), "甲方", "application/pdf")
        self.assertEqual(response.status_code, 201, response.text)
        self._wait_for_terminal_task(response.json()["task_id"])

    def test_upload_limit_cors_and_lifespan(self):
        limited_settings = Settings(
            host="127.0.0.1",
            port=8000,
            max_upload_bytes=8,
            allowed_origins=("http://allowed.test",),
            llm_mode="local_structured",
            memory_db_path=str(self.temp_path / "limited-memory.sqlite3"),
            service_name="contract-review-agent",
        )
        limited_app = create_app(limited_settings, ReviewEventStore())
        self.assertFalse(limited_app.state.accepting_tasks)

        with TestClient(limited_app) as client:
            self.assertTrue(limited_app.state.accepting_tasks)
            response = client.post(
                "/api/tasks",
                data={"review_position": "甲方"},
                files={"contract_file": ("large.docx", b"PK\x03\x04large", "application/octet-stream")},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("超过大小限制", response.json()["message"])

            allowed = client.options(
                "/api/tasks",
                headers={
                    "Origin": "http://allowed.test",
                    "Access-Control-Request-Method": "POST",
                },
            )
            self.assertEqual(allowed.headers.get("access-control-allow-origin"), "http://allowed.test")
            self.assertNotEqual(allowed.headers.get("access-control-allow-origin"), "*")

            denied = client.options(
                "/api/tasks",
                headers={
                    "Origin": "http://denied.test",
                    "Access-Control-Request-Method": "POST",
                },
            )
            self.assertNotIn("access-control-allow-origin", denied.headers)

            limited_app.state.accepting_tasks = False
            response = client.post(
                "/api/tasks",
                data={"review_position": "甲方"},
                files={"contract_file": ("sample.docx", self.docx_bytes, "application/octet-stream")},
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["status"], "TASK_ERROR")

        self.assertFalse(limited_app.state.accepting_tasks)

    def test_upload_reader_stops_after_first_over_limit_chunk(self):
        upload = _RecordingUpload([b"1234", b"56789", b"must-not-be-read"])

        with self.assertRaises(ApiError) as context:
            asyncio.run(_read_upload(upload, max_upload_bytes=8))

        self.assertIn("超过大小限制", context.exception.payload["message"])
        self.assertEqual(upload.read_count, 2)
        self.assertTrue(upload.closed)

    def _upload_docx(self) -> dict:
        response = self._post_upload("baseline.docx", self.docx_bytes, "甲方")
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _post_upload(
        self,
        file_name: str,
        content: bytes,
        review_position: str,
        content_type: str = "application/octet-stream",
        llm_mode: str = "local_structured",
    ):
        return self.client.post(
            "/api/tasks",
            data={"review_position": review_position, "llm_mode": llm_mode},
            files={"contract_file": (file_name, content, content_type)},
        )

    def _wait_for_terminal_task(self, task_id: str) -> dict:
        deadline = time.time() + 10
        while time.time() < deadline:
            response = self.client.get(f"/api/tasks/{task_id}")
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            if task["status"] in TERMINAL_STATUSES:
                return task
            time.sleep(0.05)
        self.fail("review task did not reach terminal status")


def _normalize_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "name": tool["name"],
            "input_keys": list(tool["input_schema"]),
            "output_keys": list(tool["output_schema"]),
            "calls_llm": tool["calls_llm"],
            "runtime_calls_llm": tool["runtime_calls_llm"],
            "runtime_llm_mode": tool["runtime_llm_mode"],
        }
        for tool in tools
    ]


class _RecordingUpload:
    def __init__(self, chunks: list[bytes]):
        self._chunks = iter(chunks)
        self.read_count = 0
        self.closed = False

    async def read(self, _size: int) -> bytes:
        self.read_count += 1
        return next(self._chunks, b"")

    async def close(self) -> None:
        self.closed = True


def _parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        data_line = next((line for line in lines if line.startswith("data: ")), "")
        if not data_line:
            continue
        event_line = next(line for line in lines if line.startswith("event: "))
        id_line = next(line for line in lines if line.startswith("id: "))
        events.append(
            {
                "event": event_line.removeprefix("event: "),
                "id": id_line.removeprefix("id: "),
                "data": json.loads(data_line.removeprefix("data: ")),
            }
        )
    return events


if __name__ == "__main__":
    unittest.main()
