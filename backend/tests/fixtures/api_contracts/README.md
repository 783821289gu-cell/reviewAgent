# API Contract Baseline

`current_http_server.json` records the observable HTTP contract before the planned FastAPI migration.

The baseline was captured from a real in-process `ThreadingHTTPServer` run on 2026-07-14. Dynamic identifiers, timestamps, generated paths and evaluation IDs are represented by placeholders, required fields or stable summaries instead of fixed values.

The snapshot covers:

1. Health, statuses and all 11 tool contracts.
2. Task creation and terminal task query.
3. SSE request, event example, content type and status sequence.
4. Task upload, local review, feedback, report and evaluation request/response examples.
5. Missing task, missing route, malformed JSON, invalid parameter, invalid upload and unfinished report errors.

All POST examples explicitly record their JSON body, multipart fields or empty-body behavior. `response_body_summary` is a normalized subset of the observed response; `required_keys` records the fields that must remain present when the transport is migrated.

The SSE baseline client stops reading after the terminal event. The current server advertises keep-alive without a content length, so waiting for connection EOF can time out. This is recorded as current behavior, not fixed as part of task 1.

The task 2 FastAPI migration keeps this file unchanged as the pre-migration reference. `backend/tests/test_api_fastapi.py` reads it directly and verifies the migrated success fields, status codes, tool contracts, SSE event structure and representative error payloads.

Run the FastAPI contract tests from the repository root:

```powershell
python -m unittest backend.tests.test_api_fastapi
```
