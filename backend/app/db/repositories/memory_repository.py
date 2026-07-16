import sqlite3

from db.sqlite import connect


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
