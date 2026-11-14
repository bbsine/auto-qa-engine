"""Deterministic OpenAPI v3.x (JSON/YAML) parser and $ref resolver."""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import yaml

from src.security import contains_prohibited_payment_data, scan_object_for_sensitive_data

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")
MAX_SCHEMA_DEPTH = 40
MAX_ALIAS_EXPANDED_NODES = 200_000
MAX_SPEC_TOTAL_NODES = 300_000
MAX_REF_EXPANSION_NODES = 150_000


@dataclass
class ParameterSpec:
    """Normalized OpenAPI parameter (path, query, header, cookie)."""

    name: str
    location: str  # "path" | "query" | "header" | "cookie"
    required: bool = False
    schema: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class OperationSpec:
    """Normalized representation of a single HTTP operation on a path."""

    operation_id: str
    method: str  # Uppercase HTTP method: GET, POST, PUT, PATCH, DELETE
    path: str
    summary: str = ""
    description: str = ""
    parameters: list[ParameterSpec] = field(default_factory=list)
    request_body_schema: dict[str, Any] | None = None
    request_body_required: bool = False
    response_schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    status_codes: list[str] = field(default_factory=list)
    security_required: bool = False
    resource_group: str = ""
    # Media type used to send the request body ("application/json", "application/x-www-form-urlencoded",
    # "multipart/form-data", ...). None when the operation has no request body.
    request_content_type: str | None = None


