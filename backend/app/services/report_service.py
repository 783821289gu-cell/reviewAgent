from datetime import datetime, timezone
from pathlib import Path
import os
import re

from services.event_service import review_event_store


REPORT_DIR = Path(__file__).resolve().parents[1] / "reports"
PENDING_REVIEW_STATUSES = {"NEED_MANUAL_REVIEW"}
NON_RISK_STATUSES = {"NO_RISK"}
REPORTABLE_TASK_STATUSES = {"EVIDENCE_VERIFIED", "HUMAN_REVIEW_PENDING", "MEMORY_UPDATED", "REPORT_READY"}


def generate_report(tool_input: dict) -> dict:
    if not isinstance(tool_input, dict):
        raise ValueError("tool_input must be a dict")

    task_id = str(tool_input.get("task_id", "")).strip()
    if not task_id:
        raise ValueError("task_id is required")

    task = _resolve_task(task_id, tool_input.get("task"))
    _ensure_reportable_task(task)
    risks = _exportable_risks(task.get("risk_findings") or [])
    markdown = _build_markdown(task, risks)

    report_dir = Path(
        str(
            tool_input.get("report_dir")
            or os.getenv("REVIEW_AGENT_REPORT_DIR")
            or REPORT_DIR
        )
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{_safe_file_stem(task_id)}.md"
    report_path.write_text(markdown, encoding="utf-8")

    report_file = {
        "report_id": f"report_{task_id}",
        "task_id": task_id,
        "file_name": str(task.get("file_name", "")),
        "format": "markdown",
        "path": str(report_path),
        "risk_count": len(risks),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markdown": markdown,
    }
    return {"report_file": report_file}


def _resolve_task(task_id: str, task_payload) -> dict:
    if task_payload is None:
        state = review_event_store.get_task(task_id)
        if state is None:
            raise ValueError("未找到审查任务，无法生成报告。")
        return state.to_dict()

    if hasattr(task_payload, "to_dict"):
        task_payload = task_payload.to_dict()
    if not isinstance(task_payload, dict):
        raise ValueError("task must be a dict")
    if str(task_payload.get("task_id", "")) != task_id:
        raise ValueError("task_id does not match task payload")
    return task_payload


def _ensure_reportable_task(task: dict) -> None:
    status = str(task.get("status", ""))
    if status not in REPORTABLE_TASK_STATUSES:
        raise ValueError("审查任务尚未完成，不能生成报告。")


def _exportable_risks(risks: list) -> list[dict]:
    exportable = []
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        if risk.get("include_in_report") is not True:
            continue
        review_status = str(risk.get("review_status", ""))
        if review_status in PENDING_REVIEW_STATUSES or review_status in NON_RISK_STATUSES:
            continue
        exportable.append(risk)
    return exportable


def _build_markdown(task: dict, risks: list[dict]) -> str:
    lines = [
        "# NDA 审查报告",
        "",
        "## 基本信息",
        "",
        f"- 合同名称：{_text(task.get('file_name'))}",
        f"- 审查立场：{_text(task.get('review_position'))}",
        f"- Playbook 版本：{_playbook_version(task)}",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
        "",
        "## 报告范围",
        "",
        "本报告仅包含用户明确允许进入报告且不处于待人工复核状态的风险；未加入报告、未确认或仅用于过程追踪的信息不在本报告中列示。",
        "",
        "## 风险清单",
        "",
    ]

    if not risks:
        lines.extend(["无已允许进入报告的确认风险。", ""])
        return "\n".join(lines)

    for index, risk in enumerate(risks, start=1):
        lines.extend(
            [
                f"### {index}. {_text(risk.get('risk_type'))}",
                "",
                f"- 风险 ID：{_text(risk.get('risk_id'))}",
                f"- 风险等级：{_text(risk.get('severity'))}",
                f"- 审查立场：{_risk_review_position(task, risk)}",
                f"- 立场风险重点：{_text(risk.get('risk_focus'))}",
                f"- 条款 ID：{_text(risk.get('clause_id'))}",
                f"- 人工反馈状态：{_feedback_status(risk)}",
                "",
                "**风险原因**",
                "",
                _text(risk.get("risk_reason")),
                "",
                "**原文证据**",
                "",
                f"> {_quote(risk.get('evidence_text'))}",
                "",
                "**修改建议**",
                "",
                _text(risk.get("revision_suggestion")),
                "",
            ]
        )

    return "\n".join(lines)


def _playbook_version(task: dict) -> str:
    for group in task.get("matched_rules") or []:
        if not isinstance(group, dict):
            continue
        for rule in group.get("matched_rules") or []:
            if isinstance(rule, dict) and rule.get("playbook_version"):
                return str(rule["playbook_version"])
    return "nda-v1" if task.get("matched_rules") else "未检索"


def _feedback_status(risk: dict) -> str:
    feedback = risk.get("feedback")
    if isinstance(feedback, dict):
        label = str(feedback.get("action_label") or feedback.get("user_action") or "").strip()
        if label:
            return label
    review_status = str(risk.get("review_status", ""))
    if review_status == "CONFIRMED_RISK":
        return "已确认"
    if review_status == "IGNORED_RISK":
        return "已忽略并允许列入报告"
    return "未记录人工反馈"


def _risk_review_position(task: dict, risk: dict) -> str:
    task_position = str(task.get("review_position", "")).strip()
    risk_position = str(risk.get("review_position", "")).strip()
    if not risk_position:
        raise ValueError("报告风险缺少审查立场。")
    if risk_position != task_position:
        raise ValueError("报告风险审查立场与任务不一致。")
    return risk_position


def _quote(value) -> str:
    text = _text(value)
    return text.replace("\n", "\n> ")


def _text(value) -> str:
    text = str(value or "").strip()
    return text if text else "未提供"


def _safe_file_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return stem or "report"
