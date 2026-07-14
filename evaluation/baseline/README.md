# Task 1 Baseline

This directory contains the committed, normalized verification baseline captured before the FastAPI migration.

Captured on 2026-07-14:

1. `unit_tests.md` records the full unit-test command and result.
2. `flow_evaluation.json` records the 10 synthetic NDA flow-evaluation results.
3. API request and response contracts are stored under `backend/tests/fixtures/api_contracts/`.

Runtime reports, SQLite files, generated evaluation summaries and temporary directories are intentionally not stored here. The flow evaluation only proves that the current pipeline runs end to end; it does not establish production accuracy.
