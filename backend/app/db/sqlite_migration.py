import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from sqlalchemy import Connection, create_engine, inspect, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import make_url

from db.postgres_models import APP_SCHEMA, Base


TABLE_ORDER = (
    "review_tasks",
    "documents",
    "clauses",
    "risk_findings",
    "step_logs",
    "task_events",
    "memory_items",
    "idempotency_records",
    "semantic_preferences",
    "semantic_preference_embeddings",
)

JSON_COLUMNS = {
    "review_tasks": {
        "contract_classification_json",
        "matched_rules_json",
        "review_contexts_json",
        "analysis_results_json",
        "evidence_results_json",
        "report_file_json",
        "retry_counts_json",
        "recovery_history_json",
        "last_timeout_json",
        "progress_json",
    },
    "documents": {"document_json"},
    "clauses": {"key_fields_json", "source_location_json", "payload_json"},
    "risk_findings": {"payload_json"},
    "step_logs": {"trace_summary_json"},
    "task_events": {"payload_json"},
    "idempotency_records": {"result_json"},
    "semantic_preferences": {"source_memory_ids_json", "variants_json"},
    "semantic_preference_embeddings": {"vector_json"},
}

BOOLEAN_COLUMNS = {
    "memory_items": {"include_in_report"},
}


class MigrationValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationSnapshot:
    rows_by_table: dict[str, list[dict[str, Any]]]
    row_counts: dict[str, int]
    table_hashes: dict[str, str]
    task_ids: tuple[str, ...]
    event_max_by_task: dict[str, int]

    def public_summary(self) -> dict[str, Any]:
        return {
            "row_counts": self.row_counts,
            "table_hashes": self.table_hashes,
            "task_id_count": len(self.task_ids),
            "task_ids_hash": _canonical_hash(list(self.task_ids)),
            "event_task_count": len(self.event_max_by_task),
            "event_max_by_task_hash": _canonical_hash(self.event_max_by_task),
        }


def read_sqlite_snapshot(source_path: str | Path) -> MigrationSnapshot:
    path = Path(source_path).expanduser().resolve()
    if not path.is_file():
        raise MigrationValidationError(f"SQLite source does not exist: {path}")

    connection = _open_read_only_sqlite(path)
    try:
        existing_tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing_tables = sorted(set(TABLE_ORDER) - existing_tables)
        if missing_tables:
            raise MigrationValidationError(
                f"SQLite source is missing tables: {', '.join(missing_tables)}"
            )

        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        for table_name in TABLE_ORDER:
            table = Base.metadata.tables[f"{APP_SCHEMA}.{table_name}"]
            expected_columns = tuple(column.name for column in table.columns)
            source_columns = {
                str(row["name"])
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            }
            missing_columns = sorted(set(expected_columns) - source_columns)
            if missing_columns:
                raise MigrationValidationError(
                    f"SQLite table {table_name} is missing columns: "
                    f"{', '.join(missing_columns)}"
                )
            order_columns = tuple(column.name for column in table.primary_key.columns)
            order_sql = ", ".join(f'"{name}"' for name in order_columns)
            selected_columns = ", ".join(f'"{name}"' for name in expected_columns)
            rows = connection.execute(
                f'SELECT {selected_columns} FROM "{table_name}" ORDER BY {order_sql}'
            ).fetchall()
            rows_by_table[table_name] = [
                _normalize_row(table_name, dict(row)) for row in rows
            ]
        return build_snapshot(rows_by_table)
    finally:
        connection.close()


def build_snapshot(
    rows_by_table: dict[str, list[dict[str, Any]]],
) -> MigrationSnapshot:
    normalized_tables = {
        table_name: [
            _normalize_row(table_name, row)
            for row in rows_by_table.get(table_name, [])
        ]
        for table_name in TABLE_ORDER
    }
    row_counts = {
        table_name: len(normalized_tables[table_name])
        for table_name in TABLE_ORDER
    }
    table_hashes = {
        table_name: _canonical_hash(normalized_tables[table_name])
        for table_name in TABLE_ORDER
    }
    task_ids = tuple(
        sorted(str(row["task_id"]) for row in normalized_tables["review_tasks"])
    )
    event_max_by_task: dict[str, int] = {}
    for row in normalized_tables["task_events"]:
        task_id = str(row["task_id"])
        event_max_by_task[task_id] = max(
            event_max_by_task.get(task_id, 0),
            int(row["event_id"]),
        )
    return MigrationSnapshot(
        rows_by_table=normalized_tables,
        row_counts=row_counts,
        table_hashes=table_hashes,
        task_ids=task_ids,
        event_max_by_task=event_max_by_task,
    )


