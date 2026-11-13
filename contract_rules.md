# AutoQA Engine — Platform Contract & Security Rules (`contract_rules.md`)

**Contract Specification:** `spec_version: "1.1"` (November 2026)  
**Compliance Scope:** Mandatory for all runtime endpoints, packaging, evaluation suites, and integration instructions.

---

## 1. Timing-Safe HMAC Signature Verification & Replay Protection

Every request to `POST /v1/invoke` MUST be authenticated before parsing JSON or executing business logic.

### 1.1 Verification Algorithm
- **Required Headers:**
  - `X-Platform-Timestamp`: Unix epoch timestamp in seconds (string).
  - `X-Platform-Signature`: Hex-encoded HMAC-SHA256 digest.
- **Replay Window Rule:**
  - Reject immediately with `HTTP 401 Unauthorized` if `X-Platform-Timestamp` or `X-Platform-Signature` is missing, if `ts` is not a valid integer string, or if:
    $$|\text{time.time()} - \text{int}(ts)| > 300$$
- **Canonical Signing Payload:**
  - Construct the message bytes as: `ts.encode("utf-8") + b"." + raw_request_body_bytes`
  - Compute expected digest:
    ```python
    expected = hmac.new(
        SECRET,
        ts.encode("utf-8") + b"." + body,
        hashlib.sha256
    ).hexdigest()
    ```
- **Constant-Time Comparison:**
  - MUST use `hmac.compare_digest(expected, sig)` to prevent byte-by-byte timing side-channel attacks. Standard `==` comparison on signatures is **strictly forbidden**.

---

## 2. Zero-Key Ingestion & LLM Proxy Mandate

1. **No Personal API Keys:** The repository, `Dockerfile`, environment defaults, and `config_schema` MUST NEVER contain, accept, or fallback to personal OpenAI, Anthropic, or Gemini API keys (`sk-...`, `AIza...`).
2. **Exclusive Gateway Proxy Usage:** All LLM calls must use the platform-injected environment variables:
   - `LLM_BASE_URL`
   - `LLM_API_KEY`
3. **Model Alias Enforcement:** Only the `fast` model alias (`model="fast"`) declared in `agent.yaml` (`runtime.models: [fast]`) may be called.

---

## 3. Stateless Container & Multi-Tenant Isolation

1. **Zero Persistent Disk Writes:**
   - The container filesystem is treated as ephemeral. Writing generated test files, caches, or logs to local disk paths is prohibited.
   - All generated test suites (`pytest_code`) and reports are returned in-memory via the JSON response (`output` and `artifacts` array).
2. **Zero Global Tenant State:**
   - Never store request payloads, OpenAPI specs, parsed graphs, or customer `config` in module-level globals, class attributes, or unkeyed caches.
   - If any in-memory memoization is used within a request lifecycle, it must be strictly request-scoped or keyed by `X-Tenant-Id` and cleared upon completion.
3. **Observability & Tenant Headers:**
   - Read `X-Tenant-Id` and `X-Request-Id` on every request.
   - Include `tenant_id` and `request_id` in structured stdout logs, while strictly excluding PII or secret payloads.

---

## 4. Network Isolation & SSRF (Server-Side Request Forgery) Prevention

1. **Declarative Network Policy:**
   - `agent.yaml` MUST set:
     ```yaml
     network:
       allow: []
     ```
   - The container makes outbound network requests **only** to `LLM_BASE_URL`.
2. **Zero Blind URL Fetching:**
   - `auto-qa-engine` accepts OpenAPI specifications as raw JSON/YAML strings (`openapi_spec`).
   - If a user passes a `spec_url` or defines `servers[].url` / `base_url` in their OpenAPI spec or `config`, the agent **never** performs live HTTP requests against those URLs from inside the container (`base_url` is only interpolated as a string literal into the generated Pytest code for the customer to run in their own CI/local environment).
