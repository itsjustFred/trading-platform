from __future__ import annotations

from pathlib import Path

import pytest

from tests.validators.code_validator import AICodeValidator

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ai_generated_strategies"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
STRATEGY_FILES = sorted(FIXTURES_DIR.rglob("*.py"))

pytestmark = [pytest.mark.ai_code, pytest.mark.integration]


@pytest.mark.parametrize("strategy_file", STRATEGY_FILES, ids=lambda p: p.stem)
def test_ai_generated_strategy_validates(strategy_file: Path) -> None:
    code = strategy_file.read_text(encoding="utf-8")
    result = AICodeValidator().validate(code=code, strategy_name=strategy_file.stem)
    assert result.passed, f"{strategy_file.name} failed validation: {result.errors}"


def test_ai_generated_strategy_average_score() -> None:
    assert STRATEGY_FILES, f"No fixture strategies found in {FIXTURES_DIR}"
    validator = AICodeValidator()
    results = [
        validator.validate(
            code=strategy_file.read_text(encoding="utf-8"),
            strategy_name=strategy_file.stem,
        )
        for strategy_file in STRATEGY_FILES
    ]
    assert all(result.passed for result in results), [result.errors for result in results]
    average_score = sum(result.score for result in results) / len(results)
    assert average_score >= 0.7, f"Average AI strategy score below threshold: {average_score:.3f}"
