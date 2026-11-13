"""Independent black-box tests for auto-qa-engine.

These tests were written WITHOUT reading the project's own tests: they only rely on contract_rules.md and
agent.yaml. Run from the project root:  python -m pytest independent_tests -v
"""
from __future__ import annotations

import ast
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

import src.main as main_mod
from independent_tests.conftest import SECRET, SIMPLE_SPEC, spec_with_body

HERE = Path(__file__).resolve().parent
SHOP_SPEC = (HERE / "spec_shop.yaml").read_text(encoding="utf-8")


def gen(invoke, spec, **extra):
    return invoke({"operation": "generate_tests", "input": {"openapi_spec": spec, **extra}})


# --------------------------------------------------------------------------- 1. HMAC / replay
@pytest.mark.parametrize(
    "offset,expected",
    [(0, 200), (-299, 200), (-302, 401), (302, 401)],
)
def test_replay_window(invoke, offset, expected):
    r = invoke({"operation": "generate_tests", "input": {"openapi_spec": SIMPLE_SPEC}}, ts_offset=offset)
    assert r.status_code == expected


def test_wrong_secret_and_tampered_body_rejected(invoke):
    body = {"operation": "generate_tests", "input": {"openapi_spec": SIMPLE_SPEC}}
    assert invoke(body, secret="wrong").status_code == 401
    tampered = json.dumps({**body, "operation": "analyze_coverage"}).encode()
    assert invoke(body, tamper=tampered).status_code == 401


def test_non_ascii_signature_is_401_not_500(client):
    r = client.post(
        "/v1/invoke",
        content=b"{}",
        headers={"X-Platform-Timestamp": str(int(time.time())), "X-Platform-Signature": b"\xe9" * 64},
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------- 2. Malformed input never 500
BAD_SCHEMAS = [
    {"type": "object", "properties": {"a": {"type": "string", "minLength": "2"}}},
    {"type": "object", "properties": {"a": {"type": "string", "minLength": None}}},
    {"type": "object", "properties": {"a": {"type": "integer", "minimum": "0"}}},
    {"type": "object", "properties": {"a": {"type": "integer", "minimum": None}}},
    {"type": "object", "properties": {"a": {"type": "string", "maxLength": "60"}}},
    {"type": "object", "properties": {"a": {"type": "string", "enum": []}}},
    {"type": "object", "properties": {"a": {"type": "string", "enum": "a"}}},
    {"type": "object", "properties": {"a": {"type": ["string", "null"]}}},
    {"type": "object", "properties": ["a", "b"]},
    {"type": "object", "properties": {"a": None}},
    {"type": "object", "required": "a", "properties": {"a": {"type": "string"}}},
    {"type": "object", "required": [1, None], "properties": {"a": {"type": "string"}}},
    {"type": "object", "properties": {"a": {"type": "array", "items": [{"type": "string"}]}}},
    {"oneOf": [{"type": "object"}, {"type": "string"}]},
    {"allOf": ["x", {"type": "object", "properties": {"a": {"type": "string"}}}]},
    [{"type": "string"}],
    "object",
]


@pytest.mark.parametrize("schema", BAD_SCHEMAS)
@pytest.mark.parametrize("operation", ["generate_tests", "generate_mocks"])
def test_malformed_body_schema_never_500(invoke, schema, operation):
    r = invoke({"operation": operation, "input": {"openapi_spec": spec_with_body(schema)}})
    assert r.status_code in (200, 422), r.text[:200]


def test_deeply_nested_schema_never_500(invoke):
    deep = {"type": "string"}
    for _ in range(60):
        deep = {"type": "object", "properties": {"a": deep}}
    assert invoke({"operation": "generate_tests", "input": {"openapi_spec": spec_with_body(deep)}}).status_code in (200, 422)


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": ["generate_tests"], "input": {}},
        {"operation": None, "input": {}},
        {"operation": "generate_tests", "input": {"openapi_spec": 12345}},
        {"operation": "generate_tests", "input": {"openapi_spec": ["a"]}},
        {"operation": "generate_tests", "input": {"openapi_spec": SIMPLE_SPEC, "target_endpoints": [[1]]}},
        {"operation": "generate_tests", "input": {"openapi_spec": SIMPLE_SPEC, "target_endpoints": 5}},
        {"operation": "analyze_coverage", "input": {"openapi_spec": SIMPLE_SPEC}, "config": {"min_coverage_pct": None}},
        {"operation": "analyze_coverage", "input": {"openapi_spec": SIMPLE_SPEC, "existing_test_code": 123}},
    ],
)
def test_weird_envelopes_are_not_500(invoke, payload):
    assert invoke(payload).status_code in (200, 422)


