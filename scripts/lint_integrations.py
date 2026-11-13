"""Linter for `integrations/` files verifying spec_version 1.1 §3.3 rules."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED_PLACEHOLDERS = {
    "{{AGENT_NAME}}",
    "{{AGENT_SLUG}}",
    "{{PUBLIC_KEY}}",
    "{{SAMPLE_INPUT}}",
    "{{OPERATIONS}}",
}

PLACEHOLDER_RE = re.compile(r"\{\{[^{}\n]+\}\}")
SK_KEY_RE = re.compile(r"\bsk_[A-Za-z0-9_-]*")

TOOL_FILES = ("claude-code.md", "codex.md", "cursor.mdc")

CANONICAL_WORKFLOW_FILE = "auto-qa.yml"
# Matches concrete workflow file names only; generic globs such as `.github/workflows/*.{yml,yaml}` do not match.
WORKFLOW_FILE_RE = re.compile(r"\.github/workflows/([A-Za-z0-9_.-]+\.ya?ml)")


def check_integrations(repo_root: Path = REPO_ROOT) -> list[str]:
    """Validate `integrations/` files against spec_version 1.1 §3.3 rules.

    Returns a list of human-readable error strings (empty if all checks pass).
    """
    errors: list[str] = []
    int_dir = repo_root / "integrations"
    if not int_dir.is_dir():
        return ["integrations/ directory is missing."]

    required_files = ("common.md", *TOOL_FILES)
    for fname in required_files:
        fpath = int_dir / fname
        if not fpath.is_file():
            errors.append(f"integrations/{fname}: file is missing.")

    for fpath in sorted(int_dir.iterdir()):
        if not fpath.is_file():
            continue
        text = fpath.read_text(encoding="utf-8")
        rel = f"integrations/{fpath.name}"

        # 1. No sk_ literal or placeholder
        if SK_KEY_RE.search(text):
            errors.append(f"{rel}: contains forbidden 'sk_' secret key literal or placeholder.")

        # 2. Only allowed {{PLACEHOLDER}} tokens (ignore GitHub Actions ${{ secrets.X }})
        scrubbed_gha = re.sub(r"\$\{\{[^{}\n]+\}\}", "", text)
        for match in PLACEHOLDER_RE.finditer(scrubbed_gha):
            token = match.group(0)
            if token not in ALLOWED_PLACEHOLDERS:
                errors.append(f"{rel}: contains disallowed placeholder '{token}'.")

    # 3. Verify tool files contain framework/project detection, channel selection, empty PLATFORM_SECRET_KEY= + .gitignore, and {{SAMPLE_INPUT}}
    for fname in TOOL_FILES:
        fpath = int_dir / fname
        if not fpath.is_file():
            continue
        text = fpath.read_text(encoding="utf-8")
        rel = f"integrations/{fname}"

        if "openapi" not in text.lower() and "swagger" not in text.lower():
            errors.append(f"{rel}: missing OpenAPI/framework detection step.")
        if not any(ch in text for ch in ("agent-action@v1", "mcp.platform.tld", "api.platform.tld")):
            errors.append(f"{rel}: missing channel selection (CI / MCP / API).")
        if "PLATFORM_SECRET_KEY=" not in text:
            errors.append(f"{rel}: missing instruction for empty 'PLATFORM_SECRET_KEY=' in .env.")
        if ".gitignore" not in text:
            errors.append(f"{rel}: missing '.gitignore' verification instruction for .env.")
        if "{{SAMPLE_INPUT}}" not in text:
            errors.append(f"{rel}: missing '{{SAMPLE_INPUT}}' verification step.")

    # 4. All integration files must reference the same CI workflow file name
    workflow_names: dict[str, set[str]] = {}
    for fpath in sorted(int_dir.iterdir()):
        if fpath.is_file():
            for found in WORKFLOW_FILE_RE.findall(fpath.read_text(encoding="utf-8")):
                workflow_names.setdefault(found, set()).add(f"integrations/{fpath.name}")
    if len(workflow_names) > 1:
        detail = "; ".join(f"{name} (in {', '.join(sorted(files))})" for name, files in sorted(workflow_names.items()))
        errors.append(
            "integrations/*: inconsistent CI workflow file names, expected exactly one "
            f"(use .github/workflows/auto-qa.yml everywhere): {detail}"
        )
    elif workflow_names and next(iter(workflow_names)) != CANONICAL_WORKFLOW_FILE:
        errors.append(
            f"integrations/*: CI workflow file must be .github/workflows/{CANONICAL_WORKFLOW_FILE}, "
            f"found {next(iter(workflow_names))}."
        )

    return errors


def main() -> int:
    errs = check_integrations(REPO_ROOT)
    if errs:
        print("Integration lint FAILED:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("Integration lint PASSED: all integrations/ files comply with spec_version 1.1 §3.3.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
