"""Tiny reference servers used by the end-to-end tests (correct + deliberately broken variants)."""
from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field, field_validator


class UserIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if "@" not in v or " " in v:
            raise ValueError("bad email")
        return v


class OrderIn(BaseModel):
    user_id: str
    quantity: int = Field(ge=1)


def make_app(*, bola=False, drop_email=False, producer_down=False) -> FastAPI:
    app = FastAPI()
    users: dict = {}
    orders: dict = {}

    @app.post("/users", status_code=201)
    def create_user(u: UserIn):
        if producer_down:
            raise HTTPException(500, "producer down")
        i = str(uuid.uuid4())
        users[i] = {"id": i, **u.model_dump()}
        if drop_email:
            return {"id": i, "name": u.name}
        return users[i]

    @app.get("/users/{id}")
    def get_user(id: str):
        if id not in users:
            raise HTTPException(404)
        return users[id]

    @app.delete("/users/{id}", status_code=204)
    def delete_user(id: str):
        if id not in users:
            raise HTTPException(404)
        del users[id]
        return Response(status_code=204)

    @app.post("/orders", status_code=201)
    def create_order(o: OrderIn):
        if o.user_id not in users:
            raise HTTPException(404, "no such user")
        i = str(uuid.uuid4())
        orders[i] = {"id": i, **o.model_dump()}
        return orders[i]

    @app.get("/orders/{id}")
    def get_order(id: str):
        if id not in orders:
            raise HTTPException(404)
        return orders[id]

    @app.get("/users/{user_id}/orders/{order_id}")
    def get_user_order(user_id: str, order_id: str):
        o = orders.get(order_id)
        if not o:
            raise HTTPException(404)
        if bola:
            return {**o, "user_id": "SOMEONE_ELSE"}
        if o["user_id"] != user_id:
            raise HTTPException(404)
        return o

    return app


good_app = make_app()
bola_app = make_app(bola=True)
type_app = make_app(drop_email=True)
down_app = make_app(producer_down=True)
