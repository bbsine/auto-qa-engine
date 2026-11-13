"""Regression test suite for Round 3 audit findings [1] through [5]."""

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
from typing import Any

os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")

import pytest
import uvicorn
from fastapi.testclient import TestClient

from evals.fake_llm_server import FakeLLMServer
from src.generator import _get_llm_client
from src.main import app
from src.security import contains_prohibited_payment_data

client = TestClient(app, raise_server_exceptions=False)


def _signed(body: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode("utf-8")
    ts = str(int(time.time()))
    sig = hmac.new(b"test-secret", ts.encode("utf-8") + b"." + raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Platform-Timestamp": ts,
        "X-Platform-Signature": sig,
        "X-Tenant-Id": "ten_reg_r3",
        "X-Request-Id": "req_reg_r3_77",
        "Content-Type": "application/json",
    }


def _start_uvicorn_in_thread(asgi_app: Any) -> tuple[uvicorn.Server, threading.Thread, int]:
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


# ============================================================================
# [1] Uncaught 500s on slightly malformed / unexpected-type OpenAPI inputs & deep AST
# ============================================================================
@pytest.mark.parametrize(
    "prop_schema",
    [
        {"type": "string", "minLength": "2", "maxLength": "10"},
        {"type": "string", "minLength": None, "maxLength": None},
        {"type": "integer", "minimum": "0", "maximum": "100"},
        {"type": "integer", "minimum": None, "maximum": None},
        {"type": "number", "minimum": "1.5", "maximum": None},
        {"type": "string", "minLength": True, "maxLength": False},
    ],
)
@pytest.mark.parametrize("operation", ["generate_tests", "generate_mocks"])
def test_issue_1_schema_constraint_types_do_not_500(prop_schema: dict[str, Any], operation: str) -> None:
    """[1a/1b/1c] String, None, or bool values in minLength/maxLength/minimum/maximum must return 200 (never 500)."""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Constraint Tolerance API", "version": "1.0.0"},
        "paths": {
            "/items": {
                "post": {
                    "operationId": "create_item",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["val"],
                                    "properties": {"val": prop_schema},
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            }
        },
    }
    raw, headers = _signed({"operation": operation, "input": {"openapi_spec": spec}})
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 200, f"Expected 200 for {operation} with {prop_schema}, got {resp.status_code}: {resp.text}"


def test_issue_1_deep_ast_existing_test_code_returns_422() -> None:
    """[1d] Deeply nested AST expression in analyze_coverage existing_test_code must return 422 invalid_existing_test_code, never 500."""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Deep AST API", "version": "1.0.0"},
        "paths": {
            "/a": {
                "get": {
                    "operationId": "get_a",
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    deep_code = 'c.get("/a"' + ' + "/a"' * 3000 + ")"
    raw, headers = _signed(
        {
            "operation": "analyze_coverage",
            "input": {
                "openapi_spec": spec,
                "existing_test_code": deep_code,
            },
        }
    )
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    err = resp.json()["error"]
    assert err["code"] == "invalid_existing_test_code"


def _build_deep_nested_schema(depth: int = 60) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "string"}
    for _ in range(depth):
        node = {"type": "object", "properties": {"child": node}}
    return node


@pytest.mark.parametrize(
    "fuzz_spec",
    [
        # 1. properties as a list instead of dict
        {
            "openapi": "3.0.3",
            "paths": {
                "/p1": {
                    "post": {
                        "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": ["a", "b"]}}}},
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
        # 2. items as a list instead of dict
        {
            "openapi": "3.0.3",
            "paths": {
                "/p2": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"arr": {"type": "array", "items": [{"type": "string"}]}},
                                    }
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
        # 3. OpenAPI 3.1 style type as a list: ["string", "null"]
        {
            "openapi": "3.1.0",
            "paths": {
                "/p3": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": ["object", "null"],
                                        "required": ["name", "age"],
                                        "properties": {
                                            "name": {"type": ["string", "null"], "minLength": "3"},
                                            "age": {"type": ["integer", "null"], "minimum": "18"},
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "required": ["name", "age"],
                                            "properties": {
                                                "name": {"type": ["string", "null"]},
                                                "age": {"type": ["integer", "null"]},
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
        },
        # 4. enum as a string instead of list
        {
            "openapi": "3.0.3",
            "paths": {
                "/p4": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"status": {"type": "string", "enum": "ACTIVE"}},
                                    }
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
        # 5. required as a string instead of list
        {
            "openapi": "3.0.3",
            "paths": {
                "/p5": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": "name",
                                        "properties": {"name": {"type": "string"}},
                                    }
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
        # 6. deeply nested object (depth 60)
        {
            "openapi": "3.0.3",
            "paths": {
                "/p6": {
                    "post": {
                        "requestBody": {"content": {"application/json": {"schema": _build_deep_nested_schema(60)}}},
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
        # 7. allOf/oneOf containing non-dict primitives
        {
            "openapi": "3.0.3",
            "paths": {
                "/p7": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"allOf": ["not_a_dict", 123, {"properties": {"x": {"type": "string"}}, "required": [None, 42, "x"]}]}
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
        # 8. parameter with name=None, in=123, schema as list
        {
            "openapi": "3.0.3",
            "paths": {
                "/p8/{id}": {
                    "get": {
                        "parameters": [
                            {"name": None, "in": "query", "schema": ["bad"]},
                            {"name": "q", "in": 123, "schema": ["bad"]},
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
        # 9. responses with integer status keys and non-dict media objects
        {
            "openapi": "3.0.3",
            "paths": {
                "/p9": {
                    "get": {
                        "responses": {
                            200: {"content": {"application/json": "not_a_dict"}},
                        }
                    }
                }
            },
        },
        # 10. servers containing non-dicts and security containing weird shapes
        {
            "openapi": "3.0.3",
            "servers": ["http://localhost:8000", {"url": None}, 42],
            "security": "invalid_sec",
            "paths": {
                "/p10": {
                    "get": {
                        "operationId": 99999,
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
    ],
)
def test_issue_1_malformed_openapi_fuzz_shapes_never_500(fuzz_spec: dict[str, Any]) -> None:
    """[1] Parametrized fuzz test: 10 malformed/weird OpenAPI shapes must return 200 or 422, NEVER 500."""
    for op in ("generate_tests", "generate_mocks", "analyze_coverage"):
        raw, headers = _signed({"operation": op, "input": {"openapi_spec": fuzz_spec}})
        resp = client.post("/v1/invoke", content=raw, headers=headers)
        assert resp.status_code in (200, 422), f"Op {op} returned {resp.status_code} on fuzz spec: {resp.text}"


# ============================================================================
# [2] Sensitive-data filter bypass via YAML escapes & multi-line string folding
# ============================================================================
@pytest.mark.parametrize(
    "yaml_spec",
    [
        # (a) 8-hex \U escape in YAML single-quoted / raw string or JSON string
        "openapi: '3.0.3'\ninfo:\n  title: 'T'\n  description: '4532\\U00000030151 1283 0366'\n  version: '1.0'\npaths:\n  /a:\n    get:\n      responses:\n        '200': {description: 'ok'}\n",
        # (b) multi-line literal block scalar in YAML preserving newline between PAN groups
        'openapi: "3.0.3"\ninfo:\n  title: "T"\n  description: |\n    4532 0151\n    1283 0366\n  version: "1.0"\npaths:\n  /a:\n    get:\n      responses:\n        "200": {description: "ok"}\n',
        # (c) escaped line break inside single-quoted YAML string / raw text
        "openapi: '3.0.3'\ninfo:\n  title: 'T'\n  description: '45320151\\\n        12830366'\n  version: '1.0'\npaths:\n  /a:\n    get:\n      responses:\n        '200': {description: 'ok'}\n",
        # (d) plain scalar folding in YAML
        'openapi: "3.0.3"\ninfo:\n  title: "T"\n  description: 4532 0151\n        1283 0366\n  version: "1.0"\npaths:\n  /a:\n    get:\n      responses:\n        "200": {description: "ok"}\n',
        # (e) 4-hex \u escape
        'openapi: "3.0.3"\ninfo:\n  title: "T"\n  description: "4532\\u0030151 1283 0366"\n  version: "1.0"\npaths:\n  /a:\n    get:\n      responses:\n        "200": {description: "ok"}\n',
        # (f) plain unescaped PAN
        'openapi: "3.0.3"\ninfo:\n  title: "T"\n  description: "4532 0151 1283 0366"\n  version: "1.0"\npaths:\n  /a:\n    get:\n      responses:\n        "200": {description: "ok"}\n',
    ],
)
@pytest.mark.parametrize("operation", ["generate_tests", "generate_mocks", "analyze_coverage"])
def test_issue_2_yaml_escaped_and_folded_pan_variants_blocked(yaml_spec: str, operation: str) -> None:
    """[2a..2d] All 6 YAML PAN variants must be caught both by contains_prohibited_payment_data and return 422 across all 3 operations."""
    assert contains_prohibited_payment_data(yaml_spec) is True
    raw, headers = _signed({"operation": operation, "input": {"openapi_spec": yaml_spec}})
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 422, f"Expected 422 for {operation}, got {resp.status_code}: {resp.text}"
    assert resp.json()["error"]["code"] == "prohibited_sensitive_data"


def test_issue_2_analyze_coverage_existing_test_code_pan_blocked() -> None:
    """[2] PAN inside analyze_coverage existing_test_code (multiline or string literal) must return 422."""
    clean_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Clean", "version": "1.0.0"},
        "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    test_code_with_multiline_pan = 'def test_card(c):\n    card = """4532 0151\n    1283 0366"""\n    c.get("/ping")\n'
    raw, headers = _signed(
        {
            "operation": "analyze_coverage",
            "input": {
                "openapi_spec": clean_spec,
                "existing_test_code": test_code_with_multiline_pan,
            },
        }
    )
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "prohibited_sensitive_data"

    # Also verify legitimate 16-digit non-IIN order ID and spaced TR IBAN still pass
    assert contains_prohibited_payment_data("order_id: 8000123456789012") is False
    assert contains_prohibited_payment_data("iban: TR33 0006 1005 1978 6457 8413 26") is False


# ============================================================================
# [3] Default LLM timeout 12.0s + observability logging
# ============================================================================
def test_issue_3_default_llm_timeout_and_slow_server_enrichment_and_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[3] Default LLM_TIMEOUT_S must be 12.0s when unset; 0.6s server is accepted by default and times out at 0.25s."""
    monkeypatch.delenv("LLM_TIMEOUT_S", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    llm_client = _get_llm_client()
    assert llm_client is not None
    assert llm_client.timeout == 12.0

    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Slow LLM Test API", "version": "1.0.0"},
        "paths": {
            "/items": {
                "post": {
                    "operationId": "create_item",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {"name": {"type": "string"}},
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            }
        },
    }

    with FakeLLMServer(mode="slow_timeout") as fake_llm:
        monkeypatch.setenv("LLM_BASE_URL", fake_llm.base_url)
        monkeypatch.setenv("LLM_API_KEY", "fake-key")

        # 1. With LLM_TIMEOUT_S unset (default 12.0s), the 0.6s response from slow_timeout MUST be accepted!
        monkeypatch.delenv("LLM_TIMEOUT_S", raising=False)
        raw, headers = _signed({"operation": "generate_tests", "input": {"openapi_spec": spec}})
        with caplog.at_level(logging.INFO):
            resp_ok = client.post("/v1/invoke", content=raw, headers=headers)
        assert resp_ok.status_code == 200
        out_ok = resp_ok.json()["output"]
        assert out_ok["mock_datasets"]["create_item"]["valid_payload"]["name"] == "LLM Sentezlenen Ürün"
        assert any("llm_enrichment=applied" in rec.getMessage() and "req_reg_r3_77" in rec.getMessage() for rec in caplog.records)

        # 2. With LLM_TIMEOUT_S="0.25", the 0.6s server MUST time out and fall back to deterministic mocks!
        caplog.clear()
        monkeypatch.setenv("LLM_TIMEOUT_S", "0.25")
        raw2, headers2 = _signed({"operation": "generate_tests", "input": {"openapi_spec": spec}})
        with caplog.at_level(logging.INFO):
            resp_fb = client.post("/v1/invoke", content=raw2, headers=headers2)
        assert resp_fb.status_code == 200
        out_fb = resp_fb.json()["output"]
        assert out_fb["mock_datasets"]["create_item"]["valid_payload"]["name"] != "LLM Sentezlenen Ürün"
        assert any("llm_enrichment=fallback_timeout" in rec.getMessage() and "req_reg_r3_77" in rec.getMessage() for rec in caplog.records)


# ============================================================================
# [4] Ownership assertion emitted even when 200 response schema is omitted
# ============================================================================
SCHEMALESS_NESTED_SPEC = """
openapi: "3.0.3"
info:
  title: "Schemaless Nested Order API"
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
        "201": {description: "Created"}
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
        "201": {description: "Created"}
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
          description: "OK without response schema"
"""


def test_issue_4_schemaless_get_user_order_catches_bola_e2e(tmp_path: Path) -> None:
    """[4] Even when GET /users/{user_id}/orders/{order_id} has NO response schema, generated test must catch user_id mismatch."""
    from tests.e2e.servers import broken_app, correct_app

    srv_ok, th_ok, port_ok = _start_uvicorn_in_thread(correct_app)
    srv_bad, th_bad, port_bad = _start_uvicorn_in_thread(broken_app)

    try:
        # 1. Against correct_app -> PASSES
        raw_ok, h_ok = _signed(
            {
                "operation": "generate_tests",
                "input": {
                    "openapi_spec": SCHEMALESS_NESTED_SPEC,
                    "base_url": f"http://127.0.0.1:{port_ok}",
                },
            }
        )
        code_ok = client.post("/v1/invoke", content=raw_ok, headers=h_ok).json()["output"]["test_code"]
        f_ok = tmp_path / "test_schemaless_ok.py"
        f_ok.write_text(code_ok, encoding="utf-8")
        proc_ok = subprocess.run(
            [sys.executable, "-m", "pytest", str(f_ok), "-q"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc_ok.returncode == 0, f"Expected pass on correct_app:\n{proc_ok.stdout}\n{proc_ok.stderr}"

        # 2. Against broken_app (returns user_id='SOMEONE_ELSE') -> MUST FAIL!
        raw_bad, h_bad = _signed(
            {
                "operation": "generate_tests",
                "input": {
                    "openapi_spec": SCHEMALESS_NESTED_SPEC,
                    "base_url": f"http://127.0.0.1:{port_bad}",
                },
            }
        )
        code_bad = client.post("/v1/invoke", content=raw_bad, headers=h_bad).json()["output"]["test_code"]
        f_bad = tmp_path / "test_schemaless_bad.py"
        f_bad.write_text(code_bad, encoding="utf-8")
        proc_bad = subprocess.run(
            [sys.executable, "-m", "pytest", str(f_bad), "-k", "get_user_order_positive", "-q"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc_bad.returncode != 0, (
            f"Generated schemaless test MUST fail when server returns user_id='SOMEONE_ELSE':\n{code_bad}"
        )
    finally:
        srv_ok.should_exit = True
        srv_bad.should_exit = True
        th_ok.join(timeout=3)
        th_bad.join(timeout=3)


# ============================================================================
# [5] Consumer tests skip cleanly when producer step fails to capture state
# ============================================================================
def test_issue_5_producer_500_skips_dependent_consumers_with_clear_message(tmp_path: Path) -> None:
    """[5] When POST /users returns 500, create_user_positive fails, dependent consumers SKIP, and unknown_id_and_auth still passes."""
    from tests.e2e.servers import producer_500_app

    srv_500, th_500, port_500 = _start_uvicorn_in_thread(producer_500_app)
    try:
        raw, headers = _signed(
            {
                "operation": "generate_tests",
                "input": {
                    "openapi_spec": SCHEMALESS_NESTED_SPEC,
                    "base_url": f"http://127.0.0.1:{port_500}",
                },
            }
        )
        resp = client.post("/v1/invoke", content=raw, headers=headers)
        assert resp.status_code == 200
        code = resp.json()["output"]["test_code"]

        f_500 = tmp_path / "test_producer_500.py"
        f_500.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(f_500), "-v", "-rs"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout = proc.stdout
        assert "test_step_01_create_user_positive FAILED" in stdout
        assert "test_step_02_create_order_positive SKIPPED" in stdout
        assert "test_step_03_get_user_order_positive SKIPPED" in stdout
        assert "Producer step did not capture: user_id" in stdout
        assert "test_step_03_get_user_order_security_unknown_id_and_auth PASSED" in stdout
    finally:
        srv_500.should_exit = True
        th_500.join(timeout=3)
