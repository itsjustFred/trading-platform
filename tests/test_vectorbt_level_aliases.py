"""Vectorbt level-alias resolution (#237): one level, canonical spelling first.

The native engine resolves ``take_profit``/``tp_price`` (and
``stop_loss``/``sl_price``) as ONE level with the canonical spelling winning,
independent of the signal frame's column order. The vectorbt adapter must
resolve the same pair the same way, so a strategy that publishes both spellings
of one level exits at the same price in either engine — and records that level
in ``trade.meta``, not whichever spelling came last in the dict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import (
    VectorbtAdapter,
    _find_sizing_stop_series,
    _find_stop_loss_series,
    _find_take_profit_series,
)
from tests.helpers import with_zero_fees
from tests.helpers import write_ohlcv as _write_shared_ohlcv


def _series(values: list[Any]) -> pd.Series:
    return pd.Series(values, index=pd.RangeIndex(len(values)), dtype=object)


def _indicators(*pairs: tuple[str, pd.Series]) -> dict[str, tuple[pd.Series, str]]:
    return {name: (series, "") for name, series in pairs}


def test_take_profit_alias_resolves_the_canonical_spelling_first() -> None:
    """``take_profit`` wins over ``tp_price`` — the live/native precedence."""
    canonical = _series([110.0, None, None])
    alias = _series([105.0, None, None])
    signals = pd.DataFrame({"entries": [True, False, False], "exits": [False, False, True]})

    alias_first = {"tp_price": alias, "take_profit": canonical}
    canonical_first = {"take_profit": canonical, "tp_price": alias}
    for published in (alias_first, canonical_first):
        resolved = _find_take_profit_series(signals, _indicators(*published.items()))

        assert resolved is canonical, "canonical take_profit must win, not the insertion order"


def test_stop_loss_alias_resolves_the_canonical_spelling_first() -> None:
    """The stop pair uses the same precedence as ``risk.live._LIVE_SL_KEYS``."""
    canonical = _series([90.0, None, None])
    alias = _series([95.0, None, None])
    signals = pd.DataFrame({"entries": [True, False, False], "exits": [False, False, True]})

    alias_first = {"sl_price": alias, "stop_loss": canonical}
    canonical_first = {"stop_loss": canonical, "sl_price": alias}
    for published in (alias_first, canonical_first):
        resolved = _find_stop_loss_series(signals, _indicators(*published.items()))

        assert resolved is canonical


def test_sizing_stop_alias_resolves_the_canonical_spelling_first() -> None:
    canonical = _series([90.0, None, None])
    alias = _series([95.0, None, None])
    signals = pd.DataFrame({"entries": [True, False, False], "exits": [False, False, True]})

    alias_first = {"sizing_sl_price": alias, "sizing_stop": canonical}
    canonical_first = {"sizing_stop": canonical, "sizing_sl_price": alias}
    for published in (alias_first, canonical_first):
        resolved = _find_sizing_stop_series(signals, _indicators(*published.items()))

        assert resolved is canonical


def test_alias_fallback_still_resolves_when_the_canonical_is_absent() -> None:
    """Removing the canonical spelling must not disarm the level (#237)."""
    signals = pd.DataFrame({"entries": [True, False, False], "exits": [False, False, True]})
    tp_only = _indicators(("tp_price", _series([105.0, None, None])))
    sl_only = _indicators(("sl_price", _series([95.0, None, None])))

    assert _find_take_profit_series(signals, tp_only) is not None
    assert _find_stop_loss_series(signals, sl_only) is not None


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 01:00", "2024-01-01 02:00"],
                utc=True,
            ),
            "open": [100.0, 101.0, 101.0],
            "high": [100.5, 101.5, 101.5],
            "low": [99.5, 100.5, 100.5],
            "close": [100.2, 101.2, 101.2],
            "volume": [1000.0, 1000.0, 1000.0],
        }
    )


def _intrabar_frame() -> pd.DataFrame:
    """5m sub-bars of the two hours the walk resolves (entry 01:00, exit 02:00).

    The 01:30 sub-bar rallies through BOTH take-profit spellings, so either
    resolution fills a single leg — the fill price is what distinguishes the
    canonical level (110) from the shadowed alias (105).
    """
    rungs = 24
    high = [101.5] * rungs
    low = [100.5] * rungs
    high[6] = 112.5
    low[6] = 100.4
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2024-01-01 01:00", periods=rungs, freq="5min", tz="UTC"
            ),
            "open": [101.0] * rungs,
            "high": high,
            "low": low,
            "close": [101.2] * rungs,
            "volume": [100.0] * rungs,
        }
    )


