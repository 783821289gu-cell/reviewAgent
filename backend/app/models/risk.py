"""风险、证据和 Critic 输出的领域模型与最终字段校验。"""

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
    """Critic 允许给出的三种决定。"""

    PASS = "PASS"
    REJECT = "REJECT"
    REQUEST_HUMAN_REVIEW = "REQUEST_HUMAN_REVIEW"


class CriticReasonCode(StrEnum):
    """Critic 允许使用的固定原因码，禁止自由生成解释字段。"""

    SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK = "SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK"
    CLAUSE_MISMATCH = "CLAUSE_MISMATCH"
    EVIDENCE_UNSUPPORTED = "EVIDENCE_UNSUPPORTED"
    PLAYBOOK_MISMATCH = "PLAYBOOK_MISMATCH"
    REASON_SUPPORT_AMBIGUOUS = "REASON_SUPPORT_AMBIGUOUS"
    PROMPT_INJECTION_DETECTED = "PROMPT_INJECTION_DETECTED"


# 决定与原因必须配对。例如 PASS 不能配 EVIDENCE_UNSUPPORTED。
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
    """Analyzer 通过全部校验后的标准风险对象。"""

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
        """返回可写入 Graph State/数据库的普通字典。"""

        return asdict(self)


@dataclass(frozen=True)
class EvidenceResult:
    """Evidence Verifier 对一条风险的确定性校验结果。"""

    risk_id: str
    clause_id: str
    evidence_text: str
    is_valid: bool
    failure_reason: str
    verified_clause_id: str
    source_location: dict

    def to_dict(self) -> dict:
        """返回可序列化的证据结果字典。"""

        return asdict(self)


@dataclass(frozen=True)
class CriticResult:
    """Critic 通过字段与枚举配对校验后的不可变结果。"""

    decision: CriticDecision
    reason_code: CriticReasonCode

    def to_dict(self) -> dict:
        """把枚举转换为输出 Schema 使用的字符串。"""

        return {
            "decision": self.decision.value,
            "reason_code": self.reason_code.value,
        }


def validate_critic_result(payload: dict) -> CriticResult:
    """执行 Critic 返回值的最后一道校验。"""

    # 第 1 步：外形必须刚好是 decision + reason_code，额外解释字段也不接收。
    if not isinstance(payload, dict):
        raise ValueError("critic result must be a dict")
    if set(payload) != {"decision", "reason_code"}:
        raise ValueError("critic result fields do not match the output whitelist")
    # 第 2 步：把两个字符串分别转换为固定枚举。
    try:
        decision = CriticDecision(payload["decision"])
    except (TypeError, ValueError) as exc:
        raise ValueError("critic decision is not allowed") from exc
    try:
        reason_code = CriticReasonCode(payload["reason_code"])
    except (TypeError, ValueError) as exc:
        raise ValueError("critic reason_code is not allowed") from exc
    # 第 3 步：检查组合，例如 PASS 不能搭配 EVIDENCE_UNSUPPORTED。
    if reason_code not in CRITIC_REASONS_BY_DECISION[decision]:
        raise ValueError("critic reason_code is not allowed for decision")
    return CriticResult(decision=decision, reason_code=reason_code)


def validate_risk_finding(payload: dict) -> RiskFinding:
    """执行 Analyzer 风险对象的完整领域校验。"""

    # 第 1 步：先确认对象和所有必填字段都存在。
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

    # 第 2 步：读取字符串、置信度和规则 ID，并完成基础类型检查。
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

    # 第 3 步：校验固定枚举和数值范围。
    # 模型即使返回语义相近的新名称，也不能扩展产品定义。
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

    # 第 4 步：校验剩余文本；evidence_text 允许空字符串表示没有证据。
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

    # 第 5 步：所有检查通过后才从普通 dict 变成不可变领域对象。
    # 没有 risk_id 时，用 clause_id + 首个 rule_id 生成稳定 ID。
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
    """读取并清理必填字符串；非字符串或纯空白立即拒绝。"""

    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _risk_id(payload: dict) -> str:
    """用条款 ID 和首个规则 ID 生成可复现的默认风险 ID。"""

    return f"RISK-{payload.get('clause_id', 'UNKNOWN')}-{payload.get('matched_rule_ids', ['RULE'])[0]}"
