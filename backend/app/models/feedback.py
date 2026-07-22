from dataclasses import asdict, dataclass
from datetime import datetime, timezone


FEEDBACK_ACTIONS = {
    "accept",
    "ignore",
    "update_severity",
    "update_suggestion",
    "update_evidence",
}
VALID_FINAL_SEVERITIES = {"高", "中", "低"}


@dataclass(frozen=True)
class HumanFeedback:
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


def build_human_feedback(payload: dict) -> HumanFeedback:
    action = str(payload.get("user_action", "")).strip()
    if action not in FEEDBACK_ACTIONS:
        raise ValueError(
            "feedback action must be accept, ignore, update_severity, "
            "update_suggestion or update_evidence"
        )

    final_severity = str(payload.get("final_severity", "")).strip()
    if action == "update_severity" and final_severity not in VALID_FINAL_SEVERITIES:
        raise ValueError("final_severity must be 高, 中 or 低")

    ignore_reason = str(payload.get("ignore_reason", "")).strip()
    if action == "ignore" and not ignore_reason:
        raise ValueError("ignore_reason is required when ignoring a risk")

    feedback = HumanFeedback(
        contract_type=_required(payload, "contract_type"),
        clause_type=_required(payload, "clause_type"),
        risk_type=_required(payload, "risk_type"),
        review_position=str(payload.get("review_position", "")).strip(),
        user_action=action,
        original_severity=str(payload.get("original_severity", "")).strip(),
        final_severity=final_severity,
        original_suggestion=str(payload.get("original_suggestion", "")).strip(),
        final_suggestion=str(payload.get("final_suggestion", "")).strip(),
        ignore_reason=ignore_reason,
        source_finding_id=_required(payload, "source_finding_id"),
        source_clause_id=_required(payload, "source_clause_id"),
        include_in_report=bool(payload.get("include_in_report", False)),
        created_at=str(payload.get("created_at") or datetime.now(timezone.utc).isoformat()),
    )
    if feedback.user_action == "update_suggestion" and not feedback.final_suggestion:
        raise ValueError("final_suggestion is required when updating a suggestion")
    return feedback


def _required(payload: dict, field_name: str) -> str:
    value = str(payload.get(field_name, "")).strip()
    if not value:
        raise ValueError(f"{field_name} is required")
    return value
