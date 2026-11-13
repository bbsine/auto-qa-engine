"""Regression tests: form / multipart request bodies (Item 1) and spec base path (Item 2)."""

from __future__ import annotations

import json
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

from src.generator import generate_test_suite_pipeline, resolve_effective_base_url
from src.parser import parse_openapi_spec


def _gen(spec: Any, **extra: Any) -> str:
    spec_text = spec if isinstance(spec, str) else json.dumps(spec)
    out, _status, _art = generate_test_suite_pipeline({"openapi_spec": spec_text, **extra}, {"default_locale": "tr-TR"})
    return out["test_code"]


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "")


# --------------------------------------------------------------------------- specs
REPRO_A = {
    "openapi": "3.0.3",
    "info": {"title": "Login A", "version": "1"},
    "paths": {
        "/login": {
            "post": {
                "operationId": "login",
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
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}

REPRO_B = {
    "swagger": "2.0",
    "info": {"title": "Login B", "version": "1"},
    "consumes": ["application/x-www-form-urlencoded"],
    "paths": {
        "/login": {
            "post": {
                "operationId": "login",
                "parameters": [
                    {"name": "username", "in": "formData", "type": "string", "required": True, "minLength": 3},
                    {"name": "password", "in": "formData", "type": "string", "required": True, "minLength": 3},
                ],
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}

MULTIPART_OAS3 = {
    "openapi": "3.0.3",
    "info": {"title": "Docs", "version": "1"},
    "paths": {
        "/documents": {
            "post": {
                "operationId": "upload_document",
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
                "responses": {"201": {"description": "created"}},
            }
        }
    },
}

MULTIPART_SWAGGER2 = {
    "swagger": "2.0",
    "info": {"title": "Docs2", "version": "1"},
    "paths": {
        "/documents": {
            "post": {
                "operationId": "upload_document",
                "parameters": [
                    {"name": "title", "in": "formData", "type": "string", "required": True, "minLength": 2},
                    {"name": "file", "in": "formData", "type": "file", "required": True},
                ],
                "responses": {"201": {"description": "created"}},
            }
        }
    },
}


# --------------------------------------------------------------------------- Item 1: parser
def test_item1_parser_request_content_type_openapi3() -> None:
    assert parse_openapi_spec(REPRO_A).operations[0].request_content_type == "application/x-www-form-urlencoded"
    assert parse_openapi_spec(MULTIPART_OAS3).operations[0].request_content_type == "multipart/form-data"


def test_item1_parser_swagger2_content_type_rules() -> None:
    assert parse_openapi_spec(REPRO_B).operations[0].request_content_type == "application/x-www-form-urlencoded"

    no_consumes = json.loads(json.dumps(REPRO_B))
    no_consumes.pop("consumes")
    assert parse_openapi_spec(no_consumes).operations[0].request_content_type == "application/x-www-form-urlencoded"
    assert parse_openapi_spec(MULTIPART_SWAGGER2).operations[0].request_content_type == "multipart/form-data"

    op_level = json.loads(json.dumps(REPRO_B))
    op_level["paths"]["/login"]["post"]["consumes"] = ["multipart/form-data"]
    assert parse_openapi_spec(op_level).operations[0].request_content_type == "multipart/form-data"

    body_spec = {
        "swagger": "2.0",
        "info": {"title": "j", "version": "1"},
        "paths": {
            "/x": {
                "post": {
                    "parameters": [{"name": "b", "in": "body", "schema": {"type": "object"}}],
                    "responses": {"201": {"description": "ok"}},
                },
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        },
    }
    ops = {o.method: o for o in parse_openapi_spec(body_spec).operations}
    assert ops["POST"].request_content_type == "application/json"
    assert ops["GET"].request_content_type is None


# --------------------------------------------------------------------------- Item 1: emitted code
@pytest.mark.parametrize("spec", [REPRO_A, REPRO_B])
def test_item1_urlencoded_bodies_use_data_not_json(spec: dict[str, Any]) -> None:
    code = _gen(spec)
    assert "data=" in code
    assert "json=" not in code
    assert "files=" not in code


@pytest.mark.parametrize("spec", [MULTIPART_OAS3, MULTIPART_SWAGGER2])
def test_item1_multipart_bodies_use_data_and_files(spec: dict[str, Any]) -> None:
    code = _gen(spec)
    assert "data=" in code and "files=" in code
    assert "json=" not in code
    assert "('sample.txt', b'sample', 'text/plain')" in code
    compile(code, "<generated>", "exec")


def test_item1_json_bodies_still_use_json() -> None:
    spec = json.loads(json.dumps(REPRO_A))
    content = spec["paths"]["/login"]["post"]["requestBody"]["content"]
    content["application/json"] = content.pop("application/x-www-form-urlencoded")
    code = _gen(spec)
    assert "json=" in code and "data=" not in code


def test_item1_form_value_encoding() -> None:
    from src.validator import encode_form_value, split_form_payload

    assert encode_form_value(True) == "true"
    assert encode_form_value(False) == "false"
    assert encode_form_value(5) == 5
    assert encode_form_value(["a", True]) == ["a", "true"]
    assert encode_form_value({"k": 1}) == '{"k": 1}'
    data, files = split_form_payload({"title": "x", "file": "ignored", "flag": True}, {"file"})
    assert data == {"title": "x", "flag": "true"}
    assert files == {"file": ("sample.txt", b"sample", "text/plain")}


def test_item1_spec_content_stays_repr_literal() -> None:
    spec = json.loads(json.dumps(REPRO_A))
    props = spec["paths"]["/login"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"][
        "properties"
    ]
    props['x"""\nimport os\nos.remove("/tmp/KURBAN")\n"""'] = {"type": "string"}
    code = _gen(spec)
    compile(code, "<generated>", "exec")
    assert "\nimport os\n" not in code


# --------------------------------------------------------------------------- e2e helpers
def _start_uvicorn(asgi_app: Any) -> tuple[uvicorn.Server, threading.Thread, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, thread, port


def _run_pytest(code: str, tmp_path: Path, name: str, *extra: str) -> subprocess.CompletedProcess[str]:
    f = tmp_path / f"test_{name}.py"
    f.write_text(code, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(f), "-q", "-p", "no:cacheprovider", *extra],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture()
def form_servers():
    pytest.importorskip("multipart", reason="python-multipart is a dev/test dependency (scripts/requirements-dev.txt)")
    from tests.e2e.form_servers import form_broken_app, form_correct_app

    ok = _start_uvicorn(form_correct_app)
    bad = _start_uvicorn(form_broken_app)
    try:
        yield ok[2], bad[2]
    finally:
        ok[0].should_exit = True
        bad[0].should_exit = True
        ok[1].join(timeout=3)
        bad[1].join(timeout=3)


@pytest.mark.parametrize(
    "spec,name",
    [(REPRO_A, "repro_a"), (REPRO_B, "repro_b"), (MULTIPART_OAS3, "multipart_oas3"), (MULTIPART_SWAGGER2, "multipart_sw2")],
)
def test_item1_e2e_generated_tests_pass_on_correct_server_and_negative_fails_on_broken(
    form_servers: tuple[int, int], spec: dict[str, Any], name: str, tmp_path: Path
) -> None:
    port_ok, port_bad = form_servers
    proc_ok = _run_pytest(_gen(spec, base_url=f"http://127.0.0.1:{port_ok}"), tmp_path, f"{name}_ok")
    assert proc_ok.returncode == 0, f"expected pass on correct server:\n{proc_ok.stdout}\n{proc_ok.stderr}"

    proc_bad = _run_pytest(
        _gen(spec, base_url=f"http://127.0.0.1:{port_bad}"), tmp_path, f"{name}_bad", "-k", "negative_boundary"
    )
    assert proc_bad.returncode != 0, f"negative-boundary test must FAIL on the accept-anything server:\n{proc_bad.stdout}"


# --------------------------------------------------------------------------- Item 2: base path
def test_item2_base_url_rules() -> None:
    servers = ["https://x.example/v2"]
    # no user base_url -> existing behaviour (spec server / default)
    assert resolve_effective_base_url(None, servers, "/v2") == "https://x.example/v2"
    assert resolve_effective_base_url("", [], "") == "http://localhost:8000"
    # rule 1: empty path or "/" -> append base path
    assert resolve_effective_base_url("http://127.0.0.1:9000", servers, "/v2") == "http://127.0.0.1:9000/v2"
    assert resolve_effective_base_url("http://127.0.0.1:9000/", servers, "/v2") == "http://127.0.0.1:9000/v2"
    # rule 2: already ends with base path -> no duplication
    assert resolve_effective_base_url("http://127.0.0.1:9000/v2", servers, "/v2") == "http://127.0.0.1:9000/v2"
    assert resolve_effective_base_url("http://h:1/api/v2/", servers, "/v2") == "http://h:1/api/v2/"
    # rule 3: any other non-empty path -> unchanged
    assert resolve_effective_base_url("http://127.0.0.1:9000/custom", servers, "/v2") == "http://127.0.0.1:9000/custom"
    # no spec base path -> unchanged
    assert resolve_effective_base_url("http://127.0.0.1:9000", ["https://x.example"], "") == "http://127.0.0.1:9000"


def test_item2_parser_records_base_path() -> None:
    sw = {
        "swagger": "2.0",
        "info": {"title": "p", "version": "1"},
        "host": "x.example",
        "basePath": "/v2/",
        "paths": {"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    assert parse_openapi_spec(sw).base_path == "/v2"

    oas = {
        "openapi": "3.0.3",
        "info": {"title": "p", "version": "1"},
        "servers": [{"url": "https://x.example/v2"}],
        "paths": {"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    assert parse_openapi_spec(oas).base_path == "/v2"
    oas["servers"] = [{"url": "{scheme}://x.example/v2"}]
    assert parse_openapi_spec(oas).base_path == ""
    oas["servers"] = [{"url": "https://x.example"}]
    assert parse_openapi_spec(oas).base_path == ""


def test_item2_ssrf_check_runs_on_final_base_url() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "p", "version": "1"},
        "servers": [{"url": "https://x.example/v2"}],
        "paths": {"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    with pytest.raises(ValueError, match="ssrf_blocked"):
        _gen(spec, base_url="http://169.254.169.254")


PETS_SWAGGER2 = {
    "swagger": "2.0",
    "info": {"title": "Pets", "version": "1"},
    "host": "petstore.example.com",
    "basePath": "/v2",
    "schemes": ["https"],
    "paths": {
        "/pets": {
            "post": {
                "operationId": "create_pet",
                "consumes": ["application/json"],
                "parameters": [
                    {
                        "name": "body",
                        "in": "body",
                        "required": True,
                        "schema": {
                            "type": "object",
                            "required": ["name", "tag"],
                            "properties": {
                                "name": {"type": "string", "minLength": 2},
                                "tag": {"type": "string", "minLength": 2},
                            },
                        },
                    }
                ],
                "responses": {"201": {"description": "created", "schema": {"type": "object", "properties": {"id": {"type": "string"}}}}},
            }
        },
        "/pets/{pet_id}": {
            "get": {
                "operationId": "get_pet",
                "parameters": [{"name": "pet_id", "in": "path", "required": True, "type": "string"}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def test_item2_e2e_generated_suite_passes_with_bare_host_base_url(tmp_path: Path) -> None:
    from tests.e2e.base_path_server import base_path_app

    srv, th, port = _start_uvicorn(base_path_app)
    try:
        code = _gen(PETS_SWAGGER2, base_url=f"http://127.0.0.1:{port}")
        assert f'BASE_URL = "http://127.0.0.1:{port}/v2"' in code
        proc = _run_pytest(code, tmp_path, "base_path")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    finally:
        srv.should_exit = True
        th.join(timeout=3)
