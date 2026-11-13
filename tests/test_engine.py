"""Unit tests for the Phase 2 core hybrid generator engine (parser, ODG planner, AST validator, and generator)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999/v1")
os.environ.setdefault("LLM_API_KEY", "test")

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.parser import parse_openapi_spec
from src.planner import build_odg_plan
from src.validator import (
    build_deterministic_pytest_suite,
    count_ast_assertions,
    strip_markdown_fences,
    validate_test_syntax,
)

client = TestClient(app)

SAMPLE_ECOMMERCE_YAML = """
openapi: "3.0.3"
info:
  title: "E-Ticaret Sipariş API"
  version: "1.1.0"
paths:
  /items/{id}:
    delete:
      operationId: delete_item
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        "204":
          description: "Silindi"
    get:
      operationId: get_item
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: "Detay"
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Item"
    put:
      operationId: update_item
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/ItemCreate"
      responses:
        "200":
          description: "Güncellendi"
  /items:
    post:
      operationId: create_item
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/ItemCreate"
      responses:
        "201":
          description: "Oluşturuldu"
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Item"
  /orders:
    post:
      operationId: create_order
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [item_id, quantity]
              properties:
                item_id:
                  type: string
                quantity:
                  type: integer
                  minimum: 1
      responses:
        "201":
          description: "Sipariş oluşturuldu"
components:
  schemas:
    ItemCreate:
      type: object
      required: [title, price]
      properties:
        title:
          type: string
          minLength: 3
        price:
          type: number
          minimum: 1.0
    Item:
      type: object
      required: [id, title, price]
      properties:
        id:
          type: string
        title:
          type: string
        price:
          type: number
"""


def _signed(body: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode("utf-8")
    ts = str(int(time.time()))
    sig = hmac.new(b"test-secret", ts.encode("utf-8") + b"." + raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Platform-Timestamp": ts,
        "X-Platform-Signature": sig,
        "X-Tenant-Id": "ten_engine_test",
        "X-Request-Id": "req_engine_01",
        "Content-Type": "application/json",
    }


def test_parser_resolves_refs_and_extracts_operations() -> None:
    """Parser must deterministically resolve $ref schemas and extract all operations and parameters."""
    parsed = parse_openapi_spec(SAMPLE_ECOMMERCE_YAML)
    assert parsed.title == "E-Ticaret Sipariş API"
    assert len(parsed.operations) == 5
    assert set(parsed.paths) == {"/items/{id}", "/items", "/orders"}

    create_op = next(op for op in parsed.operations if op.operation_id == "create_item")
    assert create_op.method == "POST"
    assert create_op.request_body_required is True
    assert create_op.request_body_schema is not None
    assert "title" in create_op.request_body_schema["properties"]
    assert "price" in create_op.request_body_schema["properties"]

    get_op = next(op for op in parsed.operations if op.operation_id == "get_item")
    assert any(p.name == "id" and p.location == "path" and p.required for p in get_op.parameters)


def test_parser_rejects_empty_or_invalid_specs() -> None:
    """Parser must raise ValueError on empty or malformed OpenAPI input."""
    with pytest.raises(ValueError, match="empty_openapi_spec"):
        parse_openapi_spec("   ")

    with pytest.raises(ValueError, match="missing_paths"):
        parse_openapi_spec('{"openapi": "3.0.0", "info": {"title": "No Paths"}}')


def test_odg_planner_topological_ordering() -> None:
    """Planner must order producer POST /items before consumers GET/PUT/DELETE /items/{id} and POST /orders."""
    parsed = parse_openapi_spec(SAMPLE_ECOMMERCE_YAML)
    plan = build_odg_plan(parsed)
    ordered_ids = [step.operation.operation_id for step in plan]

    assert len(ordered_ids) == 5
    idx_create_item = ordered_ids.index("create_item")
    idx_get_item = ordered_ids.index("get_item")
    idx_update_item = ordered_ids.index("update_item")
    idx_delete_item = ordered_ids.index("delete_item")
    idx_create_order = ordered_ids.index("create_order")

    # Producer POST /items must come first before any item consumer
    assert idx_create_item < idx_get_item
    assert idx_create_item < idx_update_item
    assert idx_create_item < idx_delete_item
    assert idx_create_item < idx_create_order

    # GET and PUT must run before DELETE teardown
    assert idx_get_item < idx_delete_item
    assert idx_update_item < idx_delete_item

    # Check variable bindings
    create_step = plan[idx_create_item]
    assert "item_id" in create_step.produces
    get_step = plan[idx_get_item]
    assert "item_id" in get_step.consumes


def test_validator_strips_markdown_fences_and_validates_ast() -> None:
    """Validator must strip ```python fences and validate AST syntax accurately."""
    fenced_valid = "```python\ndef test_ok():\n    x = 1 + 1\n    assert x == 2\n```"
    cleaned = strip_markdown_fences(fenced_valid)
    assert not cleaned.startswith("```")

    is_ok, err = validate_test_syntax(fenced_valid)
    assert is_ok is True
    assert err is None
    assert count_ast_assertions(fenced_valid) == 1


