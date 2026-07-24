from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from pgvector.sqlalchemy import Vector


APP_SCHEMA = "app"
metadata = MetaData(schema=APP_SCHEMA)


class Base(DeclarativeBase):
    metadata = metadata


class ReviewTaskRow(Base):
    __tablename__ = "review_tasks"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(Text, nullable=False)
    review_position: Mapped[str] = mapped_column(Text, nullable=False)
    llm_mode: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="local_structured",
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    current_node: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    contract_classification_json: Mapped[dict | None] = mapped_column(JSONB)
    matched_rules_json: Mapped[list | None] = mapped_column(JSONB)
    review_contexts_json: Mapped[list | None] = mapped_column(JSONB)
    analysis_results_json: Mapped[list | None] = mapped_column(JSONB)
    evidence_results_json: Mapped[list | None] = mapped_column(JSONB)
    report_file_json: Mapped[dict | None] = mapped_column(JSONB)
    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    recovery_from_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    retry_counts_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    recovery_history_json: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    cancel_requested_at: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    cancelled_at: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    cancel_reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    execution_owner: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    last_timeout_json: Mapped[dict | None] = mapped_column(JSONB)
    progress_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class DocumentRow(Base):
    __tablename__ = "documents"

    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(f"{APP_SCHEMA}.review_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    file_hash: Mapped[str] = mapped_column(Text, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    document_json: Mapped[dict | None] = mapped_column(JSONB)
    parsed_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class ClauseRow(Base):
    __tablename__ = "clauses"

    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(f"{APP_SCHEMA}.review_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    clause_id: Mapped[str] = mapped_column(Text, primary_key=True)
    clause_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    key_fields_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_location_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class ClauseEmbeddingRow(Base):
    __tablename__ = "clause_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "clause_id"],
            [f"{APP_SCHEMA}.clauses.task_id", f"{APP_SCHEMA}.clauses.clause_id"],
            ondelete="CASCADE",
        ),
    )

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    clause_id: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding_model: Mapped[str] = mapped_column(Text, primary_key=True)
    model_revision: Mapped[str] = mapped_column(Text, primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(1024),
        nullable=False,
    )
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class RiskFindingRow(Base):
    __tablename__ = "risk_findings"

    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(f"{APP_SCHEMA}.review_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    risk_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    clause_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class StepLogRow(Base):
    __tablename__ = "step_logs"

    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(f"{APP_SCHEMA}.review_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    log_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    step_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    step_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    input_summary: Mapped[str] = mapped_column(Text, nullable=False)
    output_summary: Mapped[str] = mapped_column(Text, nullable=False)
    token_cost_summary: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    parent_step_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    retry_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    trace_summary_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class TaskEventRow(Base):
    __tablename__ = "task_events"

    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(f"{APP_SCHEMA}.review_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    step_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)


class MemoryItemRow(Base):
    __tablename__ = "memory_items"

    memory_id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_type: Mapped[str] = mapped_column(Text, nullable=False)
    contract_type: Mapped[str] = mapped_column(Text, nullable=False)
    clause_type: Mapped[str] = mapped_column(Text, nullable=False)
    risk_type: Mapped[str] = mapped_column(Text, nullable=False)
    review_position: Mapped[str] = mapped_column(Text, nullable=False)
    user_action: Mapped[str] = mapped_column(Text, nullable=False)
    original_severity: Mapped[str] = mapped_column(Text, nullable=False)
    final_severity: Mapped[str] = mapped_column(Text, nullable=False)
    original_suggestion: Mapped[str] = mapped_column(Text, nullable=False)
    final_suggestion: Mapped[str] = mapped_column(Text, nullable=False)
    ignore_reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_finding_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_clause_id: Mapped[str] = mapped_column(Text, nullable=False)
    include_in_report: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text)


class IdempotencyRecordRow(Base):
    __tablename__ = "idempotency_records"

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class SemanticPreferenceRow(Base):
    __tablename__ = "semantic_preferences"
    __table_args__ = (
        UniqueConstraint(
            "contract_type",
            "clause_type",
            "risk_type",
            "review_position",
            name="uq_semantic_preferences_scope",
        ),
    )

    preference_id: Mapped[str] = mapped_column(Text, primary_key=True)
    contract_type: Mapped[str] = mapped_column(Text, nullable=False)
    clause_type: Mapped[str] = mapped_column(Text, nullable=False)
    risk_type: Mapped[str] = mapped_column(Text, nullable=False)
    review_position: Mapped[str] = mapped_column(Text, nullable=False)
    support_count: Mapped[int] = mapped_column(Integer, nullable=False)
    opposition_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_memory_ids_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    variants_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    conflict_status: Mapped[str] = mapped_column(Text, nullable=False)
    base_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(Text, nullable=False)
    last_feedback_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_used_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class SemanticPreferenceEmbeddingRow(Base):
    __tablename__ = "semantic_preference_embeddings"

    preference_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            f"{APP_SCHEMA}.semantic_preferences.preference_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    embedding_mode: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding_model: Mapped[str] = mapped_column(Text, primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    vector_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


Index("idx_review_tasks_status", ReviewTaskRow.status, ReviewTaskRow.updated_at)
Index("idx_review_tasks_trace", ReviewTaskRow.trace_id)
Index(
    "idx_review_tasks_execution_owner",
    ReviewTaskRow.execution_owner,
    ReviewTaskRow.updated_at,
)
Index("idx_clauses_lookup", ClauseRow.task_id, ClauseRow.clause_id, ClauseRow.clause_type)
Index(
    "idx_clauses_search_trgm",
    ClauseRow.search_text,
    postgresql_using="gin",
    postgresql_ops={"search_text": "gin_trgm_ops"},
)
Index(
    "idx_clause_embeddings_lookup",
    ClauseEmbeddingRow.task_id,
    ClauseEmbeddingRow.embedding_model,
    ClauseEmbeddingRow.model_revision,
)
Index(
    "idx_clause_embeddings_hnsw",
    ClauseEmbeddingRow.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_with={"m": 16, "ef_construction": 64},
)
Index(
    "idx_risk_findings_lookup",
    RiskFindingRow.task_id,
    RiskFindingRow.risk_id,
    RiskFindingRow.status,
    RiskFindingRow.severity,
    RiskFindingRow.clause_id,
)
Index(
    "idx_step_logs_lookup",
    StepLogRow.task_id,
    StepLogRow.status,
    StepLogRow.tool_name,
    StepLogRow.created_at,
)
Index(
    "idx_step_logs_step_id",
    StepLogRow.step_id,
    unique=True,
    postgresql_where=StepLogRow.step_id != "",
)
Index(
    "idx_task_events_lookup",
    TaskEventRow.task_id,
    TaskEventRow.event_id,
    TaskEventRow.status,
    TaskEventRow.created_at,
)
Index(
    "idx_memory_lookup",
    MemoryItemRow.contract_type,
    MemoryItemRow.clause_type,
    MemoryItemRow.risk_type,
    MemoryItemRow.review_position,
    MemoryItemRow.created_at,
)
Index(
    "idx_memory_idempotency",
    MemoryItemRow.idempotency_key,
    unique=True,
    postgresql_where=MemoryItemRow.idempotency_key.is_not(None),
)
Index(
    "idx_semantic_preferences_lookup",
    SemanticPreferenceRow.contract_type,
    SemanticPreferenceRow.clause_type,
    SemanticPreferenceRow.risk_type,
    SemanticPreferenceRow.review_position,
    SemanticPreferenceRow.confidence,
)
Index(
    "idx_semantic_preference_embeddings_model",
    SemanticPreferenceEmbeddingRow.embedding_mode,
    SemanticPreferenceEmbeddingRow.embedding_model,
    SemanticPreferenceEmbeddingRow.vector_dimension,
)
