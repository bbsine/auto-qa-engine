# AutoQA Engine — Implementation Checklist (`tasks.md`)

**Contract Specification:** `spec_version: "1.1"` (November 2026)  
**Target Release Tag:** `v-vize`

---

## Phase 0: Foundation & Architecture Memory
- [x] **`architecture.md`**: Document system architecture, 4-stage hybrid pipeline diagram, Operation Dependency Graph (ODG) design, AST self-healing loop, and `POST /v1/invoke` request/response schemas.
- [x] **`memory.md`**: Document fixed design decisions, runtime constraints (`python:3.12-slim`, `USER 1000`, `512MB` RAM, `PORT=8080`), platform environment variables, and the 100-point midterm rubric.
- [x] **`contract_rules.md`**: Document timing-safe HMAC verification, 300s timestamp window, zero-key ingestion, stateless multi-tenant isolation, SSRF guards, KVKK/payment red lines, and integration file rules.
- [x] **`tasks.md`**: Establish detailed Phase 1–Phase 5 engineering roadmap.

---

## Phase 1: Platform Identity, Packaging & Contract Skeleton
- [x] **`agent.yaml`**:
  - Define `spec_version: "1.1"`, `slug: auto-qa-engine`, `name: AutoQA API Test Üretici` ($\le 30$ chars), `category: test-ve-qa`, `types: [ci, mcp, api]`.
  - Configure `runtime: {port: 8080, memory_mb: 512, cpu: 0.5, models: [fast]}` and `network: {allow: []}`.
  - Define operations (`generate_tests`, `generate_mocks`, `analyze_coverage`) with complete JSON Schema `input_schema` and `output_schema`.
  - Define `config_schema` and rich `demo.config` + `demo.samples` for the Sandbox "Dene" button.
- [x] **`requirements.txt`**:
  - Pin lightweight dependencies (`fastapi`, `uvicorn`, `openai`, `pyyaml`, `pydantic`, `httpx`, `pytest`, `jsonschema`).
- [x] **`Dockerfile` & `.gitignore` & `.dockerignore`**:
  - Configure `FROM python:3.12-slim`, install dependencies with `--no-cache-dir`, copy `src/`, enforce `USER 1000`, `ENV PORT=8080`, `EXPOSE 8080`, and start Uvicorn.
  - Add `.gitignore` excluding `.env`, `.env.*`, `__pycache__/`, `.venv/`, `.pytest_cache/`, IDE configs, and build artifacts.
- [x] **`src/security.py`**:
  - Implement `verify_signature(body, timestamp, signature)` using `hmac.compare_digest` and 300-second window check.
  - Implement `validate_no_ssrf(url_or_spec)` blocking `127.0.0.1`, `localhost`, `169.254.169.254`, RFC 1918 private subnets, and non-HTTP schemes.
  - Implement `sanitize_sensitive_data(payload)` to block/redact credit card numbers (PAN/CVV), Turkish National IDs (TCKN), and prompt injection payloads.

---

## Phase 2: Core Hybrid Engine ("Planner-First, AI-Second")
- [x] **`src/parser.py` (Deterministic OpenAPI 3.x & Swagger 2.0 Parser)**:
  - Parse OpenAPI 3.0.x / 3.1.x and Swagger 2.0 JSON and YAML specifications.
  - Resolve internal `$ref` component schemas (`#/components/schemas/...` and `#/definitions/...`).
  - Extract normalized endpoint IR: path, HTTP method, `operationId`, path/query/header parameters, `request_content_type`, `base_path` (Swagger `basePath` / OpenAPI 3 `servers[0].url` path), request body schema (including Swagger 2.0 `in: body` and `in: formData`), required fields, constraints (`minLength`, `maxLength`, `minimum`, `maximum`, `enum`, `format`), and expected response status codes/schemas.
- [x] **`src/planner.py` (Operation Dependency Graph & Topological Planner)**:
  - Build directed dependency graph $G = (V, E)$ linking producer operations (e.g., `POST /users` returning `id`) to consumer operations (e.g., `GET /users/{id}`, `POST /orders` requiring `user_id`, `PUT /users/{id}`, `DELETE /users/{id}`).
  - Perform topological sorting with deterministic cycle breaking (CRUD priority: `POST` $\to$ `GET` $\to$ `PUT/PATCH` $\to$ `DELETE`).
  - Emit structured `odg_plan` steps with explicit `produces` and `consumes` state bindings.
