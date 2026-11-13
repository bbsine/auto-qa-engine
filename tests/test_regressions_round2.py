"""Regression test suite for Round 2 audit findings [1] through [7]."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")

import pytest
import uvicorn
from fastapi.testclient import TestClient

import src.main as main_mod
from src.generator import _get_llm_client
from src.main import app
from src.security import contains_prohibited_payment_data, is_ssrf_url

client = TestClient(app, raise_server_exceptions=False)


def _signed(body: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode("utf-8")
    ts = str(int(time.time()))
    sig = hmac.new(b"test-secret", ts.encode("utf-8") + b"." + raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Platform-Timestamp": ts,
        "X-Platform-Signature": sig,
        "X-Tenant-Id": "ten_reg_r2",
        "X-Request-Id": "req_reg_r2_99",
        "Content-Type": "application/json",
    }


# ============================================================================
# [1] analyze_coverage per-call-node matching
# ============================================================================
COVERAGE_REPRO_SPEC = """
openapi: "3.0.3"
info:
  title: "Coverage Repro API"
  version: "1.0.0"
paths:
  /users:
    post:
      operationId: create_user
      responses:
        "201": {description: "Created"}
  /users/{id}:
    get:
      operationId: get_user
      parameters:
        - name: id
          in: path
          required: true
          schema: {type: string}
      responses:
        "200": {description: "OK"}
  /orders:
    get:
      operationId: list_orders
      responses:
        "200": {description: "OK"}
"""


def test_issue_1_analyze_coverage_does_not_false_positive_on_prefix_or_substring() -> None:
    """[1] POST /users + GET /orders must NOT mark GET /users/{id} as covered, nor match substring function names."""
    existing_code = """
# c.get("/users/123")  <-- commented out call must be ignored
def test_get_user_orders(c, dynamic_url):
    unused_str = "/users/123"
    c.post("/users", json={})
    c.get("/orders")
    c.get(dynamic_url)
"""
    raw, headers = _signed(
        {
            "operation": "analyze_coverage",
            "input": {
                "openapi_spec": COVERAGE_REPRO_SPEC,
                "existing_test_code": existing_code,
            },
        }
    )
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 200
    out = resp.json()["output"]
    assert out["covered_operations"] == ["create_user", "list_orders"]
    assert out["uncovered_operations"] == ["get_user"]


def test_issue_1_analyze_coverage_fstring_and_syntax_error_handling() -> None:
    """[1] f-string path c.get(f'/users/{uid}') covers ONLY GET /users/{id} (not GET /users), and concat works."""
    spec_with_list_and_detail = """
openapi: "3.0.3"
info:
  title: "Users List vs Detail API"
  version: "1.0.0"
paths:
  /users:
    get:
      operationId: list_users
      responses:
        "200": {description: "OK"}
  /users/{id}:
    get:
      operationId: get_user
      parameters:
        - name: id
          in: path
          required: true
          schema: {type: string}
      responses:
        "200": {description: "OK"}
"""
    fstring_code = """
def test_something(c):
    uid = "42"
    c.get(f"/users/{uid}")
