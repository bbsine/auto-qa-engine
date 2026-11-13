"""Tests for `scripts/check_submission.py` and `scripts/lint_integrations.py`."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from scripts.check_submission import check_submission
from scripts.lint_integrations import check_integrations

REPO_ROOT = Path(__file__).resolve().parent.parent


def _copy_minimal_repo(tmp_path: Path) -> Path:
    """Create a lightweight copy of the repository structure in `tmp_path` for mutation testing."""
    for rel_file in (
        "agent.yaml",
        "Dockerfile",
        "EVAL_REPORT.md",
        "docs/DESCRIPTION.md",
        "evals/cases.yaml",
        "integrations/common.md",
        "integrations/claude-code.md",
        "integrations/codex.md",
        "integrations/cursor.mdc",
    ):
        src = REPO_ROOT / rel_file
        dst = tmp_path / rel_file
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, dst)

    assets_dir = REPO_ROOT / "assets"
    if assets_dir.is_dir():
        shutil.copytree(assets_dir, tmp_path / "assets", dirs_exist_ok=True)
    return tmp_path


def test_current_repository_passes_submission_check() -> None:
    """[2] The repository itself must pass `check_submission` and `check_integrations` with 0 errors."""
    assert check_integrations(REPO_ROOT) == []
    assert check_submission(REPO_ROOT) == []


def test_check_submission_detects_violations(tmp_path: Path) -> None:
    """[2 & 6] Verify that `check_submission` flags each required failure condition."""
    repo = _copy_minimal_repo(tmp_path)

    # 1. .env file present
    (repo / ".env").write_text("PLATFORM_SECRET_KEY=secret\n", encoding="utf-8")
    errs = check_submission(repo)
    assert any(".env file exists" in e for e in errs)
    (repo / ".env").unlink()

    # 2. name > 30 and short_note > 110 and missing top-level key
    manifest = yaml.safe_load((repo / "agent.yaml").read_text(encoding="utf-8"))
    manifest["name"] = "A" * 35
    manifest["short_note"] = "B" * 120
    del manifest["category"]
    (repo / "agent.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    errs = check_submission(repo)
    assert any("'name' exceeds 30" in e for e in errs)
    assert any("'short_note' exceeds 110" in e for e in errs)
    assert any("missing required top-level key: 'category'" in e for e in errs)

    # Restore agent.yaml
    shutil.copy2(REPO_ROOT / "agent.yaml", repo / "agent.yaml")

    # 3. Dockerfile missing USER 1000 or python:3.12-slim
    (repo / "Dockerfile").write_text("FROM python:3.11\nCMD ['uvicorn']\n", encoding="utf-8")
    errs = check_submission(repo)
    assert any("python:3.12-slim" in e for e in errs)
    assert any("USER 1000" in e for e in errs)
    shutil.copy2(REPO_ROOT / "Dockerfile", repo / "Dockerfile")

    # 4. integrations/* containing sk_ or invalid {{PLACEHOLDER}} or missing PLATFORM_SECRET_KEY=
    (repo / "integrations" / "claude-code.md").write_text(
        "Use key sk_live_12345 and {{BAD_PLACEHOLDER}}\n",
        encoding="utf-8",
    )
    errs = check_submission(repo)
    assert any("sk_" in e for e in errs)
    assert any("{{BAD_PLACEHOLDER}}" in e for e in errs)
    assert any("PLATFORM_SECRET_KEY=" in e for e in errs)
    shutil.copy2(REPO_ROOT / "integrations" / "claude-code.md", repo / "integrations" / "claude-code.md")

    # 5. evals/cases.yaml fewer than 20 cases
    (repo / "evals" / "cases.yaml").write_text("cases:\n  - id: c1\n    group: normal\n", encoding="utf-8")
    errs = check_submission(repo)
    assert any("at least 20 cases" in e for e in errs)
    shutil.copy2(REPO_ROOT / "evals" / "cases.yaml", repo / "evals" / "cases.yaml")

    # 6. Missing EVAL_REPORT.md or docs/DESCRIPTION.md
    (repo / "EVAL_REPORT.md").unlink()
    errs = check_submission(repo)
    assert any("EVAL_REPORT.md is missing" in e for e in errs)
