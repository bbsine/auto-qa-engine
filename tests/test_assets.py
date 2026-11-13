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