- [x] **`src/generator.py` (Semantic Synthesizer & Negative/Security Suite)**:
  - Connect to `LLM_BASE_URL` with `LLM_API_KEY` using `model="fast"`.
  - Generate realistic domain-appropriate positive payloads (Turkish/English context-aware).
  - Synthesize boundary/negative payloads (missing required fields, type mutations, numeric/string boundary violations) for `400`/`422` assertions.
  - Synthesize OWASP API Security test vectors (SQLi, XSS, Mass Assignment, Auth header omission).
  - Assemble stateful Pytest code (`pytest` + `httpx`) where producer response IDs are dynamically passed to downstream consumer tests and verified against response schemas.
- [x] **`src/validator.py` (AST Syntax Sieve & Self-Healing Loop)**:
  - Validate generated Python code with `ast.parse()`.
  - Perform AST security inspection (block `eval`, `exec`, `subprocess`, `os.system`, and ensure every `test_*` function contains meaningful `assert` statements).
  - Implement single-turn LLM repair if `SyntaxError` or `IndentationError` occurs.
  - Implement deterministic template fallback so valid Python code is guaranteed even if the LLM proxy fails or hallucinates.
- [x] **`src/main.py` (FastAPI Gateway Endpoint)**:
  - Implement `GET /health` returning `{"status": "ok"}` in `< 2s`.
  - Implement `POST /v1/invoke` orchestrating signature verification, tenant logging (`X-Tenant-Id`, `X-Request-Id`), operation dispatch (`generate_tests`, `generate_mocks`, `analyze_coverage`), CI status calculation (`ok` | `fail` | `needs_review`), and artifact packaging.

---

## Phase 3: Contract Tests, Unit Tests & 35-Scenario Eval Suite
- [x] **`tests/test_contract.py` (Mandatory Platform Contract Tests)**
- [x] **`tests/test_engine.py` & Regression Suites (`test_regressions_round1..3.py`, `test_swagger2.py`, `test_form_bodies_and_base_path.py`, `test_assets.py`, `test_docs_examples.py`, `test_check_submission.py`, `test_lint_integrations.py`)**
- [x] **`evals/cases.yaml` (35 Evaluation Scenarios: 9 `normal`, 11 `edge`, 4 `out_of_scope`, 11 `attack`)**
- [x] **`EVAL_REPORT.md`**:
  - Execute all 35 evaluation scenarios (including live `FakeLLMServer` cases) and generate the root-level `EVAL_REPORT.md`.

---

## Phase 4: AI Tool Integrations & Storefront Documentation
- [x] **`integrations/common.md`**, **`integrations/claude-code.md`**, **`integrations/codex.md`**, **`integrations/cursor.mdc`**
- [x] **`docs/DESCRIPTION.md`** & **`README.md`**
- [x] **`assets/` (Storefront Visuals)**:
  - Generated `assets/icon.png` ($512 \times 512$ transparent RGBA PNG, minimalist 4-node ODG symbol, no text).
  - Generated `assets/cover.png` ($1200 \times 630$ showcase cover rendered from real engine output).
  - Generated `assets/screenshots/1.png`, `2.png`, `3.png`, `4.png` ($1280 \times 800$ real engine input/output/CI/pytest views with demo data only).

---

## Phase 5: Automated Submission Verification & Manual Owner Release (`v-vize`)
- [x] Run full `pytest` suite (`tests/` + `independent_tests/`) and verify 100% pass rate.
- [x] Run `evals/run_evals.py` (`35/35` scenarios) and verify `EVAL_REPORT.md` is up to date.
- [x] Run `scripts/check_submission.py` and `scripts/lint_integrations.py` against `spec_version 1.1` Appendix C checklist.
- [ ] **[Manual Step for Owner]** Verify `author.github` in `agent.yaml`.
- [ ] **[Manual Step for Owner]** Run the integration instructions (`integrations/claude-code.md`, `codex.md`, or `cursor.mdc`) on two real external projects and fill the verification table in `integrations/common.md` Section 4.
- [ ] **[Manual Step for Owner]** Build and smoke-test the Docker image (`docker build -t auto-qa-engine:v-vize .`).
- [ ] **[Manual Step for Owner]** Initialize git, commit all deliverables, push to GitHub, and tag release `v-vize` (see `README.md` Section 6).
