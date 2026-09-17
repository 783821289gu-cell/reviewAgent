# Security and demo-data scope

ContractReviewAgent is a local portfolio / research demonstration using synthetic NDA samples.
Publishing this repository does not publish a running service or certify the application for
production contract processing.

- The API has no built-in user authentication, per-user authorization, or tenant isolation.
  Run on `127.0.0.1` for a single trusted user. CORS and unpredictable task IDs are not access
  controls. Do not expose the API, MCP, PostgreSQL, or Redis directly to the Internet.
- An Internet-facing deployment would require authentication, object-level authorization, TLS,
  abuse/rate controls, isolated workers, and an explicit document retention policy. These are
  outside the current implementation and are not claimed as completed features.
- External-model mode sends selected contract/context data to the configured provider. Use the
  supplied synthetic fixtures for public demos, never real client agreements without permission.
- API keys, database credentials, OTLP authorization headers, uploaded documents, runtime logs,
  generated reports, local databases, and model caches belong outside version control. `.env`
  and runtime output locations are ignored. `.env.example` contains placeholders only.
- Samples and browser fixtures are generated project data; their provenance is documented under
  `samples/` and `frontend/tests/fixtures/`. Keep the tokenizer and icon license notices intact.
- Evidence checks, Prompt Injection tests, and trace redaction reduce specific risks but do not
  establish universal injection resistance or legal correctness. Human review remains required.

The publication review checked reachable Git history using Gitleaks and compared tracked files
with local credential values. The one historical Gitleaks suppression is a specific public
Hugging Face tokenizer revision documented in `.gitleaksignore`, not a broad rule exclusion.
This review does not replace dependency vulnerability monitoring or a deployment security audit.

If a real credential is exposed, contact the repository owner privately and rotate it. Do not
post credentials or confidential contract text in public issues, traces, or screenshots.

## Dependency audit: 2026-09-17

`pip-audit` against the installed development runtime reported known advisories in seven
packages. This is a package/version audit, not proof that every vulnerable code path is reached.
The findings are **unresolved** and preclude a claim of production security readiness:

| Installed package | Representative advisory / risk |
| --- | --- |
| `starlette==0.47.3` | `PYSEC-2026-1942`: Range-header denial of service; `PYSEC-2026-2281`: Windows StaticFiles UNC/SMB exposure |
| `transformers==4.57.6` | `PYSEC-2026-2289`: untrusted model configuration code execution |
| `langgraph-checkpoint-postgres==3.1.0` | `PYSEC-2026-3635`: namespace-prefix isolation failure; feed lists 3.1.1 as fixed |
| `python-dotenv==1.1.0` | `PYSEC-2026-2270`: symlink handling when rewriting environment files; feed lists 1.2.2 as fixed |
| `cryptography==49.0.0` | `PYSEC-2026-3552`: distinguishable PKCS7 decryption outcomes |
| `accelerate==1.14.0` | `PYSEC-2026-3804`: untrusted sharded-checkpoint path traversal; no fixed version listed |
| `pip==26.1.2` | `PYSEC-2026-3721`: unsafe encoded package URLs from a malicious index |

The JavaScript development dependency audit (`pnpm audit`) reported no known advisories.
This does not offset the Python findings. Keep the app local, use synthetic inputs and trusted
model files, and do not expose it to untrusted clients. Complete a coordinated dependency upgrade
and API, document parsing, embedding, and persistence regression run before any deployment.
This audit did not silently upgrade the shared local runtime or hide advisories with exclusions.

## Public demo history

Published history uses a neutral project author identity. Personal interview notes were excluded
from all published branches; local runtime paths and original commit-hash references were
anonymized. Synthetic sample metadata does not identify an individual author. Commit hashes have
changed, but development chronology, evaluation metrics, third-party attribution, and risk
disclosures remain intact. Original history is kept only in a private local backup.
