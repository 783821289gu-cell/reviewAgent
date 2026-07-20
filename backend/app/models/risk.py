from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite


VALID_RISK_TYPES = {
    "保密信息范围过宽",
    "缺少保密信息例外",
    "保密期限不合理",
    "使用目的或使用限制不清",
    "允许披露对象过宽",
    "返还或销毁义务不明确",
    "责任无限或责任边界不清",
    "违约责任或违约金明显不合理",
}

VALID_SEVERITIES = {"高", "中", "低"}
VALID_REVIEW_STATUSES = {"CONFIRMED_RISK", "NEED_MANUAL_REVIEW", "NO_RISK"}
VALID_REVIEW_POSITIONS = {"甲方", "乙方"}


class CriticDecision(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"
    REQUEST_HUMAN_REVIEW = "REQUEST_HUMAN_REVIEW"


class CriticReasonCode(StrEnum):
    SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK = "SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK"
    CLAUSE_MISMATCH = "CLAUSE_MISMATCH"
    EVIDENCE_UNSUPPORTED = "EVIDENCE_UNSUPPORTED"
    PLAYBOOK_MISMATCH = "PLAYBOOK_MISMATCH"
    REASON_SUPPORT_AMBIGUOUS = "REASON_SUPPORT_AMBIGUOUS"
    PROMPT_INJECTION_DETECTED = "PROMPT_INJECTION_DETECTED"


CRITIC_REASONS_BY_DECISION = {
    CriticDecision.PASS: {CriticReasonCode.SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK},
    CriticDecision.REJECT: {
        CriticReasonCode.CLAUSE_MISMATCH,
        CriticReasonCode.EVIDENCE_UNSUPPORTED,
        CriticReasonCode.PLAYBOOK_MISMATCH,
    },
    CriticDecision.REQUEST_HUMAN_REVIEW: {
        CriticReasonCode.REASON_SUPPORT_AMBIGUOUS,
        CriticReasonCode.PROMPT_INJECTION_DETECTED,
    },
}


@dataclass(frozen=True)
class RiskFinding:
    risk_id: str
    risk_type: str
    severity: str
    confidence: float
    risk_reason: str
    clause_id: str
    evidence_text: str
    matched_rule_ids: list[str]
    review_position: str
    risk_focus: str
    revision_suggestion: str
    review_status: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceResult:
    risk_id: str
    clause_id: str
    evidence_text: str
    is_valid: bool
    failure_reason: str
    verified_clause_id: str
    source_location: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CriticResult:
    decision: CriticDecision
    reason_code: CriticReasonCode

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason_code": self.reason_code.value,
        }


def validate_critic_result(payload: dict) -> CriticResult:
    if not isinstance(payload, dict):
        raise ValueError("critic result must be a dict")
    if set(payload) != {"decision", "reason_code"}:
        raise ValueError("critic result fields do not match the output whitelist")
    try:
        decision = CriticDecision(payload["decision"])
    except (TypeError, ValueError) as exc:
        raise ValueError("critic decision is not allowed") from exc
    try:
        reason_code = CriticReasonCode(payload["reason_code"])
    except (TypeError, ValueError) as exc:
        raise ValueError("critic reason_code is not allowed") from exc
    if reason_code not in CRITIC_REASONS_BY_DECISION[decision]:
        raise ValueError("critic reason_code is not allowed for decision")
    return CriticResult(decision=decision, reason_code=reason_code)


def validate_risk_finding(payload: dict) -> RiskFinding:
    if not isinstance(payload, dict):
        raise ValueError("risk finding must be a dict")
    required_fields = {
        "risk_type",
        "severity",
        "confidence",
        "risk_reason",
        "clause_id",
        "evidence_text",
        "matched_rule_ids",
        "review_position",
        "risk_focus",
        "revision_suggestion",
        "review_status",
    }
    missing_fields = required_fields - set(payload)
    if missing_fields:
        raise ValueError(f"risk finding missing fields: {', '.join(sorted(missing_fields))}")

    risk_type = _required_string(payload, "risk_type")
    severity = _required_string(payload, "severity")
    review_status = _required_string(payload, "review_status")
    review_position = _required_string(payload, "review_position")
    risk_focus = _required_string(payload, "risk_focus")
    confidence_value = payload["confidence"]
    if isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)):
        raise ValueError("confidence must be numeric")
    confidence = float(confidence_value)
    matched_rule_ids = payload["matched_rule_ids"]

    if risk_type not in VALID_RISK_TYPES:
        raise ValueError(f"invalid risk_type: {risk_type}")
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"invalid severity: {severity}")
    if not isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if review_status not in VALID_REVIEW_STATUSES:
        raise ValueError(f"invalid review_status: {review_status}")
    if review_position not in VALID_REVIEW_POSITIONS:
        raise ValueError(f"invalid review_position: {review_position}")
    if not risk_focus:
        raise ValueError("risk_focus is required")
    if (
        not isinstance(matched_rule_ids, list)
        or not matched_rule_ids
        or not all(isinstance(item, str) and item.strip() for item in matched_rule_ids)
    ):
        raise ValueError("matched_rule_ids must contain non-empty strings")

    risk_reason = _required_string(payload, "risk_reason")
    clause_id = _required_string(payload, "clause_id")
    evidence_text = payload["evidence_text"]
    if not isinstance(evidence_text, str):
        raise ValueError("evidence_text must be a string")
    revision_suggestion = _required_string(payload, "revision_suggestion")
    risk_id_value = payload.get("risk_id")
    if risk_id_value is not None and (
        not isinstance(risk_id_value, str) or not risk_id_value.strip()
    ):
        raise ValueError("risk_id must be a non-empty string when provided")

    return RiskFinding(
        risk_id=risk_id_value.strip() if risk_id_value is not None else _risk_id(payload),
        risk_type=risk_type,
        severity=severity,
        confidence=confidence,
        risk_reason=risk_reason,
        clause_id=clause_id,
        evidence_text=evidence_text,
        matched_rule_ids=[item.strip() for item in matched_rule_ids],
        review_position=review_position,
        risk_focus=risk_focus,
        revision_suggestion=revision_suggestion,
        review_status=review_status,
    )


def _required_string(payload: dict, field_name: str) -> str:
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _risk_id(payload: dict) -> str:
    return f"RISK-{payload.get('clause_id', 'UNKNOWN')}-{payload.get('matched_rule_ids', ['RULE'])[0]}"
