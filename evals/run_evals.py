"""Evaluation suite runner for auto-qa-engine (`evals/cases.yaml`) with live FakeLLMServer support."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")
os.environ.setdefault("LLM_API_KEY", "test-eval-key")
os.environ.setdefault("LLM_TIMEOUT_S", "0.25")

import jsonschema
import yaml
from fastapi.testclient import TestClient

from evals.fake_llm_server import FakeLLMServer
from src.main import app
from src.validator import validate_test_syntax


def _sign_request(payload: dict[str, Any], tenant_id: str) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    secret = os.environ["PLATFORM_SIGNING_SECRET"].encode("utf-8")
    sig = hmac.new(secret, ts.encode("utf-8") + b"." + raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Platform-Timestamp": ts,
        "X-Platform-Signature": sig,
        "X-Tenant-Id": tenant_id,
        "X-Request-Id": f"req_{tenant_id}",
        "Content-Type": "application/json",
    }


def _load_operation_output_schemas() -> dict[str, dict[str, Any]]:
    agent_yaml_path = REPO_ROOT / "agent.yaml"
    with agent_yaml_path.open("r", encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)
    ops = manifest.get("operations", {})
    return {op_name: op_def.get("output_schema", {}) for op_name, op_def in ops.items()}


def evaluate_case(
    client: TestClient,
    case: dict[str, Any],
    output_schemas: dict[str, dict[str, Any]],
    fake_llm: FakeLLMServer,
) -> tuple[bool, list[str], float, str]:
    """Execute a single eval case against POST /v1/invoke and check all expectations."""
    case_id = case["id"]
    operation = case.get("operation", "generate_tests")
    inp = case.get("input", {})
    cfg = case.get("config", {})
    expect = case.get("expect", {})
    llm_mode = case.get("llm_mode")

    if llm_mode:
        fake_llm.mode = str(llm_mode)
        os.environ["LLM_BASE_URL"] = fake_llm.base_url
        os.environ["LLM_API_KEY"] = "test-eval-key"
        mode_label = f"llm:{llm_mode}"
    else:
        os.environ["LLM_BASE_URL"] = ""
        mode_label = "deterministic"

    payload = {
        "operation": operation,
        "input": inp,
        "config": cfg,
        "trigger": {"kind": "ci"},
    }
    raw_body, headers = _sign_request(payload, tenant_id=f"ten_{case_id}")

    t0 = time.perf_counter()
    resp = client.post("/v1/invoke", content=raw_body, headers=headers)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    failures: list[str] = []
    expected_http = expect.get("http_status", 200)
    if resp.status_code != expected_http:
        failures.append(f"HTTP status mismatch: expected {expected_http}, got {resp.status_code} ({resp.text})")
        return False, failures, elapsed_ms, mode_label

    body = resp.json()
    raw_resp_text = json.dumps(body, ensure_ascii=False)

    for forbidden in expect.get("not_contains", []):
        if forbidden in raw_resp_text:
            failures.append(f"Forbidden string '{forbidden}' found in response")

    if expect.get("not_contains_system_prompt"):
        for marker in (
            "SYNTHESIS_SYSTEM_PROMPT",
            "Sen bir API Test ve Güvenlik Otomasyon Mühendisisin",
        ):
            if marker in raw_resp_text:
                failures.append(f"System prompt marker '{marker}' leaked in response")

    for forbidden_field in expect.get("not_contains_fields", []):
        if forbidden_field in body or (isinstance(body.get("output"), dict) and forbidden_field in body["output"]):
            failures.append(f"Forbidden field '{forbidden_field}' present in response")

    if expected_http != 200:
        expected_err_code = expect.get("error_code")
        actual_err_code = body.get("error", {}).get("code")
        if expected_err_code and actual_err_code != expected_err_code:
            failures.append(f"Error code mismatch: expected '{expected_err_code}', got '{actual_err_code}'")
        return len(failures) == 0, failures, elapsed_ms, mode_label

    if "status" in expect and body.get("status") != expect["status"]:
        failures.append(f"Envelope status mismatch: expected '{expect['status']}', got '{body.get('status')}'")

    output = body.get("output", {})

    if expect.get("schema_valid"):
        schema = output_schemas.get(operation)
        if schema:
            try:
                jsonschema.validate(instance=output, schema=schema)
            except jsonschema.ValidationError as exc:
                failures.append(f"output_schema validation failed: {exc.message}")

    if expect.get("ast_valid"):
        code = output.get("test_code") or output.get("pytest_code") or ""
        is_valid, err = validate_test_syntax(code)
        if not is_valid:
            failures.append(f"AST syntax validation failed: {err}")

    if "min_operations" in expect:
        actual_ops = output.get("summary", {}).get("total_operations", 0)
        if actual_ops < expect["min_operations"]:
            failures.append(f"Expected >= {expect['min_operations']} operations, got {actual_ops}")

    if "min_assertions" in expect:
        actual_asserts = output.get("total_assertions", 0)
        if actual_asserts < expect["min_assertions"]:
            failures.append(f"Expected >= {expect['min_assertions']} assertions, got {actual_asserts}")

    odg_plan = output.get("odg_plan", [])
    if "odg_first_operation" in expect and odg_plan:
        first_op = odg_plan[0].get("operation_id")
        if first_op != expect["odg_first_operation"]:
            failures.append(f"ODG first op mismatch: expected '{expect['odg_first_operation']}', got '{first_op}'")

    if "odg_last_operation" in expect and odg_plan:
        last_op = odg_plan[-1].get("operation_id")
        if last_op != expect["odg_last_operation"]:
            failures.append(f"ODG last op mismatch: expected '{expect['odg_last_operation']}', got '{last_op}'")

    return len(failures) == 0, failures, elapsed_ms, mode_label


def run_all_evals() -> int:
    """Load `evals/cases.yaml`, run all cases (with FakeLLMServer for LLM cases), print summary, and return exit code."""
    cases_path = Path(__file__).resolve().parent / "cases.yaml"
    with cases_path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)

    cases: list[dict[str, Any]] = doc.get("cases", [])
    output_schemas = _load_operation_output_schemas()
    client = TestClient(app, raise_server_exceptions=False)

    passed_count = 0
    group_stats: dict[str, dict[str, int]] = {}
    latencies: list[float] = []

    with FakeLLMServer() as fake_llm:
        print("=" * 98)
        print(f"AutoQA Engine — Evaluation Suite Runner ({len(cases)} scenarios | FakeLLMServer on {fake_llm.base_url})")
        print("=" * 98)

        for case in cases:
            cid = case["id"]
            grp = case.get("group", "unknown")
            group_stats.setdefault(grp, {"total": 0, "passed": 0})
            group_stats[grp]["total"] += 1

            ok, failures, elapsed_ms, mode_label = evaluate_case(client, case, output_schemas, fake_llm)
            latencies.append(elapsed_ms)

            if ok:
                passed_count += 1
                group_stats[grp]["passed"] += 1
                print(f"[PASS] {cid:<38} | group={grp:<12} | mode={mode_label:<22} | {elapsed_ms:6.2f} ms")
            else:
                print(f"[FAIL] {cid:<38} | group={grp:<12} | mode={mode_label:<22} | {elapsed_ms:6.2f} ms")
                for f in failures:
                    print(f"       -> {f}")

        print("-" * 98)
        for grp, st in group_stats.items():
            print(f"Group '{grp}': {st['passed']}/{st['total']} passed")
        avg_ms = sum(latencies) / len(latencies) if latencies else 0.0
        max_ms = max(latencies) if latencies else 0.0
        print(f"FakeLLMServer HTTP requests handled: {fake_llm.request_count}")
        print("-" * 98)
        print(
            f"TOTAL: {passed_count}/{len(cases)} passed ({passed_count / len(cases) * 100:.1f}%) "
            f"| avg={avg_ms:.2f}ms | max={max_ms:.2f}ms"
        )
        print("=" * 98)

    return 0 if passed_count == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(run_all_evals())
