# API Contract Baseline

`current_http_server.json` records the observable HTTP contract before the planned FastAPI migration.

The baseline was captured from a real in-process `ThreadingHTTPServer` run on 2026-07-14. Dynamic identifiers, timestamps, generated paths and evaluation IDs are represented by required fields or stable summaries instead of fixed values.

The snapshot covers:

1. Health, statuses and all 11 tool contracts.
2. Task creation and terminal task query.
3. SSE event names, content type and status sequence.
4. Local review, feedback, report and evaluation responses.
5. Missing task, missing route, malformed JSON, invalid upload and unfinished report errors.

The SSE baseline client stops reading after the terminal event. The current server advertises keep-alive without a content length, so waiting for connection EOF can time out. This is recorded as current behavior, not fixed as part of task 1.
