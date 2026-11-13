"""Verification that `docs/DESCRIPTION.md` example input/output and claims match the real engine."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from src.generator import generate_test_suite_pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_description_md_example_matches_real_engine_output(monkeypatch) -> None:
    """[3a] Extract example input and output JSON blocks from DESCRIPTION.md and verify exact numbers."""
    monkeypatch.setenv("LLM_BASE_URL", "")
    desc_path = REPO_ROOT / "docs" / "DESCRIPTION.md"
    text = desc_path.read_text(encoding="utf-8")

    json_blocks = re.findall(r"```json\s*\n(.*?)```", text, flags=re.DOTALL)
    assert len(json_blocks) >= 2, "Expected at least 2 JSON blocks (Example Input and Example Output) in DESCRIPTION.md"

    example_input = json.loads(json_blocks[0])
    documented_output = json.loads(json_blocks[1])

    real_output, status, _ = generate_test_suite_pipeline(example_input, {"default_locale": "tr-TR"})
    assert status == "ok"

    assert documented_output["summary"] == real_output["summary"], (
        f"DESCRIPTION.md summary mismatch!\nDocumented: {documented_output['summary']}\nReal: {real_output['summary']}"
    )
    assert documented_output["odg_plan"] == real_output["odg_plan"]
    assert documented_output["endpoints_covered"] == real_output["endpoints_covered"]
    assert documented_output["total_assertions"] == real_output["total_assertions"], (
        f"DESCRIPTION.md total_assertions={documented_output['total_assertions']} != real={real_output['total_assertions']}"
    )


def test_description_md_has_no_overstated_claims_and_documents_limitations() -> None:
    """[3b & 3c] Verify overstated claims are removed and honest limitations are documented."""
    text = (REPO_ROOT / "docs" / "DESCRIPTION.md").read_text(encoding="utf-8")

    assert "%100 çalışır kod" not in text
    assert "test kapsamını %100'e çıkarmak" not in text
    assert "BOLA/IDOR yükleri" not in text

    # Honest limitations must mention heuristic ownership, producer failure skipping, ~2% card filter, and LLM_TIMEOUT_S
    assert "user_id" in text and "owner_id" in text
    assert "skipped" in text.lower() or "atlanır" in text.lower()
    assert "%2" in text or "2%" in text
    assert "LLM_TIMEOUT_S" in text
