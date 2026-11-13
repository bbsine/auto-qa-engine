"""FastAPI reference server for base-path E2E checks: a JSON API mounted under the `/v2` prefix.

Used to verify that the spec base path (Swagger `basePath` / OpenAPI 3 `servers[0].url` path) is kept when the
user passes a bare-host `base_url`.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

base_path_app = FastAPI(title="Base Path E2E API")
_v2 = APIRouter(prefix="/v2")


class PetCreate(BaseModel):
    name: str = Field(..., min_length=2)
    tag: str = Field(..., min_length=2)


@_v2.post("/pets", status_code=201)
def create_pet(payload: PetCreate) -> dict[str, str]:
    if "'" in payload.name or "<script>" in payload.name:
        raise HTTPException(status_code=400, detail="invalid characters")
    return {"id": "pet_1", "name": payload.name, "tag": payload.tag}


@_v2.get("/pets/{pet_id}")
def get_pet(pet_id: str) -> dict[str, str]:
    if "nonexistent-unauthorized-id" in pet_id:
        raise HTTPException(status_code=404, detail="not found")
    return {"id": pet_id, "name": "Boncuk", "tag": "kedi"}


base_path_app.include_router(_v2)
