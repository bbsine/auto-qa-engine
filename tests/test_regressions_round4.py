"""Regression tests for Round 4 audit fixes and PDF v1.1 compliance improvements."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")
os.environ.setdefault("LLM_BASE_URL", "")
os.environ.setdefault("LLM_API_KEY", "test")

from src import main as main_mod
from src.generator import (
    analyze_coverage_pipeline,
    generate_test_suite_pipeline,
)
from src.main import app
from src.parser import parse_openapi_spec
from src.planner import build_odg_plan
from src.security import contains_prohibited_payment_data


class _ItemIn(BaseModel):
    name: str = Field(..., min_length=1)


class _OrderIn(BaseModel):
    qty: int | None = None


def _sign(raw: bytes, secret: str = "test-secret") -> dict[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return {
        "X-Platform-Timestamp": ts,
        "X-Platform-Signature": sig,
        "X-Tenant-Id": "ten_r4",
        "X-Request-Id": "req_r4",
        "Content-Type": "application/json",
    }


def _invoke(client: TestClient, payload: dict[str, Any]) -> Any:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return client.post("/v1/invoke", content=raw, headers=_sign(raw))


class _UvicornThread:
    """Minimal in-process uvicorn runner on a free ephemeral port."""

    def __init__(self, target_app: FastAPI) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self._config = uvicorn.Config(target_app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        deadline = time.time() + 10.0
        while not self._server.started and time.time() < deadline:
            time.sleep(0.05)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *args: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5.0)


def _run_generated_pytest(code: str, tmp_path: Path, name: str) -> subprocess.CompletedProcess[str]:
    test_file = tmp_path / f"test_{name}.py"
    test_file.write_text(code, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-v", "--tb=short"],
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# 1. Standard {"error": {"code", "message"}} envelope on 401 / 404 / 405
# ---------------------------------------------------------------------------
def test_http_errors_401_404_405_use_standard_error_envelope() -> None:
    client = TestClient(app, raise_server_exceptions=False)

    # 401 unauthorized (missing/bad signature)
    r401 = client.post("/v1/invoke", json={"operation": "generate_tests", "input": {}})
    assert r401.status_code == 401
    b401 = r401.json()
    assert "detail" not in b401
    assert isinstance(b401.get("error"), dict)
    assert isinstance(b401["error"].get("code"), str) and b401["error"]["code"]
    assert isinstance(b401["error"].get("message"), str) and b401["error"]["message"]

    # 404 unknown route
    r404 = client.get("/nonexistent-endpoint")
    assert r404.status_code == 404
    b404 = r404.json()
    assert "detail" not in b404
    assert b404["error"]["code"] == "not_found"
    assert b404["error"]["message"]

    # 405 wrong method
    r405 = client.get("/v1/invoke")
    assert r405.status_code == 405
    b405 = r405.json()
    assert "detail" not in b405
    assert b405["error"]["code"] == "method_not_allowed"
    assert b405["error"]["message"]


# ---------------------------------------------------------------------------
# 2. Real pytest-requests framework support + E2E execution + coverage analysis
# ---------------------------------------------------------------------------
def test_pytest_requests_framework_generates_runs_e2e_and_analyzes_coverage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "")
    from tests.e2e.form_servers import form_correct_app
    from tests.e2e.servers import broken_app, correct_app

    crud_spec = """
