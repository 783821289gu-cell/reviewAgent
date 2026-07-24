from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB


APP_DIR = Path(__file__).resolve().parents[1] / "app"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_DIR))

from db.postgres_models import APP_SCHEMA, Base
from db.sqlite import connect
from db.sqlite_migration import (
    MigrationValidationError,
    TABLE_ORDER,
    build_insert_statement,
    compare_snapshots,
    dry_run_sqlite,
    read_sqlite_snapshot,
)


class SqlitePostgresMigrationTest(unittest.TestCase):
    def test_postgres_metadata_covers_every_sqlite_business_table(self):
        table_names = {
            table.name
            for table in Base.metadata.tables.values()
            if table.schema == APP_SCHEMA
        }

        self.assertEqual(table_names, set(TABLE_ORDER))
        self.assertTrue(
            isinstance(
                Base.metadata.tables[f"{APP_SCHEMA}.review_tasks"].c.progress_json.type,
                JSONB,
            )
        )
        self.assertTrue(
            Base.metadata.tables[
                f"{APP_SCHEMA}.semantic_preference_embeddings"
            ].c.preference_id.foreign_keys
        )

    def test_alembic_offline_sql_creates_schema_and_business_tables(self):
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option(
            "sqlalchemy.url",
            "postgresql+psycopg://review_agent:masked@127.0.0.1/review_agent",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            command.upgrade(config, "head", sql=True)

        sql = output.getvalue()
        self.assertIn("CREATE SCHEMA IF NOT EXISTS app", sql)
        for table_name in TABLE_ORDER:
            self.assertIn(f"CREATE TABLE app.{table_name}", sql)

    def test_dry_run_is_read_only_and_normalizes_json_before_hashing(self):
        with TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "first.sqlite3"
            second_path = Path(temp_dir) / "second.sqlite3"
            self._create_source(
                first_path,
                matched_rules_json='[{"b": 2, "a": 1}]',
                event_payload_json='{"z": 3, "task_id": "task_1"}',
            )
            self._create_source(
                second_path,
                matched_rules_json='[{"a":1,"b":2}]',
                event_payload_json='{"task_id":"task_1","z":3}',
            )
            modified_before = first_path.stat().st_mtime_ns

            first = read_sqlite_snapshot(first_path)
            result = dry_run_sqlite(first_path)
            second = read_sqlite_snapshot(second_path)

            self.assertEqual(first.table_hashes, second.table_hashes)
            self.assertEqual(first.task_ids, ("task_1",))
            self.assertEqual(first.event_max_by_task, {"task_1": 1})
            self.assertEqual(result["status"], "source_validated")
            self.assertEqual(result["validation"]["task_id_count"], 1)
            self.assertEqual(first_path.stat().st_mtime_ns, modified_before)

    def test_postgres_insert_is_idempotent_and_does_not_overwrite(self):
        statement = build_insert_statement(
            "review_tasks",
            [{"task_id": "task_1"}],
        )

        sql = str(statement.compile(dialect=postgresql.dialect()))

        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertNotIn("DO UPDATE", sql)

    def test_validation_reports_each_invariant_mismatch(self):
        with TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source.sqlite3"
            self._create_source(
                source_path,
                matched_rules_json="[]",
                event_payload_json='{"task_id":"task_1"}',
            )
            source = read_sqlite_snapshot(source_path)
            target_rows = deepcopy(source.rows_by_table)
            target_rows["task_events"][0]["event_id"] = 2
            target = source.__class__(
                rows_by_table=target_rows,
                row_counts=source.row_counts,
                table_hashes={
                    **source.table_hashes,
                    "task_events": "different",
                },
                task_ids=("task_other",),
                event_max_by_task={"task_1": 2},
            )

            with self.assertRaisesRegex(
                MigrationValidationError,
                "task IDs.*event maximum sequence.*canonical table JSON hashes",
            ):
                compare_snapshots(source, target)

    @staticmethod
    def _create_source(
        path: Path,
        *,
        matched_rules_json: str,
        event_payload_json: str,
    ) -> None:
        connection = connect(str(path))
        try:
            connection.execute(
                """
                INSERT INTO review_tasks (
                    task_id, status, file_name, file_type, review_position,
                    message, current_node, error_message, matched_rules_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "task_1",
                    "START",
                    "sample.docx",
                    "docx",
                    "甲方",
                    "created",
                    "start",
                    "",
                    matched_rules_json,
                    "2026-07-24T00:00:00+00:00",
                    "2026-07-24T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO task_events (
                    task_id, event_id, status, message, step_name, tool_name,
                    created_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "task_1",
                    1,
                    "START",
                    "created",
                    "create_task",
                    "",
                    "2026-07-24T00:00:00+00:00",
                    event_payload_json,
                ),
            )
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
