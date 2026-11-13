"""Pre-submission verification script (`scripts/check_submission.py`) for Agent Pazaryeri (spec_version 1.1)."""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lint_integrations import check_integrations  # noqa: E402

REQUIRED_AGENT_YAML_KEYS = (
    "spec_version",
    "slug",
    "name",
    "version",
    "author",
    "category",
    "short_note",
    "description_file",
    "languages",
    "assets",
    "types",
    "runtime",
    "network",
    "operations",
    "config_schema",
    "demo",
)

REQUIRED_EVAL_GROUPS = ("normal", "edge", "out_of_scope", "attack")


def inspect_png_header_and_corners(path: Path) -> tuple[int, int, int, list[int]]:
    """Return `(width, height, color_type, corner_alphas)` for a PNG file.

    Uses Pillow if installed; otherwise parses PNG `IHDR` and `IDAT` chunks using stdlib `struct` and `zlib`.
    `color_type == 6` indicates RGBA.
    """
    raw = path.read_bytes()
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a valid PNG file")

    width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", raw[16:29])

    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as img:
            w, h = img.size
            if img.mode == "RGBA":
                corners = [
                    img.getpixel((0, 0))[3],
                    img.getpixel((w - 1, 0))[3],
                    img.getpixel((0, h - 1))[3],
                    img.getpixel((w - 1, h - 1))[3],
                ]
                return w, h, 6, corners
            return w, h, color_type, []
    except ImportError:
        pass

    if color_type != 6 or bit_depth != 8 or interlace != 0:
        return width, height, color_type, []

    idat_chunks: list[bytes] = []
    pos = 8
    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        chunk_type = raw[pos + 4 : pos + 8]
        chunk_data = raw[pos + 8 : pos + 8 + length]
        if chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break
        pos += 12 + length

    decompressed = zlib.decompress(b"".join(idat_chunks))
    stride = width * 4
    row_bytes = 1 + stride
    if len(decompressed) < height * row_bytes:
        return width, height, color_type, []

    # Reconstruct first and last scanlines (supports filter types 0=None, 1=Sub, 2=Up)
    prev_row = bytearray(stride)
    first_row = bytearray(stride)
    last_row = bytearray(stride)

    for y in range(height):
        row_start = y * row_bytes
        f_type = decompressed[row_start]
        scanline = bytearray(decompressed[row_start + 1 : row_start + row_bytes])
        recon = bytearray(stride)
        for i in range(stride):
            left = recon[i - 4] if i >= 4 else 0
            up = prev_row[i]
            up_left = prev_row[i - 4] if i >= 4 else 0
            val = scanline[i]
            if f_type == 0:
                recon[i] = val
            elif f_type == 1:
                recon[i] = (val + left) & 0xFF
            elif f_type == 2:
                recon[i] = (val + up) & 0xFF
            elif f_type == 3:
                recon[i] = (val + ((left + up) >> 1)) & 0xFF
            elif f_type == 4:
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                pr = left if (pa <= pb and pa <= pc) else (up if pb <= pc else up_left)
                recon[i] = (val + pr) & 0xFF
            else:
                recon[i] = val
        if y == 0:
            first_row = recon
        if y == height - 1:
            last_row = recon
        prev_row = recon

    corners = [
        first_row[3],
        first_row[(width - 1) * 4 + 3],
        last_row[3],
        last_row[(width - 1) * 4 + 3],
    ]
    return width, height, color_type, corners


