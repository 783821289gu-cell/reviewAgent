from dataclasses import asdict, dataclass


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

    def to_dict(self) -> dict:
        return asdict(self)


def validate_risk_finding(payload: dict) -> RiskFinding:
    required_fields = {
        "risk_type",
        "severity",
        "confidence",
        "risk_reason",
        "clause_id",
        "evidence_text",
        "matched_rule_ids",
        "revision_suggestion",
        "review_status",
    }
    missing_fields = required_fields - set(payload)
    if missing_fields:
        raise ValueError(f"risk finding missing fields: {', '.join(sorted(missing_fields))}")

    risk_type = str(payload["risk_type"])
    severity = str(payload["severity"])
    review_status = str(payload["review_status"])
    confidence = float(payload["confidence"])
    matched_rule_ids = payload["matched_rule_ids"]

    if risk_type not in VALID_RISK_TYPES:
        raise ValueError(f"invalid risk_type: {risk_type}")
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"invalid severity: {severity}")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if review_status not in VALID_REVIEW_STATUSES:
        raise ValueError(f"invalid review_status: {review_status}")
    if not isinstance(matched_rule_ids, list) or not matched_rule_ids:
        raise ValueError("matched_rule_ids must be a non-empty list")

    return RiskFinding(
        risk_id=str(payload.get("risk_id") or _risk_id(payload)),
        risk_type=risk_type,
        severity=severity,
        confidence=confidence,
        risk_reason=str(payload["risk_reason"]),
        clause_id=str(payload["clause_id"]),
        evidence_text=str(payload["evidence_text"]),
        matched_rule_ids=[str(item) for item in matched_rule_ids],
        revision_suggestion=str(payload["revision_suggestion"]),
        review_status=review_status,
    )


def _risk_id(payload: dict) -> str:
    return f"RISK-{payload.get('clause_id', 'UNKNOWN')}-{payload.get('matched_rule_ids', ['RULE'])[0]}"
