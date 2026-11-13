"""Operation Dependency Graph (ODG) builder and topological test execution planner."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.parser import OperationSpec, ParsedOpenAPISpec

# Lifecycle priority for deterministic ordering and cycle breaking:
# POST (create) -> GET (collection/item read) -> PUT/PATCH (update) -> DELETE (teardown)
METHOD_PRIORITY = {
    "POST": 1,
    "GET": 2,
    "PUT": 3,
    "PATCH": 4,
    "DELETE": 5,
    "OPTIONS": 6,
    "HEAD": 7,
}


@dataclass
class PlannedStep:
    """Single topologically ordered step in the Operation Dependency Graph execution plan."""

    step: int
    operation: OperationSpec
    produces: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    param_bindings: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize step to agent.yaml output_schema format."""
        return {
            "step": self.step,
            "operation_id": self.operation.operation_id,
            "method": self.operation.method,
            "path": self.operation.path,
            "produces": list(self.produces),
            "consumes": list(self.consumes),
        }


def _singularize(resource: str) -> str:
    """Convert plural resource name to singular prefix (e.g., 'users' -> 'user', 'categories' -> 'category')."""
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", resource.lower()).strip("_")
    if clean.endswith("ies") and len(clean) > 3:
        return clean[:-3] + "y"
    if clean.endswith("ses") and len(clean) > 3:
        return clean[:-2]
    if clean.endswith("s") and not clean.endswith("ss") and len(clean) > 1:
        return clean[:-1]
    return clean or "item"


def resolve_path_param_var(op: OperationSpec, param_name: str) -> str:
    """Map a specific `{param_name}` placeholder in `op.path` to its exact canonical ODG state variable.

    Examples:
    - `/users/{id}` -> `param_name='id'` -> `'user_id'`
    - `/users/{user_id}/orders/{order_id}` -> `'user_id'` -> `'user_id'`, `'order_id'` -> `'order_id'`
    - `/users/{id}/orders/{order_id}` -> `'id'` -> `'user_id'`, `'order_id'` -> `'order_id'`
    """
    normalized_param = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", param_name)
    normalized_param = re.sub(r"[^a-zA-Z0-9_]", "_", normalized_param).strip("_").lower()

    # Find the path segment immediately preceding `{param_name}` if available
    segments = [seg for seg in op.path.strip("/").split("/") if seg]
    preceding_resource: str | None = None
    target_token = "{" + param_name + "}"
    for idx, seg in enumerate(segments):
        if seg == target_token and idx > 0:
            prev = segments[idx - 1]
            if not prev.startswith("{") and not re.match(r"^v\d+(\.\d+)?$", prev, re.IGNORECASE):
                preceding_resource = _singularize(prev)
                break

    fallback_resource = preceding_resource or _singularize(op.resource_group)

    if normalized_param in ("id", "uuid", "_id"):
        return f"{fallback_resource}_id"

    if normalized_param.endswith("_id"):
        return normalized_param

    if normalized_param.endswith("id") and len(normalized_param) > 2:
        return normalized_param[:-2].rstrip("_") + "_id"

    return f"{fallback_resource}_{normalized_param}"


def _infer_produced_variables(op: OperationSpec) -> list[str]:
    """Identify state variables produced by an operation (primarily POST/create endpoints)."""
    produced: list[str] = []
    singular = _singularize(op.resource_group)
    canonical_var = f"{singular}_id"

    if op.method == "POST":
        # Check 200/201 response schemas for id-like properties
        for code in ("201", "200", "default"):
            schema = op.response_schemas.get(code)
            if isinstance(schema, dict):
                props = schema.get("properties")
                if isinstance(props, dict):
                    for prop_name in props:
                        if prop_name.lower() in ("id", "uuid", "_id", canonical_var):
                            if canonical_var not in produced:
                                produced.append(canonical_var)
                        elif prop_name.lower().endswith("_id"):
                            if prop_name.lower() not in produced:
                                produced.append(prop_name.lower())
        # Even if response schema omits explicit properties, a POST on a resource collection produces {resource}_id
        if not op.path.rstrip("/").endswith("}") and canonical_var not in produced:
            produced.append(canonical_var)

    return produced