def test_validator_catches_syntax_and_security_errors() -> None:
    """Validator must reject broken Python syntax and forbidden unsafe modules/calls."""
    broken_code = "def test_broken(\n    assert 1 == 1"
    is_ok, err = validate_test_syntax(broken_code)
    assert is_ok is False
    assert err is not None
    assert "SyntaxError" in err

    unsafe_code = "import subprocess\ndef test_hack():\n    subprocess.run(['ls'])\n    assert True\n"
    is_safe, sec_err = validate_test_syntax(unsafe_code)
    assert is_safe is False
    assert sec_err is not None
    assert "SecurityError" in sec_err


def test_end_to_end_generate_tests_invoke() -> None:
    """POST /v1/invoke with operation=generate_tests must return ODG-ordered, AST-valid Pytest code and mocks."""
    raw, headers = _signed(
        {
            "operation": "generate_tests",
            "input": {
                "openapi_spec": SAMPLE_ECOMMERCE_YAML,
                "base_url": "https://api.shop.example.com",
                "include_negative_tests": True,
                "include_security_tests": True,
            },
            "config": {"default_locale": "tr-TR"},
            "trigger": {"kind": "ci"},
        }
    )
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"

    out = body["output"]
    assert out["summary"]["ast_valid"] is True
    assert out["summary"]["total_operations"] == 5
    assert out["total_assertions"] >= 5
    assert set(out["endpoints_covered"]) == {"/items/{id}", "/items", "/orders"}

    is_valid, err = validate_test_syntax(out["test_code"])
    assert is_valid is True, f"Generated test code failed AST validation: {err}"
    assert "create_item" in out["mock_data"]
    assert out["mock_data"]["create_item"]["valid_payload"] is not None


def test_ssrf_and_sensitive_payment_data_blocked() -> None:
    """SSRF internal/metadata URLs and §4.3 credit card numbers must be rejected with HTTP 422."""
    # 1. Cloud metadata SSRF attempt
    raw_ssrf, h_ssrf = _signed(
        {
            "operation": "generate_tests",
            "input": {
                "openapi_spec": SAMPLE_ECOMMERCE_YAML,
                "base_url": "http://169.254.169.254/latest/meta-data/",
            },
        }
    )
    resp_ssrf = client.post("/v1/invoke", content=raw_ssrf, headers=h_ssrf)
    assert resp_ssrf.status_code == 422
    assert resp_ssrf.json()["error"]["code"] == "ssrf_blocked"

    # 2. Credit card / CVV red-line check (§4.3)
    raw_pan, h_pan = _signed(
        {
            "operation": "generate_tests",
            "input": {
                "openapi_spec": SAMPLE_ECOMMERCE_YAML,
                "card_note": "cvv: 456",
            },
        }
    )
    resp_pan = client.post("/v1/invoke", content=raw_pan, headers=h_pan)
    assert resp_pan.status_code == 422
    assert resp_pan.json()["error"]["code"] == "prohibited_sensitive_data"


