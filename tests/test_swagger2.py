"""Tests for Swagger 2.0 (`"swagger": "2.0"`) support in `src/parser.py` and `src/generator.py` (Item [4] Option A)."""

from __future__ import annotations

import os

os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")

from src.generator import generate_test_suite_pipeline
from src.parser import parse_openapi_spec

SWAGGER_2_PETSTORE_SPEC = """
swagger: "2.0"
info:
  title: "Swagger 2.0 Petstore API"
  version: "1.0.0"
host: "petstore.example.com"
basePath: "/v2"
schemes:
  - "https"
consumes:
  - "application/json"
produces:
  - "application/json"
definitions:
  PetCreate:
    type: object
    required:
      - name
      - tag
    properties:
      name:
        type: string
        minLength: 2
      tag:
        type: string
  Pet:
    type: object
    required:
      - id
      - name
    properties:
      id:
        type: string
      name:
        type: string
      tag:
        type: string
paths:
  /pets/{id}:
    delete:
      operationId: delete_pet
      parameters:
        - name: id
          in: path
          required: true
          type: string
      responses:
        "204":
          description: "Deleted"
    get:
      operationId: get_pet
      parameters:
        - name: id
          in: path
          required: true
          type: string
      responses:
        "200":
          description: "Pet found"
          schema:
            $ref: "#/definitions/Pet"
  /pets:
    post:
      operationId: create_pet
      parameters:
        - in: body
          name: body
          required: true
          schema:
            $ref: "#/definitions/PetCreate"
      responses:
        "201":
          description: "Created"
          schema:
            $ref: "#/definitions/Pet"
"""


def test_swagger_2_in_body_definitions_and_odg_order(monkeypatch) -> None:
    """[4A] A Swagger 2.0 petstore-like spec must parse `in: body`, `#/definitions/...`, and generate payload + boundary tests in POST -> GET -> DELETE order."""
    monkeypatch.setenv("LLM_BASE_URL", "")

    parsed = parse_openapi_spec(SWAGGER_2_PETSTORE_SPEC)
    assert parsed.servers == ["https://petstore.example.com/v2"]
    assert "Pet" in parsed.raw_schemas
    assert "PetCreate" in parsed.raw_schemas

    output, status, _ = generate_test_suite_pipeline(
        {"openapi_spec": SWAGGER_2_PETSTORE_SPEC},
        {"default_locale": "tr-TR"},
    )
    assert status == "ok"

    # 1. ODG order must be POST /pets -> GET /pets/{id} -> DELETE /pets/{id}
    odg_ops = [(step["method"], step["path"]) for step in output["odg_plan"]]
    assert odg_ops == [
        ("POST", "/pets"),
        ("GET", "/pets/{id}"),
        ("DELETE", "/pets/{id}"),
    ]

    # 2. POST /pets must have a synthesized valid_payload and boundary_payloads (not None / 0 negative tests!)
    create_mocks = output["mock_datasets"]["create_pet"]
    assert isinstance(create_mocks["valid_payload"], dict)
    assert "name" in create_mocks["valid_payload"]
    assert "tag" in create_mocks["valid_payload"]
    assert len(create_mocks["boundary_payloads"]) >= 1
    assert output["summary"]["negative_boundary_tests"] >= 1

    # 3. Generated test code must include payload in POST and response schema assertions from #/definitions/Pet
    code = output["test_code"]
    assert "def test_step_01_create_pet_positive" in code
    assert "def test_step_01_create_pet_negative_boundary" in code
    assert "assert 'id' in data" in code
    assert "assert 'name' in data" in code


def test_swagger_2_formdata_parameters_converted_to_body_schema(monkeypatch) -> None:
    """[4A] Swagger 2.0 `in: formData` parameters must be synthesized into an object request_body_schema."""
    monkeypatch.setenv("LLM_BASE_URL", "")
    form_spec = """
swagger: "2.0"
info:
  title: "Form Upload API"
  version: "1.0.0"
paths:
  /upload:
    post:
      operationId: upload_metadata
      consumes:
        - "application/x-www-form-urlencoded"
      parameters:
        - name: title
          in: formData
          required: true
          type: string
          minLength: 3
        - name: pages
          in: formData
          required: false
          type: integer
          minimum: 1
      responses:
        "200":
          description: "OK"
"""
    output, status, _ = generate_test_suite_pipeline({"openapi_spec": form_spec}, {"default_locale": "tr-TR"})
    assert status == "ok"
    mocks = output["mock_datasets"]["upload_metadata"]
    assert isinstance(mocks["valid_payload"], dict)
    assert "title" in mocks["valid_payload"]
    assert mocks["valid_payload"]["pages"] >= 1
    assert output["summary"]["negative_boundary_tests"] == 1
