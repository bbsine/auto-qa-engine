"""Tests for storefront assets (`assets/icon.png`, `assets/cover.png`, `assets/screenshots/*.png`)."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.check_submission import inspect_png_header_and_corners

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_all_agent_yaml_assets_exist_and_have_exact_dimensions() -> None:
    """[1] Every asset referenced in agent.yaml must exist and match exact pixel & transparency rules."""
    manifest = yaml.safe_load((REPO_ROOT / "agent.yaml").read_text(encoding="utf-8"))
    assets = manifest.get("assets", {})

    # 1. Icon: 512x512 RGBA with transparent corners
    icon_rel = assets["icon"]
    icon_path = REPO_ROOT / icon_rel
    assert icon_path.is_file(), f"Missing icon: {icon_path}"
    w, h, color_type, corners = inspect_png_header_and_corners(icon_path)
    assert (w, h) == (512, 512), f"icon.png must be 512x512, got {w}x{h}"
    assert color_type == 6, "icon.png must be RGBA (color_type=6)"
    assert corners == [0, 0, 0, 0], f"icon.png corners must be transparent (alpha=0), got {corners}"

    # 2. Cover: 1200x630
    cover_rel = assets["cover"]
    cover_path = REPO_ROOT / cover_rel
    assert cover_path.is_file(), f"Missing cover: {cover_path}"
    cw, ch, _, _ = inspect_png_header_and_corners(cover_path)
    assert (cw, ch) == (1200, 630), f"cover.png must be 1200x630, got {cw}x{ch}"

    # 3. Screenshots: 3 to 5 files, each 1280x800
    screenshots = assets["screenshots"]
    assert isinstance(screenshots, list) and 3 <= len(screenshots) <= 5
    for s_rel in screenshots:
        s_path = REPO_ROOT / s_rel
        assert s_path.is_file(), f"Missing screenshot: {s_path}"
        sw, sh, _, _ = inspect_png_header_and_corners(s_path)
        assert (sw, sh) == (1280, 800), f"{s_rel} must be 1280x800, got {sw}x{sh}"

    # Also verify optional 4.png if present
    s4_path = REPO_ROOT / "assets" / "screenshots" / "4.png"
    if s4_path.is_file():
        s4w, s4h, _, _ = inspect_png_header_and_corners(s4_path)
        assert (s4w, s4h) == (1280, 800)


def test_assets_with_pillow_if_installed() -> None:
    """[1] If Pillow is installed, also verify image modes and center opacity via PIL.Image."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        # Fallback already verified by test_all_agent_yaml_assets_exist_and_have_exact_dimensions
        return

    with Image.open(REPO_ROOT / "assets" / "icon.png") as icon:
        assert icon.size == (512, 512)
        assert icon.mode == "RGBA"
        assert icon.getpixel((0, 0))[3] == 0
        assert icon.getpixel((511, 511))[3] == 0
        # Center area should have non-transparent symbol pixels
        assert any(icon.getpixel((x, 256))[3] > 0 for x in range(100, 412))


def test_glyph_coverage_helper_rejects_font_missing_character() -> None:
    """[3] missing_glyphs() must reject a font that renders a missing-glyph box or blank mask for a character."""
    from scripts.make_assets import missing_glyphs

    class _MockMask:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def tobytes(self) -> bytes:
            return self._data

    class _FakeFontMissingTurkish:
        def getbbox(self, ch: str) -> tuple[int, int, int, int]:
            if ch in ("\uffff", "\U0010fffe", "\ufdd0", "ğ", "ş"):
                return (0, 0, 8, 12)
            return (0, 0, 7, 12)

        def getmask(self, ch: str) -> _MockMask:
            if ch in ("\uffff", "\U0010fffe", "\ufdd0", "ğ", "ş"):
                return _MockMask(b"\xff\x00\xff\x00")
            return _MockMask(ch.encode("utf-8"))

    gaps = missing_glyphs(_FakeFontMissingTurkish(), "abc ğ ş")
    assert "ğ" in gaps and "ş" in gaps
    assert "a" not in gaps and "b" not in gaps and "c" not in gaps


def test_layout_lines_never_exceed_canvas_width() -> None:
    """[3] Wrapped and truncated lines must fit strictly within max_width measured by font.getlength()."""
    from PIL import ImageFont  # type: ignore

    from scripts.make_assets import find_font, layout_lines, load_font_file

    font_path = find_font("AutoQA API Test Üretici abcdefghijklmnopqrstuvwxyz")
    font = load_font_file(font_path, 16)
    max_w = 240.0
    sample_lines = [
        "https://api.ornek-magaza.example.com/v2/very/long/endpoint/path/that/would/otherwise/clip",
        "    total_assertions: 12 | estimated_coverage_pct: 100.0 | Türkçe karakterler: çğıöşü",
    ]
    for mode in ("truncate", "wrap"):
        laid = layout_lines(font, sample_lines, max_w, mode=mode)
        assert laid
        for line in laid:
            assert font.getlength(line) <= max_w + 1e-6, f"{mode} line exceeded {max_w}: {line!r}"


def test_make_assets_has_no_transliteration_table() -> None:
    """[3] scripts/make_assets.py must not contain str.maketrans / _TR_MAP transliteration or bitmap font tables."""
    src = (REPO_ROOT / "scripts" / "make_assets.py").read_text(encoding="utf-8")
    assert "maketrans" not in src
    assert "_TR_MAP" not in src
    assert "_FONT_5X7" not in src
    assert "SCREENSHOT " not in src

