"""Regression tests for Round 5 audit findings (alias bomb, /health non-blocking, planner partial schema, internal spec servers, lean requirements.txt)."""

from __future__ import annotations

import ast
import concurrent.futures
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

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")
os.environ.setdefault("LLM_BASE_URL", "")
os.environ.setdefault("LLM_API_KEY", "test")

from src.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sign(raw: bytes, secret: str = "test-secret") -> dict[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return {
        "X-Platform-Timestamp": ts,
        "X-Platform-Signature": sig,
        "X-Tenant-Id": "ten_r5",
        "X-Request-Id": "req_r5",
        "Content-Type": "application/json",
    }


def _invoke(client: TestClient, payload: dict[str, Any]) -> Any:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return client.post("/v1/invoke", content=raw, headers=_sign(raw))


class _UvicornThread:
    """In-process uvicorn HTTP server on an ephemeral localhost port."""

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


# ---------------------------------------------------------------------------
# Item 1: YAML alias bomb & $ref expansion bomb rejected quickly (not ~5-37s DoS)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("layers", [8, 10])
def test_yaml_alias_bomb_rejected_quickly(layers: int) -> None:
    bomb = 'openapi: "3.0.0"\ninfo: {title: Bomb, version: "1"}\nx0: &a0 ["a","a","a","a","a","a","a"]\n'
    for i in range(1, layers + 1):
        bomb += f"x{i}: &a{i} [*a{i-1},*a{i-1},*a{i-1},*a{i-1},*a{i-1},*a{i-1},*a{i-1}]\n"
    bomb += f'paths:\n  /p:\n    get:\n      description: ok\n      x-use: *a{layers}\n      responses: {{"200": {{description: ok}}}}\n'

    client = TestClient(app, raise_server_exceptions=False)
    t0 = time.perf_counter()
    resp = _invoke(client, {"operation": "generate_tests", "input": {"openapi_spec": bomb}})
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_openapi_syntax"
    assert elapsed < 0.5, f"{layers}-layer YAML alias bomb took {elapsed:.2f}s (expected < 0.5s)"


def test_ref_dag_bomb_rejected_quickly_and_normal_yaml_anchor_works() -> None:
    schemas: dict[str, Any] = {"L0": {"type": "string"}}
    for i in range(1, 22):
        schemas[f"L{i}"] = {
            "type": "object",
            "properties": {"a": {"$ref": f"#/components/schemas/L{i-1}"}, "b": {"$ref": f"#/components/schemas/L{i-1}"}},
        }
    ref_bomb = {
        "openapi": "3.0.3",
        "info": {"title": "RefBomb", "version": "1"},
        "components": {"schemas": schemas},
        "paths": {
            "/p": {
                "post": {
                    "operationId": "p",
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/L21"}}}},
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }

    client = TestClient(app, raise_server_exceptions=False)
    t0 = time.perf_counter()
    r_bomb = _invoke(client, {"operation": "generate_tests", "input": {"openapi_spec": json.dumps(ref_bomb)}})
    elapsed = time.perf_counter() - t0
    assert r_bomb.status_code == 422
    assert r_bomb.json()["error"]["code"] == "invalid_openapi_syntax"
    assert elapsed < 0.5

    # Legitimate small YAML anchor reuse must still succeed
    normal_yaml = (
        'openapi: "3.0.3"\n'
        'info: {title: Anchors, version: "1.0"}\n'
        'x-desc: &ok_resp {description: "OK"}\n'
        "paths:\n"
        "  /a:\n"
        "    get:\n"
        "      operationId: get_a\n"
        "      responses:\n"
        "        '200': *ok_resp\n"
        "  /b:\n"
        "    get:\n"
        "      operationId: get_b\n"
        "      responses:\n"
        "        '200': *ok_resp\n"
    )
    r_ok = _invoke(client, {"operation": "generate_tests", "input": {"openapi_spec": normal_yaml}})
    assert r_ok.status_code == 200
    assert r_ok.json()["output"]["summary"]["total_operations"] == 2


def test_yaml_anchor_reused_across_300_operations_succeeds() -> None:
    fields_yaml = "\n".join(f"      field_{k}: {{type: string}}" for k in range(15))
    paths_yaml = "".join(
        f"  /op_{i}:\n"
        f"    get:\n"
        f"      operationId: get_op_{i}\n"
        f"      responses:\n"
        f"        '200':\n"
        f"          description: OK\n"
        f"          content:\n"
        f"            application/json:\n"
        f"              schema: *shared_item\n"
        for i in range(300)
    )
    large_anchor_yaml = (
        'openapi: "3.0.3"\n'
        'info: {title: SharedAnchor300, version: "1.0"}\n'
        "x-shared: &shared_item\n"
        "  type: object\n"
        "  properties:\n"
        f"{fields_yaml}\n"
        f"paths:\n{paths_yaml}"
    )

    client = TestClient(app, raise_server_exceptions=False)
    resp = _invoke(client, {"operation": "generate_tests", "input": {"openapi_spec": large_anchor_yaml}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["output"]["summary"]["total_operations"] == 300


# ---------------------------------------------------------------------------
# Item 2: /health stays responsive (< 2s contract) during a 3000-op /v1/invoke
# ---------------------------------------------------------------------------
def test_health_stays_fast_during_3000_op_invoke(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "")
    paths: dict[str, Any] = {}
    for i in range(1500):
        paths[f"/res_{i}"] = {
            "post": {
                "operationId": f"create_res_{i}",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {"name": {"type": "string", "minLength": 2}},
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
                                    "properties": {"id": {"type": "string"}},
                                }
                            }
                        },
                    }
                },
            }
        }
        paths[f"/res_{i}/{{id}}"] = {
            "get": {
                "operationId": f"get_res_{i}",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}},
            }
        }

    spec_str = json.dumps({"openapi": "3.0.3", "info": {"title": "Huge3000", "version": "1"}, "paths": paths})
    body_bytes = json.dumps({"operation": "generate_tests", "input": {"openapi_spec": spec_str}}).encode("utf-8")

    with _UvicornThread(app) as base_url:
        def _send_heavy_invoke() -> tuple[httpx.Response, float]:
            t0 = time.perf_counter()
            r = httpx.post(f"{base_url}/v1/invoke", content=body_bytes, headers=_sign(body_bytes), timeout=30.0)
            return r, time.perf_counter() - t0

        def _probe_health_during_load() -> list[tuple[int, dict[str, Any], float]]:
            time.sleep(0.05)
            samples: list[tuple[int, dict[str, Any], float]] = []
            for _ in range(3):
                t0 = time.perf_counter()
                h = httpx.get(f"{base_url}/health", timeout=2.0)
                samples.append((h.status_code, h.json(), time.perf_counter() - t0))
                time.sleep(0.05)
            return samples

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            inv_fut = pool.submit(_send_heavy_invoke)
            health_fut = pool.submit(_probe_health_during_load)
            health_samples = health_fut.result(timeout=10.0)
            inv_resp, inv_elapsed = inv_fut.result(timeout=30.0)

    for status_code, payload, latency in health_samples:
        assert status_code == 200
        assert payload == {"status": "ok"}
        assert latency < 1.0, f"/health blocked during 3000-op invoke: {latency:.3f}s"

    assert inv_resp.status_code == 200
    assert inv_resp.json()["output"]["summary"]["total_operations"] == 3000
    assert inv_resp.json()["output"]["summary"]["ast_valid"] is True
    assert inv_elapsed < 10.0, f"3000-op invoke took {inv_elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Item 3: Partial POST response schema without 'id' still acts as ODG producer
