"""AST Syntax Sieve, static security checker, and deterministic fallback template builder."""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from src.planner import PlannedStep, resolve_path_param_var

# Dangerous functions/modules forbidden in generated test code (including file/process access)
FORBIDDEN_CALL_NAMES = {"eval", "exec", "compile", "__import__", "system", "popen", "open"}
FORBIDDEN_IMPORT_MODULES = {"subprocess", "pty", "shutil", "socket"}

_CODE_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

_SCHEMA_TYPE_TO_PY_EXPR: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "(int, float)",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def strip_markdown_fences(code: str) -> str:
    """Cleanly extract Python code from markdown code fences if present."""
    if not code or not isinstance(code, str):
        return ""
    text = code.strip()
    matches = _CODE_FENCE_RE.findall(text)
    if matches:
        return "\n\n".join(m.strip() for m in matches if m.strip())

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    return text


def validate_test_syntax(code: str) -> tuple[bool, str | None]:
    """Validate Python syntax using ast.parse() and verify AST safety rules.

    Returns:
        (True, None) if the code is valid, safe Python.
        (False, error_message) if syntax error, empty code, or forbidden unsafe AST call is found.
    """
    cleaned = strip_markdown_fences(code)
    if not cleaned:
        return False, "SyntaxError: Generated code is empty"

    try:
        tree = ast.parse(cleaned)
    except (SyntaxError, IndentationError) as exc:
        line_info = f"line {exc.lineno}, col {exc.offset}" if exc.lineno else "unknown line"
        return False, f"{type(exc).__name__} at {line_info}: {exc.msg} ({exc.text!r})"
    except Exception as exc:
        return False, f"ASTParseError: {exc}"

    # Inspect AST for forbidden imports or dangerous execution calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".")[0]
                if root_mod in FORBIDDEN_IMPORT_MODULES:
                    return False, f"SecurityError: Forbidden module import '{root_mod}' in generated test code"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_mod = node.module.split(".")[0]
                if root_mod in FORBIDDEN_IMPORT_MODULES:
                    return False, f"SecurityError: Forbidden module import from '{root_mod}' in generated test code"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALL_NAMES:
                return False, f"SecurityError: Forbidden function call '{node.func.id}()' in generated test code"
            if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_CALL_NAMES:
                return False, f"SecurityError: Forbidden method call '.{node.func.attr}()' in generated test code"

    return True, None


def count_ast_assertions(code: str) -> int:
    """Count total `assert` statements in valid Python test code."""
    cleaned = strip_markdown_fences(code)
    try:
        tree = ast.parse(cleaned)
    except Exception:
        return 0
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))


def _safe_identifier(raw: str, fallback: str = "op") -> str:
    """Guarantee a valid Python identifier for function names."""
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", str(raw or "")).strip("_").lower()
    if not clean or clean[0].isdigit():
        clean = f"{fallback}_{clean}" if clean else fallback
    return clean


def _get_success_response_schema(op_response_schemas: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first defined 2xx or default response schema for an operation."""
    for code in ("200", "201", "202", "default"):
        schema = op_response_schemas.get(code)
        if isinstance(schema, dict):
            return schema
    for code, schema in op_response_schemas.items():
        if str(code).startswith("2") and isinstance(schema, dict):
            return schema
    return None


# In-memory sample file used for multipart file fields in generated tests (never read from disk).
SAMPLE_UPLOAD_FILE: tuple[str, bytes, str] = ("sample.txt", b"sample", "text/plain")


def body_encoding_for_content_type(content_type: str | None) -> str:
    """Map a request media type to the httpx body keyword family: 'json', 'form' (data=) or 'multipart' (data= + files=)."""
    media = str(content_type or "").split(";", 1)[0].strip().lower()
    if media == "application/x-www-form-urlencoded":
        return "form"
    if media == "multipart/form-data":
        return "multipart"
    return "json"


def _file_field_names(body_schema: dict[str, Any] | None) -> set[str]:
    """Return request body property names that carry file content (type 'file' or format 'binary'/'byte')."""
    if not isinstance(body_schema, dict):
        return set()
    props = body_schema.get("properties")
    if not isinstance(props, dict):
        return set()
    names: set[str] = set()
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        raw_type = spec.get("type")
        types = [str(t).lower() for t in raw_type] if isinstance(raw_type, list) else [str(raw_type or "").lower()]
        fmt = str(spec.get("format") or "").lower()
        if "file" in types or fmt in ("binary", "byte"):
            names.add(str(name))
    return names


def _form_scalar(value: Any) -> Any:
    """Encode one value for form transport: booleans -> 'true'/'false', None -> '', dict/list -> JSON string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, ensure_ascii=False)