def compare_snapshots(
    source: MigrationSnapshot,
    target: MigrationSnapshot,
) -> None:
    mismatches: list[str] = []
    if source.row_counts != target.row_counts:
        mismatches.append("table row counts")
    if source.task_ids != target.task_ids:
        mismatches.append("task IDs")
    if source.event_max_by_task != target.event_max_by_task:
        mismatches.append("event maximum sequence")
    if source.table_hashes != target.table_hashes:
        mismatches.append("canonical table JSON hashes")
    if mismatches:
        raise MigrationValidationError(
            "PostgreSQL validation failed: " + ", ".join(mismatches)
        )


def build_insert_statement(table_name: str, rows: list[dict[str, Any]]):
    if table_name not in TABLE_ORDER:
        raise ValueError(f"unsupported migration table: {table_name}")
    table = Base.metadata.tables[f"{APP_SCHEMA}.{table_name}"]
    return postgres_insert(table).values(rows).on_conflict_do_nothing()


def apply_sqlite_to_postgres(
    source_path: str | Path,
    database_url: str,
    *,
    batch_size: int = 500,
) -> dict[str, Any]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    source = read_sqlite_snapshot(source_path)
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            _require_postgres_schema(connection)
            for table_name in TABLE_ORDER:
                rows = source.rows_by_table[table_name]
                for offset in range(0, len(rows), batch_size):
                    batch = rows[offset : offset + batch_size]
                    if batch:
                        connection.execute(build_insert_statement(table_name, batch))
            target = read_postgres_snapshot(connection)
            compare_snapshots(source, target)
    finally:
        engine.dispose()
    return {
        "mode": "apply",
        "status": "validated",
        "source": str(Path(source_path).expanduser().resolve()),
        "target": make_url(database_url).render_as_string(hide_password=True),
        "validation": source.public_summary(),
    }


def read_postgres_snapshot(connection: Connection) -> MigrationSnapshot:
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for table_name in TABLE_ORDER:
        table = Base.metadata.tables[f"{APP_SCHEMA}.{table_name}"]
        order_columns = tuple(table.primary_key.columns)
        rows = connection.execute(select(table).order_by(*order_columns)).mappings()
        rows_by_table[table_name] = [
            _normalize_row(table_name, dict(row)) for row in rows
        ]
    return build_snapshot(rows_by_table)


def dry_run_sqlite(source_path: str | Path) -> dict[str, Any]:
    source = read_sqlite_snapshot(source_path)
    return {
        "mode": "dry-run",
        "status": "source_validated",
        "source": str(Path(source_path).expanduser().resolve()),
        "validation": source.public_summary(),
    }


def _open_read_only_sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _require_postgres_schema(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        raise MigrationValidationError(
            "SQLite migration target must use the PostgreSQL dialect"
        )
    existing_tables = set(inspect(connection).get_table_names(schema=APP_SCHEMA))
    missing_tables = sorted(set(TABLE_ORDER) - existing_tables)
    if missing_tables:
        raise MigrationValidationError(
            "PostgreSQL app schema is not upgraded; missing tables: "
            + ", ".join(missing_tables)
        )


def _normalize_row(
    table_name: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(row)
    for column_name in JSON_COLUMNS.get(table_name, set()):
        value = normalized.get(column_name)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise MigrationValidationError(
                    f"Invalid JSON in {table_name}.{column_name}"
                ) from exc
        normalized[column_name] = value
    for column_name in BOOLEAN_COLUMNS.get(table_name, set()):
        normalized[column_name] = bool(normalized.get(column_name))
    return normalized


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate the current SQLite business data to PostgreSQL."
    )
    parser.add_argument("--source", required=True, help="SQLite database path")
    parser.add_argument(
        "--mode",
        choices=("dry-run", "apply"),
        default="dry-run",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("REVIEW_AGENT_DATABASE_URL", ""),
        help="PostgreSQL SQLAlchemy URL; required for apply",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    if args.mode == "dry-run":
        result = dry_run_sqlite(args.source)
    else:
        if not args.database_url.strip():
            parser.error(
                "--database-url or REVIEW_AGENT_DATABASE_URL is required for apply"
            )
        result = apply_sqlite_to_postgres(
            args.source,
            args.database_url.strip(),
            batch_size=args.batch_size,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
