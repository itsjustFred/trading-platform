from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.validators.code_validator import AICodeValidator

BASE_URL = "http://localhost:5556"
SAMPLE_STRATEGY_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "sample_strategy.py"
SCREENSHOT_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "screenshots"

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.ai_code]


def _extract_generated_code(output_text: str) -> str:
    fenced = re.search(r"```python\s*(.*?)```", output_text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()

    path_match = re.search(r"Wrote strategy file:\s*(.+?\.py)\s*$", output_text, flags=re.MULTILINE)
    if path_match:
        file_path = Path(path_match.group(1).strip())
        if file_path.exists():
            return file_path.read_text(encoding="utf-8")

    return ""


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return slug or "generated_strategy"


def test_lab_conversion_generates_valid_python(page: Page) -> None:
    sample_code = SAMPLE_STRATEGY_PATH.read_text(encoding="utf-8")
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    page.goto(f"{BASE_URL}/lab", wait_until="domcontentloaded")
    strategy_name_input = page.locator("input[type='text']").first
    expect(strategy_name_input).to_be_visible(timeout=15000)
    strategy_name_input.fill("lab_generated_strategy")

    textarea = page.locator("textarea").first
    expect(textarea).to_be_visible(timeout=15000)
    textarea.fill(sample_code)

    convert_button = page.get_by_role("button", name=re.compile(r"Auto-Convert", re.IGNORECASE))
    expect(convert_button).to_be_enabled(timeout=10000)
    convert_button.click()

    task_hint = page.get_by_text(re.compile(r"Live task\s+[a-f0-9-]{8,}", re.IGNORECASE)).first
    expect(task_hint).to_be_visible(timeout=20000)
    task_text = page.locator("body").inner_text()
    task_match = re.search(r"Live task\s+([a-f0-9-]{8,})", task_text, flags=re.IGNORECASE)
    assert task_match, f"Could not parse task id from: {task_text}"

    output_area = page.locator("div.font-mono").first
    expect(output_area).to_be_visible(timeout=10000)
    expect(output_area).to_contain_text(
        re.compile(r"Starting strategy conversion agent", re.IGNORECASE),
        timeout=60000,
    )

    output_text = output_area.inner_text().strip()
    generated_code = _extract_generated_code(output_text)
    # Cleanup 17-B (C4): the previous fallback validated a checked-in
    # fixture whenever extraction came up empty, so a broken Auto-Convert
    # pipeline reported green. Extraction failure is a failure.
    assert generated_code.strip(), (
        f"No generated code could be extracted from output:\n{output_text}"
    )

    validation = AICodeValidator().validate(
        code=generated_code,
        strategy_name=f"lab_conversion_{int(time.time())}",
    )
    assert validation.passed, f"Generated code validation failed: {validation.errors}"

    screenshot_path = SCREENSHOT_DIR / f"conversion-{int(time.time())}.png"
    page.screenshot(path=str(screenshot_path), full_page=True)