openapi: "3.0.3"
info: {title: "Users & Orders CRUD", version: "1.0"}
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
          description: Created
          content:
            application/json:
              schema:
                type: object
                required: [id, name]
                properties:
                  id: {type: string}
                  name: {type: string}
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
          description: Created
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
        - {name: user_id, in: path, required: true, schema: {type: string}}
        - {name: order_id, in: path, required: true, schema: {type: string}}
      responses:
        "200":
          description: OK
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
    with _UvicornThread(correct_app) as url_ok:
        out_ok, status, _ = generate_test_suite_pipeline(
            {"openapi_spec": crud_spec, "base_url": url_ok},
            {"test_framework": "pytest-requests"},
        )
        assert status == "ok"
        code_ok = out_ok["test_code"]
        assert "import requests" in code_ok
        assert "import httpx" not in code_ok
        assert "_BaseUrlSession" in code_ok

        res_ok = _run_generated_pytest(code_ok, tmp_path, "req_ok")
        assert res_ok.returncode == 0, f"pytest-requests E2E failed:\n{res_ok.stdout}\n{res_ok.stderr}"

        # Generated pytest-requests code must also be recognized 100% by analyze_coverage
        cov_out, cov_status, _ = analyze_coverage_pipeline(
            {"openapi_spec": crud_spec, "existing_test_code": code_ok},
            {"min_coverage_pct": 100},
        )
        assert cov_status == "ok"
        assert cov_out["coverage_pct"] == 100.0

    with _UvicornThread(broken_app) as url_bad:
        out_bad, _, _ = generate_test_suite_pipeline(
            {"openapi_spec": crud_spec, "base_url": url_bad},
            {"test_framework": "pytest-requests"},
        )
        res_bad = _run_generated_pytest(out_bad["test_code"], tmp_path, "req_bad")
        assert res_bad.returncode != 0

    # Also verify multipart/form-data + urlencoded with pytest-requests against real FastAPI
    form_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Form API", "version": "1.0"},
        "paths": {
            "/login": {
                "post": {
                    "operationId": "login_user",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/x-www-form-urlencoded": {
                                "schema": {
                                    "type": "object",
                                    "required": ["username", "password"],
                                    "properties": {
                                        "username": {"type": "string", "minLength": 3},
                                        "password": {"type": "string", "minLength": 3},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/documents": {
                "post": {
                    "operationId": "upload_doc",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "required": ["title", "file"],
                                    "properties": {
                                        "title": {"type": "string", "minLength": 2},
                                        "file": {"type": "string", "format": "binary"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            },
        },
    }
    with _UvicornThread(form_correct_app) as form_url:
        form_out, form_status, _ = generate_test_suite_pipeline(
            {"openapi_spec": json.dumps(form_spec), "base_url": form_url},
            {"test_framework": "pytest-requests"},
        )
        assert form_status == "ok"
        res_form = _run_generated_pytest(form_out["test_code"], tmp_path, "req_form")
        assert res_form.returncode == 0, f"pytest-requests form E2E failed:\n{res_form.stdout}"


# ---------------------------------------------------------------------------
# 3. Unauthenticated query-only GET, optional integer, id=0, Content-Type case
# ---------------------------------------------------------------------------
def test_e2e_query_get_optional_int_id_zero_and_content_type_case(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "")
    api = FastAPI()

    @api.get("/items")
    def search_items(q: str | None = None):
        return JSONResponse([{"id": 0, "name": q or "default"}], media_type="Application/JSON")

    @api.post("/items", status_code=201)
    def create_item(body: _ItemIn):
        return JSONResponse({"id": 0, "name": body.name}, status_code=201, media_type="Application/JSON")

    @api.get("/items/{id}")
    def get_item(id: int):
        return JSONResponse({"id": id, "name": "zero"}, media_type="Application/JSON")

    @api.post("/orders", status_code=201)
    def create_order(body: _OrderIn):
        return {"id": 1, "qty": body.qty}

    @api.post("/form-optional")
    def form_optional(grant_type: str | None = Form(None)):
        return {"grant_type": grant_type}

    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Edge API", "version": "1.0"},
        "paths": {
            "/items": {
                "get": {
                    "operationId": "search_items",
                    "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "OK"}},
                },
                "post": {
                    "operationId": "create_item",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {"name": {"type": "string", "minLength": 1}},
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {"id": {"type": "integer"}},
                                    }
                                }
                            },
                        }
                    },
                },
            },
            "/items/{id}": {
                "get": {
                    "operationId": "get_item",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/orders": {
                "post": {
                    "operationId": "create_order",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"qty": {"type": "integer"}},
                                }
                            }
                        }
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            },
            "/form-optional": {
                "post": {
                    "operationId": "form_optional",
                    "requestBody": {
                        "content": {
                            "application/x-www-form-urlencoded": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"grant_type": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
    }

    with _UvicornThread(api) as base_url:
        out, status, _ = generate_test_suite_pipeline({"openapi_spec": json.dumps(spec), "base_url": base_url}, {})
        assert status == "ok"
        code = out["test_code"]
        assert "search_items_security_unknown_id_and_auth" not in code
        assert out["mock_datasets"]["create_order"]["boundary_payloads"] == [{"qty": "invalid_number"}]
        assert out["mock_datasets"]["form_optional"]["boundary_payloads"] == []

        res = _run_generated_pytest(code, tmp_path, "edge_e2e")
        assert res.returncode == 0, f"Generated suite failed E2E:\n{res.stdout}\n{res.stderr}"
        assert "SKIPPED" not in res.stdout


# ---------------------------------------------------------------------------
# 4. Parser allOf + sibling properties, Planner child-before-parent DELETE, non-resource POST
# ---------------------------------------------------------------------------
def test_parser_allof_preserves_sibling_fields_and_planner_ordering() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Nested API", "version": "1.0"},
        "components": {
            "schemas": {
                "Base": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                }
            }
        },
        "paths": {
            "/auth/login": {
                "post": {
                    "operationId": "login",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["username"],
                                    "properties": {"username": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"access_token": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/users": {
                "post": {
                    "operationId": "create_user",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "properties": {"extra": {"type": "string"}},
                                    "required": ["extra"],
                                    "allOf": [{"$ref": "#/components/schemas/Base"}],
                                }
                            }
                        }
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            },
            "/users/{id}": {
                "delete": {
                    "operationId": "delete_user",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"204": {"description": "Deleted"}},
                }
            },
            "/users/{user_id}/orders": {
                "post": {
                    "operationId": "create_user_order",
                    "parameters": [{"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"201": {"description": "Created"}},
                }
            },
            "/users/{user_id}/orders/{order_id}": {
                "delete": {
                    "operationId": "delete_user_order",
                    "parameters": [
                        {"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "order_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {"204": {"description": "Deleted"}},
                }
            },
        },
    }
    parsed = parse_openapi_spec(json.dumps(spec))
    create_user_op = next(o for o in parsed.operations if o.operation_id == "create_user")
    assert set(create_user_op.request_body_schema["properties"].keys()) == {"name", "extra"}
    assert set(create_user_op.request_body_schema["required"]) == {"name", "extra"}

    plan = build_odg_plan(parsed)
    step_map = {s.operation.operation_id: s for s in plan}
    assert step_map["login"].produces == []
    assert step_map["delete_user_order"].step < step_map["delete_user"].step


# ---------------------------------------------------------------------------
# 5. Security: Token expiration false positive, spec.servers[0] SSRF, LLM_API_KEY leak
# ---------------------------------------------------------------------------
def test_security_expiration_ssrf_servers0_and_llm_api_key_leak(monkeypatch) -> None:
    assert contains_prohibited_payment_data("Token expiration: 3600") is False
    assert contains_prohibited_payment_data("Cache expiration: 1200") is False
    assert contains_prohibited_payment_data("card_expiry: 0529") is True
    assert contains_prohibited_payment_data('{"expiry": "12/28"}') is True

    client = TestClient(app, raise_server_exceptions=False)

    # spec.servers[0] metadata SSRF when input.base_url omitted -> 422 ssrf_blocked
    ssrf_spec = json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "SSRF Servers", "version": "1.0"},
            "servers": [{"url": "http://169.254.169.254/latest/meta-data"}],
            "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {"description": "OK"}}}}},
        }
    )
    r_ssrf = _invoke(client, {"operation": "generate_tests", "input": {"openapi_spec": ssrf_spec}})
    assert r_ssrf.status_code == 422
    assert r_ssrf.json()["error"]["code"] == "ssrf_blocked"

    # Relative servers[0] and {scheme}:// templates must still work
    rel_spec = json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Relative Server", "version": "1.0"},
            "servers": [{"url": "/api/v1"}],
            "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {"description": "OK"}}}}},
        }
    )
    r_rel = _invoke(client, {"operation": "generate_tests", "input": {"openapi_spec": rel_spec}})
    assert r_rel.status_code == 200

    # Actual LLM_API_KEY echoed by LLM must trigger leak rejection and fallback cleanly
    from evals.fake_llm_server import FakeLLMServer

    leaked_secret = "sk-live-unique-token-98765"
    monkeypatch.setenv("LLM_API_KEY", leaked_secret)

    # Start a FakeLLMServer and test via monkeypatched response containing leaked_secret
    with FakeLLMServer(mode="valid_json") as srv:
        monkeypatch.setenv("LLM_BASE_URL", srv.base_url)
        # Normal valid_json succeeds
        simple_post = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "T", "version": "1"},
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
        )
        out_clean, status_clean, _ = generate_test_suite_pipeline({"openapi_spec": simple_post}, {})
        assert status_clean == "ok"
        assert leaked_secret not in json.dumps(out_clean)

    from src.generator import _contains_leaked_prompt_or_secrets

    assert _contains_leaked_prompt_or_secrets(f'{{"create_item": {{"valid_payload": {{"name": "{leaked_secret}"}}}}}}') is True