# ---------------------------------------------------------------------------
class _UserBody(BaseModel):
    name: str


def test_post_with_partial_response_schema_without_id_captures_id_and_passes_e2e(tmp_path: Path) -> None:
    store: dict[str, dict[str, str]] = {}
    api = FastAPI()

    @api.post("/users", status_code=201)
    def create_user(body: _UserBody):
        uid = "u-101"
        store[uid] = {"id": uid, "name": body.name}
        return store[uid]

    @api.get("/users/{id}")
    def get_user(id: str):
        if id not in store:
            raise HTTPException(status_code=404, detail="not found")
        return store[id]

    @api.delete("/users/{id}", status_code=204)
    def delete_user(id: str):
        if id not in store:
            raise HTTPException(status_code=404, detail="not found")
        del store[id]
        return None

    # POST response schema defines 'name' only (omitting 'id'), which previously broke ODG producer detection
    partial_schema_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Partial POST Schema API", "version": "1.0"},
        "paths": {
            "/users": {
                "post": {
                    "operationId": "create_user",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {"name": {"type": "string", "minLength": 2}},
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
                                        "properties": {"name": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/users/{id}": {
                "get": {
                    "operationId": "get_user",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "OK"}},
                },
                "delete": {
                    "operationId": "delete_user",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"204": {"description": "Deleted"}},
                },
            },
        },
    }

    client = TestClient(app, raise_server_exceptions=False)
    with _UvicornThread(api) as api_url:
        resp = _invoke(
            client,
            {"operation": "generate_tests", "input": {"openapi_spec": json.dumps(partial_schema_spec), "base_url": api_url}},
        )
        assert resp.status_code == 200
        out = resp.json()["output"]
        step1 = out["odg_plan"][0]
        assert step1["operation_id"] == "create_user"
        assert step1["produces"] == ["user_id"]

        test_file = tmp_path / "test_partial_post.py"
        test_file.write_text(out["test_code"], encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-v", "-p", "no:cacheprovider"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"Generated suite failed on partial POST schema:\n{proc.stdout}\n{proc.stderr}"
        assert "7 passed" in proc.stdout


# ---------------------------------------------------------------------------
# Item 4: Internal/private addresses in spec.servers[0] allowed when base_url omitted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "server_url",
    [
        "http://10.0.0.5:8080/v1",
        "http://192.168.1.10/api",
        "http://172.16.0.2/v2",
        "https://api.corp.internal/v1",
        "http://service.local/api",
    ],
)
def test_internal_addresses_in_spec_servers_allowed_when_base_url_omitted(server_url: str) -> None:
    spec = json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Internal Spec Server", "version": "1"},
            "servers": [{"url": server_url}],
            "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {"description": "OK"}}}}},
        }
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = _invoke(client, {"operation": "generate_tests", "input": {"openapi_spec": spec}})
    assert resp.status_code == 200
    assert f'BASE_URL = "{server_url.rstrip("/")}"' in resp.json()["output"]["test_code"]


