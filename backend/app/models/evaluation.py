from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Scalar = str | int | float | bool | None
FailureValue = Scalar | list[Scalar] | dict[str, Scalar]
NonEmptyText = Annotated[str, Field(min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnnotationSource(StrictModel):
    source_type: Literal["synthetic", "public_authorized", "deidentified_authorized"]
    note: str = Field(min_length=1)


class PlannerExpectation(StrictModel):
    reason_code: Literal[
        "LOW_CONFIDENCE",
        "EVIDENCE_MISSING",
        "RETRIEVAL_INSUFFICIENT",
        "ANALYZER_VERIFIER_CONFLICT",
        "STRUCTURED_OUTPUT_INVALID",
    ]
    target_clause_id: str = Field(min_length=1)
    expected_action: Literal[
        "RETRIEVE_AGAIN",
        "ANALYZE_AGAIN",
        "REQUEST_HUMAN_REVIEW",
        "TERMINATE",
    ]


class ContractAnnotation(StrictModel):
    contract_id: str = Field(min_length=1)
    file: str = Field(min_length=1)
    review_position: Literal["甲方", "乙方"]
    expected_contract_type: str = Field(min_length=1)
    expected_decision: str = Field(min_length=1)
    expected_terminal_status: str = Field(min_length=1)
    manual_review_expected: bool
    planner_expectations: list[PlannerExpectation] = Field(default_factory=list)
    source: AnnotationSource


class ClauseAnnotation(StrictModel):
    annotation_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    clause_id: str = Field(min_length=1)
    exact_text: str = Field(min_length=1)
    clause_type: str = Field(min_length=1)
    expected_rule_ids: list[NonEmptyText] = Field(
        json_schema_extra={"uniqueItems": True}
    )

    @field_validator("expected_rule_ids")
    @classmethod
    def validate_unique_rule_ids(cls, values: list[str]) -> list[str]:
        return _require_unique(values, "expected_rule_ids")


class RiskAnnotation(StrictModel):
    annotation_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    clause_id: str = Field(min_length=1)
    risk_type: str = Field(min_length=1)
    acceptable_severities: list[Literal["高", "中", "低"]] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    evidence_spans: list[NonEmptyText] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    manual_review_expected: bool
    feedback_action: Literal["accept", "ignore"]
    include_in_report: bool

    @field_validator("acceptable_severities", "evidence_spans")
    @classmethod
    def validate_unique_values(cls, values: list[str], info) -> list[str]:
        return _require_unique(values, info.field_name)


class RelatedClauseItem(StrictModel):
    clause_id: str = Field(min_length=1)
    clause_type: str = Field(min_length=1)
    text: str = Field(min_length=1)
    key_fields: dict


class RelatedClauseAnnotation(StrictModel):
    case_id: str = Field(min_length=1)
    current_clause: RelatedClauseItem
    risk_type: str = Field(min_length=1)
    playbook_check_point: str = Field(min_length=1)
    candidates: list[RelatedClauseItem] = Field(min_length=1)
    relevant_clause_ids: list[NonEmptyText] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )

    @field_validator("relevant_clause_ids")
    @classmethod
    def validate_unique_relevant_ids(cls, values: list[str]) -> list[str]:
        return _require_unique(values, "relevant_clause_ids")


class MemoryEpisodeAnnotation(StrictModel):
    memory_id: str = Field(min_length=1)
    user_action: Literal["accept", "ignore", "update_severity", "update_suggestion"]
    final_severity: str
    final_suggestion: str
    created_at: datetime


class MemoryComparisonAnnotation(StrictModel):
    case_id: str = Field(min_length=1)
    contract_type: str = Field(min_length=1)
    clause_type: str = Field(min_length=1)
    risk_type: str = Field(min_length=1)
    review_position: str = Field(min_length=1)
    baseline_suggestion: str = Field(min_length=1)
    expected_suggestion: str = Field(min_length=1)
    evaluation_time: datetime
    stale_after_days: int = Field(gt=0)
    episodes: list[MemoryEpisodeAnnotation] = Field(min_length=1)
    source: AnnotationSource

    @field_validator("episodes")
    @classmethod
    def validate_unique_memory_ids(
        cls,
        values: list[MemoryEpisodeAnnotation],
    ) -> list[MemoryEpisodeAnnotation]:
        _require_unique([item.memory_id for item in values], "memory episode IDs")
        return values


class PromptInjectionAnnotation(StrictModel):
    id: str = Field(min_length=1)
    category: Literal[
        "prompt_injection",
        "forged_system_message",
        "forged_tool_result",
        "tool_instruction",
        "output_override",
        "oversized_untrusted_text",
    ]
    target: Literal["current_clause_text", "memory_note"]
    text: str = Field(min_length=1)
    repeat: int = Field(gt=0)
    expected_outcome: Literal[
        "PROMPT_INJECTION_DETECTED",
        "CONTEXT_BUDGET_EXCEEDED",
    ]


class AnnotationBundle(StrictModel):
    model_config = ConfigDict(extra="forbid", title="EffectAnnotationBundle")

    schema_version: Literal["effect-v2"]
    contracts: list[ContractAnnotation] = Field(min_length=1)
    clauses: list[ClauseAnnotation] = Field(min_length=1)
    risks: list[RiskAnnotation] = Field(min_length=1)
    related_clauses: list[RelatedClauseAnnotation] = Field(min_length=1)
    memory: list[MemoryComparisonAnnotation] = Field(min_length=1)
    prompt_injection: list[PromptInjectionAnnotation] = Field(min_length=1)


class FailureSample(StrictModel):
    sample_id: str
    failure_type: str
    reason: str
    expected: FailureValue = None
    actual: FailureValue = None


class EvaluationMetric(StrictModel):
    metric: str
    label: str
    sample_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    score: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    threshold_met: bool
    failure_samples: list[FailureSample]


class EvaluationVersion(StrictModel):
    annotation_version: str
    code_version: str
    git_dirty: bool | None
    playbook_version: str
    llm_mode: str
    llm_model: str
    embedding_mode: str
    embedding_model: str
    parameters: dict


class EvaluationMeasurement(StrictModel):
    measurement: str
    label: str
    value: int | float | str | None
    unit: str


class EvaluationRuntime(StrictModel):
    run_label: str
    provider_call_count: int = Field(ge=0)
    provider_success_count: int = Field(ge=0)
    request_id_hashes: list[str]
    measurements: list[EvaluationMeasurement]


class EffectEvaluationSummary(StrictModel):
    evaluation_id: str
    evaluation_type: Literal["effect"]
    created_at: str
    status: Literal["completed", "completed_with_failures", "annotation_failed"]
    claim: str
    sample_count: int = Field(ge=0)
    metrics: list[EvaluationMetric]
    versions: EvaluationVersion
    runtime: EvaluationRuntime
    evaluation_failures: list[FailureSample]
    summary_path: str = ""
    markdown_path: str = ""


def _require_unique(values: list[str], field_name: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} values must be unique")
    return values
