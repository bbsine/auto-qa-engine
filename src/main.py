"""FastAPI entrypoint for auto-qa-engine implementing spec_version 1.1 contract with structured error handling."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from src.generator import (
    InputShapeError,
    analyze_coverage_pipeline,
    generate_mocks_pipeline,
    generate_test_suite_pipeline,
)
from src.security import contains_prohibited_payment_data, is_ssrf_url, scan_object_for_sensitive_data, verify_signature

logger = logging.getLogger("auto_qa_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

# Limit concurrent CPU/memory-heavy invocations to prevent OOM under parallel large requests.
MAX_CONCURRENT_INVOKES = 2
CONCURRENCY_WAIT_TIMEOUT_S = 0.05
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_INVOKE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_INVOKES,
    thread_name_prefix="autoqa-worker",
)
_LOOP_SEMAPHORES: dict[int, asyncio.Semaphore] = {}


def _get_invoke_semaphore() -> asyncio.Semaphore:
    """Return an asyncio.Semaphore bound to the currently active event loop."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    sem = _LOOP_SEMAPHORES.get(key)
    if sem is None:
        if len(_LOOP_SEMAPHORES) > 8:
            _LOOP_SEMAPHORES.clear()
        sem = asyncio.Semaphore(MAX_CONCURRENT_INVOKES)
        _LOOP_SEMAPHORES[key] = sem
    return sem


def _contains_lone_surrogate(obj: Any, _seen: set[int] | None = None) -> bool:
    """Return True if a decoded JSON structure contains lone UTF-16 surrogate code points."""
    if isinstance(obj, str):
        return bool(_SURROGATE_RE.search(obj))
    if not isinstance(obj, (dict, list)):
        return False
    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        return False
    _seen.add(obj_id)
    if isinstance(obj, dict):
        return any(
            _contains_lone_surrogate(k, _seen) or _contains_lone_surrogate(v, _seen)
            for k, v in obj.items()
        )
    return any(_contains_lone_surrogate(item, _seen) for item in obj)

app = FastAPI(
    title="AutoQA Engine",
    version="1.0.0",
    description="OpenAPI & Route based deterministic ODG & AST-verified Pytest generator agent.",
)


from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException


def _http_error_code(status_code: int, detail_str: str) -> str:
    """Map HTTP status codes and Starlette/auth messages to spec §2.3 error codes."""
    lower = detail_str.lower()
    if status_code == 401:
        if "timestamp" in lower and "signature" not in lower:
            return "invalid_timestamp"
        if "signature" in lower:
            return "invalid_signature"
        return "unauthorized"
    if status_code == 404:
        return "not_found"
    if status_code == 405:
        return "method_not_allowed"
    if status_code == 400:
        return "bad_request"
    if status_code == 422:
        return "validation_error"
    if status_code == 429:
        return "server_busy"
    return f"http_{status_code}"