# ---------------------------------------------------------------------------
# 6. LLM enrichment batching >10 ops, multipart file preservation, schema check
# ---------------------------------------------------------------------------
def test_llm_enrichment_batches_over_10_and_validates_schema(monkeypatch) -> None:
    from evals.fake_llm_server import FakeLLMServer

    paths: dict[str, Any] = {}
    for i in range(1, 13):
        paths[f"/r{i}"] = {
            "post": {
                "operationId": f"create_r{i}",
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data" if i == 1 else "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name", "file"] if i == 1 else ["name"],
                                "properties": (
                                    {"name": {"type": "string"}, "file": {"type": "string", "format": "binary"}}
                                    if i == 1
                                    else {"name": {"type": "string"}}
                                ),
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Created"}},
            }
        }
    spec = json.dumps({"openapi": "3.0.3", "info": {"title": "12 Ops", "version": "1"}, "paths": paths})

    with FakeLLMServer(mode="valid_json") as srv:
        monkeypatch.setenv("LLM_BASE_URL", srv.base_url)
        monkeypatch.setenv("LLM_API_KEY", "test-eval-key")
        out, status, _ = generate_test_suite_pipeline({"openapi_spec": spec}, {})
        assert status == "ok"
        assert srv.request_count == 2  # 10 + 2 ops in two batches
        # Multipart op r1 preserves required 'file' field even when FakeLLMServer returns no 'file'
        assert "file" in out["mock_datasets"]["create_r1"]["valid_payload"]
        # Op 12 (>10) was also enriched by LLM and valid_samples[0] stays in sync
        assert out["mock_datasets"]["create_r12"]["valid_payload"]["name"] == "LLM Sentezlenen Ürün"
        assert out["mock_datasets"]["create_r12"]["valid_samples"][0] == out["mock_datasets"]["create_r12"]["valid_payload"]
        # Unrequested FakeLLMServer keys (price, quantity, email) were filtered out against schema properties
        assert "price" not in out["mock_datasets"]["create_r12"]["valid_payload"]

    # Also verify schema-invalid LLM valid_payload values are rejected and keep deterministic valid_payload
    bad_type_spec = json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Typed API", "version": "1"},
            "paths": {
                "/orders": {
                    "post": {
                        "operationId": "create_item",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["name", "age"],
                                        "properties": {
                                            "name": {"type": "string"},
                                            "age": {"type": "integer", "minimum": 18},
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {"201": {"description": "Created"}},
                    }
                }
            },
        }
    )
    # FakeLLMServer returns integer price=249.90 (not matching age) and name='LLM Sentezlenen Ürün' -> valid_payload keeps age>=18!
    with FakeLLMServer(mode="valid_json") as srv2:
        monkeypatch.setenv("LLM_BASE_URL", srv2.base_url)
        typed_out, typed_status, _ = generate_test_suite_pipeline({"openapi_spec": bad_type_spec}, {})
        assert typed_status == "ok"
        assert isinstance(typed_out["mock_datasets"]["create_item"]["valid_payload"]["age"], int)
        assert typed_out["mock_datasets"]["create_item"]["valid_payload"]["age"] >= 18


