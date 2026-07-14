from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ToolContract:
    name: str
    input_schema: dict
    output_schema: dict
    calls_llm: bool
    description: str

    def to_dict(self) -> dict:
        return asdict(self)

