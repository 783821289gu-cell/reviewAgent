from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolContract:
    name: str
    input_schema: dict
    output_schema: dict
    calls_llm: bool
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    output_key: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "calls_llm": self.calls_llm,
            "description": self.description,
            "input_json_schema": self.input_model.model_json_schema(),
            "output_json_schema": self.output_model.model_json_schema(),
        }
