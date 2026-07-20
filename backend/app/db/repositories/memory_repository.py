import sqlite3
import json

from db.sqlite import connect
from models.memory import build_semantic_preference


class MemoryRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def save(self, item: dict, idempotency_key: str | None = None) -> dict:
        connection = connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = self._get_by_idempotency(connection, idempotency_key)
                if existing is not None:
                    connection.commit()
                    return existing
            connection.execute(
                """
                INSERT INTO memory_items (
                    memory_id, memory_type, contract_type, clause_type, risk_type,
                    review_position, user_action, original_severity, final_severity,
                    original_suggestion, final_suggestion, ignore_reason,
                    source_finding_id, source_clause_id, include_in_report,
                    created_at, idempotency_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["memory_id"],
                    item["memory_type"],
                    item["contract_type"],
                    item["clause_type"],
                    item["risk_type"],
                    item["review_position"],
                    item["user_action"],
                    item["original_severity"],
                    item["final_severity"],
                    item["original_suggestion"],
                    item["final_suggestion"],
                    item["ignore_reason"],
                    item["source_finding_id"],
                    item["source_clause_id"],
                    1 if item["include_in_report"] else 0,
                    item["created_at"],
                    idempotency_key,
                ),
            )
            self._rebuild_preference(connection, item)
            connection.commit()
            return item
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def find(
        self,
        contract_type: str,
        clause_type: str,
        risk_type: str,
        review_position: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        connection = connect(self.db_path)
        try:
            return connection.execute(
                """
                SELECT * FROM memory_items
                WHERE contract_type = ?
                  AND clause_type = ?
                  AND risk_type = ?
                  AND review_position IN ('', ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (contract_type, clause_type, risk_type, review_position, limit),
            ).fetchall()
        finally:
            connection.close()

    def find_preferences(
        self,
        contract_type: str,
        clause_type: str,
        risk_type: str,
        review_position: str,
    ) -> list[sqlite3.Row]:
        connection = connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._backfill_missing_preferences(
                connection,
                contract_type,
                clause_type,
                risk_type,
                review_position,
            )
            rows = connection.execute(
                """
                SELECT * FROM semantic_preferences
                WHERE contract_type = ?
                  AND clause_type = ?
                  AND risk_type = ?
                  AND review_position IN ('', ?)
                ORDER BY
                    CASE WHEN review_position = ? THEN 0 ELSE 1 END,
                    confidence DESC,
                    last_feedback_at DESC
                """,
                (
                    contract_type,
                    clause_type,
                    risk_type,
                    review_position,
                    review_position,
                ),
            ).fetchall()
            connection.commit()
            return rows
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def update_preference_lifecycle(
        self,
        preference_id: str,
        *,
        lifecycle_status: str,
        confidence: float,
        updated_at: str,
        last_used_at: str | None = None,
    ) -> None:
        connection = connect(self.db_path)
        try:
            if last_used_at is None:
                connection.execute(
                    """
                    UPDATE semantic_preferences
                    SET lifecycle_status = ?, confidence = ?, updated_at = ?
                    WHERE preference_id = ?
                    """,
                    (lifecycle_status, confidence, updated_at, preference_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE semantic_preferences
                    SET lifecycle_status = ?, confidence = ?, last_used_at = ?, updated_at = ?
                    WHERE preference_id = ?
                    """,
                    (
                        lifecycle_status,
                        confidence,
                        last_used_at,
                        updated_at,
                        preference_id,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def _get_by_idempotency(
        self,
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> dict | None:
        row = connection.execute(
            "SELECT * FROM memory_items WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def _backfill_missing_preferences(
        self,
        connection: sqlite3.Connection,
        contract_type: str,
        clause_type: str,
        risk_type: str,
        review_position: str,
    ) -> None:
        positions = connection.execute(
            """
            SELECT DISTINCT review_position FROM memory_items
            WHERE contract_type = ?
              AND clause_type = ?
              AND risk_type = ?
              AND review_position IN ('', ?)
            """,
            (contract_type, clause_type, risk_type, review_position),
        ).fetchall()
        for position_row in positions:
            position = str(position_row["review_position"])
            exists = connection.execute(
                """
                SELECT 1 FROM semantic_preferences
                WHERE contract_type = ? AND clause_type = ? AND risk_type = ?
                  AND review_position = ?
                """,
                (contract_type, clause_type, risk_type, position),
            ).fetchone()
            if exists is None:
                self._rebuild_preference(
                    connection,
                    {
                        "contract_type": contract_type,
                        "clause_type": clause_type,
                        "risk_type": risk_type,
                        "review_position": position,
                    },
                )

    def _rebuild_preference(
        self,
        connection: sqlite3.Connection,
        item: dict,
    ) -> None:
        group = (
            str(item["contract_type"]),
            str(item["clause_type"]),
            str(item["risk_type"]),
            str(item["review_position"]),
        )
        rows = connection.execute(
            """
            SELECT * FROM memory_items
            WHERE contract_type = ? AND clause_type = ? AND risk_type = ?
              AND review_position = ?
            ORDER BY created_at, memory_id
            """,
            group,
        ).fetchall()
        if not rows:
            return
        existing = connection.execute(
            """
            SELECT last_used_at FROM semantic_preferences
            WHERE contract_type = ? AND clause_type = ? AND risk_type = ?
              AND review_position = ?
            """,
            group,
        ).fetchone()
        preference = build_semantic_preference(
            [_row_to_dict(row) for row in rows],
            last_used_at=str(existing["last_used_at"]) if existing is not None else "",
        ).to_dict()
        connection.execute(
            """
            INSERT INTO semantic_preferences (
                preference_id, contract_type, clause_type, risk_type, review_position,
                support_count, opposition_count, source_memory_ids_json, variants_json,
                conflict_status, base_confidence, confidence, lifecycle_status,
                last_feedback_at, last_used_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contract_type, clause_type, risk_type, review_position)
            DO UPDATE SET
                preference_id = excluded.preference_id,
                support_count = excluded.support_count,
                opposition_count = excluded.opposition_count,
                source_memory_ids_json = excluded.source_memory_ids_json,
                variants_json = excluded.variants_json,
                conflict_status = excluded.conflict_status,
                base_confidence = excluded.base_confidence,
                confidence = excluded.confidence,
                lifecycle_status = excluded.lifecycle_status,
                last_feedback_at = excluded.last_feedback_at,
                last_used_at = excluded.last_used_at,
                updated_at = excluded.updated_at
            """,
            (
                preference["preference_id"],
                preference["contract_type"],
                preference["clause_type"],
                preference["risk_type"],
                preference["review_position"],
                preference["support_count"],
                preference["opposition_count"],
                json.dumps(preference["source_memory_ids"], ensure_ascii=False),
                json.dumps(preference["variants"], ensure_ascii=False),
                preference["conflict_status"],
                preference["base_confidence"],
                preference["confidence"],
                preference["lifecycle_status"],
                preference["last_feedback_at"],
                preference["last_used_at"],
                preference["updated_at"],
            ),
        )


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "memory_id": str(row["memory_id"]),
        "memory_type": str(row["memory_type"]),
        "contract_type": str(row["contract_type"]),
        "clause_type": str(row["clause_type"]),
        "risk_type": str(row["risk_type"]),
        "review_position": str(row["review_position"]),
        "user_action": str(row["user_action"]),
        "original_severity": str(row["original_severity"]),
        "final_severity": str(row["final_severity"]),
        "original_suggestion": str(row["original_suggestion"]),
        "final_suggestion": str(row["final_suggestion"]),
        "ignore_reason": str(row["ignore_reason"]),
        "source_finding_id": str(row["source_finding_id"]),
        "source_clause_id": str(row["source_clause_id"]),
        "include_in_report": bool(row["include_in_report"]),
        "created_at": str(row["created_at"]),
    }
