from time import perf_counter

from models.log import StepLog
from tools.contracts import runtime_calls_llm, runtime_llm_mode, tool_contracts


def invoke_tool(
    task_id: str,
    tool_registry: dict,
    tool_name: str,
    tool_input: dict,
    logs: list[StepLog],
    step_name: str | None = None,
):
    if tool_name not in tool_registry:
        raise ValueError(f"未注册工具：{tool_name}")

    resolved_step_name = step_name or tool_name
    start = perf_counter()
    try:
        output = tool_registry[tool_name](tool_input)
    except Exception as exc:
        logs.append(
            StepLog(
                task_id=task_id,
                step_name=resolved_step_name,
                tool_name=tool_name,
                status="failed",
                latency_ms=_elapsed_ms(start),
                input_summary=_summarize_input(tool_input),
                output_summary="",
                token_cost_summary=_token_cost_summary(tool_name),
                error_message=str(exc),
            )
        )
        raise

    logs.append(
        StepLog(
            task_id=task_id,
            step_name=resolved_step_name,
            tool_name=tool_name,
            status="success",
            latency_ms=_elapsed_ms(start),
            input_summary=_summarize_input(tool_input),
            output_summary=_summarize_output(output),
            token_cost_summary=_token_cost_summary(tool_name),
            error_message="",
        )
    )
    return output


def _elapsed_ms(start: float) -> int:
    return max(0, int((perf_counter() - start) * 1000))


def _summarize_input(tool_input: dict) -> str:
    parts = []
    for key, value in tool_input.items():
        if key == "content":
            parts.append(f"content={len(value)} bytes")
        elif key == "document":
            parts.append(f"document={getattr(value, 'contract_id', 'unknown')}")
        elif key == "clause":
            parts.append(f"clause={getattr(value, 'clause_id', 'unknown')}")
        elif key == "current_clause" and isinstance(value, dict):
            parts.append(f"current_clause={value.get('clause_id', 'unknown')}")
        elif key == "clauses" and isinstance(value, list):
            parts.append(f"clauses={len(value)}")
        else:
            parts.append(f"{key}={value}")
    return ", ".join(parts)


def _summarize_output(output) -> str:
    if isinstance(output, list):
        if output and isinstance(output[0], dict) and "rerank_score" in output[0]:
            return f"items={len(output)}, top={output[0].get('clause_id')}, rerank_score={output[0].get('rerank_score')}"
        if output and isinstance(output[0], dict) and "memory_id" in output[0]:
            return f"items={len(output)}, top_memory={output[0].get('memory_id')}"
        return f"items={len(output)}"
    if isinstance(output, dict):
        return f"keys={','.join(output.keys())}"
    contract_id = getattr(output, "contract_id", "")
    if contract_id:
        return f"contract_id={contract_id}"
    return output.__class__.__name__


def _token_cost_summary(tool_name: str) -> str:
    contract = tool_contracts.get(tool_name)
    if contract is None:
        return "not_applicable"
    if not contract.calls_llm:
        return "not_applicable"
    if not runtime_calls_llm(tool_name):
        return runtime_llm_mode(tool_name)
    return "pending_llm_metering"
