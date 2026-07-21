from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

from models.feedback import FEEDBACK_ACTIONS, VALID_FINAL_SEVERITIES
from models.log import StepLog
from models.review import ReviewStatus
from services.event_service import ReviewEventStore, review_event_store
from services.log_service import invoke_tool
from tools.registry import tool_registry


ACTION_LABELS = {
    "accept": "采纳",
    "ignore": "忽略",
    "update_severity": "修改等级",
    "update_suggestion": "修改建议",
}


def apply_feedback_to_task(
    task_id: str,
    feedback_payload: dict,
    event_store: ReviewEventStore = review_event_store,
    db_path: str | None = None,
) -> dict:
    task = event_store.get_task(task_id)
    if task is None:
        raise ValueError("未找到审查任务，无法记录人工反馈。")
    if not isinstance(feedback_payload, dict):
        raise ValueError("feedback payload must be a dict")

    action = str(feedback_payload.get("action", "")).strip()
    if action not in FEEDBACK_ACTIONS:
        raise ValueError("feedback action must be accept, ignore, update_severity or update_suggestion")

    risks = deepcopy(task.risk_findings or [])
    risk = _find_risk(risks, str(feedback_payload.get("risk_id", "")).strip())
    clauses = task.clauses or []
    clause = _find_clause(clauses, str(risk.get("clause_id", "")).strip())
    final_severity = _resolve_final_severity(risk, action, feedback_payload)
    final_suggestion = _resolve_final_suggestion(risk, action, feedback_payload)
    ignore_reason = str(feedback_payload.get("ignore_reason", "")).strip()
    include_in_report = bool(feedback_payload.get("include_in_report", action != "ignore"))
    updated_at = datetime.now(timezone.utc).isoformat()

    human_feedback = {
        "contract_type": "NDA",
        "clause_type": str(clause.get("clause_type", "")),
        "risk_type": str(risk.get("risk_type", "")),
        "review_position": task.review_position.value,
        "user_action": action,
        "original_severity": str(risk.get("severity", "")),
        "final_severity": final_severity,
        "original_suggestion": str(risk.get("revision_suggestion", "")),
        "final_suggestion": final_suggestion,
        "ignore_reason": ignore_reason,
        "source_finding_id": str(risk.get("risk_id", "")),
        "source_clause_id": str(risk.get("clause_id", "")),
        "include_in_report": include_in_report,
        "created_at": updated_at,
    }

    logs = [StepLog(**item) for item in (task.logs or [])]
    tool_input = {"human_feedback": human_feedback}
    resolved_db_path = db_path or event_store.db_path
    if resolved_db_path:
        tool_input["db_path"] = resolved_db_path
        tool_input["idempotency_key"] = _feedback_idempotency_key(task_id, feedback_payload)
    memory_item = invoke_tool(
        task_id,
        tool_registry,
        "write_memory",
        tool_input,
        logs,
        step_name="memory_write",
    )

    _apply_feedback_to_risk(
        risk,
        action=action,
        final_severity=final_severity,
        final_suggestion=final_suggestion,
        ignore_reason=ignore_reason,
        include_in_report=include_in_report,
        memory_item=memory_item,
        updated_at=updated_at,
    )

    updated_task = event_store.update_task(
        task_id,
        ReviewStatus.MEMORY_UPDATED,
        f"人工反馈已记录：{ACTION_LABELS[action]}，Memory 已更新。",
        step_name="memory_updated",
        tool_name="write_memory",
        risk_findings=risks,
        logs=[log.to_dict() for log in logs],
    )
    task_payload = event_store.get_task_payload(task_id) or updated_task.to_dict()
    return {
        "status": ReviewStatus.MEMORY_UPDATED.value,
        "message": updated_task.message,
        "risk": risk,
        "memory_item": memory_item,
        "task": task_payload,
    }


def _feedback_idempotency_key(task_id: str, feedback_payload: dict) -> str:
    canonical = json.dumps(feedback_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"write_memory:{task_id}:{sha256(canonical.encode('utf-8')).hexdigest()}"


def _find_risk(risks: list[dict], risk_id: str) -> dict:
    if not risk_id:
        raise ValueError("risk_id is required")
    for risk in risks:
        if str(risk.get("risk_id", "")) == risk_id:
            return risk
    raise ValueError("未找到对应风险，无法记录人工反馈。")


def _find_clause(clauses: list[dict], clause_id: str) -> dict:
    for clause in clauses:
        if str(clause.get("clause_id", "")) == clause_id:
            return clause
    raise ValueError("未找到风险对应条款，无法写入 Memory。")


def _resolve_final_severity(risk: dict, action: str, payload: dict) -> str:
    final_severity = str(payload.get("final_severity", risk.get("severity", ""))).strip()
    if action == "update_severity" and final_severity not in VALID_FINAL_SEVERITIES:
        raise ValueError("final_severity must be 高, 中 or 低")
    return final_severity or str(risk.get("severity", ""))


def _resolve_final_suggestion(risk: dict, action: str, payload: dict) -> str:
    final_suggestion = str(payload.get("final_suggestion", risk.get("revision_suggestion", ""))).strip()
    if action == "update_suggestion" and not final_suggestion:
        raise ValueError("final_suggestion is required when updating a suggestion")
    return final_suggestion or str(risk.get("revision_suggestion", ""))


def _apply_feedback_to_risk(
    risk: dict,
    action: str,
    final_severity: str,
    final_suggestion: str,
    ignore_reason: str,
    include_in_report: bool,
    memory_item: dict,
    updated_at: str,
) -> None:
    risk["severity"] = final_severity
    risk["revision_suggestion"] = final_suggestion
    risk["include_in_report"] = include_in_report
    risk["feedback"] = {
        "user_action": action,
        "action_label": ACTION_LABELS[action],
        "ignore_reason": ignore_reason,
        "memory_id": memory_item["memory_id"],
        "updated_at": updated_at,
    }
    if action == "ignore":
        risk["review_status"] = "IGNORED_RISK"
    else:
        risk["review_status"] = "CONFIRMED_RISK"
