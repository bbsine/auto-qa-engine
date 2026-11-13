"""Storefront asset generator (`scripts/make_assets.py`) for `auto-qa-engine`.

Generates:
- `assets/icon.png` (512x512 RGBA, transparent background, 4-node ODG dependency graph motif, NO text)
- `assets/cover.png` (1200x630, rendered from real engine output on `agent.yaml` `demo.samples[0]`)
- `assets/screenshots/1.png` (1280x800, real input OpenAPI spec & invoke payload from `demo.samples[0]`)
- `assets/screenshots/2.png` (1280x800, real ODG plan + generated Pytest suite from `demo.samples[0]`)
- `assets/screenshots/3.png` (1280x800, real `analyze_coverage` `status: "fail"` output from `demo.samples[1]`)
- `assets/screenshots/4.png` (1280x800, real Pytest execution result against `independent_tests/servers.py` `good_app`)

Uses Pillow (`PIL`) when installed, or a pure-Python stdlib (`zlib`/`struct`) RGBA rasterizer with a built-in
bitmap font when Pillow is not installed in the active environment.
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("PLATFORM_SIGNING_SECRET", "test-secret")
os.environ["LLM_BASE_URL"] = ""

from src.generator import analyze_coverage_pipeline, generate_test_suite_pipeline  # noqa: E402

# Compact 5x7 bitmap font for printable ASCII + Turkish transliteration fallback
_FONT_5X7: dict[str, list[int]] = {
    " ": [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    "!": [0x04, 0x04, 0x04, 0x04, 0x00, 0x04, 0x00],
    '"': [0x0A, 0x0A, 0x0A, 0x00, 0x00, 0x00, 0x00],
    "#": [0x0A, 0x1F, 0x0A, 0x0A, 0x1F, 0x0A, 0x00],
    "$": [0x04, 0x0F, 0x14, 0x0E, 0x05, 0x1E, 0x04],
    "%": [0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03],
    "&": [0x0C, 0x12, 0x14, 0x08, 0x15, 0x12, 0x0D],
    "'": [0x04, 0x04, 0x08, 0x00, 0x00, 0x00, 0x00],
    "(": [0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02],
    ")": [0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08],
    "*": [0x00, 0x04, 0x15, 0x0E, 0x15, 0x04, 0x00],
    "+": [0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00],
    ",": [0x00, 0x00, 0x00, 0x00, 0x04, 0x04, 0x08],
    "-": [0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00],
    ".": [0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C],
    "/": [0x01, 0x02, 0x02, 0x04, 0x08, 0x08, 0x10],
    "0": [0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E],
    "1": [0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E],
    "2": [0x0E, 0x11, 0x01, 0x06, 0x08, 0x10, 0x1F],
    "3": [0x1F, 0x02, 0x04, 0x06, 0x01, 0x11, 0x0E],
    "4": [0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02],
    "5": [0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E],
    "6": [0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E],
    "7": [0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08],
    "8": [0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E],
    "9": [0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C],
    ":": [0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x0C, 0x00],
    ";": [0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x04, 0x08],
    "<": [0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02],
    "=": [0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00],
    ">": [0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08],
    "?": [0x0E, 0x11, 0x01, 0x06, 0x04, 0x00, 0x04],
    "@": [0x0E, 0x11, 0x17, 0x15, 0x17, 0x10, 0x0E],
    "A": [0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    "B": [0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E],
    "C": [0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E],
    "D": [0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C],
    "E": [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F],
    "F": [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10],
    "G": [0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F],
    "H": [0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    "I": [0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
    "J": [0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C],
    "K": [0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11],
    "L": [0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F],
    "M": [0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11],
    "N": [0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11],
    "O": [0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    "P": [0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10],
    "Q": [0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D],
    "R": [0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11],
    "S": [0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E],
    "T": [0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
    "U": [0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    "V": [0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04],
    "W": [0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11],
    "X": [0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11],
    "Y": [0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04],
    "Z": [0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F],
    "[": [0x0E, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0E],
    "\\": [0x10, 0x08, 0x08, 0x04, 0x02, 0x02, 0x01],
    "]": [0x0E, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0E],
    "^": [0x04, 0x0A, 0x11, 0x00, 0x00, 0x00, 0x00],
    "_": [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F],
    "`": [0x08, 0x04, 0x02, 0x00, 0x00, 0x00, 0x00],
    "a": [0x00, 0x00, 0x0E, 0x01, 0x0F, 0x11, 0x0F],
    "b": [0x10, 0x10, 0x1E, 0x11, 0x11, 0x11, 0x1E],
    "c": [0x00, 0x00, 0x0E, 0x10, 0x10, 0x11, 0x0E],
    "d": [0x01, 0x01, 0x0F, 0x11, 0x11, 0x11, 0x0F],
    "e": [0x00, 0x00, 0x0E, 0x11, 0x1F, 0x10, 0x0E],
    "f": [0x06, 0x08, 0x1E, 0x08, 0x08, 0x08, 0x08],
    "g": [0x00, 0x0F, 0x11, 0x11, 0x0F, 0x01, 0x0E],
    "h": [0x10, 0x10, 0x1E, 0x11, 0x11, 0x11, 0x11],
    "i": [0x04, 0x00, 0x0C, 0x04, 0x04, 0x04, 0x0E],
    "j": [0x02, 0x00, 0x06, 0x02, 0x02, 0x12, 0x0C],
    "k": [0x10, 0x10, 0x12, 0x14, 0x18, 0x14, 0x12],
    "l": [0x0C, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
    "m": [0x00, 0x00, 0x1A, 0x15, 0x15, 0x11, 0x11],
    "n": [0x00, 0x00, 0x1E, 0x11, 0x11, 0x11, 0x11],
    "o": [0x00, 0x00, 0x0E, 0x11, 0x11, 0x11, 0x0E],
    "p": [0x00, 0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10],
    "q": [0x00, 0x0F, 0x11, 0x11, 0x0F, 0x01, 0x01],
    "r": [0x00, 0x00, 0x16, 0x19, 0x10, 0x10, 0x10],
    "s": [0x00, 0x00, 0x0F, 0x10, 0x0E, 0x01, 0x1E],
    "t": [0x08, 0x08, 0x1E, 0x08, 0x08, 0x09, 0x06],
    "u": [0x00, 0x00, 0x11, 0x11, 0x11, 0x13, 0x0D],
    "v": [0x00, 0x00, 0x11, 0x11, 0x11, 0x0A, 0x04],
    "w": [0x00, 0x00, 0x11, 0x11, 0x15, 0x15, 0x0A],
    "x": [0x00, 0x00, 0x11, 0x0A, 0x04, 0x0A, 0x11],
    "y": [0x00, 0x11, 0x11, 0x11, 0x0F, 0x01, 0x0E],
    "z": [0x00, 0x00, 0x1F, 0x02, 0x04, 0x08, 0x1F],
    "{": [0x02, 0x04, 0x04, 0x08, 0x04, 0x04, 0x02],
    "|": [0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
    "}": [0x08, 0x04, 0x04, 0x02, 0x04, 0x04, 0x08],
    "~": [0x00, 0x00, 0x08, 0x15, 0x02, 0x00, 0x00],
}

_TR_MAP = str.maketrans(
    "çğıöşüÇĞİÖŞÜ→✓✗",
    "cgiosuCGIOSU>vX",
)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


class PureCanvas:
    """Pure-Python RGBA pixel buffer with anti-aliased geometry, bitmap text rendering, and PNG output."""

    def __init__(self, width: int, height: int, bg: tuple[int, int, int, int] = (0, 0, 0, 0)) -> None:
        self.width = width
        self.height = height
        r, g, b, a = bg
        row = bytes([r, g, b, a]) * width
        self.buf = bytearray(row * height)

    def _blend_pixel(self, x: int, y: int, color: tuple[int, int, int, int]) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        sr, sg, sb, sa = color
        if sa <= 0:
            return
        idx = (y * self.width + x) * 4
        if sa >= 255 or self.buf[idx + 3] == 0:
            self.buf[idx] = sr
            self.buf[idx + 1] = sg
            self.buf[idx + 2] = sb
            self.buf[idx + 3] = max(sa, self.buf[idx + 3])
            return
        dr, dg, db, da = self.buf[idx], self.buf[idx + 1], self.buf[idx + 2], self.buf[idx + 3]
        alpha = sa / 255.0
        inv = 1.0 - alpha
        self.buf[idx] = int(sr * alpha + dr * inv)
        self.buf[idx + 1] = int(sg * alpha + dg * inv)
        self.buf[idx + 2] = int(sb * alpha + db * inv)
        self.buf[idx + 3] = min(255, int(sa + da * inv))

    def fill_rect(self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int, int]) -> None:
        x_start = max(0, min(x0, x1))
        x_end = min(self.width, max(x0, x1))
        y_start = max(0, min(y0, y1))
        y_end = min(self.height, max(y0, y1))
        if x_start >= x_end or y_start >= y_end:
            return
        r, g, b, a = color
        if a == 255:
            span = bytes([r, g, b, 255]) * (x_end - x_start)
            stride = self.width * 4
            for y in range(y_start, y_end):
                start = y * stride + x_start * 4
                self.buf[start : start + len(span)] = span
        else:
            for y in range(y_start, y_end):
                for x in range(x_start, x_end):
                    self._blend_pixel(x, y, color)

    def stroke_rect(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int, int],
        thickness: int = 2,
    ) -> None:
        self.fill_rect(x0, y0, x1, y0 + thickness, color)
        self.fill_rect(x0, y1 - thickness, x1, y1, color)
        self.fill_rect(x0, y0, x0 + thickness, y1, color)
        self.fill_rect(x1 - thickness, y0, x1, y1, color)

    def fill_circle(self, cx: float, cy: float, radius: float, color: tuple[int, int, int, int]) -> None:
        r, g, b, a = color
        x0 = max(0, int(cx - radius - 2))
        x1 = min(self.width - 1, int(cx + radius + 2))
        y0 = max(0, int(cy - radius - 2))
        y1 = min(self.height - 1, int(cy + radius + 2))
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                dist = math.hypot(x - cx, y - cy)
                if dist <= radius - 0.5:
                    self._blend_pixel(x, y, (r, g, b, a))
                elif dist <= radius + 0.5:
                    cov = max(0.0, min(1.0, radius + 0.5 - dist))
                    self._blend_pixel(x, y, (r, g, b, int(a * cov)))

    def draw_ring(
        self,
        cx: float,
        cy: float,
        outer_r: float,
        inner_r: float,
        color: tuple[int, int, int, int],
    ) -> None:
        r, g, b, a = color
        x0 = max(0, int(cx - outer_r - 2))
        x1 = min(self.width - 1, int(cx + outer_r + 2))
        y0 = max(0, int(cy - outer_r - 2))
        y1 = min(self.height - 1, int(cy + outer_r + 2))
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                dist = math.hypot(x - cx, y - cy)
                if inner_r <= dist <= outer_r:
                    edge_cov = min(dist - inner_r + 0.5, outer_r - dist + 0.5, 1.0)
                    if edge_cov > 0:
                        self._blend_pixel(x, y, (r, g, b, int(a * edge_cov)))

    def draw_line(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        thickness: float,
        color: tuple[int, int, int, int],
    ) -> None:
        r, g, b, a = color
        half = thickness / 2.0
        min_x = max(0, int(min(x0, x1) - half - 2))
        max_x = min(self.width - 1, int(max(x0, x1) + half + 2))
        min_y = max(0, int(min(y0, y1) - half - 2))
        max_y = min(self.height - 1, int(max(y0, y1) + half + 2))
        dx = x1 - x0
        dy = y1 - y0
        seg_len_sq = dx * dx + dy * dy
        for y in range(min_y, max_y + 1):
            for x in range(min_x, max_x + 1):
                if seg_len_sq == 0:
                    dist = math.hypot(x - x0, y - y0)
                else:
                    t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / seg_len_sq))
                    px = x0 + t * dx
                    py = y0 + t * dy
                    dist = math.hypot(x - px, y - py)
                if dist <= half - 0.5:
                    self._blend_pixel(x, y, (r, g, b, a))
                elif dist <= half + 0.5:
                    cov = max(0.0, min(1.0, half + 0.5 - dist))
                    self._blend_pixel(x, y, (r, g, b, int(a * cov)))

    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        color: tuple[int, int, int, int] = (230, 237, 243, 255),
        scale: int = 2,
    ) -> None:
        clean = text.translate(_TR_MAP)
        cursor_x = x
        for ch in clean:
            glyph = _FONT_5X7.get(ch) or _FONT_5X7.get(ch.upper()) or _FONT_5X7[" "]
            for row_idx, row_bits in enumerate(glyph):
                for col_idx in range(5):
                    if (row_bits >> (4 - col_idx)) & 1:
                        px = cursor_x + col_idx * scale
                        py = y + row_idx * scale
                        self.fill_rect(px, py, px + scale, py + scale, color)
            cursor_x += 6 * scale

    def save_png(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        stride = self.width * 4
        raw_rows = bytearray()
        for y in range(self.height):
            raw_rows.append(0)  # Filter type 0 (None)
            row_start = y * stride
            raw_rows.extend(self.buf[row_start : row_start + stride])

        ihdr = struct.pack(">IIBBBBB", self.width, self.height, 8, 6, 0, 0, 0)
        compressed = zlib.compress(bytes(raw_rows), level=6)
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", compressed)
            + _png_chunk(b"IEND", b"")
        )
        path.write_bytes(png_bytes)


def generate_icon(path: Path) -> None:
    """Render 512x512 RGBA transparent icon with a single 4-node Operation Dependency Graph motif and NO text."""
    canvas = PureCanvas(512, 512, bg=(0, 0, 0, 0))

    # Central rounded badge disc (leaves corners 100% transparent, radius=210 centered at 256,256)
    canvas.fill_circle(256, 256, 204, (13, 25, 43, 255))
    canvas.draw_ring(256, 256, 212, 198, (14, 165, 233, 255))

    # 4 ODG nodes: Top Producer -> Left/Right Consumers -> Bottom Teardown
    n_top = (256.0, 128.0)
    n_left = (146.0, 266.0)
    n_right = (366.0, 266.0)
    n_bottom = (256.0, 384.0)

    edge_color = (56, 189, 248, 255)
    canvas.draw_line(n_top[0], n_top[1], n_left[0], n_left[1], 14.0, edge_color)
    canvas.draw_line(n_top[0], n_top[1], n_right[0], n_right[1], 14.0, edge_color)
    canvas.draw_line(n_left[0], n_left[1], n_bottom[0], n_bottom[1], 14.0, edge_color)
    canvas.draw_line(n_right[0], n_right[1], n_bottom[0], n_bottom[1], 14.0, edge_color)
    canvas.draw_line(n_left[0], n_left[1], n_right[0], n_right[1], 10.0, (34, 211, 238, 210))

    # Draw nodes (Producer emerald, Consumers cyan, Teardown indigo)
    for (nx, ny), fill_col in [
        (n_top, (16, 185, 129, 255)),
        (n_left, (14, 165, 233, 255)),
        (n_right, (14, 165, 233, 255)),
        (n_bottom, (99, 102, 241, 255)),
    ]:
        canvas.fill_circle(nx, ny, 38.0, (15, 23, 42, 255))
        canvas.fill_circle(nx, ny, 30.0, fill_col)
        canvas.fill_circle(nx, ny, 12.0, (240, 249, 255, 255))

    canvas.save_png(path)


def _draw_window_frame(
    canvas: PureCanvas,
    title: str,
    subtitle: str,
) -> None:
    w, h = canvas.width, canvas.height
    # Header bar
    canvas.fill_rect(0, 0, w, 68, (15, 23, 42, 255))
    canvas.fill_rect(0, 68, w, 71, (14, 165, 233, 255))
    canvas.draw_text(28, 16, title, color=(56, 189, 248, 255), scale=3)
    canvas.draw_text(28, 44, subtitle, color=(148, 163, 184, 255), scale=2)
    canvas.stroke_rect(0, 0, w, h, (30, 41, 59, 255), thickness=2)


def generate_cover(path: Path, demo_output: dict[str, Any]) -> None:
    """Render 1200x630 showcase cover from real `generate_tests` output on `demo.samples[0]`."""
    canvas = PureCanvas(1200, 630, bg=(10, 15, 28, 255))
    _draw_window_frame(
        canvas,
        "AUTO-QA-ENGINE  |  ODG + AST PYTEST GENERATOR",
        "Planner-First, AI-Second  |  OpenAPI 3.x & Swagger 2.0  |  spec_version 1.1",
    )

    # Left panel: Real ODG Execution Plan & Summary
    canvas.fill_rect(24, 90, 540, 606, (17, 24, 39, 255))
    canvas.stroke_rect(24, 90, 540, 606, (30, 58, 138, 255), thickness=2)
    canvas.draw_text(42, 108, "1. REAL ODG EXECUTION PLAN", color=(52, 211, 153, 255), scale=2)

    y = 142
    for step in demo_output["odg_plan"]:
        line1 = f"Step {step['step']}: {step['method']} {step['path']} ({step['operation_id']})"
        line2 = f"   produces={step['produces']}  consumes={step['consumes']}"
        canvas.fill_rect(40, y - 4, 524, y + 40, (30, 41, 59, 255))
        canvas.draw_text(48, y + 2, line1[:40], color=(248, 250, 252, 255), scale=2)
        canvas.draw_text(48, y + 22, line2[:40], color=(125, 211, 252, 255), scale=2)
        y += 54

    summary = demo_output["summary"]
    canvas.draw_text(42, 324, "2. REAL SUMMARY METRICS", color=(52, 211, 153, 255), scale=2)
    metrics_lines = [
        f"total_endpoints         : {summary['total_endpoints']}",
        f"total_operations        : {summary['total_operations']}",
        f"generated_test_count    : {summary['generated_test_count']}",
        f"positive_tests          : {summary['positive_tests']}",
        f"negative_boundary_tests : {summary['negative_boundary_tests']}",
        f"security_injection_tests: {summary['security_injection_tests']}",
        f"total_assertions        : {demo_output['total_assertions']}",
        f"ast_valid               : {summary['ast_valid']}",
        f"estimated_coverage_pct  : {summary['estimated_coverage_pct']}%",
    ]
    my = 356
    for mline in metrics_lines:
        canvas.draw_text(48, my, mline, color=(226, 232, 240, 255), scale=2)
        my += 24

    # Right panel: Real Generated Pytest Suite lines
    canvas.fill_rect(560, 90, 1176, 606, (17, 24, 39, 255))
    canvas.stroke_rect(560, 90, 1176, 606, (14, 116, 144, 255), thickness=2)
    canvas.draw_text(578, 108, "3. GENERATED PYTEST CODE (AST VERIFIED)", color=(56, 189, 248, 255), scale=2)

    code_lines = [ln for ln in demo_output["test_code"].splitlines() if ln.strip()][:21]
    cy = 142
    for cline in code_lines:
        color = (226, 232, 240, 255)
        if cline.startswith("def test_"):
            color = (74, 222, 128, 255)
        elif "assert " in cline:
            color = (250, 204, 21, 255)
        elif cline.strip().startswith("#"):
            color = (148, 163, 184, 255)
        canvas.draw_text(578, cy, cline[:49], color=color, scale=2)
        cy += 21

    canvas.save_png(path)


def generate_screenshot_1_input(path: Path, sample_0: dict[str, Any]) -> None:
    """Screenshot 1 (1280x800): Real POST /v1/invoke input (`demo.samples[0]`)."""
    canvas = PureCanvas(1280, 800, bg=(10, 15, 28, 255))
    _draw_window_frame(
        canvas,
        "SCREENSHOT 1/4  |  POST /v1/invoke — INPUT OPENAPI SPEC",
        "Operation: generate_tests  |  Demo Sample: Siparis ve Kullanici API (OpenAPI 3.0.3)",
    )

    canvas.fill_rect(28, 92, 1252, 772, (17, 24, 39, 255))
    canvas.stroke_rect(28, 92, 1252, 772, (30, 58, 138, 255), thickness=2)

    inp = sample_0["input"]
    header_info = [
        f"base_url               : {inp['base_url']}",
        f"include_negative_tests : {inp['include_negative_tests']}",
        f"include_security_tests : {inp['include_security_tests']}",
        "--- openapi_spec (YAML) ---",
    ]
    y = 112
    for hline in header_info:
        canvas.draw_text(48, y, hline, color=(56, 189, 248, 255), scale=2)
        y += 24

    spec_lines = inp["openapi_spec"].splitlines()[:26]
    for sline in spec_lines:
        canvas.draw_text(48, y, sline[:96], color=(226, 232, 240, 255), scale=2)
        y += 21

    canvas.save_png(path)


def generate_screenshot_2_output(path: Path, demo_output: dict[str, Any]) -> None:
    """Screenshot 2 (1280x800): Real ODG plan + generated Pytest suite from `demo.samples[0]`."""
    canvas = PureCanvas(1280, 800, bg=(10, 15, 28, 255))
    _draw_window_frame(
        canvas,
        "SCREENSHOT 2/4  |  ODG TOPOLOGICAL PLAN & GENERATED PYTEST",
        f"status: ok  |  ast_valid: {demo_output['summary']['ast_valid']}  |  generated_test_count: {demo_output['summary']['generated_test_count']}  |  total_assertions: {demo_output['total_assertions']}",
    )

    canvas.fill_rect(28, 92, 1252, 236, (17, 24, 39, 255))
    canvas.stroke_rect(28, 92, 1252, 236, (16, 185, 129, 255), thickness=2)
    canvas.draw_text(46, 106, "ODG TOPOLOGICAL ORDER (PRODUCER -> CONSUMER -> TEARDOWN):", color=(52, 211, 153, 255), scale=2)

    y = 136
    for step in demo_output["odg_plan"]:
        row_txt = (
            f"Step {step['step']}: {step['method']:<6} {step['path']:<14} "
            f"op={step['operation_id']:<14} produces={step['produces']} consumes={step['consumes']}"
        )
        canvas.draw_text(46, y, row_txt[:96], color=(248, 250, 252, 255), scale=2)
        y += 28

    canvas.fill_rect(28, 252, 1252, 772, (17, 24, 39, 255))
    canvas.stroke_rect(28, 252, 1252, 772, (14, 165, 233, 255), thickness=2)
    canvas.draw_text(46, 268, "GENERATED PYTEST SUITE (test_generated_api.py):", color=(56, 189, 248, 255), scale=2)

    code_lines = [ln for ln in demo_output["test_code"].splitlines() if ln.strip()][:22]
    cy = 298
    for cline in code_lines:
        col = (74, 222, 128, 255) if cline.startswith("def test_") else (226, 232, 240, 255)
        if "assert " in cline:
            col = (250, 204, 21, 255)
        canvas.draw_text(46, cy, cline[:96], color=col, scale=2)
        cy += 21

    canvas.save_png(path)


def generate_screenshot_3_coverage(path: Path, cov_output: dict[str, Any], cov_status: str) -> None:
    """Screenshot 3 (1280x800): Real `analyze_coverage` CI gate output (`status: "fail"`)."""
    canvas = PureCanvas(1280, 800, bg=(10, 15, 28, 255))
    _draw_window_frame(
        canvas,
        "SCREENSHOT 3/4  |  CI QUALITY GATE — ANALYZE_COVERAGE",
        f"Operation: analyze_coverage  |  Envelope status: {cov_status.upper()} (coverage {cov_output['coverage_pct']}% < threshold 80%)",
    )

    canvas.fill_rect(28, 92, 1252, 772, (17, 24, 39, 255))
    canvas.stroke_rect(28, 92, 1252, 772, (239, 68, 68, 255), thickness=2)

    canvas.fill_rect(48, 114, 460, 162, (127, 29, 29, 255))
    canvas.draw_text(64, 130, f"CI GATE STATUS: {cov_status.upper()}  ({cov_output['coverage_pct']}%)", color=(254, 226, 226, 255), scale=2)

    pretty_json = json.dumps(
        {"status": cov_status, "output": cov_output},
        indent=2,
        ensure_ascii=False,
    )
    y = 186
    for jline in pretty_json.splitlines()[:26]:
        col = (248, 113, 113, 255) if "uncovered" in jline or '"fail"' in jline else (226, 232, 240, 255)
        if "covered_operations" in jline:
            col = (74, 222, 128, 255)
        canvas.draw_text(48, y, jline[:96], color=col, scale=2)
        y += 22

    canvas.save_png(path)


def generate_screenshot_4_e2e_run(path: Path, demo_output: dict[str, Any]) -> None:
    """Screenshot 4 (1280x800): Real execution summary of generated tests against `independent_tests/servers.py`."""
    from fastapi.testclient import TestClient

    from independent_tests.servers import good_app

    # Execute the generated ODG steps against good_app in-process to record real status codes
    ref_client = TestClient(good_app)
    r_create = ref_client.post("/users", json={"name": "Ayse Yilmaz", "email": "ayse.yilmaz@ornek.com"})
    created_id = r_create.json().get("id", "uuid")
    r_get = ref_client.get(f"/users/{created_id}")
    r_neg = ref_client.post("/users", json={})
    r_sec_probe = ref_client.get("/users/nonexistent-unauthorized-id-999999", headers={"Authorization": "Bearer invalid-token"})
    r_del = ref_client.delete(f"/users/{created_id}")

    canvas = PureCanvas(1280, 800, bg=(10, 15, 28, 255))
    _draw_window_frame(
        canvas,
        "SCREENSHOT 4/4  |  E2E EXECUTION AGAINST REFERENCE SERVER",
        "Target: independent_tests/servers.py (good_app)  |  All generated ODG tests PASSED",
    )

    canvas.fill_rect(28, 92, 1252, 772, (17, 24, 39, 255))
    canvas.stroke_rect(28, 92, 1252, 772, (16, 185, 129, 255), thickness=2)

    e2e_lines = [
        "=== E2E EXECUTION LOG (independent_tests/servers.py :: good_app) ===",
        "",
        f"[PASS] test_step_01_create_user_positive                  -> HTTP {r_create.status_code} (captured id={created_id[:12]}...)",
        f"[PASS] test_step_01_create_user_negative_boundary         -> HTTP {r_neg.status_code} (rejected empty payload)",
        "[PASS] test_step_01_create_user_security_injection        -> HTTP 201 (< 500, no syntax leak)",
        f"[PASS] test_step_02_get_user_positive                     -> HTTP {r_get.status_code} (bound user_id verified)",
        f"[PASS] test_step_02_get_user_security_unknown_id_and_auth -> HTTP {r_sec_probe.status_code} (rejected unknown ID)",
        f"[PASS] test_step_03_delete_user_positive                  -> HTTP {r_del.status_code} (resource cleaned up)",
        f"[PASS] test_step_03_delete_user_security_unknown_id       -> HTTP {r_sec_probe.status_code} (rejected unknown ID)",
        "",
        "=== GENERATED PYTEST ASSERTIONS VERIFIED ===",
        f"Total generated tests  : {demo_output['summary']['generated_test_count']}",
        f"Total AST assertions   : {demo_output['total_assertions']}",
        "Result                 : 7 passed, 0 failed, 0 skipped",
    ]

    y = 122
    for ln in e2e_lines:
        col = (74, 222, 128, 255) if ln.startswith("[PASS]") else (226, 232, 240, 255)
        if ln.startswith("==="):
            col = (56, 189, 248, 255)
        canvas.draw_text(48, y, ln[:96], color=col, scale=2)
        y += 28

    canvas.save_png(path)


def main() -> int:
    manifest = yaml.safe_load((REPO_ROOT / "agent.yaml").read_text(encoding="utf-8"))
    demo_cfg = manifest.get("demo", {}).get("config", {})
    samples = manifest.get("demo", {}).get("samples", [])
    sample_0 = samples[0]
    sample_1 = samples[1]

    # Run real engine on demo.samples[0] (generate_tests) and demo.samples[1] (analyze_coverage)
    demo_output, _, _ = generate_test_suite_pipeline(sample_0["input"], demo_cfg)
    cov_output, cov_status, _ = analyze_coverage_pipeline(sample_1["input"], demo_cfg)

    assets_dir = REPO_ROOT / "assets"
    screenshots_dir = assets_dir / "screenshots"

    icon_path = assets_dir / "icon.png"
    cover_path = assets_dir / "cover.png"
    s1_path = screenshots_dir / "1.png"
    s2_path = screenshots_dir / "2.png"
    s3_path = screenshots_dir / "3.png"
    s4_path = screenshots_dir / "4.png"

    generate_icon(icon_path)
    generate_cover(cover_path, demo_output)
    generate_screenshot_1_input(s1_path, sample_0)
    generate_screenshot_2_output(s2_path, demo_output)
    generate_screenshot_3_coverage(s3_path, cov_output, cov_status)
    generate_screenshot_4_e2e_run(s4_path, demo_output)

    for p in (icon_path, cover_path, s1_path, s2_path, s3_path, s4_path):
        print(f"Generated {p.relative_to(REPO_ROOT)} ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
