import os
import sqlite3

from config import settings


def connect(db_path: str | None = None) -> sqlite3.Connection:
    resolved_path = db_path or settings.memory_db_path
    parent = os.path.dirname(resolved_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(resolved_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
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
            created_at TEXT NOT NULL,
            idempotency_key TEXT
        )
        """
    )
    _ensure_column(connection, "memory_items", "idempotency_key", "TEXT")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS review_tasks (
            task_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_type TEXT NOT NULL,
            review_position TEXT NOT NULL,
            message TEXT NOT NULL,
            current_node TEXT NOT NULL,
            error_message TEXT NOT NULL,
            contract_classification_json TEXT,
            matched_rules_json TEXT,
            review_contexts_json TEXT,
            analysis_results_json TEXT,
            evidence_results_json TEXT,
            report_file_json TEXT,
            recovery_count INTEGER NOT NULL DEFAULT 0,
            recovery_from_status TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS documents (
            task_id TEXT PRIMARY KEY,
            file_hash TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            document_json TEXT,
            parsed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES review_tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS clauses (
            task_id TEXT NOT NULL,
            clause_id TEXT NOT NULL,
            clause_type TEXT NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            key_fields_json TEXT NOT NULL,
            source_location_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (task_id, clause_id),
            FOREIGN KEY (task_id) REFERENCES review_tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS risk_findings (
            task_id TEXT NOT NULL,
            risk_id TEXT NOT NULL,
            status TEXT NOT NULL,
            severity TEXT NOT NULL,
            clause_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (task_id, risk_id),
            FOREIGN KEY (task_id) REFERENCES review_tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS step_logs (
            task_id TEXT NOT NULL,
            log_index INTEGER NOT NULL,
            trace_id TEXT NOT NULL DEFAULT '',
            step_id TEXT NOT NULL DEFAULT '',
            step_name TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            input_summary TEXT NOT NULL,
            output_summary TEXT NOT NULL,
            token_cost_summary TEXT NOT NULL,
            error_message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (task_id, log_index),
            FOREIGN KEY (task_id) REFERENCES review_tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_events (
            task_id TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL,
            step_name TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (task_id, event_id),
            FOREIGN KEY (task_id) REFERENCES review_tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS idempotency_records (
            idempotency_key TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS semantic_preferences (
            preference_id TEXT PRIMARY KEY,
            contract_type TEXT NOT NULL,
            clause_type TEXT NOT NULL,
            risk_type TEXT NOT NULL,
            review_position TEXT NOT NULL,
            support_count INTEGER NOT NULL,
            opposition_count INTEGER NOT NULL,
            source_memory_ids_json TEXT NOT NULL,
            variants_json TEXT NOT NULL,
            conflict_status TEXT NOT NULL,
            base_confidence REAL NOT NULL,
            confidence REAL NOT NULL,
            lifecycle_status TEXT NOT NULL,
            last_feedback_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (contract_type, clause_type, risk_type, review_position)
        );

        CREATE INDEX IF NOT EXISTS idx_review_tasks_status
        ON review_tasks (status, updated_at);

        CREATE INDEX IF NOT EXISTS idx_clauses_lookup
        ON clauses (task_id, clause_id, clause_type);

        CREATE INDEX IF NOT EXISTS idx_risk_findings_lookup
        ON risk_findings (task_id, risk_id, status, severity, clause_id);

        CREATE INDEX IF NOT EXISTS idx_step_logs_lookup
        ON step_logs (task_id, status, tool_name, created_at);

        CREATE INDEX IF NOT EXISTS idx_task_events_lookup
        ON task_events (task_id, event_id, status, created_at);

        CREATE INDEX IF NOT EXISTS idx_semantic_preferences_lookup
        ON semantic_preferences (
            contract_type, clause_type, risk_type, review_position, confidence
        );
        """
    )
    _ensure_column(connection, "review_tasks", "contract_classification_json", "TEXT")
    _ensure_column(connection, "review_tasks", "trace_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "step_logs", "trace_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "step_logs", "step_id", "TEXT NOT NULL DEFAULT ''")
    connection.execute(
        """
        UPDATE review_tasks
        SET trace_id = 'trace_' || REPLACE(task_id, 'task_', '')
        WHERE trace_id = ''
        """
    )
    connection.execute(
        """
        UPDATE step_logs
        SET trace_id = (
            SELECT trace_id FROM review_tasks WHERE review_tasks.task_id = step_logs.task_id
        )
        WHERE trace_id = ''
        """
    )
    connection.execute(
        """
        UPDATE step_logs
        SET step_id = 'step_' || REPLACE(task_id, 'task_', '') || '_' || printf('%04d', log_index)
        WHERE step_id = ''
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_step_logs_step_id
        ON step_logs (step_id)
        WHERE step_id != ''
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_review_tasks_trace
        ON review_tasks (trace_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_lookup
        ON memory_items (contract_type, clause_type, risk_type, review_position, created_at)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_idempotency
        ON memory_items (idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )
    connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
