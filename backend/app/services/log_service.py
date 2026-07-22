from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
from threading import Thread
from time import perf_counter
from typing import Callable
from uuid import uuid4

from config import settings
from models.log import StepLog
from models.review import (
    NodeExecutionTimeoutError,
    TaskCancelledError,
    TaskExecutionTimeoutError,
    trace_id_for_task,
)
from providers.embedding_provider import (
    EMBEDDING_CALL_RECORDS_INPUT_KEY,
    EmbeddingCallMetadata,
)
from providers.llm_provider import LLMCallMetadata, LLM_CALL_RECORDS_INPUT_KEY
from providers.llm_provider import effective_llm_mode
from tools.contracts import runtime_calls_llm, runtime_llm_mode, tool_contracts


IDEMPOTENT_DECISION_TOOLS = {
    "plan_review_action",
    "retrieve_related_clauses",
    "criticize_risk",
}
_EXECUTION_CONTROL: ContextVar["ToolExecutionControl | None"] = ContextVar(
    "review_agent_execution_control",
    default=None,
)


@dataclass(frozen=True)
class ToolExecutionControl:
    cancel_check: Callable[[], None]
    task_started_at: float
    task_timeout_seconds: float
    node_timeout_seconds: float
    execution_retry_index: int = 0

    def before_step(self, step_name: str) -> None:
        self.cancel_check()
        if perf_counter() - self.task_started_at > self.task_timeout_seconds:
            raise TaskExecutionTimeoutError(
                f"task timeout exceeded before node {step_name}: "
                f"{self.task_timeout_seconds:.3f}s"
            )

    def after_step(self, step_name: str, node_started_at: float) -> None:
        self.cancel_check()
        task_elapsed = perf_counter() - self.task_started_at
        if task_elapsed > self.task_timeout_seconds:
            raise TaskExecutionTimeoutError(
                f"task timeout exceeded after node {step_name}: "
                f"{self.task_timeout_seconds:.3f}s"
            )
        node_elapsed = perf_counter() - node_started_at
        if node_elapsed > self.node_timeout_seconds:
            raise NodeExecutionTimeoutError(
                step_name,
                node_elapsed,
                self.node_timeout_seconds,
            )

    def invoke(self, step_name: str, node_started_at: float, operation: Callable):
        result_queue: Queue = Queue(maxsize=1)

        def execute() -> None:
            try:
                result_queue.put((True, operation()))
            except Exception as exc:
                result_queue.put((False, exc))

        Thread(
            target=execute,
            name=f"review-tool-{step_name}",
            daemon=True,
        ).start()
        while True:
            try:
                succeeded, result = result_queue.get(timeout=0.01)
            except Empty:
                self.after_step(step_name, node_started_at)
                continue
            if succeeded:
                return result
            raise result


def bind_execution_control(control: ToolExecutionControl) -> Token:
    return _EXECUTION_CONTROL.set(control)


def reset_execution_control(token: Token) -> None:
    _EXECUTION_CONTROL.reset(token)


