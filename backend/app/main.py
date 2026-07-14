from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ntpath
import re
from urllib.parse import urlparse

from config import settings
from models.log import StepLog
from models.review import ReviewStatus
from services.event_service import review_event_store
from services.evaluation_service import run_basic_evaluation
from services.feedback_service import apply_feedback_to_task
from services.local_review_service import run_local_review
from services.log_service import invoke_tool
from services.task_service import start_review_task
from tools.registry import tool_contracts_payload, tool_registry


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


class ReviewAgentHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, {"status": "ok"})

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            json_response(
                self,
                {
                    "status": "ok",
                    "service": settings.service_name,
                    "scope": "task-9",
                },
            )
            return

        if path == "/api/statuses":
            json_response(
                self,
                {
                    "statuses": [
                        ReviewStatus.START.value,
                        ReviewStatus.UPLOAD_RECEIVED.value,
                        ReviewStatus.DOCUMENT_PARSED.value,
                        ReviewStatus.CLAUSES_STRUCTURED.value,
                        ReviewStatus.PLAYBOOK_RETRIEVED.value,
                        ReviewStatus.CONTEXT_BUILT.value,
                        ReviewStatus.RISK_ANALYZED.value,
                        ReviewStatus.EVIDENCE_VERIFIED.value,
                        ReviewStatus.HUMAN_REVIEW_PENDING.value,
                        ReviewStatus.MEMORY_UPDATED.value,
                        ReviewStatus.REPORT_READY.value,
                        ReviewStatus.PARSE_FAILED.value,
                        ReviewStatus.RETRIEVAL_FAILED.value,
                        ReviewStatus.LLM_OUTPUT_INVALID.value,
                        ReviewStatus.EVIDENCE_MISSING.value,
                        ReviewStatus.NEED_MANUAL_REVIEW.value,
                        ReviewStatus.TASK_ERROR.value,
                    ]
                },
            )
            return

        if path == "/api/tools":
            json_response(self, {"tools": tool_contracts_payload()})
            return

        task_match = re.fullmatch(r"/api/tasks/([^/]+)", path)
        if task_match:
            task = review_event_store.get_task(task_match.group(1))
            if task is None:
                json_response(self, {"error": "Task not found"}, status=404)
                return
            json_response(self, task.to_dict())
            return

        event_match = re.fullmatch(r"/api/tasks/([^/]+)/events", path)
        if event_match:
            self._stream_task_events(event_match.group(1))
            return

        json_response(self, {"error": "Not found"}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path

        feedback_match = re.fullmatch(r"/api/tasks/([^/]+)/feedback", path)
        if feedback_match:
            self._handle_feedback(feedback_match.group(1))
            return

        report_match = re.fullmatch(r"/api/tasks/([^/]+)/report", path)
        if report_match:
            self._handle_report(report_match.group(1))
            return

        if path == "/api/local-review":
            self._handle_local_review()
            return

        if path == "/api/evaluation/run":
            self._handle_evaluation()
            return

        if path != "/api/tasks":
            json_response(self, {"error": "Not found"}, status=404)
            return

        try:
            form = self._read_multipart_form()
            task = start_review_task(
                file_name=form["file_name"],
                content=form["file_content"],
                review_position_value=form["review_position"],
            )
        except ValueError as exc:
            json_response(
                self,
                {
                    "status": ReviewStatus.TASK_ERROR.value,
                    "message": str(exc),
                },
                status=400,
            )
            return

        json_response(self, task.to_dict(), status=201)

    def _handle_feedback(self, task_id: str) -> None:
        try:
            payload = self._read_json_body()
            result = apply_feedback_to_task(task_id, payload)
        except (ValueError, json.JSONDecodeError) as exc:
            json_response(
                self,
                {
                    "status": ReviewStatus.TASK_ERROR.value,
                    "message": str(exc) or "人工反馈请求无效。",
                },
                status=400,
            )
            return

        json_response(self, result, status=200)

    def _handle_report(self, task_id: str) -> None:
        task = review_event_store.get_task(task_id)
        if task is None:
            json_response(self, {"message": "未找到审查任务，无法生成报告。"}, status=404)
            return

        logs = [StepLog(**item) for item in (task.logs or [])]
        try:
            result = invoke_tool(
                task_id,
                tool_registry,
                "generate_report",
                {"task_id": task_id, "task": task.to_dict()},
                logs,
                step_name="report_generation",
            )
        except ValueError as exc:
            json_response(
                self,
                {
                    "status": ReviewStatus.TASK_ERROR.value,
                    "message": str(exc) or "报告生成请求无效。",
                },
                status=400,
            )
            return

        report_file = result["report_file"]
        updated_task = review_event_store.update_task(
            task_id,
            ReviewStatus.REPORT_READY,
            "Markdown 审查报告已生成。",
            step_name="report_ready",
            tool_name="generate_report",
            report_file=report_file,
            logs=[log.to_dict() for log in logs],
        )
        json_response(
            self,
            {
                "status": ReviewStatus.REPORT_READY.value,
                "message": updated_task.message,
                "report_file": report_file,
                "task": updated_task.to_dict(),
            },
            status=200,
        )

    def _handle_evaluation(self) -> None:
        try:
            result = run_basic_evaluation()
        except ValueError as exc:
            json_response(
                self,
                {
                    "status": ReviewStatus.TASK_ERROR.value,
                    "message": str(exc) or "基础评测请求无效。",
                },
                status=400,
            )
            return

        json_response(self, result, status=200)

    def _handle_local_review(self) -> None:
        try:
            payload = self._read_json_body()
            task_id = str(payload.get("task_id", "")).strip()
            task = review_event_store.get_task(task_id)
            if task is None:
                json_response(self, {"message": "未找到审查任务，无法执行局部审查。"}, status=404)
                return

            result = run_local_review(
                task.to_dict(),
                clause_id=str(payload.get("clause_id", "")),
                selected_text=str(payload.get("selected_text", "")),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            json_response(
                self,
                {
                    "status": ReviewStatus.TASK_ERROR.value,
                    "message": str(exc) or "局部审查请求无效。",
                },
                status=400,
            )
            return

        json_response(self, result, status=200)

    def log_message(self, format, *args):
        return

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw_body = self.rfile.read(length).decode("utf-8")
        return json.loads(raw_body)

    def _stream_task_events(self, task_id: str) -> None:
        if review_event_store.get_task(task_id) is None:
            json_response(self, {"error": "Task not found"}, status=404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        next_index = 0
        while True:
            events = review_event_store.wait_for_events(task_id, next_index, timeout_seconds=10.0)
            if not events:
                self._write_sse_comment("keep-alive")
            for event in events:
                self._write_sse_event(event)
                next_index = event["event_id"] + 1
            if review_event_store.is_terminal(task_id) and not events:
                break
            if review_event_store.is_terminal(task_id) and events:
                break

    def _write_sse_event(self, event: dict) -> None:
        body = json.dumps(event, ensure_ascii=False)
        self.wfile.write(f"event: review_event\n".encode("utf-8"))
        self.wfile.write(f"id: {event['event_id']}\n".encode("utf-8"))
        self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _write_sse_comment(self, comment: str) -> None:
        self.wfile.write(f": {comment}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_multipart_form(self) -> dict:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("请使用 multipart/form-data 上传合同文件。")

        boundary_match = re.search(r'boundary="?([^";]+)"?', content_type)
        if boundary_match is None:
            raise ValueError("上传请求缺少 multipart boundary。")

        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            raise ValueError("上传请求为空。")
        if length > settings.max_upload_bytes:
            raise ValueError(f"上传文件超过大小限制，当前上限为 {settings.max_upload_bytes} 字节。")

        body = self.rfile.read(length)
        boundary = f"--{boundary_match.group(1)}".encode("utf-8")
        fields: dict[str, str] = {}
        file_name = ""
        file_content = b""

        for raw_part in body.split(boundary):
            part = raw_part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            if part.endswith(b"--"):
                part = part[:-2].strip(b"\r\n")
            if b"\r\n\r\n" not in part:
                continue

            header_bytes, content = part.split(b"\r\n\r\n", 1)
            if content.endswith(b"\r\n"):
                content = content[:-2]

            headers = header_bytes.decode("latin-1", errors="ignore")
            disposition = _get_header_line(headers, "content-disposition")
            name = _extract_disposition_value(disposition, "name")
            filename = _extract_disposition_value(disposition, "filename")
            if not name:
                continue

            if filename:
                file_name = ntpath.basename(filename.strip())
                file_content = content
            else:
                fields[name] = content.decode("utf-8", errors="ignore").strip()

        if not file_name or not file_content:
            raise ValueError("请上传 .docx 或 .pdf 合同文件。")

        return {
            "file_name": file_name,
            "file_content": file_content,
            "review_position": fields.get("review_position", ""),
        }


def _get_header_line(headers: str, header_name: str) -> str:
    prefix = f"{header_name.lower()}:"
    for line in headers.splitlines():
        if line.lower().startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _extract_disposition_value(disposition: str, key: str) -> str:
    match = re.search(rf'{key}="([^"]*)"', disposition)
    return match.group(1) if match else ""


def run() -> None:
    server = ThreadingHTTPServer((settings.host, settings.port), ReviewAgentHandler)
    print(f"Backend running at http://{settings.host}:{settings.port}")
    print(f"Health check: http://{settings.host}:{settings.port}/health")
    server.serve_forever()


if __name__ == "__main__":
    run()