def test_audit_fix_1_path_code_injection_and_docstring_escape() -> None:
    """Malicious OpenAPI path keys containing f-string/open() injection or triple quotes must be safely escaped."""
    malicious_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Malicious Path Spec", "version": "1.0.0"},
        "paths": {
            '/x" + open("/etc/hostname").read() + "': {
                "get": {
                    "operationId": "evil_open",
                    "responses": {"200": {"description": "OK"}},
                }
            },
            '/quote"""escape': {
                "get": {
                    "operationId": "evil_docstring",
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
    }
    raw, headers = _signed(
        {
            "operation": "generate_tests",
            "input": {"openapi_spec": json.dumps(malicious_spec)},
        }
    )
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 200
    out = resp.json()["output"]
    assert out["summary"]["ast_valid"] is True
    is_valid, err = validate_test_syntax(out["test_code"])
    assert is_valid is True, f"AST validation should pass on escaped literal path: {err}"


def test_audit_fix_2_multi_param_path_binding() -> None:
    """Routes like /users/{user_id}/orders/{order_id} must bind each placeholder to its own distinct state variable."""
    multi_param_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Multi Param API", "version": "1.0.0"},
        "paths": {
            "/users/{user_id}/orders/{order_id}": {
                "get": {
                    "operationId": "get_user_order",
                    "parameters": [
                        {"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "order_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    parsed = parse_openapi_spec(multi_param_spec)
    plan = build_odg_plan(parsed)
    step = plan[0]
    assert step.param_bindings == {"user_id": "user_id", "order_id": "order_id"}
    assert "user_id" in step.consumes and "order_id" in step.consumes


def test_audit_fix_3_localhost_base_url_allowed() -> None:
    """Default base_url http://localhost:8000 must be allowed for local/CI test generation."""
    raw, headers = _signed(
        {
            "operation": "generate_tests",
            "input": {
                "openapi_spec": SAMPLE_ECOMMERCE_YAML,
                "base_url": "http://localhost:8000",
            },
        }
    )
    resp = client.post("/v1/invoke", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_audit_fix_4_zero_500_on_malformed_inputs_and_non_ascii_sig() -> None:
    """Non-ASCII signature headers (401), list operation names (422), nested target_endpoints (422), and null config (200) must never 500."""
    # Non-ASCII signature bytes (decoded as latin-1 non-ASCII str by ASGI/Starlette) -> 401
    raw, headers = _signed({"operation": "generate_tests", "input": {"openapi_spec": SAMPLE_ECOMMERCE_YAML}})
    headers_with_bytes = {**headers, "X-Platform-Signature": b"\xe9\xe012345"}
    assert client.post("/v1/invoke", content=raw, headers=headers_with_bytes).status_code == 401

    # Operation as a list -> 422
    raw_list_op, h_list_op = _signed({"operation": ["generate_tests"], "input": {}})
    assert client.post("/v1/invoke", content=raw_list_op, headers=h_list_op).status_code == 422

    # Unhashable target_endpoints: [[1]] -> 422
    raw_te, h_te = _signed(
        {
            "operation": "generate_tests",
            "input": {"openapi_spec": SAMPLE_ECOMMERCE_YAML, "target_endpoints": [[1]]},
        }
    )
    assert client.post("/v1/invoke", content=raw_te, headers=h_te).status_code == 422

    # min_coverage_pct: None -> 200
    raw_cov, h_cov = _signed(
        {
            "operation": "analyze_coverage",
            "input": {"openapi_spec": SAMPLE_ECOMMERCE_YAML},
            "config": {"min_coverage_pct": None},
        }
    )
    assert client.post("/v1/invoke", content=raw_cov, headers=h_cov).status_code == 200


def test_audit_fix_5_tckn_dot_pan_and_json_cvv_blocked() -> None:
    """Valid TCKN (10000000146), dot-separated PANs, and JSON 'cvv': 123 must be blocked with 422."""
    for sensitive_snippet in (
        "tckn: 10000000146",
        "4532.0151.1283.0366",
        '{"cvv": 123}',
        '{"security_code": "987"}',
    ):
        raw, headers = _signed(
            {
                "operation": "generate_tests",
                "input": {
                    "openapi_spec": SAMPLE_ECOMMERCE_YAML,
                    "note": sensitive_snippet,
                },
            }
        )
        resp = client.post("/v1/invoke", content=raw, headers=headers)
        assert resp.status_code == 422, f"Expected 422 for {sensitive_snippet}"
        assert resp.json()["error"]["code"] == "prohibited_sensitive_data"