def invoke_tool(
    task_id: str,
    tool_registry: dict,
    tool_name: str,
    tool_input: dict,
    logs: list[StepLog],
    step_name: str | None = None,
    execution_control: ToolExecutionControl | None = None,
):
    if tool_name not in tool_registry:
        raise ValueError(f"未注册工具：{tool_name}")

    execution_control = execution_control or _EXECUTION_CONTROL.get()
    resolved_step_name = step_name or tool_name
    trace_id = trace_id_for_task(task_id)
    step_id = f"step_{uuid4().hex}"
    parent_step_id = logs[-1].step_id if logs else ""
    retry_index = _retry_index(tool_input, execution_control)
    idempotency_key = _decision_idempotency_key(
        task_id,
        tool_name,
        resolved_step_name,
        tool_input,
        retry_index,
    )
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
        if execution_control is not None:
            execution_control.before_step(resolved_step_name)
        operation = lambda: tool_registry[tool_name](runtime_tool_input)
        output = (
            execution_control.invoke(resolved_step_name, start, operation)
            if execution_control is not None
            else operation()
        )
        if execution_control is not None:
            execution_control.after_step(resolved_step_name, start)
    except Exception as exc:
        status = _exception_status(exc)
        token_summary = _token_cost_summary(tool_name, llm_calls, embedding_calls)
        logs.append(
            StepLog(
                task_id=task_id,
                trace_id=trace_id,
                step_id=step_id,
                step_name=resolved_step_name,
                tool_name=tool_name,
                status=status,
                latency_ms=_elapsed_ms(start),
                input_summary=_summarize_input(tool_input),
                output_summary="",
                token_cost_summary=token_summary,
                error_message=_safe_error_message(exc, tool_input),
                parent_step_id=parent_step_id,
                retry_index=retry_index,
                idempotency_key=idempotency_key,
                trace_summary=_trace_summary(
                    tool_name,
                    tool_input,
                    None,
                    token_summary,
                    status,
                ),
            )
        )
        raise

    token_summary = _token_cost_summary(tool_name, llm_calls, embedding_calls)
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
            token_cost_summary=token_summary,
            error_message="",
            parent_step_id=parent_step_id,
            retry_index=retry_index,
            idempotency_key=idempotency_key,
            trace_summary=_trace_summary(
                tool_name,
                tool_input,
                output,
                token_summary,
                "success",
            ),
        )
    )
    return output


def _elapsed_ms(start: float) -> int:
    return max(0, int((perf_counter() - start) * 1000))


def _retry_index(
    tool_input: dict,
    execution_control: ToolExecutionControl | None,
) -> int:
    execution_retry = (
        execution_control.execution_retry_index
        if execution_control is not None
        else 0
    )
    explicit_retry = tool_input.get("retry_count")
    if isinstance(explicit_retry, int) and not isinstance(explicit_retry, bool):
        return max(0, execution_retry, explicit_retry)
    return max(0, execution_retry)


def _exception_status(exc: Exception) -> str:
    if isinstance(exc, TaskCancelledError):
        return "cancelled"
    if isinstance(exc, (NodeExecutionTimeoutError, TaskExecutionTimeoutError)):
        return "timeout"
    return "failed"


