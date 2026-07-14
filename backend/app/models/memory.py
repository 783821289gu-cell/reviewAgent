from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    memory_type: str
    contract_type: str
    clause_type: str
    risk_type: str
    review_position: str
    user_action: str
    original_severity: str
    final_severity: str
    original_suggestion: str
    final_suggestion: str
    ignore_reason: str
    source_finding_id: str
    source_clause_id: str
    include_in_report: bool
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def memory_item_from_row(row) -> MemoryItem:
    return MemoryItem(
        memory_id=str(row["memory_id"]),
        memory_type=str(row["memory_type"]),
        contract_type=str(row["contract_type"]),
        clause_type=str(row["clause_type"]),
        risk_type=str(row["risk_type"]),
        review_position=str(row["review_position"]),
        user_action=str(row["user_action"]),
        original_severity=str(row["original_severity"]),
        final_severity=str(row["final_severity"]),
        original_suggestion=str(row["original_suggestion"]),
        final_suggestion=str(row["final_suggestion"]),
        ignore_reason=str(row["ignore_reason"]),
        source_finding_id=str(row["source_finding_id"]),
        source_clause_id=str(row["source_clause_id"]),
        include_in_report=bool(row["include_in_report"]),
        created_at=str(row["created_at"]),
    )
