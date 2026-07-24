"""Add pgvector hybrid clause retrieval.

Revision ID: 20260724_02
Revises: 20260724_01
Create Date: 2026-07-24
"""
from typing import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa


revision: str = "20260724_02"
down_revision: str | None = "20260724_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "app"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.add_column(
        "clauses",
        sa.Column("search_text", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.execute(
        """
        UPDATE app.clauses
        SET search_text = concat_ws(
            ' ',
            title,
            clause_type,
            text,
            key_fields_json::text
        )
        """
    )
    op.alter_column(
        "clauses",
        "search_text",
        existing_type=sa.Text(),
        nullable=False,
        schema=SCHEMA,
    )
    op.create_index(
        "idx_clauses_search_trgm",
        "clauses",
        ["search_text"],
        schema=SCHEMA,
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )

    op.create_table(
        "clause_embeddings",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("clause_id", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("model_revision", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "clause_id"],
            [f"{SCHEMA}.clauses.task_id", f"{SCHEMA}.clauses.clause_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_id",
            "clause_id",
            "embedding_model",
            "model_revision",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "idx_clause_embeddings_lookup",
        "clause_embeddings",
        ["task_id", "embedding_model", "model_revision"],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_clause_embeddings_hnsw",
        "clause_embeddings",
        ["embedding"],
        schema=SCHEMA,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )


def downgrade() -> None:
    op.drop_index(
        "idx_clause_embeddings_hnsw",
        table_name="clause_embeddings",
        schema=SCHEMA,
        postgresql_using="hnsw",
    )
    op.drop_index(
        "idx_clause_embeddings_lookup",
        table_name="clause_embeddings",
        schema=SCHEMA,
    )
    op.drop_table("clause_embeddings", schema=SCHEMA)
    op.drop_index(
        "idx_clauses_search_trgm",
        table_name="clauses",
        schema=SCHEMA,
        postgresql_using="gin",
    )
    op.drop_column("clauses", "search_text", schema=SCHEMA)
