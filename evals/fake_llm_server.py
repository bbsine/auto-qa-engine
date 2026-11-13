"""Fake OpenAI-compatible HTTP server (`stdlib http.server`) for testing & evaluating LLM proxy paths.

Supports switchable behaviors via `FakeLLMServer.mode`:
- `valid_json`: Returns valid enriched JSON mock payloads for any operation_id found in the prompt.
- `garbage_text`: Returns non-JSON garbage text (`"<<<BROKEN NON JSON OUTPUT>>>"`).
- `leak_pan_tckn`: Returns JSON containing a valid Visa PAN (`4532015112830366`) and valid TCKN (`10000000146`).
- `echo_system_prompt`: Returns JSON attempting to echo `SYNTHESIS_SYSTEM_PROMPT` and `PLATFORM_SIGNING_SECRET`.
- `slow_timeout`: Sleeps longer than client timeout to trigger `APITimeoutError`.
- `http_500`: Returns HTTP 500 Internal Server Error.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeLLMServer:
    """Context-managed stdlib HTTP server emulating `/v1/chat/completions` on an ephemeral free port."""

    def __init__(self, mode: str = "valid_json") -> None:
        self.mode = mode
        self.request_count = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "FakeLLMServer":
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                # Silence noisy stdout HTTP logs during test runs
                pass

            def do_POST(self) -> None:
                parent.request_count += 1
                content_len = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_len).decode("utf-8", errors="replace")

                mode = parent.mode
                if mode == "http_500":
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": {"message": "Simulated upstream LLM 500 error"}}')
                    return

                if mode == "slow_timeout":
                    time.sleep(0.6)

                # Extract operation_ids from the decoded chat messages (JSON body escapes quotes inside strings)
                prompt_text = raw_body
                try:
                    req_obj = json.loads(raw_body)
                    if isinstance(req_obj, dict) and isinstance(req_obj.get("messages"), list):
                        prompt_text = "\n".join(
                            str(m.get("content", "")) for m in req_obj["messages"] if isinstance(m, dict)
                        )
                except Exception:
                    prompt_text = raw_body

                op_ids = re.findall(r'"operation_id"\s*:\s*"([^"]+)"', prompt_text)
                if not op_ids:
                    op_ids = ["create_item", "create_user", "list_items", "debug_env"]

                if mode == "garbage_text":
                    assistant_text = "<<<GARBAGE_NOT_JSON {{{ invalid syntax :::"
                elif mode == "leak_pan_tckn":
                    leaked_obj = {
                        op_id: {
                            "valid_payload": {
                                "name": "Kart: 4532015112830366 TCKN: 10000000146",
                                "email": "leak@ornek.com",
                            },
                            "boundary_payloads": [{}],
                            "security_payloads": [{"name": "4532.0151.1283.0366"}],
                        }
                        for op_id in op_ids
                    }
                    assistant_text = json.dumps(leaked_obj, ensure_ascii=False)
                elif mode == "echo_system_prompt":
                    echo_obj = {
                        op_id: {
                            "valid_payload": {
                                "name": "SYNTHESIS_SYSTEM_PROMPT: Sen bir API Test ve Güvenlik Otomasyon Mühendisisin | PLATFORM_SIGNING_SECRET=test-secret",
                                "email": "echo@ornek.com",
                            },
                            "boundary_payloads": [{}],
                            "security_payloads": [{"name": "test-secret"}],
                        }
                        for op_id in op_ids
                    }
                    assistant_text = json.dumps(echo_obj, ensure_ascii=False)
                else:
                    # valid_json mode
                    valid_obj = {
                        op_id: {
                            "valid_payload": {
                                "name": "LLM Sentezlenen Ürün",
                                "title": "LLM Sentezlenen Ürün",
                                "email": "llm.kullanici@ornek.com",
                                "price": 249.90,
                                "quantity": 2,
                            },
                            "boundary_payloads": [{"name": "", "price": -10}],
                            "security_payloads": [{"name": "' OR '1'='1 --", "price": 0}],
                        }
                        for op_id in op_ids
                    }
                    assistant_text = json.dumps(valid_obj, ensure_ascii=False)

                resp_payload = {
                    "id": "chatcmpl-fake-1",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "fast",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": assistant_text},
                            "finish_reason": "stop",
                        }
                    ],
                }
                encoded = json.dumps(resp_payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError, OSError):
                    # Client timed out and closed socket intentionally during slow_timeout test
                    pass

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "FakeLLMServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
