from dataclasses import asdict, dataclass
from enum import StrEnum


class PlannerAction(StrEnum):
    RETRIEVE_AGAIN = "RETRIEVE_AGAIN"
    ANALYZE_AGAIN = "ANALYZE_AGAIN"
    REQUEST_HUMAN_REVIEW = "REQUEST_HUMAN_REVIEW"
    TERMINATE = "TERMINATE"


class PlannerReasonCode(StrEnum):
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    RETRIEVAL_INSUFFICIENT = "RETRIEVAL_INSUFFICIENT"
    ANALYZER_VERIFIER_CONFLICT = "ANALYZER_VERIFIER_CONFLICT"
    STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"


@dataclass(frozen=True)
class PlannerDecision:
    action: PlannerAction
    reason_code: PlannerReasonCode
    target_clause_id: str
    query_adjustments: dict
    confidence: float

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["action"] = self.action.value
        payload["reason_code"] = self.reason_code.value
        return payload
