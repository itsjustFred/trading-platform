from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from shared.constants import DATA_DIR


def _has_ohlcv_data(symbol: str, timeframe: str) -> bool:
    root = DATA_DIR / "ohlcv"
    if not root.exists():
        return False

    symbol = str(symbol or "").strip().upper()
    timeframe = str(timeframe or "").strip().lower()
    if not symbol or not timeframe:
        return False

    # Canonical layout: .../<symbol>/<timeframe>.parquet
    if any(root.rglob(f"**/{symbol}/{timeframe}.parquet")):
        return True

    # Some datasets are exported as .../<symbol>.parquet (typically 1d).
    if timeframe == "1d" and any(root.rglob(f"**/{symbol}.parquet")):
        return True

    return False


@pytest.fixture(autouse=True, scope="module")
def skip_when_required_data_or_dependency_missing(request: pytest.FixtureRequest):
    module = request.module
    module_name = str(getattr(module, "__name__", "") or "").lower()

    if "pynecore" in module_name and importlib.util.find_spec("pynecore") is None:
        pytest.skip("pynecore is not installed in this environment")

    symbol = getattr(module, "TEST_SYMBOL", None)
    timeframe = getattr(module, "TEST_TIMEFRAME", None)
    if symbol and timeframe and not _has_ohlcv_data(str(symbol), str(timeframe)):
        pytest.skip(
            f"missing OHLCV dataset for {symbol}/{timeframe} under {Path(DATA_DIR) / 'ohlcv'}"
        )
