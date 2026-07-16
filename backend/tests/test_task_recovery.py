from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sqlite3
import sys
import time
import unittest

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from config import Settings
from db.repositories import ReviewPersistence
from main import create_app
from models.log import StepLog
from models.review import ReviewPosition, ReviewStatus
from services.event_service import ReviewEventStore
from services.log_service import invoke_tool
from test_document_pipeline import build_docx_bytes
from tools.registry import tool_registry


TERMINAL_STATUSES = {
    ReviewStatus.EVIDENCE_VERIFIED.value,
    ReviewStatus.HUMAN_REVIEW_PENDING.value,
    ReviewStatus.NEED_MANUAL_REVIEW.value,
    ReviewStatus.PARSE_FAILED.value,
    ReviewStatus.RETRIEVAL_FAILED.value,
    ReviewStatus.LLM_OUTPUT_INVALID.value,
    ReviewStatus.EVIDENCE_MISSING.value,
    ReviewStatus.TASK_ERROR.value,
}


class TaskRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = str(self.root / "review.sqlite3")
        self.upload_dir = self.root / "uploads"
        self.persistence = ReviewPersistence(self.db_path, str(self.upload_dir))
        self.settings = Settings(
            host="127.0.0.1",
            port=8000,
            max_upload_bytes=10 * 1024 * 1024,
            allowed_origins=(),
            llm_mode="local_structured",
            memory_db_path=self.db_path,
            upload_dir=str(self.upload_dir),
            service_name="contract-review-agent",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lifespan_resumes_from_last_complete_node_without_repeating_tools(self):
        task_id = self._persist_clauses_structured_task()
        restarted_store = ReviewEventStore(self.persistence)
        app = create_app(self.settings, restarted_store)

        with TestClient(app) as client:
            task = self._wait_for_terminal(client, task_id)
            self.assertEqual(app.state.recovered_task_ids, [task_id])

        self.assertIn(task["status"], {"EVIDENCE_VERIFIED", "HUMAN_REVIEW_PENDING"})
        tool_names = [log["tool_name"] for log in task["logs"]]
        self.assertEqual(tool_names.count("parse_document"), 1)
        self.assertEqual(tool_names.count("classify_contract_type"), 1)
        self.assertEqual(tool_names.count("extract_clauses"), 1)
        self.assertEqual(
            [event["step_name"] for event in task["events"]].count("recovery_started"),
            1,
        )
        metadata = self.persistence.task_repository.get(task_id)
        self.assertEqual(metadata["recovery_count"], 1)
        self.assertEqual(metadata["recovery_from_status"], "CLAUSES_STRUCTURED")

    def test_missing_upload_moves_task_to_manual_review(self):
        task_id = self._persist_upload_received_task()
        upload = self.persistence.document_repository.get_upload(task_id)
        (self.upload_dir / upload["stored_path"]).unlink()

        task = self._start_and_get_task(task_id)

        self.assertEqual(task["status"], "NEED_MANUAL_REVIEW")
        self.assertIn("上传文件缺失", task["message"])
        self.assertEqual(task["events"][-1]["step_name"], "recovery_blocked")

    def test_hash_mismatch_moves_task_to_manual_review(self):
        task_id = self._persist_upload_received_task()
        upload = self.persistence.document_repository.get_upload(task_id)
        (self.upload_dir / upload["stored_path"]).write_bytes(b"tampered")

        task = self._start_and_get_task(task_id)

        self.assertEqual(task["status"], "NEED_MANUAL_REVIEW")
        self.assertIn("SHA-256 不一致", task["message"])

    def test_incomplete_checkpoint_moves_task_to_manual_review(self):
        store = ReviewEventStore(self.persistence)
        state = store.create_task(
            "incomplete.docx",
            "docx",
            ReviewPosition.PARTY_A,
            content=build_docx_bytes(),
        )
        store.update_task(
            state.task_id,
            ReviewStatus.DOCUMENT_PARSED,
            "incomplete checkpoint",
            step_name="document_parsed",
        )

        task = self._start_and_get_task(state.task_id)

        self.assertEqual(task["status"], "NEED_MANUAL_REVIEW")
        self.assertIn("文档解析结果缺失", task["message"])

    def test_unreadable_upload_moves_task_to_manual_review_without_local_path(self):
        task_id = self._persist_upload_received_task()

        with patch.object(
            Path,
            "read_bytes",
            side_effect=PermissionError(13, "Access denied"),
        ):
            task = self._start_and_get_task(task_id)

        self.assertEqual(task["status"], "NEED_MANUAL_REVIEW")
        self.assertIn("上传文件无法读取", task["message"])
        self.assertNotIn(str(self.upload_dir), task["message"])

    def test_recovery_metadata_and_event_roll_back_together(self):
        task_id = self._persist_upload_received_task()
        restarted_store = ReviewEventStore(self.persistence)
        restarted_store.load_persisted()

        with patch.object(
            self.persistence.event_repository,
            "append",
            side_effect=sqlite3.OperationalError("forced recovery event failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "forced recovery event failure"):
                restarted_store.record_recovery(task_id)

        metadata = self.persistence.task_repository.get(task_id)
        self.assertEqual(metadata["recovery_count"], 0)
        self.assertEqual(metadata["recovery_from_status"], "")
        self.assertEqual(len(self.persistence.event_repository.list_for_task(task_id)), 2)
        current = restarted_store.get_task(task_id)
        self.assertEqual(current.status, ReviewStatus.UPLOAD_RECEIVED)
        self.assertEqual(len(current.events), 2)

    def _persist_upload_received_task(self) -> str:
        store = ReviewEventStore(self.persistence)
        state = store.create_task(
            "pending.docx",
            "docx",
            ReviewPosition.PARTY_A,
            content=build_docx_bytes(),
        )
        state = store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "上传已接收，开始执行文档解析。",
            step_name="upload_received",
        )
        return state.task_id

    def _persist_clauses_structured_task(self) -> str:
        store = ReviewEventStore(self.persistence)
        state = store.create_task(
            "recover.docx",
            "docx",
            ReviewPosition.PARTY_A,
            content=build_docx_bytes(),
        )
        state = store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "上传已接收，开始执行文档解析。",
            step_name="upload_received",
        )
        logs: list[StepLog] = []
        document = invoke_tool(
            state.task_id,
            tool_registry,
            "parse_document",
            {
                "file_id": state.task_id,
                "file_name": state.file_name,
                "file_type": state.file_type,
                "content": build_docx_bytes(),
            },
            logs,
            step_name="document_parse",
        )
        state = store.update_task(
            state.task_id,
            ReviewStatus.DOCUMENT_PARSED,
            "文档解析完成。",
            step_name="document_parsed",
            tool_name="parse_document",
            document=document.to_dict(),
            logs=[log.to_dict() for log in logs],
        )
        contract_classification = invoke_tool(
            state.task_id,
            tool_registry,
            "classify_contract_type",
            {"document": document},
            logs,
            step_name="contract_type_classification",
        )
        state = store.update_task(
            state.task_id,
            ReviewStatus.CONTRACT_TYPE_CLASSIFIED,
            "已确认合同类型为 NDA，开始执行条款结构化。",
            step_name="contract_type_classified",
            tool_name="classify_contract_type",
            contract_classification=contract_classification,
            logs=[log.to_dict() for log in logs],
        )
        clauses = invoke_tool(
            state.task_id,
            tool_registry,
            "extract_clauses",
            {"document": document},
            logs,
            step_name="clause_structure",
        )
        clauses_with_fields = []
        for clause in clauses:
            key_fields = invoke_tool(
                state.task_id,
                tool_registry,
                "extract_key_fields",
                {"clause": clause},
                logs,
                step_name="key_field_extract",
            )
            clauses_with_fields.append(replace(clause, key_fields=key_fields))
        store.update_task(
            state.task_id,
            ReviewStatus.CLAUSES_STRUCTURED,
            "合同已解析并完成条款结构化，开始检索 NDA Playbook 规则。",
            step_name="clauses_structured",
            tool_name="extract_key_fields",
            clauses=[clause.to_dict() for clause in clauses_with_fields],
            logs=[log.to_dict() for log in logs],
        )
        return state.task_id

    def _start_and_get_task(self, task_id: str) -> dict:
        restarted_store = ReviewEventStore(self.persistence)
        app = create_app(self.settings, restarted_store)
        with TestClient(app) as client:
            response = client.get(f"/api/tasks/{task_id}")
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

    def _wait_for_terminal(self, client: TestClient, task_id: str) -> dict:
        deadline = time.time() + 10
        while time.time() < deadline:
            response = client.get(f"/api/tasks/{task_id}")
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            if task["status"] in TERMINAL_STATUSES:
                return task
            time.sleep(0.05)
        self.fail("recovered task did not reach terminal status")


if __name__ == "__main__":
    unittest.main()
