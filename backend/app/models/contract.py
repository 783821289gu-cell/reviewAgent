from dataclasses import asdict, dataclass


CONTRACT_TYPE_DECISIONS = {
    "SUPPORTED",
    "UNSUPPORTED_CONTRACT_TYPE",
    "NEED_MANUAL_REVIEW",
}
CONTRACT_TYPES = {"NDA", "PROCUREMENT", "SERVICE", "EMPLOYMENT", "UNKNOWN"}


@dataclass(frozen=True)
class ContractTypeClassification:
    contract_type: str
    confidence: float
    evidence: list[str]
    decision: str

    def __post_init__(self) -> None:
        if self.contract_type not in CONTRACT_TYPES:
            raise ValueError(f"unsupported contract_type: {self.contract_type}")
        if self.decision not in CONTRACT_TYPE_DECISIONS:
            raise ValueError(f"unsupported contract type decision: {self.decision}")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise ValueError("contract type confidence must be numeric")
        if not 0 <= self.confidence <= 1:
            raise ValueError("contract type confidence must be between 0 and 1")
        if (
            not isinstance(self.evidence, list)
            or not self.evidence
            or not all(isinstance(item, str) and item.strip() for item in self.evidence)
        ):
            raise ValueError("contract type evidence must contain non-empty strings")
        if self.decision == "SUPPORTED" and self.contract_type != "NDA":
            raise ValueError("only NDA can be classified as supported")
        if self.decision == "UNSUPPORTED_CONTRACT_TYPE" and self.contract_type not in {
            "PROCUREMENT",
            "SERVICE",
            "EMPLOYMENT",
        }:
            raise ValueError("unsupported decision requires a known non-NDA contract type")
        if self.decision == "NEED_MANUAL_REVIEW" and self.contract_type != "UNKNOWN":
            raise ValueError("manual review decision requires UNKNOWN contract type")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TextBlock:
    block_id: str
    block_type: str
    text: str
    order: int
    source_location: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ContractDocument:
    contract_id: str
    file_name: str
    file_type: str
    content_hash: str
    paragraphs: list[TextBlock]
    tables: list[TextBlock]
    blocks: list[TextBlock]
    page_map: list[dict]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["paragraphs"] = [paragraph.to_dict() for paragraph in self.paragraphs]
        payload["tables"] = [table.to_dict() for table in self.tables]
        payload["blocks"] = [block.to_dict() for block in self.blocks]
        return payload


@dataclass(frozen=True)
class Clause:
    clause_id: str
    title: str
    text: str
    clause_type: str
    key_fields: dict
    source_location: dict

    def to_dict(self) -> dict:
        return asdict(self)


def contract_document_from_dict(payload: dict) -> ContractDocument:
    if not isinstance(payload, dict):
        raise ValueError("persisted document payload is invalid")
    return ContractDocument(
        contract_id=str(payload["contract_id"]),
        file_name=str(payload["file_name"]),
        file_type=str(payload["file_type"]),
        content_hash=str(payload["content_hash"]),
        paragraphs=[_text_block_from_dict(item) for item in payload.get("paragraphs") or []],
        tables=[_text_block_from_dict(item) for item in payload.get("tables") or []],
        blocks=[_text_block_from_dict(item) for item in payload.get("blocks") or []],
        page_map=list(payload.get("page_map") or []),
    )


def clause_from_dict(payload: dict) -> Clause:
    if not isinstance(payload, dict):
        raise ValueError("persisted clause payload is invalid")
    return Clause(
        clause_id=str(payload["clause_id"]),
        title=str(payload.get("title", "")),
        text=str(payload["text"]),
        clause_type=str(payload["clause_type"]),
        key_fields=dict(payload.get("key_fields") or {}),
        source_location=dict(payload.get("source_location") or {}),
    )


def _text_block_from_dict(payload: dict) -> TextBlock:
    return TextBlock(
        block_id=str(payload["block_id"]),
        block_type=str(payload["block_type"]),
        text=str(payload["text"]),
        order=int(payload["order"]),
        source_location=dict(payload.get("source_location") or {}),
    )