"""
    raw, headers = _signed(
        {
            "operation": "analyze_coverage",
            "input": {
                "openapi_spec": spec_with_list_and_detail,
                "existing_test_code": fstring_code,
            },
        }
    )
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 200
    # Must ONLY cover get_user (/users/{id}), NOT list_users (/users)!
    assert resp.json()["output"]["covered_operations"] == ["get_user"]
    assert resp.json()["output"]["uncovered_operations"] == ["list_users"]

    # Syntax-error test code -> safe fallback, 0 covered, no 500
    broken_code = 'def test_broken(:\n    c.get("/users/1")\n'
    raw_b, headers_b = _signed(
        {
            "operation": "analyze_coverage",
            "input": {
                "openapi_spec": spec_with_list_and_detail,
                "existing_test_code": broken_code,
            },
        }
    )
    resp_b = client.post("/v1/invoke", content=raw_b, headers=headers_b)
    assert resp_b.status_code == 200
    assert resp_b.json()["output"]["covered_operations"] == []


# ============================================================================
# [2] Sensitive-data filter: IIN prefix, YAML escape, IBAN, expiry keys
# ============================================================================
def test_issue_2_legitimate_16_digit_id_and_iban_pass_while_yaml_escaped_pan_blocked() -> None:
    """[2a/2b/2c] Non-IIN 16-digit Luhn numbers and TR IBANs must pass; YAML-escaped PANs and expiry keys must be 422."""
    # 1. Legitimate 16-digit order ID that happens to pass Luhn but starts with '80' (not a card IIN) -> MUST PASS
    # 8000123456789013: check Luhn
    non_iin_luhn = "8000123456789012"
    # Find the exact check digit for 800012345678901X so it is Luhn-valid:
    for d in range(10):
        cand = f"800012345678901{d}"
        # Luhn check
        digits = [int(c) for c in cand]
        checksum = sum(digits[-1::-2]) + sum(sum(divmod(2 * x, 10)) for x in digits[-2::-2])
        if checksum % 10 == 0:
            non_iin_luhn = cand
            break

    assert contains_prohibited_payment_data(f"order_id: {non_iin_luhn}") is False

    # 2. Spaced Turkish IBAN -> MUST PASS
    iban_text = "iban: TR33 0006 1005 1978 6457 8413 26"
    assert contains_prohibited_payment_data(iban_text) is False

    # 3. YAML escape bypass in OpenAPI description -> MUST BE BLOCKED (422 prohibited_sensitive_data)
    yaml_escaped_pan_spec = (
        'openapi: "3.0.3"\n'
        'info:\n'
        '  title: "Test"\n'
        '  description: "Card: 4532\\u0030151 1283 0366"\n'
        '  version: "1.0.0"\n'
        'paths:\n'
        '  /ping:\n'
        '    get:\n'
        '      responses:\n'
        '        "200": {description: "ok"}\n'
    )
    raw, headers = _signed({"operation": "generate_tests", "input": {"openapi_spec": yaml_escaped_pan_spec}})
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "prohibited_sensitive_data"

    # 4. "expiry" and "exp_date" keywords -> MUST BE BLOCKED
    assert contains_prohibited_payment_data('{"expiry": "12/28"}') is True
    assert contains_prohibited_payment_data('{"exp_date": "05/29"}') is True


def test_issue_2_false_positive_rate_2000_random_samples() -> None:
    """[2a/2c] Measure false-positive rate over 2000 random 16-digit and 2000 random 11-digit numbers (must be < 5%)."""
    import random

    rng = random.Random(20261107)
    fp_16 = 0
    for _ in range(2000):
        num16 = str(rng.randint(10**15, 10**16 - 1))
        if contains_prohibited_payment_data(f"order_id: {num16}"):
            fp_16 += 1

    fp_11 = 0
    for _ in range(2000):
        num11 = str(rng.randint(10**10, 10**11 - 1))
        if contains_prohibited_payment_data(f"ref_id: {num11}"):
            fp_11 += 1

    rate_16 = (fp_16 / 2000) * 100.0
    rate_11 = (fp_11 / 2000) * 100.0
    assert rate_16 < 5.0, f"16-digit false positive rate too high: {rate_16:.2f}% ({fp_16}/2000)"
    assert rate_11 < 2.0, f"11-digit false positive rate too high: {rate_11:.2f}% ({fp_11}/2000)"


# ============================================================================
# [3] Global exception handler: no leaked internal exception text, logs request_id
# ============================================================================
def test_issue_3_internal_exception_does_not_leak_details_and_logs_request_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[3] KeyError('secret /etc/passwd') inside an operation must return generic error without leaking message."""
    def _boom(inp: dict, cfg: dict):
        raise KeyError("secret /etc/passwd")

    monkeypatch.setitem(main_mod.OPERATIONS, "generate_tests", _boom)

    raw, headers = _signed(
        {
            "operation": "generate_tests",
            "input": {"openapi_spec": COVERAGE_REPRO_SPEC},
        }
    )
    with caplog.at_level(logging.ERROR):
        resp = client.post("/v1/invoke", content=raw, headers=headers)

    body_text = resp.text
    assert "secret" not in body_text
    assert "/etc/passwd" not in body_text
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == "processing_failed"
    assert data["error"]["message"] == "Request could not be processed."
    assert any("req_reg_r2_99" in rec.getMessage() for rec in caplog.records)


