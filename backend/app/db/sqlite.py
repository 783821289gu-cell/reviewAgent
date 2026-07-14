import os
import sqlite3

from config import settings


def connect(db_path: str | None = None) -> sqlite3.Connection:
    resolved_path = db_path or settings.memory_db_path
    parent = os.path.dirname(resolved_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(resolved_path)
    connection.row_factory = sqlite3.Row
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_items (
            memory_id TEXT PRIMARY KEY,
            memory_type TEXT NOT NULL,
            contract_type TEXT NOT NULL,
            clause_type TEXT NOT NULL,
            risk_type TEXT NOT NULL,
            review_position TEXT NOT NULL,
            user_action TEXT NOT NULL,
            original_severity TEXT NOT NULL,
            final_severity TEXT NOT NULL,
            original_suggestion TEXT NOT NULL,
            final_suggestion TEXT NOT NULL,
            ignore_reason TEXT NOT NULL,
            source_finding_id TEXT NOT NULL,
            source_clause_id TEXT NOT NULL,
            include_in_report INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_lookup
        ON memory_items (contract_type, clause_type, risk_type, review_position, created_at)
        """
    )
    connection.commit()
