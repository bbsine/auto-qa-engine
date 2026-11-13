"""Storefront asset generator (`scripts/make_assets.py`) for `auto-qa-engine` (developer tool, NOT used at runtime).

Generates (demo data and REAL engine output only, no third-party logos):
- `assets/icon.png`            512x512 RGBA, transparent background, single 4-node dependency-graph symbol, NO text
- `assets/cover.png`           1200x630, real `generate_tests` output of `agent.yaml` demo.samples[0] (LLM disabled)
- `assets/screenshots/1.png`   1280x800, real input (spec + flags) of demo.samples[0]
- `assets/screenshots/2.png`   1280x800, real ODG plan + generated Pytest code
- `assets/screenshots/3.png`   1280x800, real `analyze_coverage` result of demo.samples[1]
- `assets/screenshots/4.png`   1280x800, REAL pytest run of the generated suite against `independent_tests/servers.py`
                               (written only when that run is fully green; otherwise skipped with a warning)

Requires Pillow (`pip install -r scripts/requirements-dev.txt`) and a real monospaced TrueType font that can render
every character that is drawn (Turkish letters included). Font search order: env `ASSET_FONT_PATH`, then Windows
(Consolas, Cascadia Mono, Courier New), macOS (Menlo), Linux (DejaVu Sans Mono). If no suitable font is found the
script exits with an error; it never falls back to a bitmap font and never rewrites engine text.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml

try:  # Pillow is a dev-only dependency (scripts/requirements-dev.txt)
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - exercised only on machines without Pillow
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ELLIPSIS = "\u2026"
# Turkish alphabet extras, used only to vet that the chosen font can render them (as escapes, not a replacement table)
TURKISH_LETTERS = "\u00e7\u011f\u0131\u00f6\u015f\u00fc\u00c7\u011e\u0130\u00d6\u015e\u00dc"

# --------------------------------------------------------------------------- colours
BG = (10, 15, 28, 255)
PANEL = (17, 24, 39, 255)
HEADER = (15, 23, 42, 255)
TEXT = (226, 232, 240, 255)
MUTED = (148, 163, 184, 255)
SKY = (56, 189, 248, 255)
GREEN = (74, 222, 128, 255)
YELLOW = (250, 204, 21, 255)
RED = (248, 113, 113, 255)
EMERALD = (52, 211, 153, 255)
BORDER_BLUE = (30, 58, 138, 255)
BORDER_TEAL = (14, 116, 144, 255)
BORDER_RED = (239, 68, 68, 255)

Color = tuple[int, int, int, int]


# --------------------------------------------------------------------------- font discovery & glyph coverage
class FontCoverageError(RuntimeError):
    """Raised when the selected font cannot render a character that is about to be drawn."""


def font_candidates() -> list[Path]:
    """Ordered list of monospaced TrueType font candidates (env ASSET_FONT_PATH first)."""
    candidates: list[Path] = []
    env_path = os.environ.get("ASSET_FONT_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    win_dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        win_dirs.append(Path(local_app) / "Microsoft" / "Windows" / "Fonts")
    for d in win_dirs:
        candidates += [d / "consola.ttf", d / "CascadiaMono.ttf", d / "cour.ttf"]
    candidates += [
        Path("/System/Library/Fonts/Menlo.ttc"),
        Path("/Library/Fonts/Menlo.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSansMono.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSansMono.ttf"),
        Path("/usr/local/share/fonts/DejaVuSansMono.ttf"),
    ]
    return candidates


_NOTDEF_PROBES = ("\uffff", "\U0010fffe", "\ufdd0")


def _glyph_signature(font: Any, ch: str) -> tuple[Any, bytes]:
    mask = font.getmask(ch)
    raw = mask.tobytes() if hasattr(mask, "tobytes") else bytes(mask)
    return (tuple(font.getbbox(ch)), bytes(raw))


def missing_glyphs(font: Any, text: str) -> list[str]:
    """Return characters of `text` that `font` cannot render (renders as the missing-glyph box or nothing)."""
    notdef = {_glyph_signature(font, probe) for probe in _NOTDEF_PROBES}
    missing: list[str] = []
    for ch in dict.fromkeys(text):
        if ch.isspace() or ord(ch) < 32:
            continue
        signature = _glyph_signature(font, ch)
        invisible = not any(signature[1])
        if signature in notdef or invisible:
            missing.append(ch)
    return missing


def load_font_file(path: Path, size: int) -> Any:
    if ImageFont is None:
        raise SystemExit("ERROR: Pillow is required. Install it with: pip install -r scripts/requirements-dev.txt")
    return ImageFont.truetype(str(path), size)


def find_font(required_text: str) -> Path:
    """Pick the first candidate font that exists and renders every character of `required_text`."""
    tried: list[str] = []
    for cand in font_candidates():
        if not cand.is_file():
            continue
        try:
            font = load_font_file(cand, 16)
            gaps = missing_glyphs(font, required_text)
        except OSError as exc:
            tried.append(f"{cand} (cannot open: {exc})")
            continue
        if gaps:
            tried.append(f"{cand} (missing glyphs: {''.join(gaps)})")
            continue
        return cand
    detail = "; ".join(tried) if tried else "no candidate font file exists on this machine"
    raise SystemExit(
        "ERROR: no suitable monospaced TrueType font found (needs Turkish glyphs). "
        f"Set ASSET_FONT_PATH to a font such as Consolas / DejaVu Sans Mono. Details: {detail}"
    )


# --------------------------------------------------------------------------- measured layout (never clip)
def truncate_with_ellipsis(font: Any, text: str, max_width: float) -> str:
    """Return `text` unchanged if it fits, otherwise the longest prefix + ellipsis that fits."""
    if font.getlength(text) <= max_width:
        return text
    body = text.rstrip(ELLIPSIS)
    while body and font.getlength(body + ELLIPSIS) > max_width:
        body = body[:-1]
    return body.rstrip() + ELLIPSIS if body else ELLIPSIS


def wrap_text(font: Any, text: str, max_width: float) -> list[str]:
    """Character-wrap `text` to `max_width`, keeping the original indentation plus two spaces on continuations."""
    if font.getlength(text) <= max_width:
        return [text]
    stripped = text.lstrip(" ")
    indent = text[: len(text) - len(stripped)]
    cont_indent = indent + "  "
    if font.getlength(cont_indent + "m") >= max_width:
        cont_indent = ""
    lines: list[str] = []
    current = ""
    for ch in text:
        if current and font.getlength(current + ch) > max_width:
            lines.append(current.rstrip())
            current = cont_indent + ch if ch != " " else cont_indent
        else:
            current += ch
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


def layout_lines(
    font: Any,
    raw_lines: list[str],
    max_width: float,
    mode: str = "truncate",
    max_lines: int | None = None,
) -> list[str]:
    """Lay out text lines so that none is wider than `max_width` (mode: 'truncate' or 'wrap')."""
    laid: list[str] = []
    for raw in raw_lines:
        raw = raw.replace("\t", "    ")
        if mode == "wrap":
            laid.extend(wrap_text(font, raw, max_width))
        else:
            laid.append(truncate_with_ellipsis(font, raw, max_width))
    if max_lines is not None and len(laid) > max_lines:
        laid = laid[:max_lines]
        if laid:
            laid[-1] = truncate_with_ellipsis(font, laid[-1].rstrip() + ELLIPSIS, max_width)
    return laid


# --------------------------------------------------------------------------- renderer
class Renderer:
    """Thin Pillow wrapper: real TrueType text only, glyph coverage verified before every draw call."""

    def __init__(self, width: int, height: int, font_path: Path, bg: Color = BG) -> None:
        if Image is None:
            raise SystemExit("ERROR: Pillow is required. Install it with: pip install -r scripts/requirements-dev.txt")
        self.width, self.height = width, height
        self.font_path = font_path
        self.img = Image.new("RGBA", (width, height), bg)
        self.draw = ImageDraw.Draw(self.img)
        self._fonts: dict[int, Any] = {}
        self._checked: set[tuple[int, str]] = set()

    def font(self, size: int) -> Any:
        if size not in self._fonts:
            self._fonts[size] = load_font_file(self.font_path, size)
        return self._fonts[size]

    def _verify(self, font: Any, size: int, text: str) -> None:
        fresh = [ch for ch in dict.fromkeys(text) if (size, ch) not in self._checked]
        gaps = missing_glyphs(font, "".join(fresh))
        if gaps:
            raise FontCoverageError(
                f"font {self.font_path.name} cannot render {', '.join(f'U+{ord(c):04X} {c!r}' for c in gaps)}"
            )
        self._checked.update((size, ch) for ch in fresh)

    def text(self, x: float, y: float, text: str, size: int, fill: Color) -> None:
        font = self.font(size)
        self._verify(font, size, text)
        self.draw.text((x, y), text, font=font, fill=fill)

    def rect(self, box: tuple[int, int, int, int], fill: Color, outline: Color | None = None, width: int = 2) -> None:
        self.draw.rectangle(box, fill=fill, outline=outline, width=width)

    def frame(self, title: str, subtitle: str) -> None:
        w = self.width
        self.rect((0, 0, w, 64), HEADER)
        self.rect((0, 64, w, 66), SKY)
        self.text(24, 8, truncate_with_ellipsis(self.font(24), title, w - 48), 24, SKY)
        self.text(24, 40, truncate_with_ellipsis(self.font(15), subtitle, w - 48), 15, MUTED)

    def panel(self, box: tuple[int, int, int, int], title: str, border: Color, title_color: Color) -> tuple[int, int, int, int]:
        """Draw a bordered panel with a title; return the inner content box (x0, y0, x1, y1)."""
        x0, y0, x1, y1 = box
        self.rect(box, PANEL, outline=border, width=2)
        self.text(x0 + 14, y0 + 10, truncate_with_ellipsis(self.font(17), title, x1 - x0 - 28), 17, title_color)
        return (x0 + 14, y0 + 40, x1 - 14, y1 - 10)

    def block(
        self,
        box: tuple[int, int, int, int],
        items: list[tuple[str, Color]],
        size: int,
        mode: str = "truncate",
        line_gap: int = 6,
    ) -> int:
        """Draw colour-tagged lines inside `box`, wrapped/truncated to the box width; returns the lines drawn."""
        x0, y0, x1, y1 = box
        font = self.font(size)
        line_h = size + line_gap
        laid: list[tuple[str, Color]] = []
        for text, color in items:
            for line in layout_lines(font, [text], x1 - x0, mode):
                laid.append((line, color))
        capacity = max(1, int((y1 - y0) // line_h))
        if len(laid) > capacity:
            laid = laid[:capacity]
            last_text, last_color = laid[-1]
            laid[-1] = (truncate_with_ellipsis(font, last_text.rstrip() + ELLIPSIS, x1 - x0), last_color)
        for i, (line, color) in enumerate(laid):
            if line:
                self.text(x0, y0 + i * line_h, line, size, color)
        return len(laid)

    def flow_columns(
        self,
        box: tuple[int, int, int, int],
        items: list[tuple[str, Color]],
        size: int,
        columns: int = 2,
        gutter: int = 24,
        mode: str = "truncate",
        line_gap: int = 6,
    ) -> None:
        """Flow lines across `columns` equal columns inside `box` (the last column is truncated with an ellipsis)."""
        x0, y0, x1, y1 = box
        col_w = (x1 - x0 - gutter * (columns - 1)) // columns
        font = self.font(size)
        line_h = size + line_gap
        capacity = max(1, int((y1 - y0) // line_h))
        laid: list[tuple[str, Color]] = []
        for text, color in items:
            for line in layout_lines(font, [text], col_w, mode):
                laid.append((line, color))
        for col in range(columns):
            chunk = laid[col * capacity : (col + 1) * capacity]
            if col == columns - 1 and len(laid) > columns * capacity and chunk:
                chunk[-1] = (truncate_with_ellipsis(font, chunk[-1][0].rstrip() + ELLIPSIS, col_w), chunk[-1][1])
            cx = x0 + col * (col_w + gutter)
            for i, (line, color) in enumerate(chunk):
                if line:
                    self.text(cx, y0 + i * line_h, line, size, color)

    def save(self, path: Path, rgba: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        image = self.img if rgba else self.img.convert("RGB")
        image.save(path, format="PNG", optimize=True)


# --------------------------------------------------------------------------- icon (no text)
def generate_icon(path: Path) -> None:
    """512x512 RGBA icon: transparent corners, one 4-node dependency-graph symbol, no text (supersampled 4x)."""
    if Image is None:
        raise SystemExit("ERROR: Pillow is required. Install it with: pip install -r scripts/requirements-dev.txt")
    scale = 4
    size = 512 * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def s(v: float) -> float:
        return v * scale

    def disc(cx: float, cy: float, r: float, fill: Color) -> None:
        d.ellipse((s(cx - r), s(cy - r), s(cx + r), s(cy + r)), fill=fill)

    def edge(a: tuple[float, float], b: tuple[float, float], width: float, color: Color) -> None:
        d.line((s(a[0]), s(a[1]), s(b[0]), s(b[1])), fill=color, width=int(s(width)))
        disc(a[0], a[1], width / 2, color)
        disc(b[0], b[1], width / 2, color)

    disc(256, 256, 212, (14, 165, 233, 255))
    disc(256, 256, 198, (13, 25, 43, 255))

    top, left, right, bottom = (256.0, 128.0), (146.0, 266.0), (366.0, 266.0), (256.0, 384.0)
    edge_color = (56, 189, 248, 255)
    for a, b in ((top, left), (top, right), (left, bottom), (right, bottom)):
        edge(a, b, 14, edge_color)
    edge(left, right, 10, (34, 211, 238, 210))

    for (nx, ny), fill in (
        (top, (16, 185, 129, 255)),
        (left, (14, 165, 233, 255)),
        (right, (14, 165, 233, 255)),
        (bottom, (99, 102, 241, 255)),
    ):
        disc(nx, ny, 38, (15, 23, 42, 255))
        disc(nx, ny, 30, fill)
        disc(nx, ny, 12, (240, 249, 255, 255))

    icon = img.resize((512, 512), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    icon.save(path, format="PNG", optimize=True)


# --------------------------------------------------------------------------- content builders (real engine strings)
def _plan_items(demo_output: dict[str, Any]) -> list[tuple[str, Color]]:
    items: list[tuple[str, Color]] = []
    for step in demo_output["odg_plan"]:
        items.append((f"Step {step['step']}: {step['method']} {step['path']} ({step['operation_id']})", TEXT))
        items.append((f"  produces={step['produces']} consumes={step['consumes']}", SKY))
    return items


def _summary_items(demo_output: dict[str, Any]) -> list[tuple[str, Color]]:
    summary = demo_output["summary"]
    rows = [f"{key}: {value}" for key, value in summary.items()]
    rows.append(f"total_assertions: {demo_output['total_assertions']}")
    return [(row, TEXT) for row in rows]


def _code_items(test_code: str) -> list[tuple[str, Color]]:
    lines = [ln for ln in test_code.splitlines() if ln.strip()]
    start = next((i for i, ln in enumerate(lines) if ln.startswith("def test_")), 0)
    items: list[tuple[str, Color]] = []
    for ln in lines[start:]:
        color = TEXT
        if ln.startswith("def test_"):
            color = GREEN
        elif "assert " in ln:
            color = YELLOW
        elif ln.strip().startswith("#"):
            color = MUTED
        items.append((ln, color))
    return items


def all_drawn_text_for_font_check(demo_output: dict[str, Any], sample_0: dict[str, Any], cov_output: dict[str, Any]) -> str:
    """Concatenate every string that will be drawn so the font can be vetted before any image is created."""
    parts = [
        TURKISH_LETTERS,
        "AutoQA API Test Üretici generate_tests analyze_coverage girdi ODG planı Pytest kodu çalıştırılması kalite kapısı",
        ELLIPSIS,
        json.dumps(cov_output, indent=2, ensure_ascii=False),
        json.dumps(demo_output["summary"], ensure_ascii=False),
        demo_output["test_code"],
        json.dumps(demo_output["odg_plan"], ensure_ascii=False),
        str(sample_0["input"].get("openapi_spec", "")),
        str(sample_0["input"].get("base_url", "")),
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- images
def generate_cover(path: Path, font_path: Path, demo_output: dict[str, Any]) -> None:
    r = Renderer(1200, 630, font_path)
    r.frame(
        "AutoQA API Test Üretici",
        "OpenAPI -> ODG sıralı, AST doğrulamalı Pytest | gerçek motor çıktısı (agent.yaml demo.samples[0])",
    )
    left = r.panel((20, 80, 560, 610), "ODG planı ve özet", BORDER_BLUE, EMERALD)
    r.block(left, _plan_items(demo_output) + [("", TEXT)] + _summary_items(demo_output), 15, mode="wrap", line_gap=5)
    right = r.panel((580, 80, 1180, 610), "Üretilen Pytest kodu", BORDER_TEAL, SKY)
    r.block(right, _code_items(demo_output["test_code"]), 15, mode="truncate", line_gap=5)
    r.save(path)


def generate_screenshot_input(path: Path, font_path: Path, sample_0: dict[str, Any]) -> None:
    r = Renderer(1280, 800, font_path)
    inp = sample_0["input"]
    r.frame("generate_tests - girdi", "POST /v1/invoke | agent.yaml demo.samples[0] girdisi (yalnızca demo verisi)")
    box = r.panel((20, 80, 1260, 784), "input", BORDER_BLUE, SKY)
    items: list[tuple[str, Color]] = [
        (f"base_url: {inp.get('base_url')}", SKY),
        (f"include_negative_tests: {inp.get('include_negative_tests')}", SKY),
        (f"include_security_tests: {inp.get('include_security_tests')}", SKY),
        ("openapi_spec:", EMERALD),
    ]
    items += [(line, TEXT) for line in str(inp["openapi_spec"]).splitlines()]
    r.flow_columns(box, items, 17, columns=2)
    r.save(path)


def generate_screenshot_output(path: Path, font_path: Path, demo_output: dict[str, Any]) -> None:
    r = Renderer(1280, 800, font_path)
    s = demo_output["summary"]
    r.frame(
        "generate_tests - ODG planı ve Pytest kodu",
        f"status: ok | ast_valid: {s['ast_valid']} | generated_test_count: {s['generated_test_count']} "
        f"| total_assertions: {demo_output['total_assertions']}",
    )
    left = r.panel((20, 80, 560, 784), "ODG topolojik plan ve özet", BORDER_BLUE, EMERALD)
    r.block(left, _plan_items(demo_output) + [("", TEXT)] + _summary_items(demo_output), 16, mode="wrap")
    right = r.panel((580, 80, 1260, 784), "test_generated_api.py", BORDER_TEAL, SKY)
    r.block(right, _code_items(demo_output["test_code"]), 16, mode="truncate")
    r.save(path)


def generate_screenshot_coverage(path: Path, font_path: Path, cov_output: dict[str, Any], cov_status: str) -> None:
    r = Renderer(1280, 800, font_path)
    r.frame(
        "analyze_coverage - CI kalite kapısı",
        f"status: {cov_status} | coverage_pct: {cov_output['coverage_pct']} | agent.yaml demo.samples[1] girdisi",
    )
    box = r.panel((20, 80, 1260, 784), f"status: {cov_status}", BORDER_RED if cov_status == "fail" else BORDER_BLUE, RED if cov_status == "fail" else GREEN)
    pretty = json.dumps({"status": cov_status, "output": cov_output}, indent=2, ensure_ascii=False)
    items: list[tuple[str, Color]] = []
    for line in pretty.splitlines():
        color = TEXT
        if "uncovered_operations" in line or '"fail"' in line:
            color = RED
        elif "covered_operations" in line:
            color = GREEN
        items.append((line, color))
    r.flow_columns(box, items, 17, columns=2, mode="wrap")
    r.save(path)


def _start_uvicorn(asgi_app: Any) -> tuple[Any, threading.Thread, int]:
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, thread, port


def run_generated_suite_on_reference_server(sample_0: dict[str, Any], cfg: dict[str, Any]) -> tuple[int, list[str]]:
    """Generate tests for demo.samples[0] and run them with pytest against `independent_tests/servers.py::good_app`."""
    from independent_tests.servers import good_app
    from src.generator import generate_test_suite_pipeline

    server, thread, port = _start_uvicorn(good_app)
    try:
        inp = dict(sample_0["input"])
        inp["base_url"] = f"http://127.0.0.1:{port}"
        output, _status, _artifacts = generate_test_suite_pipeline(inp, cfg)
        with tempfile.TemporaryDirectory() as tmp:
            test_file = Path(tmp) / "test_generated_api.py"
            test_file.write_text(output["test_code"], encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", test_file.name, "-v", "--color=no", "--no-header", "-p", "no:cacheprovider"],
                cwd=tmp,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=120,
            )
        return proc.returncode, proc.stdout.splitlines()
    finally:
        server.should_exit = True
        thread.join(timeout=3)


def generate_screenshot_pytest_run(path: Path, font_path: Path, returncode: int, lines: list[str]) -> None:
    r = Renderer(1280, 800, font_path)
    summary = next((ln.strip("= ").strip() for ln in reversed(lines) if " passed" in ln or " failed" in ln), "")
    r.frame(
        "generate_tests - üretilen testlerin çalıştırılması",
        f"pytest -v | hedef: independent_tests/servers.py (good_app) | {summary}",
    )
    box = r.panel((20, 80, 1260, 784), "pytest çıktısı", BORDER_TEAL, GREEN if returncode == 0 else RED)
    items: list[tuple[str, Color]] = []
    for ln in lines:
        color = TEXT
        if "PASSED" in ln:
            color = GREEN
        elif "FAILED" in ln or "ERROR" in ln:
            color = RED
        elif "SKIPPED" in ln:
            color = YELLOW
        items.append((ln, color))
    r.flow_columns(box, items, 16, columns=1)
    r.save(path)


def main() -> int:
    if Image is None:
        print("ERROR: Pillow is required. Install it with: pip install -r scripts/requirements-dev.txt", file=sys.stderr)
        return 2

    os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")
    os.environ["LLM_BASE_URL"] = ""  # deterministic, reproducible demo output

    from src.generator import analyze_coverage_pipeline, generate_test_suite_pipeline

    manifest = yaml.safe_load((REPO_ROOT / "agent.yaml").read_text(encoding="utf-8"))
    demo_cfg = manifest.get("demo", {}).get("config", {})
    samples = manifest.get("demo", {}).get("samples", [])
    sample_0, sample_1 = samples[0], samples[1]

    demo_output, _, _ = generate_test_suite_pipeline(sample_0["input"], demo_cfg)
    cov_output, cov_status, _ = analyze_coverage_pipeline(sample_1["input"], demo_cfg)

    try:
        font_path = find_font(all_drawn_text_for_font_check(demo_output, sample_0, cov_output))
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Using font: {font_path}")

    assets_dir = REPO_ROOT / "assets"
    shots = assets_dir / "screenshots"
    written: list[Path] = []
    try:
        generate_icon(assets_dir / "icon.png")
        written.append(assets_dir / "icon.png")
        generate_cover(assets_dir / "cover.png", font_path, demo_output)
        written.append(assets_dir / "cover.png")
        generate_screenshot_input(shots / "1.png", font_path, sample_0)
        generate_screenshot_output(shots / "2.png", font_path, demo_output)
        generate_screenshot_coverage(shots / "3.png", font_path, cov_output, cov_status)
        written += [shots / "1.png", shots / "2.png", shots / "3.png"]

        returncode, run_lines = run_generated_suite_on_reference_server(sample_0, demo_cfg)
        shot4 = shots / "4.png"
        if returncode == 0:
            generate_screenshot_pytest_run(shot4, font_path, returncode, run_lines)
            written.append(shot4)
        else:
            if shot4.exists():
                shot4.unlink()
            print(
                "WARNING: generated tests did not pass against the reference server; 4.png was NOT written. "
                "Last output lines:\n" + "\n".join(run_lines[-15:]),
                file=sys.stderr,
            )
    except FontCoverageError as exc:
        print(f"ERROR: {exc}. Set ASSET_FONT_PATH to a font with full glyph coverage.", file=sys.stderr)
        return 2

    for p in written:
        print(f"Generated {p.relative_to(REPO_ROOT)} ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
