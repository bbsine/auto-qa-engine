"""FastAPI reference servers for form-body E2E checks of generated Pytest suites.

Requires `python-multipart` (dev/test dependency only, see scripts/requirements-dev.txt) because FastAPI's
`Form(...)` / `UploadFile` parsing depends on it. It is NOT a runtime dependency of the agent.

1. `form_correct_app`: correct server
   - POST /login     application/x-www-form-urlencoded, Form fields `username`, `password` (both required).
   - POST /documents multipart/form-data, Form field `title` + UploadFile `file` (both required).
2. `form_broken_app`: deliberately broken variant that accepts ANY body (no validation) on the same routes.
"""

from __future__ import annotations

from fastapi import FastAPI, File, Form, Request, UploadFile

# ============================================================================
# 1. Correct form / multipart server
# ============================================================================
form_correct_app = FastAPI(title="Correct Form E2E API")


@form_correct_app.post("/login")
def login_correct(
    username: str = Form(..., min_length=3),
    password: str = Form(..., min_length=3),
) -> dict[str, str]:
    return {"token": "demo-token", "username": username}


@form_correct_app.post("/documents", status_code=201)
async def upload_document_correct(
    title: str = Form(..., min_length=2),
    file: UploadFile = File(...),
) -> dict[str, object]:
    content = await file.read()
    return {"id": "doc_1", "title": title, "filename": file.filename or "", "size": len(content)}


# ============================================================================
# 2. Broken variant: accepts anything (never validates the body)
# ============================================================================
form_broken_app = FastAPI(title="Broken Form E2E API")


@form_broken_app.post("/login")
async def login_broken(request: Request) -> dict[str, str]:
    await request.body()
    return {"token": "demo-token", "username": "anyone"}


@form_broken_app.post("/documents", status_code=201)
async def upload_document_broken(request: Request) -> dict[str, object]:
    await request.body()
    return {"id": "doc_1", "title": "anything", "filename": "", "size": 0}