@dataclass
class ParsedOpenAPISpec:
    """Normalized OpenAPI specification Intermediate Representation (IR)."""

    title: str
    version: str
    servers: list[str] = field(default_factory=list)
    operations: list[OperationSpec] = field(default_factory=list)
    raw_schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Path prefix under which the API is mounted (Swagger 2.0 `basePath`, or the path component of the
    # OpenAPI 3 `servers[0].url`). Empty string when there is no prefix or the server URL is templated.
    base_path: str = ""

    @property
    def paths(self) -> list[str]:
        """Return unique API paths preserving order."""
        return list(dict.fromkeys(op.path for op in self.operations))


def _validate_structure_bounds(root: Any) -> None:
    """Detect cyclic YAML aliases and exponential alias bombs in O(unique_nodes) time before traversal."""
    memo_size: dict[int, int] = {}
    active: set[int] = set()
    alias_reused_nodes = 0

    def _measure(node: Any, depth: int) -> int:
        nonlocal alias_reused_nodes
        if depth > MAX_SCHEMA_DEPTH * 2:
            raise ValueError("invalid_openapi_syntax: Şema derinliği izin verilen sınırı aşıyor.")
        if not isinstance(node, (dict, list)):
            return 1
        obj_id = id(node)
        if obj_id in active:
            raise ValueError("invalid_openapi_syntax: Şemada döngüsel YAML alias referansı tespit edildi.")
        if obj_id in memo_size:
            reused = memo_size[obj_id]
            alias_reused_nodes += reused
            if alias_reused_nodes > MAX_ALIAS_EXPANDED_NODES:
                raise ValueError(
                    "invalid_openapi_syntax: Şema boyutu veya YAML alias genişlemesi izin verilen sınırı aşıyor."
                )
            return reused
        active.add(obj_id)
        total = 1
        if isinstance(node, dict):
            for _, v in node.items():
                total += 1 + _measure(v, depth + 1)
                if total > MAX_SPEC_TOTAL_NODES or alias_reused_nodes > MAX_ALIAS_EXPANDED_NODES:
                    raise ValueError(
                        "invalid_openapi_syntax: Şema boyutu veya YAML alias genişlemesi izin verilen sınırı aşıyor."
                    )
        else:
            for item in node:
                total += _measure(item, depth + 1)
                if total > MAX_SPEC_TOTAL_NODES or alias_reused_nodes > MAX_ALIAS_EXPANDED_NODES:
                    raise ValueError(
                        "invalid_openapi_syntax: Şema boyutu veya YAML alias genişlemesi izin verilen sınırı aşıyor."
                    )
        active.remove(obj_id)
        memo_size[obj_id] = total
        return total

    _measure(root, 0)


def _deref_shallow(node: Any, root: dict[str, Any]) -> Any:
    """Resolve top-level `$ref` pointers without recursively expanding child schemas."""
    current = node
    visited: set[str] = set()
    depth = 0
    while isinstance(current, dict) and isinstance(current.get("$ref"), str) and depth <= MAX_SCHEMA_DEPTH:
        ref = current["$ref"]
        if not ref.startswith("#/") or ref in visited:
            break
        visited.add(ref)
        parts = [p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/")]
        target: Any = root
        for part in parts:
            if isinstance(target, dict) and part in target:
                target = target[part]
            else:
                target = None
                break
        if not isinstance(target, dict):
            break
        siblings = {k: v for k, v in current.items() if k != "$ref"}
        current = {**target, **siblings} if siblings else target
        depth += 1
    return current


def _has_circular_marker(node: Any) -> bool:
    """Return True if a resolved subtree contains a circular-$ref placeholder marker."""
    if isinstance(node, dict):
        if "x-circular-ref" in node:
            return True
        return any(_has_circular_marker(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_circular_marker(item) for item in node)
    return False


def _resolve_json_pointer(
    root: dict[str, Any],
    ref: str,
    visited: set[str] | None = None,
    depth: int = 0,
    _budget: list[int] | None = None,
    _ref_cache: dict[str, tuple[dict[str, Any], int]] | None = None,
) -> dict[str, Any]:
    """Resolve an internal JSON pointer like '#/components/schemas/User' with recursion and expansion guards."""
    if not isinstance(ref, str) or not ref.startswith("#/") or depth > MAX_SCHEMA_DEPTH:
        return {"type": "object"}

    if visited is None:
        visited = set()
    if ref in visited:
        # Break circular $ref cleanly
        return {"type": "object", "x-circular-ref": ref}

    if _budget is None:
        _budget = [0]
    if _ref_cache is None:
        _ref_cache = {}

    if ref in _ref_cache:
        cached_schema, node_weight = _ref_cache[ref]
        _budget[0] += node_weight
        if _budget[0] > MAX_REF_EXPANSION_NODES:
            raise ValueError("invalid_openapi_syntax: Şema referans ($ref) genişlemesi sınırı aşıyor.")
        return copy.deepcopy(cached_schema)

    visited = visited | {ref}
    parts = [p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/")]
    current: Any = root
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return {"type": "object"}

    if not isinstance(current, dict):
        return {"type": "object"}

    budget_before = _budget[0]
    resolved = resolve_schema_refs(current, root, visited, depth + 1, _budget=_budget, _ref_cache=_ref_cache)
    if isinstance(resolved, dict) and not _has_circular_marker(resolved):
        _ref_cache[ref] = (copy.deepcopy(resolved), max(1, _budget[0] - budget_before))
    return resolved


def resolve_schema_refs(
    node: Any,
    root: dict[str, Any],
    visited: set[str] | None = None,
    depth: int = 0,
    _budget: list[int] | None = None,
    _ref_cache: dict[str, tuple[dict[str, Any], int]] | None = None,
) -> Any:
    """Recursively resolve all internal $ref pointers within a schema dictionary or list."""
    if _budget is None:
        _budget = [0]
    if _ref_cache is None:
        _ref_cache = {}

    if depth > MAX_SCHEMA_DEPTH:
        return {"type": "object"} if isinstance(node, dict) else node

    if isinstance(node, list):
        _budget[0] += 1
        if _budget[0] > MAX_REF_EXPANSION_NODES:
            raise ValueError("invalid_openapi_syntax: Şema referans ($ref) genişlemesi sınırı aşıyor.")
        return [
            resolve_schema_refs(item, root, visited, depth + 1, _budget=_budget, _ref_cache=_ref_cache)
            for item in node
        ]

    if not isinstance(node, dict):
        return node

    _budget[0] += 1
    if _budget[0] > MAX_REF_EXPANSION_NODES:
        raise ValueError("invalid_openapi_syntax: Şema referans ($ref) genişlemesi sınırı aşıyor.")

    if "$ref" in node and isinstance(node["$ref"], str):
        resolved = copy.deepcopy(
            _resolve_json_pointer(
                root,
                node["$ref"],
                visited,
                depth + 1,
                _budget=_budget,
                _ref_cache=_ref_cache,
            )
        )
        # Merge any sibling keys overriding the $ref (OpenAPI 3.1 style)
        for k, v in node.items():
            if k != "$ref":
                resolved[k] = resolve_schema_refs(
                    v,
                    root,
                    visited,
                    depth + 1,
                    _budget=_budget,
                    _ref_cache=_ref_cache,
                )
        return resolved

    result: dict[str, Any] = {}
    all_of_subs: list[Any] | None = None
    for k, v in node.items():
        if k == "allOf" and isinstance(v, list):
            all_of_subs = [
                resolve_schema_refs(sub, root, visited, depth + 1, _budget=_budget, _ref_cache=_ref_cache)
                for sub in v
            ]
        elif k in ("anyOf", "oneOf") and isinstance(v, list):
            result[k] = [
                resolve_schema_refs(sub, root, visited, depth + 1, _budget=_budget, _ref_cache=_ref_cache)
                for sub in v
            ]
        else:
            result[k] = resolve_schema_refs(
                v,
                root,
                visited,
                depth + 1,
                _budget=_budget,
                _ref_cache=_ref_cache,
            )

    if all_of_subs is not None:
        merged_props: dict[str, Any] = {}
        merged_req: list[str] = []
        inferred_type: Any = None
        for sub in all_of_subs:
            if not isinstance(sub, dict):
                continue
            if inferred_type is None and "type" in sub:
                inferred_type = sub["type"]
            if isinstance(sub.get("properties"), dict):
                merged_props.update(sub["properties"])
            if isinstance(sub.get("required"), list):
                for req_field in sub["required"]:
                    if isinstance(req_field, str) and req_field not in merged_req:
                        merged_req.append(req_field)
            for sk, sv in sub.items():
                if sk not in ("properties", "required", "type") and sk not in result:
                    result[sk] = sv

        sibling_props = result.get("properties")
        if isinstance(sibling_props, dict):
            merged_props.update(sibling_props)
        if merged_props or isinstance(sibling_props, dict):
            result["properties"] = merged_props

        sibling_req = result.get("required")
        if isinstance(sibling_req, list):
            for req_field in sibling_req:
                if isinstance(req_field, str) and req_field not in merged_req:
                    merged_req.append(req_field)
        if merged_req or isinstance(sibling_req, list):
            result["required"] = merged_req

        if "type" not in result:
            if inferred_type is not None:
                result["type"] = inferred_type
            elif "properties" in result:
                result["type"] = "object"
    return result


def _extract_resource_group(path: str) -> str:
    """Extract primary resource name from a path (e.g., '/v1/users/{id}/orders' -> 'users')."""
    segments = [seg for seg in path.strip("/").split("/") if seg and not seg.startswith("{")]
    # Skip common API version prefixes
    filtered = [s for s in segments if not re.match(r"^v\d+(\.\d+)?$", s, re.IGNORECASE)]
    if filtered:
        return filtered[-1].lower()
    if segments:
        return segments[0].lower()
    return "root"


JSON_MEDIA_TYPE = "application/json"
FORM_URLENCODED_MEDIA_TYPE = "application/x-www-form-urlencoded"
MULTIPART_MEDIA_TYPE = "multipart/form-data"


def _normalize_base_path(raw: Any) -> str:
    """Normalize a URL path prefix: leading slash, no trailing slash, '' for root/empty/templated values."""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text or "{" in text or "}" in text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    text = text.rstrip("/")
    return text


def _base_path_from_server_url(url: Any) -> str:
    """Return the path component of an OpenAPI 3 server URL, ignoring templated URLs like '{scheme}://host/v1'."""
    if not isinstance(url, str) or "{" in url or "}" in url:
        return ""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return ""
    return _normalize_base_path(parsed.path)


def _clean_media_types(raw: Any) -> list[str]:
    """Return a lowercase list of media type strings from a Swagger 2.0 `consumes` value."""
    if not isinstance(raw, list):
        return []
    return [str(m).strip().lower() for m in raw if isinstance(m, str) and m.strip()]


def _swagger2_content_type(
    op_consumes: list[str],
    doc_consumes: list[str],
    has_body_param: bool,
    has_form_params: bool,
    has_file_param: bool,
) -> str | None:
    """Select the request media type for a Swagger 2.0 operation.

    Operation-level `consumes` wins over document-level `consumes`. Within the chosen list, the entry that fits the
    parameter kind is preferred (multipart when a `type: file` form parameter exists, urlencoded/multipart for other
    formData parameters, JSON for `in: body`). Without any `consumes`, formData defaults to urlencoded (multipart when a
    file parameter exists) and `in: body` defaults to JSON.
    """
    if not has_body_param and not has_form_params:
        return None
    consumes = op_consumes or doc_consumes
    if has_form_params:
        preferred = (
            [MULTIPART_MEDIA_TYPE, FORM_URLENCODED_MEDIA_TYPE]
            if has_file_param
            else [FORM_URLENCODED_MEDIA_TYPE, MULTIPART_MEDIA_TYPE]
        )
        for mime in preferred:
            if mime in consumes:
                return mime
        if consumes:
            return consumes[0]
        return MULTIPART_MEDIA_TYPE if has_file_param else FORM_URLENCODED_MEDIA_TYPE
    if JSON_MEDIA_TYPE in consumes:
        return JSON_MEDIA_TYPE
    if consumes:
        return consumes[0]
    return JSON_MEDIA_TYPE


def _sanitize_operation_id(method: str, path: str, raw_op_id: str | None) -> str:
    """Generate a deterministic, snake_case Python-safe identifier for an operation."""
    if raw_op_id and isinstance(raw_op_id, str) and raw_op_id.strip():
        clean = re.sub(r"[^a-zA-Z0-9_]", "_", raw_op_id.strip())
        clean = re.sub(r"_+", "_", clean).strip("_").lower()
        if clean:
            return clean

    slug = re.sub(r"\{([^}]+)\}", r"by_\1", path)
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_").lower()
    return f"{method.lower()}_{slug or 'root'}"


def parse_openapi_spec(
    spec_input: str | dict[str, Any],
    target_endpoints: list[str] | None = None,
) -> ParsedOpenAPISpec:
    """Parse an OpenAPI 3.x (or Swagger 2.0 compatible) JSON/YAML specification into a normalized IR.

    Raises ValueError with an explicit error code prefix if the input is malformed or empty.
    """
    if isinstance(spec_input, dict):
        _validate_structure_bounds(spec_input)
        raw_doc = copy.deepcopy(spec_input)
    elif isinstance(spec_input, str):
        text = spec_input.strip()
        if not text:
            raise ValueError("empty_openapi_spec: OpenAPI şema metni boş olamaz.")
        try:
            if text.startswith("{"):
                raw_doc = json.loads(text)
            else:
                raw_doc = yaml.safe_load(text)
        except Exception as exc:
            raise ValueError(f"invalid_openapi_syntax: OpenAPI JSON/YAML ayrıştırma hatası: {exc}") from exc
    else:
        raise ValueError("invalid_openapi_type: OpenAPI şeması metin (JSON/YAML) veya nesne olmalıdır.")

    if not isinstance(raw_doc, dict):
        raise ValueError("invalid_openapi_root: OpenAPI şeması bir kök nesne (mapping) olmalıdır.")

    _validate_structure_bounds(raw_doc)

    # Scan both raw string (if provided) and parsed YAML/JSON structure (after YAML escape/folding) for prohibited PAN/CVV/TCKN data
    if (isinstance(spec_input, str) and contains_prohibited_payment_data(spec_input)) or scan_object_for_sensitive_data(raw_doc):
        raise ValueError(
            "prohibited_sensitive_data: Girdide kart numarası, CVV, TCKN veya banka şifresi gibi hassas veri tespit edildi."
        )

    paths_obj = raw_doc.get("paths")
    if not isinstance(paths_obj, dict) or not paths_obj:
        raise ValueError("missing_paths: OpenAPI şemasında geçerli ve dolu bir 'paths' alanı bulunamadı.")

    info_obj = raw_doc.get("info") if isinstance(raw_doc.get("info"), dict) else {}
    title = str(info_obj.get("title") or "Untitled API")
    version = str(info_obj.get("version") or "1.0.0")

    servers: list[str] = []
    base_path = ""
    if isinstance(raw_doc.get("servers"), list):
        for srv in raw_doc["servers"]:
            if isinstance(srv, dict) and isinstance(srv.get("url"), str):
                servers.append(srv["url"])
        if servers:
            base_path = _base_path_from_server_url(servers[0])
    elif isinstance(raw_doc.get("host"), str) and raw_doc["host"].strip():
        # Swagger 2.0 host + basePath + schemes support
        schemes = raw_doc.get("schemes") if isinstance(raw_doc.get("schemes"), list) else []
        scheme = (
            str(schemes[0]).strip().lower()
            if schemes and isinstance(schemes[0], str) and schemes[0].strip()
            else "http"
        )
        host = raw_doc["host"].strip().rstrip("/")
        base_path = str(raw_doc.get("basePath") or "").strip()
        if base_path and not base_path.startswith("/"):
            base_path = "/" + base_path
        base_path = base_path.rstrip("/")
        servers.append(f"{scheme}://{host}{base_path}")
        base_path = _normalize_base_path(base_path)
    if not base_path and "swagger" in raw_doc:
        # Swagger 2.0 without host still declares the mount prefix in basePath
        base_path = _normalize_base_path(raw_doc.get("basePath"))

    doc_consumes = _clean_media_types(raw_doc.get("consumes"))

    global_security = bool(raw_doc.get("security"))

    ref_cache: dict[str, tuple[dict[str, Any], int]] = {}
    ref_budget: list[int] = [0]

    # Resolve component schemas (OpenAPI 3.x components.schemas + Swagger 2.0 definitions)
    components = raw_doc.get("components") if isinstance(raw_doc.get("components"), dict) else {}
    schemas_raw = components.get("schemas") if isinstance(components.get("schemas"), dict) else {}
    definitions_raw = raw_doc.get("definitions") if isinstance(raw_doc.get("definitions"), dict) else {}
    resolved_schemas: dict[str, dict[str, Any]] = {}
    for s_name, s_val in {**definitions_raw, **schemas_raw}.items():
        if isinstance(s_val, dict):
            resolved_schemas[str(s_name)] = resolve_schema_refs(
                s_val,
                raw_doc,
                _budget=ref_budget,
                _ref_cache=ref_cache,
            )

    target_set = set(target_endpoints) if target_endpoints else None
    operations: list[OperationSpec] = []
    seen_op_ids: set[str] = set()

    for path_idx, (path_str, path_item) in enumerate(paths_obj.items()):
        if path_idx and path_idx % 250 == 0:
            time.sleep(0)
        if not isinstance(path_str, str) or not isinstance(path_item, dict):
            continue
        if target_set is not None and path_str not in target_set:
            continue

        # Path-level parameters
        path_level_params: list[dict[str, Any]] = (
            path_item.get("parameters") if isinstance(path_item.get("parameters"), list) else []
        )

        for method_lower in HTTP_METHODS:
            op_obj = path_item.get(method_lower)
            if not isinstance(op_obj, dict):
                continue

            base_op_id = _sanitize_operation_id(method_lower, path_str, op_obj.get("operationId"))
            op_id = base_op_id
            counter = 2
            while op_id in seen_op_ids:
                op_id = f"{base_op_id}_{counter}"
                counter += 1
            seen_op_ids.add(op_id)

            # Merge path-level and operation-level parameters
            op_params_raw = op_obj.get("parameters") if isinstance(op_obj.get("parameters"), list) else []
            merged_params: dict[tuple[str, str], ParameterSpec] = {}
            swagger2_body_schema: dict[str, Any] | None = None
            swagger2_body_required = False
            has_body_param = False
            has_file_param = False
            form_props: dict[str, Any] = {}
            form_required: list[str] = []

            for p_raw in path_level_params + op_params_raw:
                p_resolved = resolve_schema_refs(p_raw, raw_doc, _budget=ref_budget, _ref_cache=ref_cache)
                if not isinstance(p_resolved, dict):
                    continue
                p_in = str(p_resolved.get("in") or "query").strip().lower()
                if p_in == "body":
                    candidate_body = p_resolved.get("schema")
                    if isinstance(candidate_body, dict):
                        swagger2_body_schema = candidate_body
                        swagger2_body_required = bool(p_resolved.get("required", False))
                        has_body_param = True
                    continue

                p_name = str(p_resolved.get("name") or "").strip()
                if not p_name:
                    continue

                if p_in == "formdata":
                    f_schema = p_resolved.get("schema")
                    if not isinstance(f_schema, dict):
                        f_schema = {
                            k: v
                            for k, v in p_resolved.items()
                            if k
                            in (
                                "type",
                                "format",
                                "enum",
                                "minimum",
                                "maximum",
                                "minLength",
                                "maxLength",
                                "items",
                                "default",
                            )
                        }
                        if "type" not in f_schema:
                            f_schema["type"] = "string"
                    form_props[p_name] = f_schema
                    if str(form_props[p_name].get("type") or "").strip().lower() == "file":
                        has_file_param = True
                    if p_resolved.get("required") and p_name not in form_required:
                        form_required.append(p_name)
                    continue

                p_schema = p_resolved.get("schema")
                if not isinstance(p_schema, dict):
                    p_schema = {
                        k: v
                        for k, v in p_resolved.items()
                        if k
                        in (
                            "type",
                            "format",
                            "enum",
                            "minimum",
                            "maximum",
                            "minLength",
                            "maxLength",
                            "items",
                            "default",
                        )
                    }
                    if "type" not in p_schema:
                        p_schema["type"] = "string"
                merged_params[(p_name, p_in)] = ParameterSpec(
                    name=p_name,
                    location=p_in,
                    required=bool(p_resolved.get("required", p_in == "path")),
                    schema=p_schema,
                    description=str(p_resolved.get("description") or ""),
                )

            # Ensure any {param} in path template is registered as a required path parameter
            for path_var in re.findall(r"\{([^}]+)\}", path_str):
                key = (path_var, "path")
                if key not in merged_params:
                    merged_params[key] = ParameterSpec(
                        name=path_var,
                        location="path",
                        required=True,
                        schema={"type": "string"},
                    )

            # Extract requestBody schema (OpenAPI 3.x requestBody or Swagger 2.0 in: body / in: formData)
            req_body_schema: dict[str, Any] | None = None
            req_body_required = False
            req_content_type: str | None = None
            req_body_raw = _deref_shallow(op_obj.get("requestBody"), raw_doc)
            if isinstance(req_body_raw, dict):
                req_body_required = bool(req_body_raw.get("required", False))
                content_map = req_body_raw.get("content")
                if isinstance(content_map, dict):
                    normalized_content = {
                        str(k).strip().lower(): v for k, v in content_map.items() if isinstance(k, str)
                    }
                    for mime in (JSON_MEDIA_TYPE, FORM_URLENCODED_MEDIA_TYPE, MULTIPART_MEDIA_TYPE, "*/*"):
                        if mime in normalized_content and isinstance(normalized_content[mime], dict):
                            candidate = normalized_content[mime].get("schema")
                            if isinstance(candidate, dict):
                                req_body_schema = resolve_schema_refs(
                                    candidate,
                                    raw_doc,
                                    _budget=ref_budget,
                                    _ref_cache=ref_cache,
                                )
                                req_content_type = mime
                                break
                    if req_body_schema is None:
                        for mime_key, media_obj in normalized_content.items():
                            if isinstance(media_obj, dict) and isinstance(media_obj.get("schema"), dict):
                                req_body_schema = resolve_schema_refs(
                                    media_obj["schema"],
                                    raw_doc,
                                    _budget=ref_budget,
                                    _ref_cache=ref_cache,
                                )
                                req_content_type = mime_key
                                break

            if req_body_schema is None:
                if swagger2_body_schema is not None:
                    req_body_schema = swagger2_body_schema
                    req_body_required = swagger2_body_required
                elif form_props:
                    req_body_schema = {
                        "type": "object",
                        "properties": form_props,
                        "required": form_required,
                    }
                    req_body_required = bool(form_required)
                req_content_type = _swagger2_content_type(
                    _clean_media_types(op_obj.get("consumes")),
                    doc_consumes,
                    has_body_param=swagger2_body_schema is not None and has_body_param,
                    has_form_params=swagger2_body_schema is None and bool(form_props),
                    has_file_param=has_file_param,
                )

            # Extract responses and status codes (OpenAPI 3.x content.*.schema + Swagger 2.0 responses.<code>.schema)
            responses_raw = op_obj.get("responses") if isinstance(op_obj.get("responses"), dict) else {}
            status_codes: list[str] = []
            response_schemas: dict[str, dict[str, Any]] = {}

            for status_key, resp_val in responses_raw.items():
                code_str = str(status_key)
                status_codes.append(code_str)
                resp_resolved = _deref_shallow(resp_val, raw_doc)
                if isinstance(resp_resolved, dict):
                    resp_content = resp_resolved.get("content")
                    if isinstance(resp_content, dict):
                        for media_obj in resp_content.values():
                            if isinstance(media_obj, dict) and isinstance(media_obj.get("schema"), dict):
                                response_schemas[code_str] = resolve_schema_refs(
                                    media_obj["schema"],
                                    raw_doc,
                                    _budget=ref_budget,
                                    _ref_cache=ref_cache,
                                )
                                break
                    if code_str not in response_schemas and isinstance(resp_resolved.get("schema"), dict):
                        response_schemas[code_str] = resolve_schema_refs(
                            resp_resolved["schema"],
                            raw_doc,
                            _budget=ref_budget,
                            _ref_cache=ref_cache,
                        )

            if not status_codes:
                status_codes = ["200"] if method_lower != "post" else ["201"]

            has_op_security = "security" in op_obj and bool(op_obj.get("security"))
            security_required = has_op_security or (global_security and op_obj.get("security") != [])

            operations.append(
                OperationSpec(
                    operation_id=op_id,
                    method=method_lower.upper(),
                    path=path_str,
                    summary=str(op_obj.get("summary") or ""),
                    description=str(op_obj.get("description") or ""),
                    parameters=list(merged_params.values()),
                    request_body_schema=req_body_schema,
                    request_body_required=req_body_required,
                    response_schemas=response_schemas,
                    status_codes=status_codes,
                    security_required=security_required,
                    resource_group=_extract_resource_group(path_str),
                    request_content_type=req_content_type,
                )
            )

    if not operations:
        raise ValueError("no_operations_found: OpenAPI şemasında çalıştırılabilir HTTP operasyonu bulunamadı.")

    return ParsedOpenAPISpec(
        title=title,
        version=version,
        servers=servers,
        operations=operations,
        raw_schemas=resolved_schemas,
        base_path=base_path,
    )