def _alias_strategy_text(*, canonical_price: str, alias_price: str, alias_first: bool) -> str:
    """One take-profit level published under both spellings, conflicting prices.

    ``alias_first`` publishes the alias spelling (``tp_price``) ahead of the
    canonical one, so the indicators dict's insertion order favours the loser.
    An order-dependent resolution exits at / records the alias price instead.
    """
    levels = "'tp_price': alias, 'take_profit': canonical"
    if alias_first:
        levels = "'take_profit': canonical, 'tp_price': alias"
    return (
        "import pandas as pd\n"
        "STRATEGY_META = {'name': 'vbt_tp_alias', 'version': '1.0'}\n"
        "def generate_signals(df, params):\n"
        "    n = len(df)\n"
        "    entries = pd.Series([i == 0 for i in range(n)], index=df.index)\n"
        "    exits = pd.Series([i == n - 1 for i in range(n)], index=df.index)\n"
        "    direction = pd.Series(['LONG'] + [None] * (n - 1), index=df.index, dtype=object)\n"
        f"    alias = pd.Series([{alias_price}, None, None], index=df.index, dtype=object)\n"
        f"    canonical = pd.Series([{canonical_price}, None, None], index=df.index)\n"
        f"    return entries, exits, {{'direction': direction, {levels}}}\n"
    )


def _alias_env(tmp_path: Path, name: str, **kwargs):
    _write_shared_ohlcv(tmp_path, "VBA", _frame())
    _write_shared_ohlcv(tmp_path, "VBA", _intrabar_frame(), timeframe="5m")
    path = tmp_path / f"{name}.py"
    path.write_text(_alias_strategy_text(**kwargs))
    return tmp_path, str(path)


@pytest.fixture
def unusable_tp_env(tmp_path: Path):
    """A canonical take-profit no bar can reach, plus the usable alias price."""
    return _alias_env(
        tmp_path,
        "vbt_tp_unusable",
        canonical_price="float('inf')",
        alias_price="110.0",
        alias_first=True,
    )


class TestUnusableTakeProfitPrice:
    """A level price no bar can reach is not a level, in either engine."""

    def test_non_finite_take_profit_falls_back_to_the_usable_alias(
        self, unusable_tp_env
    ) -> None:
        tmp_path, strategy_path = unusable_tp_env

        result = VectorbtAdapter().run(_config(tmp_path, strategy_path), strategy_path)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exits, "the usable take-profit level must fill"
        assert trade.exits[0].price == pytest.approx(110.0)
        assert trade.meta["tp_price"] == pytest.approx(110.0)


def _config(tmp_path: Path, strategy_path: str) -> BacktestConfig:
    return BacktestConfig(
        **with_zero_fees(
            {
                "strategy_name": "vbt_tp_alias",
                "symbols": ["VBA"],
                "timeframe": "1h",
                "start_date": "2024-01-01",
                "end_date": "2024-01-02",
                "initial_capital": 100_000.0,
                # C1: engines size zero times without explicit sizing.
                "risk_per_trade_pct": 0.01,
                "modeling_resolution": "5m",
                "data_dir": str(tmp_path),
            }
        )
    )


@pytest.fixture
def alias_env(tmp_path: Path, request):
    """Both publication orders: the resolution must not depend on either."""
    alias_first = bool(getattr(request, "param", True))
    return _alias_env(
        tmp_path,
        "vbt_tp_alias",
        canonical_price="110.0",
        alias_price="105.0",
        alias_first=alias_first,
    )


@pytest.mark.parametrize("alias_env", [True, False], indirect=True)
class TestEndToEndAliasResolution:
    def test_aliased_level_exits_at_the_canonical_price(self, alias_env) -> None:
        """The exit fires at the canonical ``take_profit``, not the alias."""
        tmp_path, strategy_path = alias_env

        result = VectorbtAdapter().run(_config(tmp_path, strategy_path), strategy_path)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exits, "the take-profit level must fill"
        assert trade.exits[0].reason == "take_profit"
        assert trade.exits[0].price == pytest.approx(110.0)

    def test_aliased_level_records_the_canonical_price_in_meta(self, alias_env) -> None:
        """``trade.meta`` reports the level that fired, not the shadowed alias."""
        tmp_path, strategy_path = alias_env

        result = VectorbtAdapter().run(_config(tmp_path, strategy_path), strategy_path)

        assert len(result.trades) == 1
        assert result.trades[0].meta["tp_price"] == pytest.approx(110.0)
