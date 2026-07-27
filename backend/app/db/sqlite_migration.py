import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Sequence

from sqlalchemy import Connection, create_engine, delete, inspect, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import make_url

from db.clause_text import clause_search_text
from db.postgres_models import APP_SCHEMA, Base
from models.memory import build_semantic_preference


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
TARGET_ONLY_COLUMNS = {
    "clauses": {"search_text"},
}
ADDITIONAL_MEMORY_TABLES = {
    "memory_items",
    "semantic_preferences",
    "semantic_preference_embeddings",
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
            expected_columns = _migration_column_names(table_name, table)
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
        different_tables = sorted(
            table_name
            for table_name in TABLE_ORDER
            if source.table_hashes[table_name] != target.table_hashes[table_name]
        )
        mismatches.append(
            "canonical table JSON hashes (" + ", ".join(different_tables) + ")"
        )
    if mismatches:
        raise MigrationValidationError(
            "PostgreSQL validation failed: " + ", ".join(mismatches)
        )


def build_insert_statement(table_name: str, rows: list[dict[str, Any]]):
    if table_name not in TABLE_ORDER:
        raise ValueError(f"unsupported migration table: {table_name}")
    table = Base.metadata.tables[f"{APP_SCHEMA}.{table_name}"]
    insert_rows = rows
    if table_name == "clauses":
        insert_rows = [
            {
                **row,
                "search_text": clause_search_text(
                    row,
                    key_fields_name="key_fields_json",
                ),
            }
            for row in rows
        ]
    return postgres_insert(table).values(insert_rows).on_conflict_do_nothing()


def apply_sqlite_to_postgres(
    source_path: str | Path,
    database_url: str,
    *,
    batch_size: int = 500,
    additional_memory_sources: Sequence[str | Path] = (),
) -> dict[str, Any]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    source = build_composite_snapshot(
        read_sqlite_snapshot(source_path),
        tuple(read_sqlite_snapshot(path) for path in additional_memory_sources),
    )
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            _require_postgres_schema(connection)
            if additional_memory_sources:
                connection.execute(
                    delete(
                        Base.metadata.tables[
                            f"{APP_SCHEMA}.semantic_preference_embeddings"
                        ]
                    )
                )
                connection.execute(
                    delete(
                        Base.metadata.tables[f"{APP_SCHEMA}.semantic_preferences"]
                    )
                )
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
        "additional_memory_sources": [
            str(Path(path).expanduser().resolve())
            for path in additional_memory_sources
        ],
        "target": make_url(database_url).render_as_string(hide_password=True),
        "validation": source.public_summary(),
    }


def read_postgres_snapshot(connection: Connection) -> MigrationSnapshot:
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for table_name in TABLE_ORDER:
        table = Base.metadata.tables[f"{APP_SCHEMA}.{table_name}"]
        order_columns = tuple(table.primary_key.columns)
        selected_columns = [
            table.c[column_name]
            for column_name in _migration_column_names(table_name, table)
        ]
        rows = connection.execute(
            select(*selected_columns).order_by(*order_columns)
        ).mappings()
        rows_by_table[table_name] = [
            _normalize_row(table_name, dict(row)) for row in rows
        ]
    return build_snapshot(rows_by_table)


def dry_run_sqlite(
    source_path: str | Path,
    *,
    additional_memory_sources: Sequence[str | Path] = (),
) -> dict[str, Any]:
    source = build_composite_snapshot(
        read_sqlite_snapshot(source_path),
        tuple(read_sqlite_snapshot(path) for path in additional_memory_sources),
    )
    return {
        "mode": "dry-run",
        "status": "source_validated",
        "source": str(Path(source_path).expanduser().resolve()),
        "additional_memory_sources": [
            str(Path(path).expanduser().resolve())
            for path in additional_memory_sources
        ],
        "validation": source.public_summary(),
    }


