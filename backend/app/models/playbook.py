from dataclasses import asdict, dataclass


SUPPORTED_REVIEW_POSITIONS = {"甲方", "乙方"}
VALID_POSITION_SEVERITIES = {"高", "中", "低"}


@dataclass(frozen=True)
class PlaybookPosition:
    severity_default: str
    risk_focus: str
    revision_template: str

    @classmethod
    def from_dict(cls, payload: dict) -> "PlaybookPosition":
        if not isinstance(payload, dict):
            raise ValueError("Playbook position config must be a dict")

        severity_default = str(payload.get("severity_default", "")).strip()
        risk_focus = str(payload.get("risk_focus", "")).strip()
        revision_template = str(payload.get("revision_template", "")).strip()
        if severity_default not in VALID_POSITION_SEVERITIES:
            raise ValueError(f"invalid position severity_default: {severity_default}")
        if not risk_focus:
            raise ValueError("Playbook position risk_focus is required")
        if not revision_template:
            raise ValueError("Playbook position revision_template is required")

        return cls(
            severity_default=severity_default,
            risk_focus=risk_focus,
            revision_template=revision_template,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PlaybookRule:
    rule_id: str
    contract_type: str
    clause_type: str
    risk_type: str
    check_point: str
    risk_criteria: str
    positions: dict[str, PlaybookPosition]
    key_field_triggers: list[str]

    @classmethod
    def from_dict(cls, payload: dict) -> "PlaybookRule":
        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, dict) or not raw_positions:
            raise ValueError(f"Playbook rule {payload.get('rule_id', '')} positions are required")

        return cls(
            rule_id=str(payload["rule_id"]),
            contract_type=str(payload["contract_type"]),
            clause_type=str(payload["clause_type"]),
            risk_type=str(payload["risk_type"]),
            check_point=str(payload["check_point"]),
            risk_criteria=str(payload["risk_criteria"]),
            positions={
                str(position): PlaybookPosition.from_dict(config)
                for position, config in raw_positions.items()
            },
            key_field_triggers=list(payload.get("key_field_triggers", [])),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def matches(self, contract_type: str, clause_type: str) -> bool:
        return self.contract_type == contract_type and self.clause_type == clause_type

    def resolve_position(self, review_position: str) -> PlaybookPosition:
        position = self.positions.get(review_position)
        if position is None:
            raise ValueError(
                f"Playbook rule {self.rule_id} missing position config: {review_position}"
            )
        return position

    def to_resolved_dict(self, review_position: str) -> dict:
        position = self.resolve_position(review_position)
        payload = self.to_dict()
        payload["review_position"] = review_position
        payload["position_config"] = position.to_dict()
        payload.update(position.to_dict())
        return payload
