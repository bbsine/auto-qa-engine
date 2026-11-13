# CLAUDE.md — auto-qa-engine

## What this is
FastAPI agent (`spec_version 1.1`) that turns an OpenAPI v3.x spec (JSON/YAML string) into dependency-ordered Pytest
integration tests, mock/boundary/security payloads, and coverage reports. Design: "Planner-First, AI-Second" —
parsing and ODG ordering are deterministic; the LLM (`model="fast"` via `LLM_BASE_URL`) only enriches mock data and
does one-shot syntax repair.

## Architecture (src/)
- `main.py`      FastAPI app: `GET /health`, `POST /v1/invoke` (HMAC check -> JSON parse -> security gate -> operation dispatch)
- `security.py`  HMAC-SHA256 + 300s replay window, SSRF URL check, PAN/CVV (Luhn) filter
- `parser.py`    OpenAPI -> IR (`ParsedOpenAPISpec`, `OperationSpec`), local `$ref` resolution, allOf merge
- `planner.py`   Operation Dependency Graph + Kahn topological order (POST -> GET -> PUT/PATCH -> DELETE)
- `generator.py` Pipelines for `generate_tests`, `generate_mocks`, `analyze_coverage`; deterministic mocks + optional LLM
- `validator.py` `ast.parse` sieve, forbidden-call check, deterministic Pytest template builder
- `agent.yaml` is the contract: operation input/output schemas, runtime limits, demo samples.

## Commands
```bash
python -m pip install -r requirements.txt
export PLATFORM_SIGNING_SECRET=test-secret LLM_BASE_URL="" LLM_API_KEY=test
uvicorn src.main:app --host 0.0.0.0 --port 8080
python -m pytest -v                 # all unit, contract, regression & E2E tests
python evals/run_evals.py           # 38 scenarios in evals/cases.yaml
docker build -t auto-qa-engine:v-vize . && docker run --rm -p 8080:8080 \
  -e PLATFORM_SIGNING_SECRET=test-secret -e LLM_BASE_URL="" -e LLM_API_KEY=test auto-qa-engine:v-vize
```
Note: leaving `LLM_BASE_URL` (or `LLM_API_KEY`) unset/empty disables the LLM client (deterministic mode).

## Conventions & hard rules (see contract_rules.md)
- Never put personal API keys, `.env` files, or `sk_` placeholders in the repo or `integrations/`.
- All LLM calls go through `LLM_BASE_URL`/`LLM_API_KEY`, model alias `fast` only.
- Stateless: no disk writes, no module-level tenant state; log only `tenant_id`/`request_id`, never payloads.
- Never fetch URLs from specs/config; `base_url` is only interpolated into generated test code.
- Spec content is untrusted input: anything derived from it that is emitted into generated code must be escaped.
- Errors: `422 {"error": {"code", "message"}}`; `401` for auth/replay failures; never an unhandled 500.
- Python 3.12, `python:3.12-slim`, `USER 1000`, type hints + `from __future__ import annotations`.

## Independent tests
`python -m pytest independent_tests -v` runs the black-box suite (contract, fuzz, SSRF, PII red line, code injection, coverage, end-to-end against real servers, fake LLM). See independent_tests/README.md for scope and known limitations.