def encode_form_value(value: Any) -> Any:
    """Make a payload value form-encodable: scalars stay scalars, arrays stay lists, nested objects become JSON strings."""
    if isinstance(value, list):
        return [_form_scalar(item) for item in value]
    return _form_scalar(value)


def split_form_payload(
    payload: Any,
    file_fields: set[str],
) -> tuple[dict[str, Any], dict[str, tuple[str, bytes, str]]]:
    """Split a mock payload into form `data` and multipart `files` dictionaries.

    File fields present in the payload are replaced by the in-memory SAMPLE_UPLOAD_FILE tuple; every other value is
    made form-encodable via `encode_form_value`. Non-dict payloads yield an empty form.
    """
    data: dict[str, Any] = {}
    files: dict[str, tuple[str, bytes, str]] = {}
    if not isinstance(payload, dict):
        return data, files
    for key, value in payload.items():
        key_str = str(key)
        if key_str in file_fields:
            files[key_str] = SAMPLE_UPLOAD_FILE
        else:
            data[key_str] = encode_form_value(value)
    return data, files


def _emit_body_request(
    lines: list[str],
    method_lower: str,
    var_name: str,
    payload: Any,
    encoding: str,
    file_fields: set[str],
    apply_odg_state: bool = False,
) -> None:
    """Append the payload assignment and the httpx call using json=, data= or data= + files= (all values via repr())."""
    if encoding == "json":
        lines.append(f"    {var_name} = {repr(payload)}")
        if apply_odg_state:
            lines.append(f"    if isinstance({var_name}, dict):")
            lines.append(f"        for key in list({var_name}.keys()):")
            lines.append("            if key in ODG_STATE:")
            lines.append(f"                {var_name}[key] = ODG_STATE[key]")
        lines.append(f"    response = api_client.{method_lower}(url, json={var_name})")
        return

    data_dict, files_dict = split_form_payload(payload, file_fields)
    lines.append(f"    {var_name} = {repr(data_dict)}")
    if apply_odg_state:
        lines.append(f"    for key in list({var_name}.keys()):")
        lines.append("        if key in ODG_STATE:")
        lines.append(f"            {var_name}[key] = str(ODG_STATE[key])")
    if encoding == "multipart":
        lines.append(f"    {var_name}_files = {repr(files_dict)}")
        lines.append(f"    response = api_client.{method_lower}(url, data={var_name}, files={var_name}_files)")
    else:
        lines.append(f"    response = api_client.{method_lower}(url, data={var_name})")


