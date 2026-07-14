from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import escape
import json
from zipfile import ZipFile
from io import BytesIO

from models.log import StepLog
from models.review import ReviewPosition, ReviewStatus
from services.event_service import ReviewEventStore
from services.feedback_service import apply_feedback_to_task
from services.log_service import invoke_tool
from services.review_service import ReviewOrchestratorAgent
from tools.registry import tool_registry


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SAMPLES_DIR = PROJECT_ROOT / "samples"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "evaluation"
NO_PRODUCTION_CLAIM = "基础评测仅验证流程跑通，不代表生产级准确率。"
SUMMARY_FIELDS = (
    "task_ran",
    "document_parsed",
    "clauses_structured",
    "playbook_hit",
    "risk_has_evidence",
    "feedback_memory_written",
    "report_exported",
)


def run_basic_evaluation(tool_input: dict | None = None) -> dict:
    tool_input = tool_input or {}
    samples_dir = Path(str(tool_input.get("samples_dir") or DEFAULT_SAMPLES_DIR))
    output_dir = Path(str(tool_input.get("output_dir") or DEFAULT_OUTPUT_DIR))
    report_dir = Path(str(tool_input.get("report_dir") or output_dir / "reports"))
    memory_db_path = str(tool_input.get("memory_db_path") or output_dir / "evaluation_memory.sqlite3")

    manifest = _load_manifest(samples_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    results = [
        _run_sample(sample, samples_dir, report_dir, memory_db_path)
        for sample in manifest
    ]
    summary = {
        "evaluation_id": f"eval_{uuid4().hex[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(results),
        "passed_count": sum(1 for result in results if _sample_passed(result)),
        "claim": NO_PRODUCTION_CLAIM,
        "results": results,
    }

    summary_path = output_dir / "evaluation_summary.json"
    markdown_path = output_dir / "evaluation_summary.md"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_summary_markdown(summary), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    summary["markdown_path"] = str(markdown_path)
    return summary


def _load_manifest(samples_dir: Path) -> list[dict]:
    manifest_path = samples_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("samples manifest not found")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = payload.get("samples")
    if not isinstance(samples, list) or len(samples) != 10:
        raise ValueError("samples manifest must contain exactly 10 samples")
    return samples


def _run_sample(sample: dict, samples_dir: Path, report_dir: Path, memory_db_path: str) -> dict:
    sample_name = str(sample.get("sample_name", "")).strip()
    result = {
        "sample_name": sample_name,
        "file": str(sample.get("file", "")),
        "review_position": str(sample.get("review_position", "")),
        "source": str(sample.get("source", "")),
        "task_ran": False,
        "document_parsed": False,
        "clauses_structured": False,
        "playbook_hit": False,
        "risk_has_evidence": False,
        "feedback_memory_written": False,
        "report_exported": False,
        "failure_reason": "",
    }

    try:
        text = (samples_dir / result["file"]).read_text(encoding="utf-8")
        review_position = _review_position(result["review_position"])
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)
        state = agent.run_sync(
            file_name=f"{Path(result['file']).stem}.docx",
            file_type="docx",
            content=_docx_bytes_from_text(text),
            review_position=review_position,
        )
        task = state.to_dict()
        result["task_ran"] = True
        result["document_parsed"] = bool(task.get("document"))
        result["clauses_structured"] = bool(task.get("clauses"))
        result["playbook_hit"] = any(group.get("matched_rules") for group in task.get("matched_rules") or [])
        result["risk_has_evidence"] = _risks_have_evidence(task.get("risk_findings") or [])

        risk = _first_risk(task.get("risk_findings") or [])
        if risk is None:
            raise ValueError("no exportable risk candidate found")

        feedback_result = apply_feedback_to_task(
            task["task_id"],
            {
                "risk_id": risk["risk_id"],
                "action": "accept",
                "include_in_report": True,
            },
            event_store=event_store,
            db_path=memory_db_path,
        )
        result["feedback_memory_written"] = bool(feedback_result.get("memory_item", {}).get("memory_id"))
        updated_task = feedback_result["task"]
        logs = [StepLog(**item) for item in updated_task.get("logs") or []]
        report_result = invoke_tool(
            updated_task["task_id"],
            tool_registry,
            "generate_report",
            {
                "task_id": updated_task["task_id"],
                "task": updated_task,
                "report_dir": str(report_dir),
            },
            logs,
            step_name="evaluation_report_generation",
        )
        report_file = report_result["report_file"]
        event_store.update_task(
            updated_task["task_id"],
            ReviewStatus.REPORT_READY,
            "评测样本报告已生成。",
            step_name="evaluation_report_ready",
            tool_name="generate_report",
            report_file=report_file,
            logs=[log.to_dict() for log in logs],
        )
        result["report_exported"] = Path(report_file["path"]).exists() and report_file["risk_count"] > 0
    except Exception as exc:
        result["failure_reason"] = str(exc)

    if not result["failure_reason"]:
        missing = [field for field in SUMMARY_FIELDS if not result[field]]
        if missing:
            result["failure_reason"] = f"missing checks: {', '.join(missing)}"
    return result


def _review_position(value: str) -> ReviewPosition:
    if value == ReviewPosition.PARTY_A.value:
        return ReviewPosition.PARTY_A
    if value == ReviewPosition.PARTY_B.value:
        return ReviewPosition.PARTY_B
    raise ValueError("review_position must be 甲方 or 乙方")


def _docx_bytes_from_text(text: str) -> bytes:
    paragraphs = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        paragraphs.append(f"<w:p><w:r><w:t>{escape(stripped)}</w:t></w:r></w:p>")
    if not paragraphs:
        raise ValueError("sample text is empty")

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(paragraphs)
        + "</w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _risks_have_evidence(risks: list[dict]) -> bool:
    return bool(risks) and all(
        str(risk.get("clause_id", "")).strip() and str(risk.get("evidence_text", "")).strip()
        for risk in risks
    )


def _first_risk(risks: list[dict]) -> dict | None:
    for risk in risks:
        if str(risk.get("review_status", "")) in {"CONFIRMED_RISK", "NEED_MANUAL_REVIEW"}:
            return risk
    return None


def _sample_passed(result: dict) -> bool:
    return not result.get("failure_reason") and all(result[field] for field in SUMMARY_FIELDS)


def _summary_markdown(summary: dict) -> str:
    lines = [
        "# 基础评测摘要",
        "",
        summary["claim"],
        "",
        f"- 样本数：{summary['sample_count']}",
        f"- 跑通数：{summary['passed_count']}",
        "",
        "| 样本 | 任务跑通 | 文档解析 | 条款结构化 | Playbook 命中 | 风险证据 | Memory 写入 | 报告导出 | 失败原因 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in summary["results"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(item["sample_name"]),
                    _yes_no(item["task_ran"]),
                    _yes_no(item["document_parsed"]),
                    _yes_no(item["clauses_structured"]),
                    _yes_no(item["playbook_hit"]),
                    _yes_no(item["risk_has_evidence"]),
                    _yes_no(item["feedback_memory_written"]),
                    _yes_no(item["report_exported"]),
                    _cell(item["failure_reason"]),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _cell(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
