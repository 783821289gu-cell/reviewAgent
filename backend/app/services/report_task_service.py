from hashlib import sha256
import json

from models.log import StepLog
from models.review import ReviewStatus
from services.event_service import ReviewEventStore, review_event_store
from services.log_service import invoke_tool
from tools.registry import tool_registry


class ReportTaskNotFoundError(ValueError):
    pass


def generate_task_report(
    task_id: str,
    event_store: ReviewEventStore = review_event_store,
) -> dict:
    task = event_store.get_task(task_id)
    if task is None:
        raise ReportTaskNotFoundError("未找到审查任务，无法生成报告。")

    logs = [StepLog(**item) for item in (task.logs or [])]
    tool_input = {"task_id": task_id, "task": task.to_dict()}
    result = None
    idempotency_key = _report_idempotency_key(task)
    if event_store.persistence is not None:
        result = event_store.persistence.review_result_repository.get_idempotent_result(
            idempotency_key,
            "generate_report",
        )
    if result is None:
        result = invoke_tool(
            task_id,
            tool_registry,
            "generate_report",
            tool_input,
            logs,
            step_name="report_generation",
        )
        if event_store.persistence is not None:
            result = event_store.persistence.review_result_repository.save_idempotent_result(
                idempotency_key,
                "generate_report",
                result,
            )

    report_file = result["report_file"]
    updated_task = event_store.update_task(
        task_id,
        ReviewStatus.REPORT_READY,
        "Markdown 审查报告已生成。",
        step_name="report_ready",
        tool_name="generate_report",
        report_file=report_file,
        logs=[log.to_dict() for log in logs],
    )
    return {
        "status": ReviewStatus.REPORT_READY.value,
        "message": updated_task.message,
        "report_file": report_file,
        "task": updated_task.to_dict(),
    }


def _report_idempotency_key(task) -> str:
    report_input = {
        "task_id": task.task_id,
        "file_name": task.file_name,
        "review_position": task.review_position.value,
        "risk_findings": task.risk_findings or [],
    }
    canonical = json.dumps(report_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"generate_report:{task.task_id}:{sha256(canonical.encode('utf-8')).hexdigest()}"