def test_huge_concatenation_in_existing_test_code_is_422(invoke):
    code = 'c.get("/a"' + ' + "/a"' * 3000 + ")"
    r = invoke({"operation": "analyze_coverage", "input": {"openapi_spec": SIMPLE_SPEC, "existing_test_code": code}})
    assert r.status_code == 422


def test_internal_fault_returns_generic_500_without_leak(invoke, monkeypatch):
    def boom(inp, cfg):
        raise KeyError("secret /etc/passwd")

    monkeypatch.setitem(main_mod.OPERATIONS, "generate_mocks", boom)
    r = invoke({"operation": "generate_mocks", "input": {"openapi_spec": SIMPLE_SPEC}})
    assert r.status_code == 500
    assert "secret" not in r.text and "/etc/passwd" not in r.text


# --------------------------------------------------------------------------- 3. SSRF / base_url
@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254/latest", "http://metadata.google.internal/", "http://10.0.0.5/", "http://192.168.1.1/",
     "http://172.16.0.1/", "file:///etc/passwd", "gopher://x", "ftp://x", "http://[::ffff:169.254.169.254]/"],
)
def test_dangerous_base_url_blocked(invoke, url):
    r = gen(invoke, SIMPLE_SPEC, base_url=url)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ssrf_blocked"


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000", "http://[::ffff:127.0.0.1]:8000"])
def test_local_base_url_allowed(invoke, url):
    assert gen(invoke, SIMPLE_SPEC, base_url=url).status_code == 200


def test_spec_url_never_fetched(invoke):
    r = gen(invoke, SIMPLE_SPEC, spec_url="http://example.com/spec.yaml")
    assert r.status_code == 422


# --------------------------------------------------------------------------- 4. Sensitive data red line
PAN = "4532015112830366"  # well-known Visa test number, Luhn valid


def _yaml_spec(description_literal: str) -> str:
    return (
        "openapi: 3.0.0\npaths:\n  /x:\n    get:\n      description: " + description_literal +
        "\n      responses: {'200': {description: ok}}\n"
    )


@pytest.mark.parametrize(
    "literal",
    [
        '"4532015112830366"',
        '"4532 0151 1283 0366"',
        '"4532-0151-1283-0366"',
        '"4532.0151.1283.0366"',
        '"4532\\u0030151 1283 0366"',
        '"4532\\U00000030151 1283 0366"',
        '"4532 0151\n        1283 0366"',
        '"45320151\\\n        12830366"',
        "4532 0151\n        1283 0366",
        "&a '4532015112830366'",
        '"cvv: 123"',
        '"security_code: 123"',
        '"expiry: 12/28"',
        '"tckn: 10000000146"',
    ],
)
def test_sensitive_values_rejected_even_when_obfuscated(invoke, literal):
    r = gen(invoke, _yaml_spec(literal))
    assert r.status_code == 422, r.text[:200]
    assert r.json()["error"]["code"] == "prohibited_sensitive_data"
    assert PAN not in r.text


@pytest.mark.parametrize("literal", ['"TR33 0006 1005 1978 6457 8413 26"', '"order 1234567890123456"', '"2026-11-07 2026-11-08"'])
def test_legitimate_values_pass(invoke, literal):
    assert gen(invoke, _yaml_spec(literal)).status_code == 200


