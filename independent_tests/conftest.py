"""Shared helpers for the independent test suite (run from the project root: python -m pytest independent_tests -v)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "independent-test-secret"
os.environ["PLATFORM_SIGNING_SECRET"] = SECRET
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = "independent-key"

from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def sign(raw: bytes, ts: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()


@pytest.fixture()
def invoke(client):
    def _invoke(body, *, ts_offset=0, secret=SECRET, tamper=None, extra_headers=None):
        raw = json.dumps(body).encode()
        ts = str(int(time.time()) + ts_offset)
        headers = {
            "X-Platform-Timestamp": ts,
            "X-Platform-Signature": sign(raw, ts, secret),
            "X-Tenant-Id": "ten_independent",
            "X-Request-Id": "req_independent",
        }
        if extra_headers:
            headers.update(extra_headers)
        return client.post("/v1/invoke", content=tamper if tamper is not None else raw, headers=headers)

    return _invoke


SIMPLE_SPEC = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {"description": "ok"}}}}},
    }
)


def spec_with_body(schema, resp_schema=None):
    return json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/x": {
                    "post": {
                        "operationId": "create_x",
                        "requestBody": {"content": {"application/json": {"schema": schema}}},
                        "responses": {
                            "201": {
                                "description": "ok",
                                "content": {"application/json": {"schema": resp_schema or {"type": "object"}}},
                            }
                        },
                    }
                }
            },
        }
    )
