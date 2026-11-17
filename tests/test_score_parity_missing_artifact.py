"""score_parity must fail with an actionable message when the artifact is absent."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "strategy-comparisons/intraday_volatility_breakout/audit/score_parity.py"
)


def test_missing_platform_artifact_names_regenerate_script(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("score_parity_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit) as exc:
        module.load_platform(tmp_path / "absent.json", "eurusd", "dukascopy_mt5_forex")
    assert "regenerate_mt5_parity_artifacts.py" in str(exc.value)