def _decision_idempotency_key(
    task_id: str,
    tool_name: str,
    step_name: str,
    tool_input: dict,
    retry_index: int,
) -> str:
    if tool_name not in IDEMPOTENT_DECISION_TOOLS:
        return ""
    finding = tool_input.get("finding") if isinstance(tool_input.get("finding"), dict) else {}
    current_clause = (
        tool_input.get("current_clause")
        if isinstance(tool_input.get("current_clause"), dict)
        else {}
    )
    matched_rule = (
        tool_input.get("matched_rule")
        if isinstance(tool_input.get("matched_rule"), dict)
        else {}
    )
    identity = {
        "task_id": task_id,
        "tool_name": tool_name,
        "step_name": step_name,
        "retry_index": retry_index,
        "trigger_reason": str(tool_input.get("trigger_reason", "")),
        "current_status": str(tool_input.get("current_status", "")),
        "target_clause_id": str(tool_input.get("target_clause_id", "")),
        "failure_reason_fingerprint": _identity_digest(
            str(tool_input.get("failure_reason", ""))
        ),
        "contract_clause_ids": sorted(
            str(clause_id) for clause_id in (tool_input.get("contract_clause_ids") or [])
        ),
        "planner_retry_count": tool_input.get("retry_count", 0),
        "current_clause_id": str(current_clause.get("clause_id", "")),
        "finding_id": str(finding.get("risk_id", "")),
        "finding_clause_id": str(finding.get("clause_id", "")),
        "finding_fingerprint": _identity_digest(
            {
                "risk_type": finding.get("risk_type"),
                "risk_reason": finding.get("risk_reason"),
                "evidence_text": finding.get("evidence_text"),
                "confidence": finding.get("confidence"),
                "matched_rule_ids": finding.get("matched_rule_ids"),
            }
        ) if finding else "",
        "rule_id": str(matched_rule.get("rule_id", "")),
        "risk_type": str(tool_input.get("risk_type", matched_rule.get("risk_type", ""))),
        "playbook_check_point": str(
            tool_input.get("playbook_check_point", matched_rule.get("check_point", ""))
        ),
        "query_adjustments": _canonical_identity_value(
            tool_input.get("query_adjustments") or {}
        ),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    return f"decision:{tool_name}:{task_id}:{digest}"


def _canonical_identity_value(value):
    if isinstance(value, dict):
        return {
            str(key): _canonical_identity_value(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_identity_value(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _identity_digest(value) -> str:
    canonical = json.dumps(
        _canonical_identity_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _trace_summary(
    tool_name: str,
    tool_input: dict,
    output,
    token_summary: str,
    status: str,
) -> dict:
    summary = {
        "input_sources": sorted(
            str(key)
            for key in tool_input
            if key not in {LLM_CALL_RECORDS_INPUT_KEY, EMBEDDING_CALL_RECORDS_INPUT_KEY}
        ),
        "versions": _runtime_versions(tool_input),
        "provider": _provider_trace(token_summary),
        "decision": _decision_trace(tool_name, output),
        "token_allocation": _token_allocation(tool_input),
        "final_step_status": status,
    }
    return {key: value for key, value in summary.items() if value not in ({}, [], "", None)}


def _runtime_versions(tool_input: dict) -> dict:
    review_context = (
        tool_input.get("review_context")
        if isinstance(tool_input.get("review_context"), dict)
        else {}
    )
    matched_rule = review_context.get("matched_rule") or tool_input.get("matched_rule") or {}
    llm_mode = effective_llm_mode()
    return {
        "prompt": str(review_context.get("prompt_version") or "not_applicable"),
        "llm_mode": llm_mode,
        "llm_model": (
            settings.llm_model or "unconfigured"
            if llm_mode == "openai_compatible"
            else "no_external_llm"
        ),
        "embedding_mode": settings.embedding_mode,
        "embedding_model": settings.embedding_model or "unconfigured",
        "playbook": _playbook_version(),
        "playbook_rule": str(matched_rule.get("rule_id", "")),
        "annotations": _annotation_version(),
        "code": _code_version(),
    }


def _token_allocation(tool_input: dict) -> dict:
    review_context = tool_input.get("review_context")
    if not isinstance(review_context, dict):
        return {}
    token_budget = review_context.get("token_budget")
    if not isinstance(token_budget, dict):
        return {}
    return {
        "category_tokens": dict(token_budget.get("category_tokens") or {}),
        "final_prompt_tokens": token_budget.get("final_prompt_tokens"),
        "max_prompt_tokens": token_budget.get("max_prompt_tokens"),
        "reduction_trace": list(token_budget.get("reduction_trace") or []),
    }


def _decision_trace(tool_name: str, output) -> dict:
    if not isinstance(output, (dict, list)):
        return {}
    if tool_name == "plan_review_action" and isinstance(output, dict):
        return {
            "type": "planner",
            "action": output.get("action"),
            "reason_code": output.get("reason_code"),
            "target_clause_id": output.get("target_clause_id"),
            "query_adjustment_fields": sorted(
                str(key) for key in (output.get("query_adjustments") or {})
            ),
        }
    if tool_name == "retrieve_related_clauses" and isinstance(output, list):
        query_context = (
            output[0].get("query_context")
            if output and isinstance(output[0], dict)
            else {}
        ) or {}
        return {
            "type": "retrieval",
            "candidate_count": len(output),
            "query": {
                "current_clause_id": query_context.get("current_clause_id"),
                "current_clause_type": query_context.get("current_clause_type"),
                "risk_type": query_context.get("risk_type"),
                "embedding_query_components": list(
                    query_context.get("embedding_query_components") or []
                ),
                "query_adjustment_fields": sorted(
                    str(key) for key in (query_context.get("query_adjustments") or {})
                ),
            },
            "top_k": {
                "requested": query_context.get("requested_top_k"),
                "effective": query_context.get("effective_top_k"),
                "maximum": query_context.get("max_top_k"),
                "score_cutoff": query_context.get("score_cutoff"),
            },
            "candidates": [
                {
                    "clause_id": str(item.get("clause_id", "")),
                    "vector_rank": item.get("vector_rank"),
                    "keyword_rank": item.get("keyword_rank"),
                    "rerank_score": item.get("rerank_score"),
                    "rerank_factors": _rerank_factor_summary(
                        item.get("rerank_factors") or {}
                    ),
                }
                for item in output[:5]
                if isinstance(item, dict)
            ],
        }
    if tool_name == "criticize_risk" and isinstance(output, dict):
        return {
            "type": "critic",
            "decision": output.get("decision"),
            "reason_code": output.get("reason_code"),
        }
    if tool_name == "verify_evidence" and isinstance(output, dict):
        return {
            "type": "evidence_verifier",
            "is_valid": bool(output.get("is_valid")),
            "failure_reason": str(output.get("failure_reason", "")),
        }
    return {}


def _rerank_factor_summary(factors: dict) -> dict:
    summary = {
        str(key): value
        for key, value in factors.items()
        if isinstance(value, (int, float, bool))
    }
    for key, value in factors.items():
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    return summary


def _provider_trace(token_summary: str) -> dict:
    if not token_summary.startswith("{"):
        return {"summary": token_summary}
    try:
        payload = json.loads(token_summary)
    except json.JSONDecodeError:
        return {"summary": "invalid_provider_summary"}
    calls = payload.get("calls") or payload.get("embedding_calls") or []
    return {
        "mode": payload.get("mode"),
        "calls": [
            {
                "provider_request_id": call.get("provider_request_id"),
                "model": call.get("model"),
                "prompt_tokens": call.get("prompt_tokens"),
                "completion_tokens": call.get("completion_tokens"),
                "latency_ms": call.get("latency_ms"),
                "estimated_cost": call.get("estimated_cost"),
                "cost_status": call.get("cost_status"),
                "error_type": call.get("error_type"),
            }
            for call in calls
            if isinstance(call, dict)
        ],
    }


@lru_cache(maxsize=1)
def _code_version() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            capture_output=True,
            check=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=Path(__file__).resolve().parents[3],
                capture_output=True,
                check=True,
                text=True,
                timeout=3,
            ).stdout.strip()
        )
        return f"{commit or 'unavailable'}{'-dirty' if dirty else ''}"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


@lru_cache(maxsize=1)
def _playbook_version() -> str:
    path = Path(__file__).resolve().parents[1] / "playbooks" / "nda.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("playbook_version") or "unknown")
    except (OSError, json.JSONDecodeError):
        return "unavailable"


@lru_cache(maxsize=1)
def _annotation_version() -> str:
    path = Path(__file__).resolve().parents[3] / "samples" / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str((payload.get("effect_evaluation") or {}).get("annotation_version") or "unknown")
    except (OSError, json.JSONDecodeError):
        return "unavailable"


def _summarize_input(tool_input: dict) -> str:
    if {
        "trigger_reason",
        "current_status",
        "target_clause_id",
        "retry_count",
    }.issubset(tool_input):
        return (
            f"trigger={tool_input.get('trigger_reason')}, "
            f"status={tool_input.get('current_status')}, "
            f"target_clause={tool_input.get('target_clause_id')}, "
            f"retry_count={tool_input.get('retry_count')}"
        )
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
        if "action" in output and "reason_code" in output:
            adjustments = output.get("query_adjustments") or {}
            return (
                f"action={output.get('action')}, reason={output.get('reason_code')}, "
                f"target_clause={output.get('target_clause_id')}, "
                f"adjustments={','.join(sorted(str(item) for item in adjustments)) or 'none'}"
            )
        if "decision" in output and "reason_code" in output:
            return (
                f"decision={output.get('decision')}, "
                f"reason={output.get('reason_code')}"
            )
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