@pytest.mark.parametrize(
    "blocked_server_url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/",
        "file:///etc/passwd",
        "http://0.0.0.0:8080",
    ],
)
def test_metadata_and_unsafe_scheme_still_blocked_in_spec_servers(blocked_server_url: str) -> None:
    spec = json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Blocked Server", "version": "1"},
            "servers": [{"url": blocked_server_url}],
            "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {"description": "OK"}}}}},
        }
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = _invoke(client, {"operation": "generate_tests", "input": {"openapi_spec": spec}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "ssrf_blocked"


# ---------------------------------------------------------------------------
# Item 5: Runtime requirements.txt excludes pytest/httpx/requests and src/ never imports them
# ---------------------------------------------------------------------------
def test_runtime_requirements_txt_excludes_test_only_packages() -> None:
    reqs_raw = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pkg_names = {
        line.strip().split(">=")[0].split("==")[0].split("<")[0].strip().lower()
        for line in reqs_raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for forbidden in ("pytest", "httpx", "requests", "pillow", "python-multipart"):
        assert forbidden not in pkg_names, f"{forbidden} should not be in runtime requirements.txt"

    for py_file in (REPO_ROOT / "src").glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in ("pytest", "httpx", "requests", "PIL")
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in ("pytest", "httpx", "requests", "PIL")