# ---------------------------------------------------------------------------
# 7. fail_on_missing_auth across ALL endpoints + no traceback leaked to logs
# ---------------------------------------------------------------------------
def test_fail_on_missing_auth_applies_to_get_and_analyze_coverage() -> None:
    spec = json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Public Read API", "version": "1"},
            "paths": {
                "/public": {
                    "get": {
                        "operationId": "get_public",
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
    )
    gen_out, gen_status, _ = generate_test_suite_pipeline(
        {"openapi_spec": spec},
        {"fail_on_missing_auth": True},
    )
    assert gen_status == "needs_review"

    cov_out, cov_status, _ = analyze_coverage_pipeline(
        {"openapi_spec": spec, "existing_test_code": gen_out["test_code"]},
        {"min_coverage_pct": 100, "fail_on_missing_auth": True},
    )
    assert cov_out["coverage_pct"] == 100.0
    assert cov_status == "needs_review"
    assert any("GET /public" in rec for rec in cov_out["recommendations"])


def test_internal_error_logs_exclude_traceback_and_sensitive_exception_text(caplog, monkeypatch) -> None:
    client = TestClient(app, raise_server_exceptions=False)

    def _boom(*args, **kwargs):
        raise RuntimeError("SUPER_SECRET_USER_DATA_LEAK")

    monkeypatch.setitem(main_mod.OPERATIONS, "generate_tests", _boom)
    with caplog.at_level("ERROR"):
        resp = _invoke(client, {"operation": "generate_tests", "input": {"openapi_spec": "x"}})
    assert resp.status_code == 500
    assert resp.json() == {"error": {"code": "processing_failed", "message": "Request could not be processed."}}
    assert "req_r4" in caplog.text
    assert "SUPER_SECRET_USER_DATA_LEAK" not in caplog.text
    assert "Traceback (most recent call last)" not in caplog.text
