"""Mandatory contract and security tests for auto-qa-engine (spec_version 1.1 Appendix B)."""

import hashlib
import hmac
import json
import os
import time

os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999/v1")
os.environ.setdefault("LLM_API_KEY", "test")

from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

SAMPLE_OPENAPI = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Sample Users API", "version": "1.0.0"},
        "paths": {
            "/users": {
                "post": {
                    "operationId": "create_user",
                    "responses": {"201": {"description": "Created"}},
                }
            }
        },
    }
)


def signed(body: dict, secret: bytes = b"test-secret", ts: int | None = None) -> tuple[bytes, dict[str, str]]:
    """Sign request body with HMAC-SHA256 matching Platform Gateway rules."""
    raw = json.dumps(body).encode("utf-8")
    ts_str = str(ts if ts is not None else int(time.time()))
    sig = hmac.new(secret, ts_str.encode("utf-8") + b"." + raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Platform-Timestamp": ts_str,
        "X-Platform-Signature": sig,
        "X-Tenant-Id": "ten_contract_test",
        "X-Request-Id": "req_contract_01",
        "Content-Type": "application/json",
    }


def test_health() -> None:
    """GET /health must return 200 and {"status": "ok"}."""
    start = time.perf_counter()
    resp = client.get("/health")
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert elapsed < 2.0


def test_rejects_bad_signature() -> None:
    """Invalid HMAC signature must be rejected with HTTP 401."""
    raw, h = signed({"operation": "generate_tests", "input": {"openapi_spec": SAMPLE_OPENAPI}}, secret=b"wrong")
    assert client.post("/v1/invoke", content=raw, headers=h).status_code == 401


def test_rejects_missing_headers() -> None:
    """Missing signature or timestamp headers must be rejected with HTTP 401."""
    raw = json.dumps({"operation": "generate_tests", "input": {"openapi_spec": SAMPLE_OPENAPI}}).encode("utf-8")
    assert client.post("/v1/invoke", content=raw).status_code == 401


def test_rejects_old_timestamp() -> None:
    """Requests older than 300 seconds must be rejected with HTTP 401."""
    raw, h = signed(
        {"operation": "generate_tests", "input": {"openapi_spec": SAMPLE_OPENAPI}},
        ts=int(time.time()) - 600,
    )
    assert client.post("/v1/invoke", content=raw, headers=h).status_code == 401


def test_rejects_future_timestamp() -> None:
    """Requests with timestamp > 300 seconds in the future must be rejected with HTTP 401."""
    raw, h = signed(
        {"operation": "generate_tests", "input": {"openapi_spec": SAMPLE_OPENAPI}},
        ts=int(time.time()) + 600,
    )
    assert client.post("/v1/invoke", content=raw, headers=h).status_code == 401


def test_unknown_operation_is_422() -> None:
    """Unknown operation name must return HTTP 422 with structured error JSON."""
    raw, h = signed({"operation": "yok", "input": {}})
    resp = client.post("/v1/invoke", content=raw, headers=h)
    assert resp.status_code == 422
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "unknown_operation"


def test_valid_generate_tests_envelope() -> None:
    """Valid signed generate_tests request returns 200 OK with required contract fields."""
    raw, h = signed(
        {
            "operation": "generate_tests",
            "input": {"openapi_spec": SAMPLE_OPENAPI},
            "config": {"test_framework": "pytest-httpx"},
            "trigger": {"kind": "ci"},
        }
    )
    resp = client.post("/v1/invoke", content=raw, headers=h)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "fail", "needs_review")
    assert "output" in data
    assert "summary" in data["output"]
    assert "odg_plan" in data["output"]
    assert "pytest_code" in data["output"]
    assert "mock_datasets" in data["output"]
    assert isinstance(data.get("artifacts"), list)
    assert isinstance(data.get("actions"), list)
