"""Item 4: all integration docs must name the same CI workflow file (.github/workflows/auto-qa.yml)."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from lint_integrations import check_integrations  # noqa: E402

WORKFLOW_RE = re.compile(r"\.github/workflows/([A-Za-z0-9_.-]+\.ya?ml)")


def _copy_repo_integrations(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "integrations", tmp_path / "integrations")
    return tmp_path


def test_all_integration_files_use_auto_qa_yml() -> None:
    names: set[str] = set()
    for f in (REPO_ROOT / "integrations").iterdir():
        if f.is_file():
            names.update(WORKFLOW_RE.findall(f.read_text(encoding="utf-8")))
    assert names == {"auto-qa.yml"}, f"workflow file names found: {sorted(names)}"


def test_repository_integrations_pass_lint() -> None:
    assert check_integrations(REPO_ROOT) == []


def test_lint_fails_when_two_workflow_names_appear(tmp_path: Path) -> None:
    root = _copy_repo_integrations(tmp_path)
    common = root / "integrations" / "common.md"
    common.write_text(common.read_text(encoding="utf-8") + "\nAyrıca `.github/workflows/ci.yml` kullanın.\n", encoding="utf-8")
    errors = check_integrations(root)
    assert any("workflow" in e.lower() and "ci.yml" in e and "auto-qa.yml" in e for e in errors), errors


def test_lint_ignores_generic_workflow_glob(tmp_path: Path) -> None:
    root = _copy_repo_integrations(tmp_path)
    mdc = root / "integrations" / "cursor.mdc"
    text = mdc.read_text(encoding="utf-8")
    assert ".github/workflows/*.{yml,yaml}" in text  # generic glob must not count as a second file name
    assert check_integrations(root) == []