def check_submission(repo_root: Path = REPO_ROOT) -> list[str]:
    """Run all submission readiness checks on `repo_root` and return a list of failure messages."""
    errors: list[str] = []

    # 1. Check .env does not exist in repo
    if (repo_root / ".env").exists():
        errors.append(".env file exists in repository root (prohibited by spec_version 1.1).")

    # 2. Check EVAL_REPORT.md and docs/DESCRIPTION.md exist
    if not (repo_root / "EVAL_REPORT.md").is_file():
        errors.append("EVAL_REPORT.md is missing at repository root.")
    if not (repo_root / "docs" / "DESCRIPTION.md").is_file():
        errors.append("docs/DESCRIPTION.md is missing.")

    # 3. Check Dockerfile
    dockerfile_path = repo_root / "Dockerfile"
    if not dockerfile_path.is_file():
        errors.append("Dockerfile is missing.")
    else:
        df_text = dockerfile_path.read_text(encoding="utf-8")
        if "python:3.12-slim" not in df_text:
            errors.append("Dockerfile does not use 'python:3.12-slim'.")
        if "USER 1000" not in df_text:
            errors.append("Dockerfile does not enforce non-root 'USER 1000'.")

    # 4. Check agent.yaml
    agent_yaml_path = repo_root / "agent.yaml"
    if not agent_yaml_path.is_file():
        errors.append("agent.yaml is missing.")
        return errors

    try:
        manifest: dict[str, Any] = yaml.safe_load(agent_yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        errors.append(f"agent.yaml failed YAML parsing: {exc}")
        return errors

    for req_key in REQUIRED_AGENT_YAML_KEYS:
        if req_key not in manifest:
            errors.append(f"agent.yaml is missing required top-level key: '{req_key}'.")

    name_val = str(manifest.get("name") or "")
    if len(name_val) > 30:
        errors.append(f"agent.yaml 'name' exceeds 30 characters ({len(name_val)} chars): {name_val!r}")

    short_note_val = str(manifest.get("short_note") or "").strip()
    if len(short_note_val) > 110:
        errors.append(
            f"agent.yaml 'short_note' exceeds 110 characters ({len(short_note_val)} chars): {short_note_val!r}"
        )

    # 5. Check assets referenced in agent.yaml
    assets_cfg = manifest.get("assets") if isinstance(manifest.get("assets"), dict) else {}
    icon_rel = assets_cfg.get("icon")
    cover_rel = assets_cfg.get("cover")
    screenshots_rel = assets_cfg.get("screenshots")

    if not icon_rel or not isinstance(icon_rel, str):
        errors.append("agent.yaml 'assets.icon' is missing.")
    else:
        icon_path = repo_root / icon_rel
        if not icon_path.is_file():
            errors.append(f"Asset '{icon_rel}' does not exist.")
        else:
            try:
                w, h, ctype, corners = inspect_png_header_and_corners(icon_path)
                if (w, h) != (512, 512):
                    errors.append(f"Asset '{icon_rel}' must be 512x512, got {w}x{h}.")
                if ctype != 6:
                    errors.append(f"Asset '{icon_rel}' must have an RGBA alpha channel (color_type=6).")
                elif corners and any(a != 0 for a in corners):
                    errors.append(f"Asset '{icon_rel}' must have transparent corners (got corner alphas {corners}).")
            except Exception as exc:
                errors.append(f"Asset '{icon_rel}' invalid image: {exc}")

    if not cover_rel or not isinstance(cover_rel, str):
        errors.append("agent.yaml 'assets.cover' is missing.")
    else:
        cover_path = repo_root / cover_rel
        if not cover_path.is_file():
            errors.append(f"Asset '{cover_rel}' does not exist.")
        else:
            try:
                w, h, _, _ = inspect_png_header_and_corners(cover_path)
                if (w, h) != (1200, 630):
                    errors.append(f"Asset '{cover_rel}' must be 1200x630, got {w}x{h}.")
            except Exception as exc:
                errors.append(f"Asset '{cover_rel}' invalid image: {exc}")

    if not isinstance(screenshots_rel, list) or not (3 <= len(screenshots_rel) <= 5):
        errors.append("agent.yaml 'assets.screenshots' must be a list of 3 to 5 screenshot paths.")
    else:
        for s_rel in screenshots_rel:
            s_path = repo_root / str(s_rel)
            if not s_path.is_file():
                errors.append(f"Screenshot '{s_rel}' does not exist.")
            else:
                try:
                    w, h, _, _ = inspect_png_header_and_corners(s_path)
                    if (w, h) != (1280, 800):
                        errors.append(f"Screenshot '{s_rel}' must be 1280x800, got {w}x{h}.")
                except Exception as exc:
                    errors.append(f"Screenshot '{s_rel}' invalid image: {exc}")

    # 6. Check integrations/*
    errors.extend(check_integrations(repo_root))

    # 7. Check evals/cases.yaml
    cases_path = repo_root / "evals" / "cases.yaml"
    if not cases_path.is_file():
        errors.append("evals/cases.yaml is missing.")
    else:
        try:
            eval_doc = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
            cases = eval_doc.get("cases") if isinstance(eval_doc, dict) else []
            if not isinstance(cases, list) or len(cases) < 20:
                errors.append(
                    f"evals/cases.yaml must have at least 20 cases (found {len(cases) if isinstance(cases, list) else 0})."
                )
            else:
                groups_found = {str(c.get("group") or "") for c in cases if isinstance(c, dict)}
                for req_grp in REQUIRED_EVAL_GROUPS:
                    if req_grp not in groups_found:
                        errors.append(f"evals/cases.yaml is missing required group '{req_grp}'.")
        except Exception as exc:
            errors.append(f"evals/cases.yaml failed YAML parsing: {exc}")

    return errors


def main() -> int:
    errors = check_submission(REPO_ROOT)
    if errors:
        print("Submission check FAILED with the following issues:", file=sys.stderr)
        for idx, err in enumerate(errors, start=1):
            print(f"  {idx}. {err}", file=sys.stderr)
        return 1
    print("Submission check PASSED: all spec_version 1.1 packaging, asset, integration, and eval rules verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
