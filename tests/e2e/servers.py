"""Three tiny FastAPI servers for E2E verification of generated Pytest suites:
1. `correct_app`: Correctly implements POST /users, POST /orders, and GET /users/{user_id}/orders/{order_id} returning matching `user_id`.
2. `broken_app`: Deliberately broken BOLA server that returns `user_id="SOMEONE_ELSE"` on GET /users/{user_id}/orders/{order_id}.
3. `producer_500_app`: Deliberately fails POST /users with HTTP 500 so consumer tests must skip cleanly with a clear producer-capture message.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ============================================================================
# 1. Correct Reference Server
# ============================================================================
correct_app = FastAPI(title="Correct E2E API")


class UserCreate(BaseModel):
    name: str = Field(..., min_length=2)
    email: str


class OrderCreate(BaseModel):
    user_id: str
    item_name: str = Field(..., min_length=2)


@correct_app.post("/users", status_code=201)
def create_user_correct(payload: UserCreate) -> dict[str, str]:
    if "'" in payload.name or "<script>" in payload.name:
        raise HTTPException(status_code=400, detail="invalid characters")
    return {"id": "usr_101", "name": payload.name, "email": payload.email}


@correct_app.post("/orders", status_code=201)
def create_order_correct(payload: OrderCreate) -> dict[str, str]:
    if "'" in payload.item_name or "<script>" in payload.item_name:
        raise HTTPException(status_code=400, detail="invalid characters")
    return {"id": "ord_202", "user_id": payload.user_id, "item_name": payload.item_name}


@correct_app.get("/users/{user_id}/orders/{order_id}")
def get_user_order_correct(user_id: str, order_id: str) -> dict[str, str]:
    if "nonexistent-unauthorized-id" in user_id or "nonexistent-unauthorized-id" in order_id:
        raise HTTPException(status_code=404, detail="not found")
    return {"id": order_id, "user_id": user_id, "item_name": "Notebook"}


# ============================================================================
# 2. Broken Server (Returns another user's order: user_id="SOMEONE_ELSE")
# ============================================================================
broken_app = FastAPI(title="Broken BOLA E2E API")


@broken_app.post("/users", status_code=201)
def create_user_broken(payload: UserCreate) -> dict[str, str]:
    return {"id": "usr_101", "name": payload.name, "email": payload.email}


@broken_app.post("/orders", status_code=201)
def create_order_broken(payload: OrderCreate) -> dict[str, str]:
    return {"id": "ord_202", "user_id": payload.user_id, "item_name": payload.item_name}


@broken_app.get("/users/{user_id}/orders/{order_id}")
def get_user_order_broken(user_id: str, order_id: str) -> dict[str, str]:
    if "nonexistent-unauthorized-id" in user_id or "nonexistent-unauthorized-id" in order_id:
        raise HTTPException(status_code=404, detail="not found")
    # BUG: Returns an order belonging to SOMEONE_ELSE instead of the requested user_id!
    return {"id": order_id, "user_id": "SOMEONE_ELSE", "item_name": "Stolen Order"}


# ============================================================================
# 3. Producer-500 Server (POST /users returns 500; consumers must skip cleanly)
# ============================================================================
producer_500_app = FastAPI(title="Producer 500 E2E API")


@producer_500_app.post("/users", status_code=201)
def create_user_500(payload: UserCreate) -> dict[str, str]:
    if "'" in payload.name or "<script>" in payload.name:
        raise HTTPException(status_code=400, detail="invalid characters")
    raise HTTPException(status_code=500, detail="simulated database outage on user creation")


@producer_500_app.post("/orders", status_code=201)
def create_order_500(payload: OrderCreate) -> dict[str, str]:
    if "'" in payload.item_name or "<script>" in payload.item_name:
        raise HTTPException(status_code=400, detail="invalid characters")
    return {"id": "ord_202", "user_id": payload.user_id, "item_name": payload.item_name}


@producer_500_app.get("/users/{user_id}/orders/{order_id}")
def get_user_order_500(user_id: str, order_id: str) -> dict[str, str]:
    if "nonexistent-unauthorized-id" in user_id or "nonexistent-unauthorized-id" in order_id:
        raise HTTPException(status_code=404, detail="not found")
    return {"id": order_id, "user_id": user_id, "item_name": "Notebook"}

