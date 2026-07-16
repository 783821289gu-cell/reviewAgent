from dataclasses import asdict, dataclass
from enum import StrEnum
from uuid import uuid4


class ReviewStatus(StrEnum):
    START = "START"
    UPLOAD_RECEIVED = "UPLOAD_RECEIVED"
    DOCUMENT_PARSED = "DOCUMENT_PARSED"
    CONTRACT_TYPE_CLASSIFIED = "CONTRACT_TYPE_CLASSIFIED"
    CLAUSES_STRUCTURED = "CLAUSES_STRUCTURED"
    PLAYBOOK_RETRIEVED = "PLAYBOOK_RETRIEVED"
    CONTEXT_BUILT = "CONTEXT_BUILT"
    RISK_ANALYZED = "RISK_ANALYZED"
    EVIDENCE_VERIFIED = "EVIDENCE_VERIFIED"
    HUMAN_REVIEW_PENDING = "HUMAN_REVIEW_PENDING"
    MEMORY_UPDATED = "MEMORY_UPDATED"
    REPORT_READY = "REPORT_READY"
    PARSE_FAILED = "PARSE_FAILED"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    NEED_MANUAL_REVIEW = "NEED_MANUAL_REVIEW"
    UNSUPPORTED_CONTRACT_TYPE = "UNSUPPORTED_CONTRACT_TYPE"
    TASK_ERROR = "TASK_ERROR"


class ReviewPosition(StrEnum):
    PARTY_A = "甲方"
    PARTY_B = "乙方"


@dataclass(frozen=True)
class ReviewTask:
    task_id: str
    status: ReviewStatus
    file_name: str
    file_type: str
    review_position: ReviewPosition
    message: str
    document: dict | None = None
    contract_classification: dict | None = None
    clauses: list[dict] | None = None
    matched_rules: list[dict] | None = None
    review_contexts: list[dict] | None = None
    analysis_results: list[dict] | None = None
    risk_findings: list[dict] | None = None
    evidence_results: list[dict] | None = None
    report_file: dict | None = None
    logs: list[dict] | None = None
    trace_id: str = ""
    recovery_count: int = 0
    recovery_from_status: str = ""

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["review_position"] = self.review_position.value
        return payload


@dataclass
class AgentState:
    task_id: str
    status: ReviewStatus
    file_name: str
    file_type: str
    review_position: ReviewPosition
    message: str
    document: dict | None = None
    contract_classification: dict | None = None
    clauses: list[dict] | None = None
    matched_rules: list[dict] | None = None
    review_contexts: list[dict] | None = None
    analysis_results: list[dict] | None = None
    risk_findings: list[dict] | None = None
    evidence_results: list[dict] | None = None
    report_file: dict | None = None
    logs: list[dict] | None = None
    events: list[dict] | None = None
    trace_id: str = ""
    recovery_count: int = 0
    recovery_from_status: str = ""

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["review_position"] = self.review_position.value
        return payload

    def to_review_task(self) -> ReviewTask:
        return ReviewTask(
            task_id=self.task_id,
            status=self.status,
            file_name=self.file_name,
            file_type=self.file_type,
            review_position=self.review_position,
            message=self.message,
            document=self.document,
            contract_classification=self.contract_classification,
            clauses=self.clauses,
            matched_rules=self.matched_rules,
            review_contexts=self.review_contexts,
            analysis_results=self.analysis_results,
            risk_findings=self.risk_findings,
            evidence_results=self.evidence_results,
            report_file=self.report_file,
            logs=self.logs,
            trace_id=self.trace_id,
            recovery_count=self.recovery_count,
            recovery_from_status=self.recovery_from_status,
        )


def new_task(
    file_name: str,
    file_type: str,
    review_position: ReviewPosition,
    status: ReviewStatus = ReviewStatus.UPLOAD_RECEIVED,
    message: str = "任务壳已创建。",
    document: dict | None = None,
    contract_classification: dict | None = None,
    clauses: list[dict] | None = None,
    matched_rules: list[dict] | None = None,
    review_contexts: list[dict] | None = None,
    analysis_results: list[dict] | None = None,
    risk_findings: list[dict] | None = None,
    evidence_results: list[dict] | None = None,
    report_file: dict | None = None,
    logs: list[dict] | None = None,
    task_id: str | None = None,
) -> ReviewTask:
    resolved_task_id = task_id or f"task_{uuid4().hex[:12]}"
    return ReviewTask(
        task_id=resolved_task_id,
        status=status,
        file_name=file_name,
        file_type=file_type,
        review_position=review_position,
        message=message,
        document=document,
        contract_classification=contract_classification,
        clauses=clauses,
        matched_rules=matched_rules,
        review_contexts=review_contexts,
        analysis_results=analysis_results,
        risk_findings=risk_findings,
        evidence_results=evidence_results,
        report_file=report_file,
        logs=logs,
        trace_id=trace_id_for_task(resolved_task_id),
    )


def trace_id_for_task(task_id: str) -> str:
    suffix = task_id.removeprefix("task_")
    return f"trace_{suffix}"
