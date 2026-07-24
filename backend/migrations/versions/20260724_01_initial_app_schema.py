"""Create the PostgreSQL business schema.

Revision ID: 20260724_01
Revises:
Create Date: 2026-07-24
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260724_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "app"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.create_table(
        "review_tasks",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_type", sa.Text(), nullable=False),
        sa.Column("review_position", sa.Text(), nullable=False),
        sa.Column(
            "llm_mode",
            sa.Text(),
            server_default="local_structured",
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("current_node", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("contract_classification_json", postgresql.JSONB(), nullable=True),
        sa.Column("matched_rules_json", postgresql.JSONB(), nullable=True),
        sa.Column("review_contexts_json", postgresql.JSONB(), nullable=True),
        sa.Column("analysis_results_json", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_results_json", postgresql.JSONB(), nullable=True),
        sa.Column("report_file_json", postgresql.JSONB(), nullable=True),
        sa.Column("recovery_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "recovery_from_status",
            sa.Text(),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "retry_counts_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "recovery_history_json",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("cancel_requested_at", sa.Text(), server_default="", nullable=False),
        sa.Column("cancelled_at", sa.Text(), server_default="", nullable=False),
        sa.Column("cancel_reason", sa.Text(), server_default="", nullable=False),
        sa.Column("execution_owner", sa.Text(), server_default="", nullable=False),
        sa.Column("last_timeout_json", postgresql.JSONB(), nullable=True),
        sa.Column("progress_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
        schema=SCHEMA,
    )
    op.create_table(
        "documents",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("file_hash", sa.Text(), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("document_json", postgresql.JSONB(), nullable=True),
        sa.Column("parsed_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            [f"{SCHEMA}.review_tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id"),
        schema=SCHEMA,
    )
    op.create_table(
        "clauses",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("clause_id", sa.Text(), nullable=False),
        sa.Column("clause_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("key_fields_json", postgresql.JSONB(), nullable=False),
        sa.Column("source_location_json", postgresql.JSONB(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            [f"{SCHEMA}.review_tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "clause_id"),
        schema=SCHEMA,
    )
    op.create_table(
        "risk_findings",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("risk_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("clause_id", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            [f"{SCHEMA}.review_tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "risk_id"),
        schema=SCHEMA,
    )
    op.create_table(
        "step_logs",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.Text(), server_default="", nullable=False),
        sa.Column("step_id", sa.Text(), server_default="", nullable=False),
        sa.Column("step_name", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_summary", sa.Text(), nullable=False),
        sa.Column("output_summary", sa.Text(), nullable=False),
        sa.Column("token_cost_summary", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("parent_step_id", sa.Text(), server_default="", nullable=False),
        sa.Column("retry_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("idempotency_key", sa.Text(), server_default="", nullable=False),
        sa.Column("trace_summary_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            [f"{SCHEMA}.review_tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "log_index"),
        schema=SCHEMA,
    )
    op.create_table(
        "task_events",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("step_name", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            [f"{SCHEMA}.review_tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "event_id"),
        schema=SCHEMA,
    )
    op.create_table(
        "memory_items",
        sa.Column("memory_id", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.Text(), nullable=False),
        sa.Column("contract_type", sa.Text(), nullable=False),
        sa.Column("clause_type", sa.Text(), nullable=False),
        sa.Column("risk_type", sa.Text(), nullable=False),
        sa.Column("review_position", sa.Text(), nullable=False),
        sa.Column("user_action", sa.Text(), nullable=False),
        sa.Column("original_severity", sa.Text(), nullable=False),
        sa.Column("final_severity", sa.Text(), nullable=False),
        sa.Column("original_suggestion", sa.Text(), nullable=False),
        sa.Column("final_suggestion", sa.Text(), nullable=False),
        sa.Column("ignore_reason", sa.Text(), nullable=False),
        sa.Column("source_finding_id", sa.Text(), nullable=False),
        sa.Column("source_clause_id", sa.Text(), nullable=False),
        sa.Column("include_in_report", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("memory_id"),
        schema=SCHEMA,
    )
    op.create_table(
        "idempotency_records",
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("result_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("idempotency_key"),
        schema=SCHEMA,
    )
    op.create_table(
        "semantic_preferences",
        sa.Column("preference_id", sa.Text(), nullable=False),
        sa.Column("contract_type", sa.Text(), nullable=False),
        sa.Column("clause_type", sa.Text(), nullable=False),
        sa.Column("risk_type", sa.Text(), nullable=False),
        sa.Column("review_position", sa.Text(), nullable=False),
        sa.Column("support_count", sa.Integer(), nullable=False),
        sa.Column("opposition_count", sa.Integer(), nullable=False),
        sa.Column("source_memory_ids_json", postgresql.JSONB(), nullable=False),
        sa.Column("variants_json", postgresql.JSONB(), nullable=False),
        sa.Column("conflict_status", sa.Text(), nullable=False),
        sa.Column("base_confidence", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("lifecycle_status", sa.Text(), nullable=False),
        sa.Column("last_feedback_at", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("preference_id"),
        sa.UniqueConstraint(
            "contract_type",
            "clause_type",
            "risk_type",
            "review_position",
            name="uq_semantic_preferences_scope",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "semantic_preference_embeddings",
        sa.Column("preference_id", sa.Text(), nullable=False),
        sa.Column("embedding_mode", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("vector_dimension", sa.Integer(), nullable=False),
        sa.Column("vector_json", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["preference_id"],
            [f"{SCHEMA}.semantic_preferences.preference_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "preference_id",
            "embedding_mode",
            "embedding_model",
        ),
        schema=SCHEMA,
    )

    op.create_index(
        "idx_review_tasks_status",
        "review_tasks",
        ["status", "updated_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_review_tasks_trace",
        "review_tasks",
        ["trace_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_review_tasks_execution_owner",
        "review_tasks",
        ["execution_owner", "updated_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_clauses_lookup",
        "clauses",
        ["task_id", "clause_id", "clause_type"],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_risk_findings_lookup",
        "risk_findings",
        ["task_id", "risk_id", "status", "severity", "clause_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_step_logs_lookup",
        "step_logs",
        ["task_id", "status", "tool_name", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_step_logs_step_id",
        "step_logs",
        ["step_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("step_id <> ''"),
    )
    op.create_index(
        "idx_task_events_lookup",
        "task_events",
        ["task_id", "event_id", "status", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_memory_lookup",
        "memory_items",
        [
            "contract_type",
            "clause_type",
            "risk_type",
            "review_position",
            "created_at",
        ],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_memory_idempotency",
        "memory_items",
        ["idempotency_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "idx_semantic_preferences_lookup",
        "semantic_preferences",
        [
            "contract_type",
            "clause_type",
            "risk_type",
            "review_position",
            "confidence",
        ],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_semantic_preference_embeddings_model",
        "semantic_preference_embeddings",
        ["embedding_mode", "embedding_model", "vector_dimension"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    for table_name in (
        "semantic_preference_embeddings",
        "semantic_preferences",
        "idempotency_records",
        "memory_items",
        "task_events",
        "step_logs",
        "risk_findings",
        "clauses",
        "documents",
        "review_tasks",
    ):
        op.drop_table(table_name, schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
