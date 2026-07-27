from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


JsonObject = dict[str, Any]
ReviewPosition = Literal["甲方", "乙方"]


class ToolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBlockPayload(ToolSchema):
    block_id: str
    block_type: str
    text: str
    order: int = Field(ge=0)
    source_location: JsonObject = Field(default_factory=dict)


class ContractDocumentPayload(ToolSchema):
    contract_id: str
    file_name: str
    file_type: Literal["docx", "pdf"]
    content_hash: str
    paragraphs: list[TextBlockPayload]
    tables: list[TextBlockPayload]
    blocks: list[TextBlockPayload]
    page_map: list[JsonObject]


class ClausePayload(ToolSchema):
    clause_id: str
    title: str = ""
    text: str
    clause_type: str
    key_fields: JsonObject = Field(default_factory=dict)
    source_location: JsonObject = Field(default_factory=dict)


class RiskFindingPayload(ToolSchema):
    risk_id: str
    risk_type: str
    severity: Literal["高", "中", "低"]
    confidence: float = Field(ge=0, le=1)
    risk_reason: str
    clause_id: str
    evidence_text: str
    matched_rule_ids: list[str]
    review_position: ReviewPosition
    risk_focus: str
    revision_suggestion: str
    review_status: Literal["CONFIRMED_RISK", "NEED_MANUAL_REVIEW", "NO_RISK"]


class QueryAdjustments(ToolSchema):
    additional_keywords: list[str] = Field(default_factory=list, max_length=5)
    top_k: int | None = Field(default=None, ge=1, le=5)


class ParseDocumentInput(ToolSchema):
    file_id: str = ""
    file_name: str
    file_type: Literal["docx", "pdf"]
    content: bytes


class ParseDocumentOutput(ContractDocumentPayload):
    pass


class ClassifyContractTypeInput(ToolSchema):
    document: ContractDocumentPayload


class ClassifyContractTypeOutput(ToolSchema):
    contract_type: Literal["NDA", "PROCUREMENT", "SERVICE", "EMPLOYMENT", "UNKNOWN"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]
    decision: Literal[
        "SUPPORTED",
        "UNSUPPORTED_CONTRACT_TYPE",
        "NEED_MANUAL_REVIEW",
    ]


class ExtractClausesInput(ToolSchema):
    document: ContractDocumentPayload


class ExtractClausesOutput(ToolSchema):
    clauses: list[ClausePayload]


class ExtractKeyFieldsInput(ToolSchema):
    clause: ClausePayload


class ExtractKeyFieldsOutput(ToolSchema):
    key_fields: dict[str, list[str]]


class RetrievePlaybookRulesInput(ToolSchema):
    contract_type: str
    clause_type: str
    key_fields: JsonObject = Field(default_factory=dict)
    review_position: ReviewPosition


class RetrievePlaybookRulesOutput(ToolSchema):
    matched_rules: list[JsonObject]


class RetrieveRelatedClausesInput(ToolSchema):
    contract_type: str
    current_clause: ClausePayload
    clauses: list[ClausePayload]
    risk_type: str
    playbook_check_point: str
    limit: int = Field(default=3, ge=1, le=5)
    query_adjustments: QueryAdjustments | None = None


class RetrieveRelatedClausesOutput(ToolSchema):
    related_clauses: list[JsonObject]


class RetrieveMemoryInput(ToolSchema):
    contract_type: str
    clause: ClausePayload
    risk_type: str
    review_position: ReviewPosition
    memory_items: list[JsonObject] | None = None
    limit: int = Field(default=3, ge=1, le=20)
    stale_after_days: int = Field(default=365, ge=1)
    now: str | None = None


class RetrieveMemoryOutput(ToolSchema):
    memories: list[JsonObject]


class AnalyzeRiskInput(ToolSchema):
    review_context: JsonObject


class AnalyzeRiskOutput(ToolSchema):
    risk_finding: RiskFindingPayload


class CriticizeRiskInput(ToolSchema):
    finding: RiskFindingPayload
    current_clause: ClausePayload
    matched_rule: JsonObject


