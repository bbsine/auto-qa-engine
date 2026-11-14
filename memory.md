# AutoQA Engine — Project Memory & Fixed Decisions (`memory.md`)

**Project Name:** `auto-qa-engine` (OpenAPI & Route Tabanlı Otomatik API Test ve Veri Üretici)  
**Contract Version:** `spec_version: "1.1"` (November 2026)  
**Target Release Tag:** `v-vize`

---

## 1. Fixed Architectural & Design Decisions

1. **Category & Niche Selection:**
   - **Category:** `test-ve-qa` (Test ve QA)
   - **Agent Slug:** `auto-qa-engine`
   - **Display Name:** `AutoQA API Test Üretici` (23 characters, strictly $\le 30$ chars)
   - **Short Note:** `OpenAPI şemasından bağımlılık sıralı Pytest entegrasyon testleri ve gerçekçi test verisi üretir.` (96 characters, strictly $\le 110$ chars)
   - **Integration Types:** `types: [ci, mcp, api]` (All powered via `POST /v1/invoke`).

2. **Hybrid Execution Model ("Planner-First, AI-Second"):**
   - OpenAPI parsing (`src/parser.py`) and Operation Dependency Graph (ODG) topological sorting (`src/planner.py`) are **100% deterministic Python**.
   - The LLM (`model="fast"`) is invoked **only** in `src/generator.py` for semantic business data synthesis, boundary/mutation payloads, and OWASP injection vectors, plus single-turn syntax healing in `src/validator.py`.
   - Every generated Python test suite MUST pass `ast.parse()` before returning. If the LLM proxy is unreachable or returns unfixable syntax after 1 retry, the engine deterministically compiles a fallback Pytest suite from the ODG IR so the agent never fails schema validation.

3. **Technology Stack:**
   - **Language & Runtime:** Python 3.12 (`python:3.12-slim` Docker image)
   - **Web Framework:** FastAPI + Uvicorn (`uvicorn src.main:app --host 0.0.0.0 --port ${PORT}`)
   - **LLM Client:** Official `openai` Python SDK configured exclusively with `base_url=os.environ["LLM_BASE_URL"]` and `api_key=os.environ["LLM_API_KEY"]` using model alias `fast`.
   - **Spec Parsing & Validation:** `pyyaml`, `jsonschema`, `pydantic` (v2), Python built-in `ast`.
   - **Testing:** `pytest`, `httpx` (for `TestClient` and generated test templates).

---

## 2. Runtime & Container Configuration

| Parameter | Value / Constraint | Source / Enforcement |
| :--- | :--- | :--- |
| **Base Image** | `python:3.12-slim` | `Dockerfile` |
| **Container User** | `USER 1000` (non-root) | `Dockerfile` & `spec_version 1.1` §2.2 |
| **Listening Port** | `PORT` env var (default `8080`) | `Dockerfile` & `agent.yaml` |
| **Memory Limit** | `512 MB` (`memory_mb: 512`) | `agent.yaml` (`runtime.memory_mb`) |
| **CPU Allocation** | `0.5 vCPU` (`cpu: 0.5`) | `agent.yaml` (`runtime.cpu`) |
| **Allowed Models** | `[fast]` | `agent.yaml` (`runtime.models`) |
| **Network Allowlist** | `allow: []` (Zero external network access except LLM Proxy) | `agent.yaml` (`network.allow`) |
| **Max Image Size** | `< 1 GB` (Target: `< 250 MB`) | `spec_version 1.1` §2.6 |
| **Health Probe SLA** | `GET /health` -> `200 OK` `{"status": "ok"}` in `< 2s` (Cold boot `< 20s`) | `spec_version 1.1` §2.2, §4.4 |
| **Operation Timeout** | `timeout_s: 60` (Max allowed by spec is 300s; P95 target `< 30s`) | `agent.yaml` (`operations.*.timeout_s`) |

---

## 3. Platform Environment Variables

The container reads configuration strictly from environment variables injected by the Platform Runtime:

| Variable | Provided By | Purpose & Handling Rule |
| :--- | :--- | :--- |
| `PORT` | Platform | HTTP port for Uvicorn to bind (defaults to `8080`). |
| `PLATFORM_SIGNING_SECRET` | Platform | Shared HMAC-SHA256 secret used to verify `X-Platform-Signature`. Different in sandbox vs. production. |
| `LLM_BASE_URL` | Platform | OpenAI-compatible Platform LLM Proxy URL. |
| `LLM_API_KEY` | Platform | Platform LLM Proxy token (billed to customer subscription). |
| `AGENT_ENV` | Platform | Execution environment (`sandbox` or `production`). |