# ============================================================================
# [4] estimated_coverage_pct computed from target_endpoints vs total spec ops
# ============================================================================
def test_issue_4_estimated_coverage_pct_reflects_filtered_subset() -> None:
    """[4] Filtering 1 of 3 operations via target_endpoints must yield estimated_coverage_pct < 100.0 (33.33%)."""
    raw_sub, headers_sub = _signed(
        {
            "operation": "generate_tests",
            "input": {
                "openapi_spec": COVERAGE_REPRO_SPEC,
                "target_endpoints": ["/users"],
            },
        }
    )
    resp_sub = client.post("/v1/invoke", content=raw_sub, headers=headers_sub)
    assert resp_sub.status_code == 200
    pct_sub = resp_sub.json()["output"]["summary"]["estimated_coverage_pct"]
    assert pct_sub == pytest.approx(33.33, abs=0.1)

    raw_full, headers_full = _signed(
        {
            "operation": "generate_tests",
            "input": {"openapi_spec": COVERAGE_REPRO_SPEC},
        }
    )
    resp_full = client.post("/v1/invoke", content=raw_full, headers=headers_full)
    assert resp_full.status_code == 200
    assert resp_full.json()["output"]["summary"]["estimated_coverage_pct"] == 100.0


# ============================================================================
# [5] Renamed *_security_unknown_id_and_auth + strong response schema & parent-ID oracle E2E
# ============================================================================
E2E_NESTED_SPEC = """
openapi: "3.0.3"
info:
  title: "Nested User Orders E2E API"
  version: "1.0.0"
paths:
  /users:
    post:
      operationId: create_user
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [name, email]
              properties:
                name: {type: string, minLength: 2}
                email: {type: string, format: email}
      responses:
        "201":
          description: "Created"
          content:
            application/json:
              schema:
                type: object
                required: [id, name, email]
                properties:
                  id: {type: string}
                  name: {type: string}
                  email: {type: string}
  /orders:
    post:
      operationId: create_order
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [user_id, item_name]
              properties:
                user_id: {type: string}
                item_name: {type: string, minLength: 2}
      responses:
        "201":
          description: "Created"
          content:
            application/json:
              schema:
                type: object
                required: [id, user_id, item_name]
                properties:
                  id: {type: string}
                  user_id: {type: string}
                  item_name: {type: string}
  /users/{user_id}/orders/{order_id}:
    get:
      operationId: get_user_order
      parameters:
        - name: user_id
          in: path
          required: true
          schema: {type: string}
        - name: order_id
          in: path
          required: true
          schema: {type: string}
      responses:
        "200":
          description: "Order detail"
          content:
            application/json:
              schema:
                type: object
                required: [id, user_id, item_name]
                properties:
                  id: {type: string}
                  user_id: {type: string}
                  item_name: {type: string}
"""


def _start_uvicorn_in_thread(asgi_app) -> tuple[uvicorn.Server, threading.Thread, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    return server, thread, port


def test_issue_5_renamed_security_test_and_e2e_oracle_catches_bola(tmp_path: Path) -> None:
    """[5] Generated tests must use *_security_unknown_id_and_auth, pass on correct_app, and FAIL on broken_app."""
    from tests.e2e.servers import broken_app, correct_app

    srv_ok, th_ok, port_ok = _start_uvicorn_in_thread(correct_app)
    srv_bad, th_bad, port_bad = _start_uvicorn_in_thread(broken_app)

    try:
        raw_ok, h_ok = _signed(
            {
                "operation": "generate_tests",
                "input": {
                    "openapi_spec": E2E_NESTED_SPEC,
                    "base_url": f"http://127.0.0.1:{port_ok}",
                },
            }
        )
        resp_ok = client.post("/v1/invoke", content=raw_ok, headers=h_ok)
        assert resp_ok.status_code == 200
        code_ok = resp_ok.json()["output"]["test_code"]

        assert "_security_unknown_id_and_auth" in code_ok
        assert "_security_bola_and_auth" not in code_ok

        test_file_ok = tmp_path / "test_correct_api.py"
        test_file_ok.write_text(code_ok, encoding="utf-8")
        proc_ok = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file_ok), "-q"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc_ok.returncode == 0, f"Expected pass on correct_app:\n{proc_ok.stdout}\n{proc_ok.stderr}"

        # Now generate against broken_app (which returns user_id="SOMEONE_ELSE" on GET /users/{user_id}/orders/{order_id})
        raw_bad, h_bad = _signed(
            {
                "operation": "generate_tests",
                "input": {
                    "openapi_spec": E2E_NESTED_SPEC,
                    "base_url": f"http://127.0.0.1:{port_bad}",
                },
            }
        )
        code_bad = client.post("/v1/invoke", content=raw_bad, headers=h_bad).json()["output"]["test_code"]
        test_file_bad = tmp_path / "test_broken_api.py"
        test_file_bad.write_text(code_bad, encoding="utf-8")
        proc_bad = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file_bad), "-k", "get_user_order_positive", "-q"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc_bad.returncode != 0, "Generated test MUST fail when server returns user_id='SOMEONE_ELSE'"
    finally:
        srv_ok.should_exit = True
        srv_bad.should_exit = True
        th_ok.join(timeout=3)
        th_bad.join(timeout=3)