# --------------------------------------------------------------------------- 5. Spec -> generated code injection
EVIL_PATHS = [
    '/x"""\n    import os\n    os.remove("/tmp/KURBAN")\n    """',
    '/x" + open("/etc/hostname").read() + "',
    "/x/{__import__('os').system('id')}",
]


@pytest.mark.parametrize("path", EVIL_PATHS)
def test_spec_path_never_becomes_executable_code(invoke, path):
    spec = json.dumps({"openapi": "3.0.0", "paths": {path: {"get": {"responses": {"200": {"description": "ok"}}}}}})
    r = gen(invoke, spec)
    assert r.status_code == 200
    out = r.json()["output"]
    assert out["summary"]["ast_valid"] is True
    tree = ast.parse(out["test_code"])
    risky = {"open", "__import__", "exec", "eval", "system", "remove", "urlopen", "popen"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            assert name not in risky, f"executable dangerous call: {name}"


# --------------------------------------------------------------------------- 6. analyze_coverage
COV_SPEC = (
    "openapi: 3.0.3\ninfo: {title: T, version: '1'}\npaths:\n"
    "  /users: {post: {operationId: create_user, responses: {'201': {description: ok}}}}\n"
    "  /users/{id}: {get: {operationId: get_user, parameters: [{name: id, in: path, required: true, schema: {type: string}}], responses: {'200': {description: ok}}}}\n"
    "  /orders: {get: {operationId: list_orders, responses: {'200': {description: ok}}}}\n"
)


def _cov(invoke, code):
    r = invoke({"operation": "analyze_coverage", "input": {"openapi_spec": COV_SPEC, "existing_test_code": code}})
    assert r.status_code == 200
    return r.json()["output"]["covered_operations"]


def test_coverage_untested_get_is_not_covered(invoke):
    assert sorted(_cov(invoke, 'def test_a(c):\n    c.post("/users", json={})\n    c.get("/orders")\n')) == ["create_user", "list_orders"]


def test_coverage_ignores_comments_and_substring_names(invoke):
    assert _cov(invoke, "# TODO: GET /users/{id} and POST /users\n") == []
    assert "get_user" not in _cov(invoke, "def test_get_user_orders(c):\n    pass\n")


def test_coverage_understands_fstrings(invoke):
    assert "get_user" in _cov(invoke, 'def test_a(c, uid):\n    c.get(f"/users/{uid}")\n')


# --------------------------------------------------------------------------- 7. End-to-end: generated tests vs real servers
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, app_name: str):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "--app-dir", str(HERE), f"servers:{app_name}",
             "--port", str(self.port), "--log-level", "error"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/openapi.json", timeout=0.5).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("server did not start")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _run_generated(invoke, tmp_path, app_name, spec=SHOP_SPEC):
    srv = _Server(app_name)
    try:
        r = gen(invoke, spec, base_url=f"http://127.0.0.1:{srv.port}")
        assert r.status_code == 200
        f = tmp_path / "test_generated.py"
        f.write_text(r.json()["output"]["test_code"], encoding="utf-8")
        p = subprocess.run(
            [sys.executable, "-m", "pytest", str(f), "-p", "no:cacheprovider", "-q", "-rfs", "--no-header"],
            capture_output=True, text=True, cwd=tmp_path,
        )
        return p.returncode, p.stdout
    finally:
        srv.stop()


def _no_nested_schema(spec: str) -> str:
    i = spec.index("/users/{user_id}/orders/{order_id}:")
    tail = spec[i:]
    j = tail.index("responses:")
    return spec[:i] + tail[:j] + 'responses: {"200": {description: ok}}\n'


def test_odg_order_is_producer_first(invoke):
    out = gen(invoke, SHOP_SPEC).json()["output"]
    steps = [(s["method"], s["path"]) for s in out["odg_plan"]]
    assert steps.index(("POST", "/users")) < steps.index(("POST", "/orders")) < steps.index(("GET", "/orders/{id}"))
    assert steps[-1] == ("DELETE", "/users/{id}")


