from dataclasses import asdict, dataclass


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
