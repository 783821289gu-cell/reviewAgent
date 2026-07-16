import json
import sqlite3
from datetime import datetime, timezone

from db.sqlite import connect
from models.review import AgentState


class DocumentRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def save_upload(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        file_hash: str,
        stored_path: str,
    ) -> None:
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO documents (
                task_id, file_hash, stored_path, document_json, parsed_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                file_hash = excluded.file_hash,
                stored_path = excluded.stored_path,
                updated_at = excluded.updated_at
            """,
            (task_id, file_hash, stored_path, now, now),
        )

    def save_results(self, connection: sqlite3.Connection, state: AgentState) -> None:
        now = _utc_now()
        if state.document is not None:
            connection.execute(
                """
                INSERT INTO documents (
                    task_id, file_hash, stored_path, document_json, parsed_at,
                    created_at, updated_at
                )
                VALUES (?, '', '', ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    document_json = excluded.document_json,
                    parsed_at = COALESCE(documents.parsed_at, excluded.parsed_at),
                    updated_at = excluded.updated_at
                """,
                (state.task_id, _json_dump(state.document), now, now, now),
            )

        if state.clauses is None:
            return

        clause_ids = []
        for clause in state.clauses:
            clause_id = str(clause.get("clause_id", ""))
            if not clause_id:
                raise ValueError("persisted clause requires clause_id")
            clause_ids.append(clause_id)
            connection.execute(
                """
                INSERT INTO clauses (
                    task_id, clause_id, clause_type, title, text, key_fields_json,
                    source_location_json, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, clause_id) DO UPDATE SET
                    clause_type = excluded.clause_type,
                    title = excluded.title,
                    text = excluded.text,
                    key_fields_json = excluded.key_fields_json,
                    source_location_json = excluded.source_location_json,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.task_id,
                    clause_id,
                    str(clause.get("clause_type", "")),
                    str(clause.get("title", "")),
                    str(clause.get("text", "")),
                    _json_dump(clause.get("key_fields") or {}),
                    _json_dump(clause.get("source_location") or {}),
                    _json_dump(clause),
                    now,
                    now,
                ),
            )
        _delete_missing(connection, "clauses", state.task_id, "clause_id", clause_ids)

    def get_upload(self, task_id: str) -> dict | None:
        connection = connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT file_hash, stored_path FROM documents WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return {"file_hash": str(row["file_hash"]), "stored_path": str(row["stored_path"])}
        finally:
            connection.close()

    def get_document(self, task_id: str):
        connection = connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT document_json FROM documents WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["document_json"] is None:
                return None
            return json.loads(row["document_json"])
        finally:
            connection.close()

    def get_clauses(self, task_id: str) -> list[dict] | None:
        connection = connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT payload_json FROM clauses WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows] if rows else None
        finally:
            connection.close()


def _delete_missing(
    connection: sqlite3.Connection,
    table_name: str,
    task_id: str,
    id_column: str,
    identifiers: list[str],
) -> None:
    if not identifiers:
        connection.execute(f"DELETE FROM {table_name} WHERE task_id = ?", (task_id,))
        return
    placeholders = ",".join("?" for _ in identifiers)
    connection.execute(
        f"DELETE FROM {table_name} WHERE task_id = ? AND {id_column} NOT IN ({placeholders})",
        (task_id, *identifiers),
    )


def _json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