def build_composite_snapshot(
    primary: MigrationSnapshot,
    additional_memory: Sequence[MigrationSnapshot],
) -> MigrationSnapshot:
    if not additional_memory:
        return primary

    for index, snapshot in enumerate(additional_memory, start=1):
        unexpected = {
            table_name: count
            for table_name, count in snapshot.row_counts.items()
            if table_name not in ADDITIONAL_MEMORY_TABLES and count
        }
        if unexpected:
            details = ", ".join(
                f"{table_name}={count}"
                for table_name, count in sorted(unexpected.items())
            )
            raise MigrationValidationError(
                f"Additional Memory source {index} contains business rows: {details}"
            )

    rows_by_table = {
        table_name: [dict(row) for row in primary.rows_by_table[table_name]]
        for table_name in TABLE_ORDER
    }
    memory_by_id = {
        str(row["memory_id"]): dict(row)
        for row in rows_by_table["memory_items"]
    }
    idempotency_owners = {
        str(row["idempotency_key"]): str(row["memory_id"])
        for row in memory_by_id.values()
        if row.get("idempotency_key")
    }
    for snapshot in additional_memory:
        for row in snapshot.rows_by_table["memory_items"]:
            memory_id = str(row["memory_id"])
            existing = memory_by_id.get(memory_id)
            if existing is not None and existing != row:
                raise MigrationValidationError(
                    f"Memory ID {memory_id} has conflicting source rows"
                )
            idempotency_key = str(row.get("idempotency_key") or "")
            owner = idempotency_owners.get(idempotency_key)
            if idempotency_key and owner not in (None, memory_id):
                raise MigrationValidationError(
                    f"Memory idempotency key {idempotency_key} belongs to "
                    f"both {owner} and {memory_id}"
                )
            memory_by_id[memory_id] = dict(row)
            if idempotency_key:
                idempotency_owners[idempotency_key] = memory_id

    all_snapshots = (primary, *additional_memory)
    last_used_by_group: dict[tuple[str, str, str, str], str] = {}
    for snapshot in all_snapshots:
        for row in snapshot.rows_by_table["semantic_preferences"]:
            group = _memory_group(row)
            last_used_by_group[group] = max(
                last_used_by_group.get(group, ""),
                str(row.get("last_used_at") or ""),
            )

    grouped_memory: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in memory_by_id.values():
        grouped_memory.setdefault(_memory_group(row), []).append(row)

    preferences = []
    for group, memory_rows in sorted(grouped_memory.items()):
        preference = build_semantic_preference(
            memory_rows,
            last_used_at=last_used_by_group.get(group, ""),
        ).to_dict()
        preference.pop("memory_type")
        preference["source_memory_ids_json"] = preference.pop("source_memory_ids")
        preference["variants_json"] = preference.pop("variants")
        preferences.append(preference)

    rows_by_table["memory_items"] = sorted(
        memory_by_id.values(),
        key=lambda row: str(row["memory_id"]),
    )
    rows_by_table["semantic_preferences"] = sorted(
        preferences,
        key=lambda row: str(row["preference_id"]),
    )
    # Embeddings are derived from preference content and must not survive a rebuild.
    rows_by_table["semantic_preference_embeddings"] = []
    return build_snapshot(rows_by_table)


def _memory_group(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row["contract_type"]),
        str(row["clause_type"]),
        str(row["risk_type"]),
        str(row["review_position"]),
    )


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


def _migration_column_names(table_name: str, table) -> tuple[str, ...]:
    excluded = TARGET_ONLY_COLUMNS.get(table_name, set())
    return tuple(
        column.name for column in table.columns if column.name not in excluded
    )


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
    parser.add_argument(
        "--additional-memory-source",
        action="append",
        default=[],
        help=(
            "Additional SQLite source containing Memory rows only; may be repeated. "
            "Semantic preferences are rebuilt and legacy embeddings are discarded."
        ),
    )
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    if args.mode == "dry-run":
        result = dry_run_sqlite(
            args.source,
            additional_memory_sources=args.additional_memory_source,
        )
    else:
        if not args.database_url.strip():
            parser.error(
                "--database-url or REVIEW_AGENT_DATABASE_URL is required for apply"
            )
        result = apply_sqlite_to_postgres(
            args.source,
            args.database_url.strip(),
            batch_size=args.batch_size,
            additional_memory_sources=args.additional_memory_source,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
