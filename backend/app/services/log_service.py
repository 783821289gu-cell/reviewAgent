import json
import re
from time import perf_counter
from uuid import uuid4

from models.log import StepLog
from models.review import trace_id_for_task
from providers.embedding_provider import (
    EMBEDDING_CALL_RECORDS_INPUT_KEY,
    EmbeddingCallMetadata,
)
from providers.llm_provider import LLMCallMetadata, LLM_CALL_RECORDS_INPUT_KEY
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
    trace_id = trace_id_for_task(task_id)
    step_id = f"step_{uuid4().hex}"
    start = perf_counter()
    llm_calls: list[LLMCallMetadata] = []
    embedding_calls: list[EmbeddingCallMetadata] = []
    runtime_tool_input = tool_input
    contract = tool_contracts.get(tool_name)
    if (contract is not None and contract.calls_llm) or tool_name == "retrieve_related_clauses":
        runtime_tool_input = dict(tool_input)
    if contract is not None and contract.calls_llm:
        runtime_tool_input[LLM_CALL_RECORDS_INPUT_KEY] = llm_calls
    if tool_name == "retrieve_related_clauses":
        runtime_tool_input[EMBEDDING_CALL_RECORDS_INPUT_KEY] = embedding_calls
    try:
        output = tool_registry[tool_name](runtime_tool_input)
    except Exception as exc:
        logs.append(
            StepLog(
                task_id=task_id,
                trace_id=trace_id,
                step_id=step_id,
                step_name=resolved_step_name,
                tool_name=tool_name,
                status="failed",
                latency_ms=_elapsed_ms(start),
                input_summary=_summarize_input(tool_input),
                output_summary="",
                token_cost_summary=_token_cost_summary(tool_name, llm_calls, embedding_calls),
                error_message=_safe_error_message(exc, tool_input),
            )
        )
        raise

    logs.append(
        StepLog(
            task_id=task_id,
            trace_id=trace_id,
            step_id=step_id,
            step_name=resolved_step_name,
            tool_name=tool_name,
            status="success",
            latency_ms=_elapsed_ms(start),
            input_summary=_summarize_input(tool_input),
            output_summary=_summarize_output(output),
            token_cost_summary=_token_cost_summary(tool_name, llm_calls, embedding_calls),
            error_message="",
        )
    )
    return output


def _elapsed_ms(start: float) -> int:
    return max(0, int((perf_counter() - start) * 1000))


def _summarize_input(tool_input: dict) -> str:
    parts = []
    for key, value in tool_input.items():
        normalized_key = key.lower()
        if key in {"db_path", "idempotency_key", "embedding_cache"}:
            continue
        if _is_sensitive_key(normalized_key):
            parts.append(f"{key}=[redacted]")
            continue
        if key == "content":
            parts.append(f"content={len(value)} bytes")
        elif key == "document":
            document_id = (
                value.get("contract_id", "unknown")
                if isinstance(value, dict)
                else getattr(value, "contract_id", "unknown")
            )
            parts.append(f"document={document_id}")
        elif key == "clause":
            clause_id = (
                value.get("clause_id", "unknown")
                if isinstance(value, dict)
                else getattr(value, "clause_id", "unknown")
            )
            parts.append(f"clause={clause_id}")
        elif key == "review_context" and isinstance(value, dict):
            rule = value.get("matched_rule") or {}
            clause = value.get("current_clause") or {}
            parts.append(
                "review_context="
                f"{value.get('context_id', 'unknown')},"
                f"clause={clause.get('clause_id', 'unknown')},"
                f"rule={rule.get('rule_id', 'unknown')}"
            )
        elif key == "finding" and isinstance(value, dict):
            parts.append(
                "finding="
                f"{value.get('risk_id', 'unknown')},"
                f"clause={value.get('clause_id', 'unknown')}"
            )
        elif key == "current_clause" and isinstance(value, dict):
            parts.append(f"current_clause={value.get('clause_id', 'unknown')}")
        elif key == "clauses" and isinstance(value, list):
            parts.append(f"clauses={len(value)}")
        elif key == "task" and isinstance(value, dict):
            parts.append(
                f"task={value.get('task_id', 'unknown')},status={value.get('status', 'unknown')}"
            )
        elif key == "human_feedback" and isinstance(value, dict):
            parts.append(
                "human_feedback="
                f"risk={value.get('source_finding_id', 'unknown')},"
                f"action={value.get('user_action', 'unknown')}"
            )
        elif isinstance(value, dict):
            parts.append(f"{key}=object(keys={','.join(str(item) for item in value)})")
        elif isinstance(value, (list, tuple, set)):
            parts.append(f"{key}=items({len(value)})")
        elif isinstance(value, bytes):
            parts.append(f"{key}={len(value)} bytes")
        elif isinstance(value, str) and key not in _SAFE_SCALAR_KEYS:
            parts.append(f"{key}=text({len(value)} chars)")
        else:
            parts.append(f"{key}={_redact_secrets(str(value))}")
    return ", ".join(parts)


