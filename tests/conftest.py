"""Pytest configuration for `tests/`: isolate PLATFORM_SIGNING_SECRET from `independent_tests/`.

Why this exists: `independent_tests/conftest.py` sets `PLATFORM_SIGNING_SECRET` at import time to its own value.
When both suites are collected in one pytest session (`pytest tests independent_tests`), that import overwrote the
value the `tests/` modules sign with (`test-secret`, set via `os.environ.setdefault`), so valid signed requests in
`tests/` were rejected with 401. This fixture restores the expected secret per-test; it does not change any assertion.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ensure_tests_signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep PLATFORM_SIGNING_SECRET='test-secret' for tests/ even when independent_tests/ is collected in the same run."""
    monkeypatch.setenv("PLATFORM_SIGNING_SECRET", "test-secret")