def test_generated_tests_pass_on_correct_server(invoke, tmp_path):
    rc, out = _run_generated(invoke, tmp_path, "good_app")
    assert rc == 0, out


def test_generated_tests_catch_cross_user_leak_with_schema(invoke, tmp_path):
    rc, out = _run_generated(invoke, tmp_path, "bola_app")
    assert rc != 0 and "get_user_order_positive" in out, out


def test_generated_tests_catch_cross_user_leak_without_response_schema(invoke, tmp_path):
    rc, out = _run_generated(invoke, tmp_path, "bola_app", spec=_no_nested_schema(SHOP_SPEC))
    assert rc != 0 and "get_user_order_positive" in out, out


def test_generated_tests_catch_missing_required_response_field(invoke, tmp_path):
    rc, out = _run_generated(invoke, tmp_path, "type_app")
    assert rc != 0 and "create_user_positive" in out, out


def test_dependent_tests_skip_when_producer_is_down(invoke, tmp_path):
    rc, out = _run_generated(invoke, tmp_path, "down_app")
    assert "create_user_positive" in out and "skipped" in out, out
    assert " failed" in out and "1 failed" in out, out


# --------------------------------------------------------------------------- 8. LLM path (fake OpenAI-compatible server)
class _FakeLLM:
    mode = "ok"
    delay = 0.0

    def __init__(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                req = json.loads(self.rfile.read(n))
                time.sleep(outer.delay)
                m = outer.mode
                if m == "500":
                    self.send_response(500)
                    self.end_headers()
                    return
                content = {
                    "ok": json.dumps({"create_x": {"valid_payload": {"a": "FROM_LLM"}, "boundary_payloads": [{}], "security_payloads": [{"a": "x'--"}]}}),
                    "pan": json.dumps({"create_x": {"valid_payload": {"a": "4532015112830366"}}}),
                    "tckn": json.dumps({"create_x": {"valid_payload": {"a": "10000000146"}}}),
                    "echo_prompt": req["messages"][0]["content"],
                    "echo_secret": json.dumps({"create_x": {"valid_payload": {"a": SECRET}}}),
                    "garbage": "sorry I cannot help",
                }[m]
                body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "fast",
                                   "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture()
def fake_llm(monkeypatch):
    f = _FakeLLM()
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{f.port}/v1")
    yield f
    f.srv.shutdown()


LLM_SPEC = spec_with_body({"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}})


def test_llm_enrichment_is_used_when_valid(invoke, fake_llm):
    blob = json.dumps(gen(invoke, LLM_SPEC).json()["output"])
    assert "FROM_LLM" in blob


@pytest.mark.parametrize("mode", ["pan", "tckn", "echo_prompt", "echo_secret", "garbage", "500"])
def test_llm_misbehaviour_falls_back_without_leak(invoke, fake_llm, mode):
    fake_llm.mode = mode
    r = gen(invoke, LLM_SPEC)
    assert r.status_code == 200
    out = r.json()["output"]
    blob = json.dumps(out)
    assert out["summary"]["ast_valid"] is True
    for needle in ("4532015112830366", "10000000146", SECRET, "Sen bir API Test"):
        assert needle not in blob
    assert "FROM_LLM" not in blob


def test_slow_llm_respects_timeout_env(invoke, fake_llm, monkeypatch):
    fake_llm.delay = 0.6
    monkeypatch.setenv("LLM_TIMEOUT_S", "0.25")
    t0 = time.time()
    r = gen(invoke, LLM_SPEC)
    assert r.status_code == 200 and "FROM_LLM" not in json.dumps(r.json()["output"])
    assert time.time() - t0 < 5
    monkeypatch.delenv("LLM_TIMEOUT_S")
    assert "FROM_LLM" in json.dumps(gen(invoke, LLM_SPEC).json()["output"])
