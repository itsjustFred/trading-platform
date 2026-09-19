"""
Tests fuer Strategie-Skelett: Tiingo News Sentiment Long-Short (BLOCKED).

Das Skelett darf niemals stille Signale liefern: generate_signals muss mit
einer klaren Blocked-Begruendung scheitern (kein News-Feed im Repo, offline
nicht baubar, kein Fake-Sentiment aus Preisdaten).
"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent.parent
STRATEGY_PATH = PROJECT_ROOT / "strategies" / "tiingo_sentiment" / "tiingo_sentiment.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("tiingo_sentiment", str(STRATEGY_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame() -> pd.DataFrame:
    closes = [100.0 + 0.1 * i for i in range(30)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=30, freq="D", tz="UTC"),
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1000.0] * 30,
        }
    )


def test_generate_signals_is_blocked():
    module = _load_module()
    try:
        module.generate_signals(_frame(), {})
    except NotImplementedError as exc:
        message = str(exc)
    else:
        raise AssertionError("generate_signals must raise NotImplementedError while blocked")
    assert "BLOCKED" in message
    assert "TiingoNews" in message or "news" in message.lower()


def test_skeleton_meta_marks_blocked_status():
    module = _load_module()
    meta = module.STRATEGY_META
    assert "tiingo_sentiment_long_short" in meta["source_url"]
    assert meta["tags"]["status"] == "blocked-missing-data-feed"
    assert "BLOCKED" in meta["notes"]
