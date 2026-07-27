from dataclasses import asdict, is_dataclass
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from models.contract import contract_document_from_dict
from tools.contracts import tool_contracts
from tools.registry import tool_registry


def invoke_validated_tool(tool_name: str, tool_input: dict[str, Any]) -> dict:
    if tool_name not in tool_registry:
        raise ValueError(f"unknown tool: {tool_name}")
    contract = tool_contracts[tool_name]
    try:
        validated_input = contract.input_model.model_validate(tool_input)
    except ValidationError as exc:
        raise ValueError(f"{tool_name} input validation failed: {exc}") from exc

    runtime_input = validated_input.model_dump(mode="python", exclude_none=True)
    if tool_name == "extract_clauses":
        runtime_input["document"] = contract_document_from_dict(
            runtime_input["document"]
        )

    raw_output = tool_registry[tool_name](runtime_input)
    serialized_output = _json_payload(raw_output)
    if contract.output_key:
        serialized_output = {contract.output_key: serialized_output}
    try:
        validated_output = contract.output_model.model_validate(serialized_output)
    except ValidationError as exc:
        raise ValueError(f"{tool_name} output validation failed: {exc}") from exc
    return validated_output.model_dump(mode="json")


def structured_tools() -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            func=_tool_callable(tool_name),
            name=tool_name,
            description=tool_contracts[tool_name].description,
            args_schema=tool_contracts[tool_name].input_model,
            infer_schema=False,
        )
        for tool_name in tool_registry
    ]


def _tool_callable(tool_name: str):
    def call_tool(**kwargs):
        return invoke_validated_tool(tool_name, kwargs)

    call_tool.__name__ = tool_name
    call_tool.__doc__ = tool_contracts[tool_name].description
    return call_tool


def _json_payload(value):
    if hasattr(value, "to_dict"):
        return _json_payload(value.to_dict())
    if is_dataclass(value):
        return _json_payload(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_payload(item) for item in value]
    return value
