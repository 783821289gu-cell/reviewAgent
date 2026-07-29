"""Planner 的动作、触发原因和最终决定模型。"""

from dataclasses import asdict, dataclass
from enum import StrEnum


class PlannerAction(StrEnum):
    """Planner 可以建议的全部动作白名单。"""

    RETRIEVE_AGAIN = "RETRIEVE_AGAIN"
    ANALYZE_AGAIN = "ANALYZE_AGAIN"
    REQUEST_HUMAN_REVIEW = "REQUEST_HUMAN_REVIEW"
    TERMINATE = "TERMINATE"


class PlannerReasonCode(StrEnum):
    """允许触发 Planner 的异常原因白名单。"""

    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    RETRIEVAL_INSUFFICIENT = "RETRIEVAL_INSUFFICIENT"
    ANALYZER_VERIFIER_CONFLICT = "ANALYZER_VERIFIER_CONFLICT"
    STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"


@dataclass(frozen=True)
class PlannerDecision:
    """通过 PlannerService 权限校验后的不可变决定。"""

    action: PlannerAction
    reason_code: PlannerReasonCode
    # 目标条款和检索调整都已在 PlannerService 中通过白名单校验。
    target_clause_id: str
    query_adjustments: dict
    confidence: float

    def to_dict(self) -> dict:
        """转换为 LangGraph 节点可以读取的字符串字典。"""

        payload = asdict(self)
        # dataclass 中保留枚举便于代码判断；传给 LangGraph 时转换为普通字符串。
        payload["action"] = self.action.value
        payload["reason_code"] = self.reason_code.value
        return payload