def _format_http_error(exc: StarletteHTTPException) -> JSONResponse:
    """Render any HTTPException in the standard {"error": {"code": ..., "message": ...}} envelope."""
    headers = getattr(exc, "headers", None)
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(exc.detail, status_code=exc.status_code, headers=headers)
    msg = str(exc.detail or "HTTP error")
    code = _http_error_code(exc.status_code, msg)
    return JSONResponse(
        {"error": {"code": code, "message": msg}},
        status_code=exc.status_code,
        headers=headers,
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_envelope_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Ensure 401, 404, 405 and other HTTP errors return the {"error": {"code", "message"}} structure."""
    return _format_http_error(exc)


@app.exception_handler(RequestValidationError)
async def request_validation_envelope_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Ensure FastAPI request validation errors return the {"error": {"code", "message"}} structure."""
    return JSONResponse(
        {"error": {"code": "validation_error", "message": "Request validation failed."}},
        status_code=422,
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global catch-all exception handler: logs tenant_id/request_id without leaking exception text or tracebacks."""
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        return _format_http_error(exc)

    tenant_id = getattr(request.state, "tenant_id", None) or request.headers.get("X-Tenant-Id", "unknown")
    request_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-Id", "unknown")
    logger.error(
        "Unhandled internal exception: tenant_id=%s request_id=%s exc_type=%s",
        tenant_id,
        request_id,
        type(exc).__name__,
    )
    return JSONResponse(
        {"error": {"code": "processing_failed", "message": "Request could not be processed."}},
        status_code=500,
    )


def _validate_request_security(inp: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str, str]:
    """Enforce SSRF and §4.3 payment/TCKN data red-line checks on incoming payload."""
    small_inp = {
        k: v
        for k, v in inp.items()
        if not (k in ("openapi_spec", "existing_test_code") and isinstance(v, str))
    }
    raw_small_dump = json.dumps({"input": small_inp, "config": cfg}, ensure_ascii=False)
    if (
        contains_prohibited_payment_data(raw_small_dump)
        or scan_object_for_sensitive_data(inp)
        or scan_object_for_sensitive_data(cfg)
    ):
        return (
            False,
            "prohibited_sensitive_data",
            "Girdide kart numarası, CVV, TCKN veya banka şifresi gibi hassas veri tespit edildi (Bölüm 4.3 / KVKK kuralı).",
        )

    for url_field in ("spec_url", "katalog_url", "webhook_url", "callback_url", "target_url"):
        val = inp.get(url_field) or cfg.get(url_field)
        if isinstance(val, str) and val.strip():
            unsafe, reason = is_ssrf_url(val, allow_local_base_url=False)
            if unsafe:
                return False, "ssrf_blocked", reason
            return (
                False,
                "external_network_disabled",
                "Container ağ politikası (network.allow: []) dış URL çağrılarına izin vermez; OpenAPI şemasını metin olarak gönderin.",
            )

    base_url = inp.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        unsafe, reason = is_ssrf_url(base_url, allow_local_base_url=True)
        if unsafe:
            return False, "ssrf_blocked", reason

    return True, "", ""


OPERATIONS: dict[str, Callable[[dict[str, Any], dict[str, Any]], tuple[dict[str, Any], str, list[dict[str, Any]]]]] = {
    "generate_tests": generate_test_suite_pipeline,
    "generate_mocks": generate_mocks_pipeline,
    "analyze_coverage": analyze_coverage_pipeline,
}


@app.get("/health")
def health() -> dict[str, str]:
    """Fast health check probe responding in < 2 seconds."""
    return {"status": "ok"}


def _execute_invoke_sync(
    op: Callable[[dict[str, Any], dict[str, Any]], tuple[dict[str, Any], str, list[dict[str, Any]]]],
    inp: dict[str, Any],
    cfg: dict[str, Any],
    tenant_id: str,
    request_id: str,
) -> tuple[bool, str, str, bytes | None]:
    """Run blocking request validation, operation pipeline, and JSON encoding off the asyncio event loop."""
    is_safe, err_code, err_msg = _validate_request_security(inp, cfg)
    if not is_safe:
        return False, err_code, err_msg, None

    cfg_with_meta = dict(cfg)
    cfg_with_meta["__tenant_id__"] = tenant_id
    cfg_with_meta["__request_id__"] = request_id
    output, status, artifacts = op(inp, cfg_with_meta)
    body_bytes = json.dumps(
        {
            "output": output,
            "status": status,
            "artifacts": artifacts,
            "actions": [],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    return True, "", "", body_bytes


@app.post("/v1/invoke")
async def invoke(request: Request) -> Any:
    """Primary operation endpoint for CI, MCP, and API triggers."""
    tenant_id = request.headers.get("X-Tenant-Id", "unknown")
    request_id = request.headers.get("X-Request-Id", "unknown")
    request.state.tenant_id = tenant_id
    request.state.request_id = request_id

    body = await request.body()
    verify_signature(
        body=body,
        ts=request.headers.get("X-Platform-Timestamp"),
        sig=request.headers.get("X-Platform-Signature"),
    )

    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object")
    except Exception:
        err = {"code": "invalid_json", "message": "İstek gövdesi geçerli bir JSON nesnesi değil."}
        return JSONResponse({"error": err}, status_code=422)

    if _contains_lone_surrogate(data):
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_encoding",
                    "message": "İstek gövdesi geçersiz UTF-8 veya tekil vekil (surrogate) karakter içeriyor.",
                }
            },
            status_code=422,
        )

    operation_name = data.get("operation")
    if not isinstance(operation_name, str) or not operation_name:
        err = {"code": "unknown_operation", "message": "Operasyon adı geçerli bir metin (string) olmalıdır."}
        return JSONResponse({"error": err}, status_code=422)

    op = OPERATIONS.get(operation_name)
    if op is None:
        err = {"code": "unknown_operation", "message": f"Bilinmeyen operasyon: {operation_name}"}
        return JSONResponse({"error": err}, status_code=422)

    inp = data.get("input") if isinstance(data.get("input"), dict) else {}
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}

    logger.info("Invoke request received: tenant_id=%s request_id=%s operation=%s", tenant_id, request_id, operation_name)

    sem = _get_invoke_semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=CONCURRENCY_WAIT_TIMEOUT_S)
    except TimeoutError:
        return JSONResponse(
            {
                "error": {
                    "code": "server_busy",
                    "message": "Sunucu şu anda maksimum eşzamanlı istek kapasitesinde çalışıyor; lütfen kısa süre sonra tekrar deneyin.",
                }
            },
            status_code=429,
            headers={"Retry-After": "2"},
        )

    try:
        loop = asyncio.get_running_loop()
        is_safe, err_code, err_msg, body_bytes = await loop.run_in_executor(
            _INVOKE_EXECUTOR,
            _execute_invoke_sync,
            op,
            inp,
            cfg,
            tenant_id,
            request_id,
        )
        if not is_safe:
            return JSONResponse({"error": {"code": err_code, "message": err_msg}}, status_code=422)
        assert body_bytes is not None
    except HTTPException:
        raise
    except UnicodeError:
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_encoding",
                    "message": "İstek veya şema geçersiz UTF-8 / vekil (surrogate) karakter içeriyor.",
                }
            },
            status_code=422,
        )
    except ValueError as exc:
        msg = str(exc)
        candidate = msg.split(":", 1)[0].strip() if ":" in msg else ""
        if _ERROR_CODE_RE.fullmatch(candidate):
            code = candidate
        else:
            code = "validation_error"
            msg = "Girdi doğrulama hatası."
        return JSONResponse({"error": {"code": code, "message": msg}}, status_code=422)
    except InputShapeError as exc:
        logger.warning(
            "Input shape error in operation '%s': tenant_id=%s request_id=%s exc_type=%s",
            operation_name,
            tenant_id,
            request_id,
            type(exc.__cause__ or exc).__name__,
        )
        return JSONResponse(
            {"error": {"code": "invalid_input_shape", "message": "Input could not be processed."}},
            status_code=422,
        )
    except Exception as exc:
        logger.error(
            "Internal processing error in operation '%s': tenant_id=%s request_id=%s exc_type=%s",
            operation_name,
            tenant_id,
            request_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"error": {"code": "processing_failed", "message": "Request could not be processed."}},
            status_code=500,
        )
    finally:
        sem.release()

    return Response(content=body_bytes, media_type="application/json")