> **Strict Rule:** No `.env` files, personal OpenAI/Anthropic keys, or hardcoded secrets may ever exist in the repository or Docker image.

---

## 4. Midterm Evaluation Rubric Breakdown (100 Points — `spec_version 1.1` §5)

| Criterion | Points | Evaluation Details & Our Target Deliverable |
| :--- | :---: | :--- |
| **1. Çalışırlık ve Sözleşme Uyumu** *(Operability & Contract Compliance)* | **20** | Container boots cleanly (`USER 1000`), `GET /health` responds `< 2s`, constant-time HMAC signature verification (`401` on invalid/expired), `422` on unknown operation/invalid input, output matches `agent.yaml` `output_schema`, `tests/test_contract.py` passes 100%. |
| **2. Fonksiyonel Değer** *(Functional Value)* | **20** | Solves a real engineering problem: parses complex OpenAPI specs, resolves producer-consumer dependencies via ODG, generates AST-verified Pytest code + realistic mocks in `demo.samples`. |
| **3. Eval Seti** *(Evaluation Suite)* | **15** | At least 20 scenarios in `evals/cases.yaml` covering all 4 mandatory groups (`normal`, `edge`, `invalid`/`out_of_scope`, `attack`), realistic assertions, and a complete `EVAL_REPORT.md` in repo root. |
| **4. Güvenlik** *(Security)* | **15** | Passes prompt injection, multi-tenant isolation (`X-Tenant-Id`), KVKK/PII redaction (no card/CVV/PII logging), and SSRF blocking tests (rejecting localhost, `169.254.169.254`, RFC 1918 IPs). |
| **5. Entegrasyon Talimatları** *(Integration Instructions)* | **15** | Complete `integrations/common.md`, `claude-code.md`, `codex.md`, and `cursor.mdc`. Uses valid `{{DOUBLE_CURLY}}` placeholders, **zero `sk_` placeholders**, clear channel selection (`ci`, `mcp`, `api`), tested on 2 project types. |
| **6. Dokümantasyon ve Vitrin** *(Documentation & Storefront)* | **10** | Complete `README.md`, `docs/DESCRIPTION.md` (with honest limitations), valid `agent.yaml`, `assets/icon.png` (512x512), `assets/cover.png` (1200x630), and 3–5 screenshots (1280x800). |
| **7. Sunum ve Demo** *(Presentation & Live Demo)* | **5** | 5-minute live walkthrough: OpenAPI input $\to$ ODG & Pytest output $\to$ live CI/MCP integration demonstration, with full code mastery. |
| **TOTAL** | **100** | **Target Release Tag:** `v-vize` |

---

## 5. Required Repository Structure (`spec_version 1.1` §2.1)

```text
auto-qa-engine/
├── architecture.md          # Phase 0: System architecture & ODG design
├── memory.md                # Phase 0: Fixed decisions, env vars & rubric
├── contract_rules.md        # Phase 0: Security & contract compliance rules
├── tasks.md                 # Phase 0: Phased implementation checklist
├── agent.yaml               # Agent identity card, schemas, runtime, demo samples
├── Dockerfile               # python:3.12-slim, USER 1000, port 8080
├── requirements.txt         # Lightweight runtime dependencies
├── EVAL_REPORT.md           # Midterm deliverable: execution report of cases.yaml
├── README.md                # Setup, architecture, local run & test guide
├── src/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, HMAC middleware, /health, /v1/invoke
│   ├── security.py          # HMAC verification, SSRF guard, PII/KVKK sanitizer
│   ├── parser.py            # Deterministic OpenAPI 3.x & route parser
│   ├── planner.py           # Operation Dependency Graph (ODG) builder & sorter
│   ├── generator.py         # Hybrid Pytest & semantic mock synthesizer (model="fast")
│   └── validator.py         # AST syntax sieve & single-turn self-healing loop
├── tests/
│   ├── test_contract.py     # Mandatory platform contract & security tests
│   └── test_engine.py       # Unit tests for parser, ODG planner, validator, SSRF
├── evals/
│   └── cases.yaml           # 20+ evaluation scenarios across all 4 groups
├── integrations/
│   ├── common.md            # Channel selection (CI vs MCP vs API) & architecture notes
│   ├── claude-code.md       # Claude Code step-by-step integration instructions
│   ├── codex.md             # OpenAI Codex AGENTS.md rules & task prompt
│   └── cursor.mdc           # Cursor .cursor/rules/*.mdc integration rule
├── docs/
│   └── DESCRIPTION.md       # Marketplace storefront long description & limitations
└── assets/
    ├── icon.png             # 512x512 transparent PNG icon (no text)
    ├── cover.png            # 1200x630 cover image
    └── screenshots/         # 3-5 screenshots at 1280x800
```
