# Agent Evolution Baseline

## 1. Baseline scope

This document freezes the observable Agent baseline before `AGENT_EVOLUTION_PLAN.md` changes runtime behavior. The machine-readable source is `evaluation/baseline/agent-evolution-baseline.json`.

This is a local deterministic baseline. It does not claim production accuracy, and it does not claim that DeepSeek has been called or verified.

## 2. Captured versions

| Item | Captured value |
|---|---|
| Code commit | `historical-commit` |
| Git dirty at capture | `false` |
| Playbook | `nda-v1` |
| Annotation set | `synthetic_nda_v1` / `effect-v1` |
| Prompt version | No explicit version exists; frozen as `unversioned-inline-2026-07-17` |
| LLM | `local_structured` / `no_external_llm` |
| Embedding | `local_sparse` / `local_sparse_hash_v1`, 256 dimensions |

The current external-provider prompt is assembled inline in `backend/app/providers/llm_provider.py` from a system schema instruction and a JSON user payload. Explicit Prompt version management is not implemented in this baseline and remains future work.

## 3. Flow evaluation

The flow evaluation was run against all 10 synthetic NDA samples on 2026-07-17. Result: `10/10` passed.

The machine baseline retains all seven boolean results and the failure reason for every sample: task execution, document parsing, clause structure, Playbook hit, evidence-bound risk, feedback-to-Memory write, and report export. Every recorded check is `true` and every recorded failure reason is empty. This result only proves that the local flow ran through; it does not prove legal or model accuracy.

## 4. Effect evaluation

The effect evaluation was run against 6 annotated contracts and produced 14 metrics.

| Metric | Score | Threshold | Result |
|---|---:|---:|---|
| NDA classification accuracy | 1.0 | 0.9 | Pass |
| Non-NDA rejection rate | 1.0 | 1.0 | Pass |
| Clause boundary accuracy | 1.0 | 0.9 | Pass |
| Clause type accuracy | 1.0 | 0.9 | Pass |
| Playbook Recall@3 | 1.0 | 0.9 | Pass |
| Related clause Recall@1 | **0.3333** | **0.8** | **Fail** |
| Risk precision | 1.0 | 0.8 | Pass |
| Risk recall | 1.0 | 0.8 | Pass |
| Risk F1 | 1.0 | 0.8 | Pass |
| Evidence span hit rate | 1.0 | 0.9 | Pass |
| Manual-review trigger rate | 1.0 | 1.0 | Pass |
| Report filter accuracy | 1.0 | 1.0 | Pass |
| Tool-call success rate | 1.0 | 0.95 | Pass |
| End-to-end success rate | 1.0 | 0.9 | Pass |

The run status is `completed_with_failures`. Related clause Recall@1 passed 1 of 3 cases and failed these two cases:

1. `return-destroy-synonym`: expected `CL-022`, actual Top-1 `CL-023`.
2. `compelled-disclosure-notice-synonym`: expected `CL-032`, actual Top-1 `CL-031`.

The `0.3333` result does not meet the `0.8` threshold and is not reported as passed.

## 5. API and SSE contract

The current FastAPI contract continues to be checked against `backend/tests/fixtures/api_contracts/current_http_server.json`; the filename remains as a historical pre-migration reference.

The baseline records required success fields for health, tasks, local review, feedback, reports, flow evaluation, and effect evaluation. The main HTTP error statuses are `400` for invalid requests and `404` for missing tasks or routes.

SSE remains:

```text
Content-Type: text/event-stream; charset=utf-8
event: review_event
data keys: event_id, task_id, status, message, step_name, tool_name, created_at, task
```

## 6. DeepSeek contract fixtures

`backend/tests/fixtures/deepseek/chat_completions_contracts.json` defines redacted synthetic MockTransport cases for:

1. Successful structured JSON.
2. Empty `content`.
3. Truncated JSON.
4. Schema mismatch.
5. Timeout.
6. Rate limiting.
7. Authentication failure.
8. Temporary service failure.

The request contract uses `https://api.deepseek.com/chat/completions`, `deepseek-v4-pro`, and `response_format={"type":"json_object"}`. It contains no real API Key. These fixtures validate the expected application boundary only and are not evidence of a real DeepSeek response.

The fixture separates two layers instead of presenting the current implementation as the final contract:

1. Current Task 1 behavior: empty content, truncated JSON, and Schema mismatch are treated as retryable, so the application can make two attempts.
2. Task 2 target policy: only timeout, rate limiting, and temporary service errors may be retried once; deterministic Schema errors and authentication failures are non-retryable.

Task 1 only records this gap. It does not change Provider retry behavior.

## 7. Pre-change verification

Commands actually run before adding the baseline files:

```powershell
python -m unittest discover -s backend\tests -p "test_*.py"
pnpm exec playwright test
```

Results:

1. Backend: 134 tests passed in 11.591 seconds.
2. Playwright: 10 tests passed in 24.4 seconds, comprising 8 workbench tests and 2 visual/accessibility tests.

Task 1 does not connect to DeepSeek and does not change the default `local_structured` behavior.