def build_deterministic_pytest_suite(
    plan: list[PlannedStep],
    mock_datasets: dict[str, dict[str, Any]],
    base_url: str = "http://localhost:8000",
    include_negative_tests: bool = True,
    include_security_tests: bool = True,
) -> str:
    """Compile a 100% syntactically valid, injection-safe Pytest suite from the ODG plan and mock datasets.

    Security & Oracle guarantees:
    - All spec-derived strings (paths, parameter names, property names, payloads, comments) are emitted strictly via
      `repr()` / `json.dumps()` literals and never interpolated into raw f-strings or raw docstrings.
    - Positive tests verify response schema required fields, JSON field types, and parent/bound parameter ownership
      (e.g. verifying `data['user_id'] == ODG_STATE['user_id']` on `/users/{user_id}/orders/{order_id}` to catch BOLA).
    - Unknown-ID and unauthorized probes are emitted as `*_security_unknown_id_and_auth`.
    """
    safe_base_url = str(base_url or "http://localhost:8000").rstrip("/") or "http://localhost:8000"
    lines: list[str] = [
        '"""Auto-generated deterministic ODG integration & security test suite by auto-qa-engine."""',
        "",
        "import pytest",
        "import httpx",
        "",
        f"BASE_URL = {json.dumps(safe_base_url)}",
        "",
        "# Shared state store for Producer -> Consumer ODG resource ID bindings",
        "ODG_STATE: dict[str, object] = {}",
        "ATTEMPTED_PRODUCERS: set[str] = set()",
        "",
        "",
        "def _format_url(path_template: str, param_bindings: dict[str, str], override_val: str | None = None) -> str:",
        "    url = path_template",
        "    for param_name, state_key in param_bindings.items():",
        "        token = '{' + param_name + '}'",
        "        if override_val is not None:",
        "            val = override_val",
        "        else:",
        "            val = str(ODG_STATE.get(state_key, f'test-{state_key}-1'))",
        "        url = url.replace(token, val)",
        "    return url",
        "",
        "",
        "@pytest.fixture(scope='module')",
        "def api_client():",
        "    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:",
        "        yield client",
        "",
    ]

    produced_vars_so_far: set[str] = set()

    for step in plan:
        op = step.operation
        op_id = _safe_identifier(op.operation_id, fallback=f"step_{step.step}")
        method_lower = _safe_identifier(op.method.lower(), fallback="get")
        if method_lower not in ("get", "post", "put", "patch", "delete", "options", "head"):
            method_lower = "get"

        op_mocks = mock_datasets.get(op.operation_id, {})
        valid_payload = op_mocks.get("valid_payload")
        boundary_payloads = op_mocks.get("boundary_payloads", [])
        security_payloads = op_mocks.get("security_payloads", [])
        body_encoding = body_encoding_for_content_type(getattr(op, "request_content_type", None))
        file_fields = _file_field_names(op.request_body_schema) if body_encoding == "multipart" else set()

        pos_codes = [int(c) for c in op.status_codes if str(c).isdigit() and 200 <= int(c) < 300]
        if not pos_codes:
            pos_codes = [201, 200] if op.method == "POST" else [200, 204]

        param_bindings: dict[str, str] = dict(step.param_bindings)
        for param in op.parameters:
            if param.location == "path" and param.name not in param_bindings:
                param_bindings[param.name] = resolve_path_param_var(op, param.name)
        for raw_var in re.findall(r"\{([^}]+)\}", op.path):
            if raw_var not in param_bindings:
                param_bindings[raw_var] = resolve_path_param_var(op, raw_var)

        bound_producer_keys = [
            k for k in step.consumes if isinstance(k, str) and k in produced_vars_so_far
        ]
        for bound_val in param_bindings.values():
            if bound_val in produced_vars_so_far and bound_val not in bound_producer_keys:
                bound_producer_keys.append(bound_val)

        path_literal = repr(op.path)
        bindings_literal = repr(param_bindings)
        doc_summary = repr(
            f"Step {step.step}: {op.method} {op.path} (Produces: {step.produces}, Consumes: {step.consumes})"
        )

        # 1. Positive ODG Lifecycle Test
        lines.append(f"def test_step_{step.step:02d}_{op_id}_positive(api_client):")
        lines.append(f"    # {doc_summary}")
        if step.produces:
            lines.append(f"    ATTEMPTED_PRODUCERS.update({repr(list(step.produces))})")
        if bound_producer_keys:
            lines.append(
                f"    missing = [k for k in {repr(bound_producer_keys)} if k in ATTEMPTED_PRODUCERS and k not in ODG_STATE]"
            )
            lines.append("    if missing:")
            lines.append('        pytest.skip("Producer step did not capture: " + ", ".join(missing))')
        lines.append(f"    url = _format_url({path_literal}, {bindings_literal})")
        if valid_payload is not None and op.method in ("POST", "PUT", "PATCH"):
            _emit_body_request(
                lines, method_lower, "payload", valid_payload, body_encoding, file_fields, apply_odg_state=True
            )
        else:
            lines.append(f"    response = api_client.{method_lower}(url)")

        lines.append(f"    assert response.status_code in {repr(pos_codes)}")
        lines.append("    if response.status_code != 204 and response.headers.get('content-type', '').startswith('application/json'):")
        lines.append("        data = response.json()")
        lines.append("        assert data is not None")

        resp_schema = _get_success_response_schema(op.response_schemas)
        dict_body_lines: list[str] = []
        if step.produces:
            prod_var = step.produces[0]
            dict_body_lines.append(
                "            captured_id = data.get('id') or data.get('uuid') or data.get(" + repr(prod_var) + ")"
            )
            dict_body_lines.append("            if captured_id is not None:")
            dict_body_lines.append(f"                ODG_STATE[{repr(prod_var)}] = captured_id")

        # Emit response schema assertions for required fields and their types (using repr() literals)
        if isinstance(resp_schema, dict):
            req_fields = resp_schema.get("required") if isinstance(resp_schema.get("required"), list) else []
            props_map = resp_schema.get("properties") if isinstance(resp_schema.get("properties"), dict) else {}
            for req_field in req_fields:
                if isinstance(req_field, str):
                    req_lit = repr(req_field)
                    dict_body_lines.append(f"            assert {req_lit} in data")
                    prop_spec = props_map.get(req_field)
                    if isinstance(prop_spec, dict):
                        raw_t = prop_spec.get("type")
                        if isinstance(raw_t, list):
                            s_type = next(
                                (str(x).lower() for x in raw_t if isinstance(x, str) and x.lower() != "null"),
                                "",
                            )
                        else:
                            s_type = str(raw_t or "").lower()
                        py_type_expr = _SCHEMA_TYPE_TO_PY_EXPR.get(s_type)
                        if py_type_expr:
                            if s_type in ("integer", "number"):
                                dict_body_lines.append(
                                    f"            assert isinstance(data[{req_lit}], {py_type_expr}) and not isinstance(data[{req_lit}], bool)"
                                )
                            else:
                                dict_body_lines.append(f"            assert isinstance(data[{req_lit}], {py_type_expr})")

        # Always emit resource ownership / parent parameter binding assertions (even when response schema is omitted!)
        for param_name, state_key in param_bindings.items():
            candidate_props: list[str] = []
            for cand in (param_name, state_key):
                if isinstance(cand, str) and cand and cand not in candidate_props:
                    candidate_props.append(cand)
            if state_key == "user_id" or param_name.lower() in ("user_id", "userid", "owner_id"):
                for extra_key in ("user_id", "userId", "owner_id"):
                    if extra_key not in candidate_props:
                        candidate_props.append(extra_key)

            for c_prop in candidate_props:
                c_prop_lit = repr(c_prop)
                state_key_lit = repr(state_key)
                default_id_lit = repr(f"test-{state_key}-1")
                dict_body_lines.append(f"            if {c_prop_lit} in data:")
                dict_body_lines.append(
                    f"                assert str(data[{c_prop_lit}]) == str(ODG_STATE.get({state_key_lit}, {default_id_lit}))"
                )

        if dict_body_lines:
            lines.append("        if isinstance(data, dict):")
            lines.extend(dict_body_lines)
        lines.append("")

        # 2. Negative / Boundary Test
        if include_negative_tests and boundary_payloads and op.method in ("POST", "PUT", "PATCH"):
            neg_payload = boundary_payloads[0]
            lines.append(f"def test_step_{step.step:02d}_{op_id}_negative_boundary(api_client):")
            if bound_producer_keys:
                lines.append(
                    f"    missing = [k for k in {repr(bound_producer_keys)} if k in ATTEMPTED_PRODUCERS and k not in ODG_STATE]"
                )
                lines.append("    if missing:")
                lines.append('        pytest.skip("Producer step did not capture: " + ", ".join(missing))')
            lines.append(f"    url = _format_url({path_literal}, {bindings_literal})")
            _emit_body_request(lines, method_lower, "bad_payload", neg_payload, body_encoding, file_fields)
            lines.append("    assert response.status_code in (400, 422)")
            lines.append("")

        # 3. Security / OWASP API Test
        if include_security_tests:
            if security_payloads and op.method in ("POST", "PUT", "PATCH"):
                sec_payload = security_payloads[0]
                lines.append(f"def test_step_{step.step:02d}_{op_id}_security_injection(api_client):")
                if bound_producer_keys:
                    lines.append(
                        f"    missing = [k for k in {repr(bound_producer_keys)} if k in ATTEMPTED_PRODUCERS and k not in ODG_STATE]"
                    )
                    lines.append("    if missing:")
                    lines.append('        pytest.skip("Producer step did not capture: " + ", ".join(missing))')
                lines.append(f"    url = _format_url({path_literal}, {bindings_literal})")
                _emit_body_request(lines, method_lower, "malicious_payload", sec_payload, body_encoding, file_fields)
                lines.append("    assert response.status_code < 500")
                lines.append("    assert 'syntax error' not in response.text.lower()")
                lines.append("")
            elif param_bindings or op.parameters or op.security_required:
                lines.append(f"def test_step_{step.step:02d}_{op_id}_security_unknown_id_and_auth(api_client):")
                lines.append(
                    f"    probe_url = _format_url({path_literal}, {bindings_literal}, override_val='nonexistent-unauthorized-id-999999')"
                )
                lines.append(
                    f"    response = api_client.{method_lower}(probe_url, headers={{'Authorization': 'Bearer invalid-token'}})"
                )
                lines.append("    assert response.status_code in (400, 401, 403, 404, 422)")
                lines.append("")

        for prod_item in step.produces:
            if isinstance(prod_item, str) and prod_item:
                produced_vars_so_far.add(prod_item)

    return "\n".join(lines).strip() + "\n"
