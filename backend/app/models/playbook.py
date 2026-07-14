from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PlaybookRule:
    rule_id: str
    contract_type: str
    clause_type: str
    review_position: list[str]
    risk_type: str
    check_point: str
    risk_criteria: str
    severity_default: str
    revision_template: str
    key_field_triggers: list[str]

    @classmethod
    def from_dict(cls, payload: dict) -> "PlaybookRule":
        return cls(
            rule_id=str(payload["rule_id"]),
            contract_type=str(payload["contract_type"]),
            clause_type=str(payload["clause_type"]),
            review_position=list(payload["review_position"]),
            risk_type=str(payload["risk_type"]),
            check_point=str(payload["check_point"]),
            risk_criteria=str(payload["risk_criteria"]),
            severity_default=str(payload["severity_default"]),
            revision_template=str(payload["revision_template"]),
            key_field_triggers=list(payload.get("key_field_triggers", [])),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def matches(self, contract_type: str, clause_type: str, review_position: str) -> bool:
        return (
            self.contract_type == contract_type
            and self.clause_type == clause_type
            and review_position in self.review_position
        )
