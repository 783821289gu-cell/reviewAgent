from uuid import uuid4

from db.sqlite import connect
from models.feedback import build_human_feedback
from models.memory import MemoryItem, memory_item_from_row


def retrieve_memory(tool_input: dict) -> list[dict]:
    contract_type = str(tool_input.get("contract_type", "")).strip()
    clause = tool_input.get("clause")
    risk_type = str(tool_input.get("risk_type", "")).strip()
    review_position = str(tool_input.get("review_position", "")).strip()
    memory_items = tool_input.get("memory_items")
    limit = int(tool_input.get("limit", 3))

    if not contract_type:
        raise ValueError("contract_type is required")
    if not isinstance(clause, dict):
        raise ValueError("clause must be a dict")
    if not risk_type:
        raise ValueError("risk_type is required")
    if not review_position:
        raise ValueError("review_position is required")
    if limit <= 0:
        return []

    if isinstance(memory_items, list) and memory_items:
        return _filter_provided_memory_items(contract_type, clause, risk_type, review_position, memory_items, limit)
    if memory_items not in (None, []):
        raise ValueError("memory_items must be a list")

    return _query_sqlite_memory(
        contract_type=contract_type,
        clause_type=str(clause.get("clause_type", "")),
        risk_type=risk_type,
        review_position=review_position,
        limit=limit,
        db_path=tool_input.get("db_path"),
    )


def write_memory(tool_input: dict) -> dict:
    raw_feedback = tool_input.get("human_feedback")
    if not isinstance(raw_feedback, dict):
        raise ValueError("human_feedback must be a dict")

    feedback = build_human_feedback(raw_feedback)
    item = MemoryItem(
        memory_id=str(raw_feedback.get("memory_id") or f"MEM-{uuid4().hex[:12]}"),
        memory_type="human_feedback",
        contract_type=feedback.contract_type,
        clause_type=feedback.clause_type,
        risk_type=feedback.risk_type,
        review_position=feedback.review_position,
        user_action=feedback.user_action,
        original_severity=feedback.original_severity,
        final_severity=feedback.final_severity,
        original_suggestion=feedback.original_suggestion,
        final_suggestion=feedback.final_suggestion,
        ignore_reason=feedback.ignore_reason,
        source_finding_id=feedback.source_finding_id,
        source_clause_id=feedback.source_clause_id,
        include_in_report=feedback.include_in_report,
        created_at=feedback.created_at,
    )

    connection = connect(tool_input.get("db_path"))
    try:
        connection.execute(
            """
            INSERT INTO memory_items (
                memory_id,
                memory_type,
                contract_type,
                clause_type,
                risk_type,
                review_position,
                user_action,
                original_severity,
                final_severity,
                original_suggestion,
                final_suggestion,
                ignore_reason,
                source_finding_id,
                source_clause_id,
                include_in_report,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.memory_id,
                item.memory_type,
                item.contract_type,
                item.clause_type,
                item.risk_type,
                item.review_position,
                item.user_action,
                item.original_severity,
                item.final_severity,
                item.original_suggestion,
                item.final_suggestion,
                item.ignore_reason,
                item.source_finding_id,
                item.source_clause_id,
                1 if item.include_in_report else 0,
                item.created_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    return item.to_dict()


def _filter_provided_memory_items(
    contract_type: str,
    clause: dict,
    risk_type: str,
    review_position: str,
    memory_items: list,
    limit: int,
) -> list[dict]:
    clause_type = str(clause.get("clause_type", ""))
    matched_items = []
    for item in memory_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("contract_type", "")) != contract_type:
            continue
        score = 0
        if str(item.get("risk_type", "")) == risk_type:
            score += 3
        if str(item.get("clause_type", "")) == clause_type:
            score += 2
        if str(item.get("review_position", "")) in ("", review_position):
            score += 1
        if score == 0:
            continue
        payload = dict(item)
        payload["match_score"] = score
        payload["memory_source"] = "provided_memory_items"
        matched_items.append(payload)

    return sorted(matched_items, key=lambda item: (item["match_score"], item.get("created_at", "")), reverse=True)[
        :limit
    ]


def _query_sqlite_memory(
    contract_type: str,
    clause_type: str,
    risk_type: str,
    review_position: str,
    limit: int,
    db_path: str | None = None,
) -> list[dict]:
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT *
            FROM memory_items
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

    payloads = []
    for row in rows:
        payload = memory_item_from_row(row).to_dict()
        payload["match_score"] = 6
        payload["memory_source"] = "sqlite_memory"
        payloads.append(payload)
    return payloads