_SAFE_SCALAR_KEYS = {
    "action",
    "clause_id",
    "contract_type",
    "file_name",
    "file_type",
    "review_position",
    "risk_id",
    "task_id",
}
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "prompt",
    "secret",
)


def _is_sensitive_key(key: str) -> bool:
    return any(part in key for part in _SENSITIVE_KEY_PARTS)


def _safe_error_message(exc: Exception, tool_input: dict | None = None) -> str:
    redacted_message = _redact_secrets(str(exc)).strip()
    message = redacted_message.splitlines()[0] if redacted_message else ""
    for sensitive_value in _sensitive_input_strings(tool_input or {}):
        message = message.replace(sensitive_value, "[redacted]")
    if not message:
        return exc.__class__.__name__
    return message[:300]


def _sensitive_input_strings(value, key: str = ""):
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            yield from _sensitive_input_strings(child_value, str(child_key))
        return
    if isinstance(value, (list, tuple, set)):
        for child_value in value:
            yield from _sensitive_input_strings(child_value, key)
        return
    if value and isinstance(value, str) and (
        _is_sensitive_key(key.lower()) or len(value) >= 80
    ):
        yield value


def _redact_secrets(value: str) -> str:
    redacted = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+",
        r"\1[redacted]",
        value,
    )
    redacted = re.sub(
        r"(?i)((?:api[_ -]?key|password|secret)\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]",
        redacted,
    )
    redacted = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", redacted)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", redacted)


def _summarize_output(output) -> str:
    if isinstance(output, list):
        if output and isinstance(output[0], dict) and "rerank_score" in output[0]:
            query_context = output[0].get("query_context") or {}
            sources = "+".join(str(item) for item in output[0].get("retrieval_sources") or [])
            return (
                f"items={len(output)}, top={output[0].get('clause_id')}, "
                f"rerank_score={output[0].get('rerank_score')}, sources={sources}, "
                f"effective_top_k={query_context.get('effective_top_k', len(output))}"
            )
        if output and isinstance(output[0], dict) and "memory_id" in output[0]:
            return f"items={len(output)}, top_memory={output[0].get('memory_id')}"
        return f"items={len(output)}"
    if isinstance(output, dict):
        return f"keys={','.join(output.keys())}"
    contract_id = getattr(output, "contract_id", "")
    if contract_id:
        return f"contract_id={contract_id}"
    return output.__class__.__name__


def _token_cost_summary(
    tool_name: str,
    llm_calls: list[LLMCallMetadata] | None = None,
    embedding_calls: list[EmbeddingCallMetadata] | None = None,
) -> str:
    if tool_name == "retrieve_related_clauses":
        if embedding_calls:
            if all(
                call.mode == "local_sparse" and not call.error_type
                for call in embedding_calls
            ):
                return "local_sparse_no_external_embedding"
            return json.dumps(
                {
                    "mode": embedding_calls[-1].mode,
                    "embedding_calls": [call.to_dict() for call in embedding_calls],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return "embedding_not_invoked"
    contract = tool_contracts.get(tool_name)
    if contract is None:
        return "not_applicable"
    if not contract.calls_llm:
        return "not_applicable"
    if llm_calls:
        if all(call.mode == "local_structured" and not call.error_type for call in llm_calls):
            return runtime_llm_mode(tool_name)
        return json.dumps(
            {
                "mode": llm_calls[-1].mode,
                "calls": [call.to_dict() for call in llm_calls],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if not runtime_calls_llm(tool_name):
        return runtime_llm_mode(tool_name)
    return "openai_compatible_not_invoked"