# ============================================================================
# [6] _get_llm_client no longer hardcodes localhost:9999 exclusion
# ============================================================================
def test_issue_6_get_llm_client_does_not_special_case_localhost_9999(monkeypatch: pytest.MonkeyPatch) -> None:
    """[6] _get_llm_client must return an OpenAI client whenever LLM_BASE_URL and LLM_API_KEY are set, and FakeLLMServer works across all 6 modes."""
    from evals.fake_llm_server import FakeLLMServer

    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    assert _get_llm_client() is not None

    monkeypatch.setenv("LLM_BASE_URL", "")
    assert _get_llm_client() is None

    monkeypatch.setenv("LLM_TIMEOUT_S", "0.25")
    with FakeLLMServer() as fake_llm:
        monkeypatch.setenv("LLM_BASE_URL", fake_llm.base_url)
        monkeypatch.setenv("LLM_API_KEY", "fake-key")

        for mode in ("valid_json", "garbage_text", "leak_pan_tckn", "echo_system_prompt", "slow_timeout", "http_500"):
            fake_llm.mode = mode
            raw, headers = _signed(
                {
                    "operation": "generate_tests",
                    "input": {"openapi_spec": E2E_NESTED_SPEC},
                }
            )
            resp = client.post("/v1/invoke", content=raw, headers=headers)
            assert resp.status_code == 200, f"Mode {mode} returned {resp.status_code}"
            body_str = resp.text
            assert "4532015112830366" not in body_str
            assert "10000000146" not in body_str
            assert "SYNTHESIS_SYSTEM_PROMPT" not in body_str
            assert "test-secret" not in body_str
            assert resp.json()["output"]["summary"]["ast_valid"] is True

        assert fake_llm.request_count >= 6


# ============================================================================
# [7] IPv4-mapped IPv6 loopback consistency
# ============================================================================
def test_issue_7_ipv4_mapped_ipv6_loopback_consistency(monkeypatch: pytest.MonkeyPatch) -> None:
    """[7] http://[::ffff:127.0.0.1]/ must unwrap ipv4_mapped so Python 3.12 (where IPv6Address.is_loopback is False) allows it when allow_local_base_url=True."""
    import ipaddress

    real_ip_address = ipaddress.ip_address

    class _Py312IPv6Compat(ipaddress.IPv6Address):
        @property
        def is_loopback(self) -> bool:
            # Emulate Python 3.12 stdlib behavior where IPv6Address('::ffff:127.0.0.1').is_loopback is False
            return int(self) == 1

    def _patched_ip_address(address):
        res = real_ip_address(address)
        if isinstance(res, ipaddress.IPv6Address):
            return _Py312IPv6Compat(int(res))
        return res

    monkeypatch.setattr(ipaddress, "ip_address", _patched_ip_address)

    unsafe_local_allowed, reason = is_ssrf_url("http://[::ffff:127.0.0.1]:8000/", allow_local_base_url=True)
    assert unsafe_local_allowed is False, f"Expected allowed for local base_url, got: {reason}"

    unsafe_local_blocked, _ = is_ssrf_url("http://[::ffff:127.0.0.1]:8000/", allow_local_base_url=False)
    assert unsafe_local_blocked is True