class CriticizeRiskOutput(ToolSchema):
    decision: Literal["PASS", "REJECT", "REQUEST_HUMAN_REVIEW"]
    reason_code: Literal[
        "SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK",
        "CLAUSE_MISMATCH",
        "EVIDENCE_UNSUPPORTED",
        "PLAYBOOK_MISMATCH",
        "REASON_SUPPORT_AMBIGUOUS",
        "PROMPT_INJECTION_DETECTED",
    ]


class PlanReviewActionInput(ToolSchema):
    trigger_reason: Literal[
        "LOW_CONFIDENCE",
        "EVIDENCE_MISSING",
        "RETRIEVAL_INSUFFICIENT",
        "ANALYZER_VERIFIER_CONFLICT",
        "STRUCTURED_OUTPUT_INVALID",
    ]
    current_status: str
    target_clause_id: str
    contract_clause_ids: list[str]
    retry_count: int = Field(ge=0, le=1)
    failure_reason: str = ""


class PlanReviewActionOutput(ToolSchema):
    action: Literal[
        "RETRIEVE_AGAIN",
        "ANALYZE_AGAIN",
        "REQUEST_HUMAN_REVIEW",
        "TERMINATE",
    ]
    reason_code: Literal[
        "LOW_CONFIDENCE",
        "EVIDENCE_MISSING",
        "RETRIEVAL_INSUFFICIENT",
        "ANALYZER_VERIFIER_CONFLICT",
        "STRUCTURED_OUTPUT_INVALID",
    ]
    target_clause_id: str
    query_adjustments: JsonObject
    confidence: float = Field(ge=0, le=1)


class VerifyEvidenceInput(ToolSchema):
    finding: RiskFindingPayload
    clauses: list[ClausePayload]
    matched_rule: JsonObject


class EvidencePayload(ToolSchema):
    risk_id: str
    clause_id: str
    evidence_text: str
    is_valid: bool
    failure_reason: str
    verified_clause_id: str
    source_location: JsonObject


class VerifyEvidenceOutput(ToolSchema):
    citation_status: EvidencePayload


class GenerateRevisionInput(ToolSchema):
    finding: RiskFindingPayload
    preferred_position: ReviewPosition


class GenerateRevisionOutput(ToolSchema):
    revision_suggestion: str
    preferred_position: ReviewPosition
    risk_focus: str
    basis_rule_ids: list[str]
    memory_references: list[JsonObject]


class HumanFeedbackPayload(ToolSchema):
    memory_id: str | None = None
    contract_type: str
    clause_type: str
    risk_type: str
    review_position: ReviewPosition | Literal[""] = ""
    user_action: Literal[
        "accept",
        "ignore",
        "update_severity",
        "update_suggestion",
        "update_evidence",
    ]
    original_severity: str = ""
    final_severity: str = ""
    original_suggestion: str = ""
    final_suggestion: str = ""
    ignore_reason: str = ""
    source_finding_id: str
    source_clause_id: str
    include_in_report: bool = False
    created_at: str | None = None


class WriteMemoryInput(ToolSchema):
    human_feedback: HumanFeedbackPayload
    idempotency_key: str | None = None


class MemoryItemPayload(ToolSchema):
    memory_id: str
    memory_type: str
    contract_type: str
    clause_type: str
    risk_type: str
    review_position: str
    user_action: str
    original_severity: str
    final_severity: str
    original_suggestion: str
    final_suggestion: str
    ignore_reason: str
    source_finding_id: str
    source_clause_id: str
    include_in_report: bool
    created_at: str


class WriteMemoryOutput(ToolSchema):
    memory_item: MemoryItemPayload


class GenerateReportInput(ToolSchema):
    task_id: str
    task: JsonObject | None = None
    report_dir: str | None = None


class ReportFilePayload(ToolSchema):
    report_id: str
    task_id: str
    file_name: str
    format: Literal["markdown"]
    path: str
    risk_count: int = Field(ge=0)
    generated_at: str
    markdown: str


class GenerateReportOutput(ToolSchema):
    report_file: ReportFilePayload
