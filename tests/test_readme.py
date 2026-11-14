"""Anti-drift tests keeping README.md, README.tr.md, docs/SUBMISSION.md, src/, and agent.yaml in sync."""

from __future__ import annotations

import ast
import io
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from src.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
README_EN = REPO_ROOT / "README.md"
README_TR = REPO_ROOT / "README.tr.md"
SUBMISSION_DOC = REPO_ROOT / "docs" / "SUBMISSION.md"

_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_ENV_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_FORBIDDEN_SK_RE = re.compile(r"\bsk_[A-Za-z0-9]{6,}")
_WORKFLOW_RE = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")


def _strip_fenced_blocks(md_text: str) -> str:
    """Remove fenced code blocks so regexes only inspect prose and tables."""
    out: list[str] = []
    in_fence = False
    for line in md_text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def _github_slug(heading: str) -> str:
    """Approximate GitHub Flavored Markdown heading anchor generation."""
    text = re.sub(r"`+", "", heading).replace("İ", "i").strip().lower()
    cleaned: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in (" ", "-", "_"):
            cleaned.append("-" if ch == " " else ch)
    return "".join(cleaned)


def _heading_anchors(md_text: str) -> set[str]:
    """Collect heading anchors present in a Markdown document."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in _strip_fenced_blocks(md_text).splitlines():
        m = re.match(r"^#{1,6}\s+(.+)$", line)
        if not m:
            continue
        base = _github_slug(m.group(1))
        idx = counts.get(base, 0)
        counts[base] = idx + 1
        anchors.add(base if idx == 0 else f"{base}-{idx}")
    return anchors


def _extract_h2_sections(md_text: str) -> list[tuple[str, str]]:
    """Return ordered `(title, body)` pairs for every H2 section outside code fences."""
    sections: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in md_text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return [(title, "\n".join(body_lines)) for title, body_lines in sections]


def _section_by_prefix(md_text: str, prefix: str) -> str:
    for title, body in _extract_h2_sections(md_text):
        if title.startswith(prefix):
            return body
    raise AssertionError(f"Missing H2 section starting with {prefix!r}")


def _parse_tree_paths(section_body: str) -> list[Path]:
    """Reconstruct relative paths from the ASCII directory tree in Section 13."""
    m = re.search(r"```text\s*\n(.*?)```", section_body, re.DOTALL)
    assert m, "Project layout tree ```text block missing"
    lines = m.group(1).splitlines()
    assert lines and lines[0].rstrip("/").endswith("auto-qa-engine")

    stack: list[tuple[int, str]] = []
    discovered: list[Path] = []
    for line in lines[1:]:
        match = re.match(r"^([│ ]*)(?:├──|└──)\s+([^\s#]+)", line)
        if not match:
            continue
        indent_str, entry = match.groups()
        depth = len(indent_str) // 4
        while stack and stack[-1][0] >= depth:
            stack.pop()
        is_dir = entry.endswith("/")
        clean = entry.rstrip("/")
        rel = Path(*(part for _, part in stack), clean)
        discovered.append(REPO_ROOT / rel)
        if is_dir:
            stack.append((depth, clean))
    return discovered


def _collect_src_env_vars() -> set[str]:
    """Collect every environment variable read via `os.environ` in `src/` and `Dockerfile`."""
    found: set[str] = set()
    for py_path in sorted((REPO_ROOT / "src").glob("*.py")):
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (
                    node.func.attr == "get"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "environ"
                    and node.args
                ):
                    arg0 = node.args[0]
                    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                        found.add(arg0.value)
        # Also catch loop constants passed into os.environ.get(env_name, ...)
        for lit in re.findall(r'"(PLATFORM_SIGNING_SECRET|LLM_BASE_URL|LLM_API_KEY|LLM_TIMEOUT_S)"', source):
            found.add(lit)

    dockerfile_text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    if "PORT=" in dockerfile_text or "${PORT}" in dockerfile_text:
        found.add("PORT")
    return found


def _collect_src_error_codes() -> set[str]:
    """Extract every error.code literal that `src/` can emit across HTTP 4xx/5xx paths."""
    codes: set[str] = set()
    for py_path in sorted((REPO_ROOT / "src").glob("*.py")):
        src_text = py_path.read_text(encoding="utf-8")
        # Direct `"code": "xyz"` dicts in main.py
        for m in re.finditer(r'"code"\s*:\s*"([a-z][a-z0-9_]+)"', src_text):
            codes.add(m.group(1))
        # raise ValueError("code: msg") / return ..., "code", ...
        for m in re.finditer(r'raise ValueError\(\s*f?"([a-z][a-z0-9_]+):', src_text):
            codes.add(m.group(1))
        for m in re.finditer(r'InputShapeError\(\s*"([a-z][a-z0-9_]+)"', src_text):
            codes.add(m.group(1))
        if py_path.name == "main.py":
            # _http_error_code return literals
            fn_match = re.search(r"def _http_error_code.*?def _format_http_error", src_text, re.DOTALL)
            assert fn_match
            for lit in re.findall(r'return "([a-z][a-z0-9_]+)"', fn_match.group(0)):
                codes.add(lit)
            if 'f"http_{status_code}"' in fn_match.group(0):
                codes.add("http_<status_code>")
            for m in re.finditer(r'return \([^)]*?"([a-z][a-z0-9_]+)"', src_text, re.DOTALL):
                codes.add(m.group(1))
            if '"ssrf_blocked"' in src_text:
                codes.add("ssrf_blocked")
    assert len(codes) >= 20, f"Unexpectedly few error codes parsed: {sorted(codes)}"
    return codes


@pytest.mark.parametrize("doc_path", [README_EN, README_TR, SUBMISSION_DOC])
def test_markdown_links_and_anchors_resolve(doc_path: Path) -> None:
    text = doc_path.read_text(encoding="utf-8")
    anchors = _heading_anchors(text)
    prose = _strip_fenced_blocks(text)

    for raw_target in _LINK_RE.findall(prose):
        target = raw_target.strip()
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.startswith("#"):
            assert target[1:] in anchors, f"Broken anchor {target!r} in {doc_path.name}"
            continue
        rel_part, _, frag = target.partition("#")
        resolved = (doc_path.parent / rel_part).resolve()
        assert resolved.exists(), f"Missing link target {target!r} (resolved {resolved}) in {doc_path.name}"
        if frag and resolved.suffix.lower() == ".md":
            target_anchors = _heading_anchors(resolved.read_text(encoding="utf-8"))
            assert frag in target_anchors, f"Missing anchor #{frag} in {rel_part} linked from {doc_path.name}"


@pytest.mark.parametrize("doc_path,section_prefix", [(README_EN, "13."), (README_TR, "13.")])
def test_project_layout_paths_exist(doc_path: Path, section_prefix: str) -> None:
    section_body = _section_by_prefix(doc_path.read_text(encoding="utf-8"), section_prefix)
    paths = _parse_tree_paths(section_body)
    assert len(paths) >= 40
    for p in paths:
        assert p.exists(), f"Path listed in {doc_path.name} layout does not exist: {p}"


def test_bilingual_heading_structure_and_language_links() -> None:
    en_text = README_EN.read_text(encoding="utf-8")
    tr_text = README_TR.read_text(encoding="utf-8")

    en_top = "\n".join(en_text.splitlines()[:12])
    tr_top = "\n".join(tr_text.splitlines()[:12])
    assert "(README.tr.md)" in en_top
    assert "(README.md)" in tr_top

    en_h2 = [title for title, _ in _extract_h2_sections(en_text)]
    tr_h2 = [title for title, _ in _extract_h2_sections(tr_text)]

    assert len(en_h2) == 17
    assert len(tr_h2) == 17

    en_nums = [t.split(".", 1)[0] for t in en_h2]
    tr_nums = [t.split(".", 1)[0] for t in tr_h2]
    assert en_nums == [str(i) for i in range(1, 18)]
    assert tr_nums == [str(i) for i in range(1, 18)]


@pytest.mark.parametrize("doc_path", [README_EN, README_TR])
def test_all_env_vars_in_src_are_documented(doc_path: Path) -> None:
    env_vars = _collect_src_env_vars()
    config_section = _section_by_prefix(doc_path.read_text(encoding="utf-8"), "6.")
    for var in sorted(env_vars):
        assert var in config_section, f"Env var {var!r} missing from section 6 of {doc_path.name}"


@pytest.mark.parametrize("doc_path", [README_EN, README_TR])
def test_all_error_codes_in_src_are_documented(doc_path: Path) -> None:
    codes = _collect_src_error_codes()
    api_section = _section_by_prefix(doc_path.read_text(encoding="utf-8"), "5.")
    for code in sorted(codes):
        assert f"`{code}`" in api_section, f"Error code {code!r} missing from section 5 of {doc_path.name}"


@pytest.mark.parametrize("doc_path", [README_EN, README_TR])
def test_agent_yaml_operations_inputs_and_config_are_documented(doc_path: Path) -> None:
    manifest: dict[str, Any] = yaml.safe_load((REPO_ROOT / "agent.yaml").read_text(encoding="utf-8"))
    text = doc_path.read_text(encoding="utf-8")

    for op_name, op_spec in manifest["operations"].items():
        assert op_name in text, f"Operation {op_name!r} missing from {doc_path.name}"
        for field_name in op_spec["input_schema"]["properties"]:
            assert field_name in text, f"Input field {field_name!r} of {op_name} missing from {doc_path.name}"

    for cfg_name in manifest["config_schema"]["properties"]:
        assert cfg_name in text, f"Config field {cfg_name!r} missing from {doc_path.name}"


@pytest.mark.parametrize("doc_path", [README_EN, README_TR, SUBMISSION_DOC])
def test_no_sk_secret_literals_in_docs(doc_path: Path) -> None:
    text = doc_path.read_text(encoding="utf-8")
    matches = _FORBIDDEN_SK_RE.findall(text)
    assert not matches, f"Forbidden sk_ pattern found in {doc_path.name}: {matches}"


def test_workflow_filename_matches_integrations() -> None:
    integration_workflows: set[str] = set()
    for p in (REPO_ROOT / "integrations").iterdir():
        if p.is_file():
            integration_workflows.update(_WORKFLOW_RE.findall(p.read_text(encoding="utf-8")))
    assert integration_workflows == {".github/workflows/auto-qa.yml"}

    for doc in (README_EN, README_TR):
        doc_workflows = set(_WORKFLOW_RE.findall(doc.read_text(encoding="utf-8")))
        assert doc_workflows == integration_workflows, f"Workflow filename mismatch in {doc.name}: {doc_workflows}"


def test_quick_start_runnable_snippet_executes_against_in_process_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    en_text = README_EN.read_text(encoding="utf-8")
    match = re.search(r"<!--\s*runnable\s*-->\s*```python\s*\n(.*?)```", en_text, re.DOTALL)
    assert match, "README.md must contain a <!-- runnable --> ```python ... ``` Quick Start block"
    code = match.group(1)

    monkeypatch.setenv("PLATFORM_SIGNING_SECRET", "local-test-secret")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.chdir(tmp_path)

    client = TestClient(app)
    captured_responses: list[tuple[int, dict[str, Any]]] = []

    class _FakeHTTPResponse:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self._buf = io.BytesIO(body)

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self) -> "_FakeHTTPResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 30.0) -> _FakeHTTPResponse:
        parsed = urllib.parse.urlsplit(req.full_url)
        headers = {k: v for k, v in req.header_items()}
        resp = client.post(parsed.path, content=req.data or b"", headers=headers)
        captured_responses.append((resp.status_code, json.loads(resp.content.decode("utf-8"))))
        return _FakeHTTPResponse(resp.status_code, resp.content)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    exec(compile(code, "README_quick_start", "exec"), {})

    assert len(captured_responses) == 1
    http_status, body = captured_responses[0]
    assert http_status == 200
    assert body["status"] == "ok"
    assert body["output"]["summary"]["ast_valid"] is True
    assert body["output"]["summary"]["generated_test_count"] == 5
    generated_file = tmp_path / "test_generated_api.py"
    assert generated_file.is_file()
    ast.parse(generated_file.read_text(encoding="utf-8"))