3. **Strict URL & IP Sanitizer (`src/security.py`):**
   - Any URL inspected or validated by the agent must pass an SSRF validator that explicitly blocks:
     - Loopback addresses: `localhost`, `127.0.0.0/8`, `::1`, `0.0.0.0`
     - Cloud Metadata endpoints: `169.254.169.254`, `metadata.google.internal`, `169.254.0.0/16`
     - Private RFC 1918 networks: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
     - Non-HTTP/HTTPS schemes: `file://`, `ftp://`, `gopher://`, `dict://`, `ldap://`
   - Any attempt to pass an internal/metadata URL as a remote fetch target must be rejected with `HTTP 422` (`{"error": {"code": "ssrf_blocked", "message": "..."}}`).

---

## 5. Prompt Injection Defense & Red-Line Data Rules (KVKK & §4.3)

1. **Untrusted Input Isolation:**
   - OpenAPI descriptions, parameter names, and example values inside `input.openapi_spec` are treated as **untrusted user data**.
   - Prompts sent to `LLM_BASE_URL` must wrap user-supplied schema fragments inside strict delimiter tags and enforce system instructions that refuse role-switching (`"Artık sen X'sin"`), system prompt disclosure (`"Önceki tüm talimatları unut"`), or arbitrary code injection.
2. **Payment & Sensitive Data Red Line (`spec_version 1.1` §4.3):**
   - Credit card numbers (PAN / Luhn-matching 13–19 digit sequences), CVV/CVC codes, expiration dates, bank passwords, or real Turkish Republic ID numbers (TCKN) must **never** be accepted into prompts, logged, or emitted in generated mock outputs.
   - If sensitive payment/PII patterns are detected in the input payload, the sanitizer scrubs or rejects them before any processing or logging.
3. **Output Schema Guarantee & Error Taxonomy (§5.3):**
   - Even if the LLM returns malformed JSON or refuses a prompt injection attempt, the agent must catch the failure and return either a valid `output_schema`-compliant fallback response or a structured `422` error JSON `{"error": {"code": "...", "message": "..."}}`—never an unhandled `500` traceback.
   - **Input-shape problems vs. Real internal faults:**
     - Any validation failure, malformed OpenAPI syntax, excessive `existing_test_code` size/AST depth (`> 200 KB` or `> 80` AST depth -> `invalid_existing_test_code`), or unexpected input-shape exception (`TypeError`, `KeyError`, `AttributeError`, `IndexError`, `RecursionError` raised while processing untrusted user input inside an operation pipeline) MUST return `HTTP 422` (`{"error": {"code": "invalid_input_shape", "message": "Input could not be processed."}}`) with **zero** raw exception text.
     - Genuine internal runtime faults outside input-shape processing return `HTTP 500` with a generic `{"error": {"code": "processing_failed", "message": "Request could not be processed."}}` response and log `tenant_id` and `request_id` server-side—never leaking a traceback or internal exception message to the caller.

---

## 6. Integration Files & Packaging Security Rules

1. **Forbidden `sk_` Placeholders (`spec_version 1.1` §3.3):**
   - Files under `integrations/` (`common.md`, `claude-code.md`, `codex.md`, `cursor.mdc`) MUST NEVER contain `sk_...` literals or placeholders like `{{SECRET_KEY}}`.
   - Allowed placeholders are strictly limited to:
     - `{{AGENT_NAME}}`
     - `{{AGENT_SLUG}}`
     - `{{PUBLIC_KEY}}` *(only if widget is used; not used for `sk_`)*
     - `{{SAMPLE_INPUT}}`
     - `{{OPERATIONS}}`
   - Integration instructions must instruct the developer/AI tool to create an **empty** `PLATFORM_SECRET_KEY=` entry in `.env` and verify `.env` is in `.gitignore`.
2. **Container Hardening (`spec_version 1.1` §2.6):**
   - Base image: `python:3.12-slim`
   - Non-root execution: `USER 1000`
   - Fast health check: `GET /health` returns `200 OK` with `{"status": "ok"}` in `< 2s`.
   - Total Docker image size `< 1 GB`.