def _infer_consumed_variables(op: OperationSpec) -> tuple[list[str], dict[str, str]]:
    """Identify state variables required by an operation and return `(consumed_list, param_bindings_map)`."""
    consumed: list[str] = []
    param_bindings: dict[str, str] = {}
    singular = _singularize(op.resource_group)
    canonical_var = f"{singular}_id"

    # 1. Path parameters (each `{placeholder}` mapped uniquely to its own state variable)
    for param in op.parameters:
        if param.location == "path":
            var_name = resolve_path_param_var(op, param.name)
            param_bindings[param.name] = var_name
            if var_name not in consumed:
                consumed.append(var_name)

    # Also catch any raw `{placeholder}` in path string not listed in parameters
    for raw_var in re.findall(r"\{([^}]+)\}", op.path):
        if raw_var not in param_bindings:
            var_name = resolve_path_param_var(op, raw_var)
            param_bindings[raw_var] = var_name
            if var_name not in consumed:
                consumed.append(var_name)

    # 2. Request body foreign-key properties (e.g. POST /orders requiring 'user_id')
    if isinstance(op.request_body_schema, dict):
        props = op.request_body_schema.get("properties")
        if isinstance(props, dict):
            for prop_name in props:
                p_lower = prop_name.lower()
                if p_lower.endswith("_id") and p_lower != canonical_var:
                    if p_lower not in consumed:
                        consumed.append(p_lower)

    return consumed, param_bindings


def build_odg_plan(parsed_spec: ParsedOpenAPISpec) -> list[PlannedStep]:
    """Construct the Operation Dependency Graph (ODG) and return a topologically sorted execution plan."""
    ops = list(parsed_spec.operations)
    if not ops:
        return []

    produced_by_op: dict[str, list[str]] = {}
    consumed_by_op: dict[str, list[str]] = {}
    bindings_by_op: dict[str, dict[str, str]] = {}
    producer_for_var: dict[str, str] = {}

    for op in ops:
        prod = _infer_produced_variables(op)
        cons, bindings = _infer_consumed_variables(op)
        produced_by_op[op.operation_id] = prod
        consumed_by_op[op.operation_id] = cons
        bindings_by_op[op.operation_id] = bindings
        for var in prod:
            if var not in producer_for_var:
                producer_for_var[var] = op.operation_id

    # Build adjacency list: dependencies[op_id] = set of op_ids that MUST run before op_id
    dependencies: dict[str, set[str]] = {op.operation_id: set() for op in ops}
    op_map: dict[str, OperationSpec] = {op.operation_id: op for op in ops}

    # 1. Producer -> Consumer edges based on state variable bindings
    for op in ops:
        for var in consumed_by_op[op.operation_id]:
            producer_id = producer_for_var.get(var)
            if producer_id and producer_id != op.operation_id:
                dependencies[op.operation_id].add(producer_id)

    # 2. Same-resource CRUD lifecycle edges: POST -> GET -> PUT/PATCH -> DELETE
    by_resource: dict[str, list[OperationSpec]] = {}
    for op in ops:
        by_resource.setdefault(op.resource_group, []).append(op)

    for res_ops in by_resource.values():
        posts = [o for o in res_ops if o.method == "POST"]
        gets = [o for o in res_ops if o.method == "GET"]
        updates = [o for o in res_ops if o.method in ("PUT", "PATCH")]
        deletes = [o for o in res_ops if o.method == "DELETE"]

        for g in gets:
            for p in posts:
                if p.operation_id != g.operation_id:
                    dependencies[g.operation_id].add(p.operation_id)
        for u in updates:
            for p in posts:
                if p.operation_id != u.operation_id:
                    dependencies[u.operation_id].add(p.operation_id)
            for g in gets:
                if g.path == u.path and g.operation_id != u.operation_id:
                    dependencies[u.operation_id].add(g.operation_id)
        for d in deletes:
            for prior in posts + gets + updates:
                if prior.operation_id != d.operation_id:
                    dependencies[d.operation_id].add(prior.operation_id)

    # 3. Kahn's topological sort with deterministic tie-breaking and cycle fallback
    remaining: set[str] = {op.operation_id for op in ops}
    ordered_ids: list[str] = []
    original_index = {op.operation_id: idx for idx, op in enumerate(ops)}

    def _sort_key(op_id: str) -> tuple[int, int, int]:
        op_obj = op_map[op_id]
        return (
            METHOD_PRIORITY.get(op_obj.method, 99),
            len(consumed_by_op[op_id]),
            original_index[op_id],
        )

    while remaining:
        ready = [
            op_id
            for op_id in remaining
            if not (dependencies[op_id] & remaining)
        ]
        if ready:
            ready.sort(key=_sort_key)
            chosen = ready[0]
        else:
            cycle_candidates = sorted(
                remaining,
                key=lambda oid: (len(dependencies[oid] & remaining), *_sort_key(oid)),
            )
            chosen = cycle_candidates[0]

        ordered_ids.append(chosen)
        remaining.remove(chosen)

    plan: list[PlannedStep] = []
    for idx, op_id in enumerate(ordered_ids, start=1):
        op_obj = op_map[op_id]
        plan.append(
            PlannedStep(
                step=idx,
                operation=op_obj,
                produces=produced_by_op[op_id],
                consumes=consumed_by_op[op_id],
                depends_on=sorted(dependencies[op_id]),
                param_bindings=bindings_by_op[op_id],
            )
        )

    return plan
