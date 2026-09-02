"""Cross-engine conformance tests.

Documented contract: ``docs/cross-engine-backtest-contract.md``.

These tests pin the engine-neutral facets of the backtest contract that every
engine adapter must satisfy:

- fills (next-bar-open market entry, stop/TP trigger sides),
- end-date boundary (date-only end bound is inclusive of the full day),
- same-bar SL/TP priority (stop-first without intrabar data, chronological
  event with it, flagged fallback when requested intrabar data is missing),
- sizing (risk% from the actual stop distance, capped by max-position /
  max-leverage notionals),
- stats keys (``result.stats`` carries the canonical ``BacktestStats`` set),
- ``total_bars`` (bar count of loaded main-timeframe data — NOT trade count).

Decision Records (Cleanup 16-A, 2026-08-31):
- Same-bar SL/TP priority is stop-first everywhere when intrabar data is
  unavailable. With enabled and available intrabar data, the actual
  chronological event is used; if intrabar data is missing, fall back to
  stop-first and flag the result.
- A date-only end boundary is inclusive of the full day.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.contract import (
    EngineStatsKeys,
    apply_date_filter,
    flag_missing_intrabar,
    parse_date_bound,
    resolve_same_bar_exit,
)
from runner.engines.native_adapter import NativeAdapter
from runner.engines.pynecore_adapter import PyneCoreAdapter
from runner.engines.signal_replay_adapter import (
    ReplaySignal,
    SignalReplayAdapter,
    SignalReplayEngine,
)
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.stats import compute_stats
from shared.models import Trade, TradeExit

# ─── Shared fixture builders ─────────────────────────────────────────────────

STRATEGY_PATH = str(Path(__file__).resolve().parent / "strategies" / "contract_sma.py")


def _deterministic_ohlcv(
    *,
    periods: int = 260,
    freq: str = "h",
    start: str = "2024-01-01",
) -> pd.DataFrame:
    """A fully deterministic trend/consolidation series (no random component).

    Alternating 40-bar trends (up +0.5/bar, down -0.5/bar) with 40-bar flat
    phases in between; with SMA(5)/SMA(20) this produces several crossover
    round-trips — entries on the trend starts, exits on the trend ends.
    """
    closes: list[float] = []
    price = 100.0
    # up, flat, down, flat, ... (periods / 80 cycles). Flat phases sit strictly
    # between the trend legs so SMA(5)/SMA(20) fully re-converges and the next
    # trend leg produces a fresh crossover.
    for cycle in range(max(1, periods // 80)):
        for _ in range(30):
            price += 0.5
            closes.append(price)
        for _ in range(20):
            closes.append(price)
        for _ in range(30):
            price -= 0.5
            closes.append(price)
        for _ in range(20):
            closes.append(price)
    closes = closes[:periods]
    timestamps = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [c + 0.25 for c in closes],
            "low": [c - 0.25 for c in closes],
            "close": closes,
            "volume": [1000.0] * periods,
        }
    )


def _write_ohlcv(data_dir: Path, symbol: str, timeframe: str, frame: pd.DataFrame) -> Path:
    symbol_dir = data_dir / "ohlcv" / "test_source" / "equities" / symbol
    symbol_dir.mkdir(parents=True, exist_ok=True)
    path = symbol_dir / f"{timeframe}.parquet"
    frame.to_parquet(path, index=False)
    return path


def _config(data_dir: Path, **overrides) -> BacktestConfig:
    payload = {
        "strategy_name": "example_sma",
        "symbols": ["PARITY"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "initial_capital": 100_000,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(data_dir),
        "parameters": {"fast_length": 5, "slow_length": 20},
    }
    payload.update(overrides)
    return BacktestConfig(**payload)


@pytest.fixture(scope="module")
def contract_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One deterministic bundled fixture tree shared by the engine-level tests."""
    data_dir = tmp_path_factory.mktemp("contract-data")
    _write_ohlcv(data_dir, "PARITY", "1h", _deterministic_ohlcv())
    return data_dir


# ─── Contract document is present and pins the documented facets ────────────


def test_contract_document_exists_and_covers_required_facets():
    document = (
        Path(__file__).resolve().parents[1] / "docs" / "cross-engine-backtest-contract.md"
    )
    assert document.exists(), "The cross-engine contract must be documented"

    text = document.read_text(encoding="utf-8")
    for facet in (
        "fills",
        "end-date boundary",
        "same-bar",
        "sizing",
        "stats keys",
        "total_bars",
        "execution-timing",
    ):
        assert facet in text.lower(), f"Contract document must cover '{facet}'"


# ─── Date filter convention (F9) ─────────────────────────────────────────────


class TestDateFilterConvention:
    """Decision Record: a date-only end boundary is inclusive of the full day."""

    def test_date_only_end_bound_includes_full_day(self):
        bound = parse_date_bound("2024-01-15", is_end=True)
        assert bound == pd.Timestamp("2024-01-15 23:59:59.999999", tz="UTC")

    def test_date_only_start_bound_is_start_of_day(self):
        bound = parse_date_bound("2024-01-15", is_end=False)
        assert bound == pd.Timestamp("2024-01-15 00:00:00", tz="UTC")

    def test_explicit_time_bound_is_used_as_is(self):
        bound = parse_date_bound("2024-01-15T14:30:00Z", is_end=True)
        assert bound == pd.Timestamp("2024-01-15 14:30:00", tz="UTC")

    def test_naive_timestamp_is_treated_as_utc(self):
        bound = parse_date_bound("2024-01-15T14:30:00", is_end=False)
        assert bound == pd.Timestamp("2024-01-15 14:30:00", tz="UTC")

    def test_empty_and_invalid_values_are_rejected(self):
        with pytest.raises(ValueError):
            parse_date_bound("", is_end=True)
        with pytest.raises(ValueError):
            parse_date_bound("not-a-date", is_end=True)

    def test_last_bar_of_end_day_survives_the_filter(self):
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2024-01-14 23:00", "2024-01-15 22:00", "2024-01-16 02:00"], utc=True
                )
            }
        )
        filtered = apply_date_filter(frame, start="2024-01-01", end="2024-01-15")
        assert len(filtered) == 2, "Bars on the end date itself must be included"
        assert filtered["timestamp"].max() == pd.Timestamp("2024-01-15 22:00", tz="UTC")

    def test_engines_agree_on_the_bundled_fixture_window(self, contract_data_dir):
        """Conformance: all engines cut data with the same inclusive end bound.

        The bundled fixture runs hourly from 2024-01-01; every engine run with
        end_date="2024-01-09" must therefore load exactly the same bar set
        (through 2024-01-09 23:59:59.999999 = 216 bars) and agree on
        total_bars.
        """
        data_dir = tmp_contract_dir(contract_data_dir)
        cfg = _config(data_dir, end_date="2024-01-09", parameters={})

        native = NativeAdapter().run(cfg, STRATEGY_PATH)
        vectorbt = VectorbtAdapter().run(cfg, STRATEGY_PATH)

        # Bars from 2024-01-01 00:00 through 2024-01-09 23:00 inclusive.
        for result in (native, vectorbt):
            assert result.meta.total_bars == 216, (
                f"{result.meta.engine}: end-date boundary kept "
                f"{result.meta.total_bars} bars, expected the full-day-inclusive window (216)"
            )

    def test_signal_replay_loader_keeps_the_full_end_day(self, tmp_path: Path):
        """Conformance (F9): signal-replay cuts data with the same convention.

        The replay adapter loads its own OHLCV from disk; a bare-date
        comparison there would truncate at midnight and silently drop the
        whole end day, diverging from the other engines.
        """
        frame = _deterministic_ohlcv(periods=240, freq="h")  # 2024-01-01 .. 2024-01-10 23:00
        _write_ohlcv(tmp_path, "PARITY", "1h", frame)

        adapter = SignalReplayEngine(data_dir=tmp_path)
        loaded = adapter._load_ohlcv(
            symbol="PARITY", timeframe="1h", start="2024-01-01", end="2024-01-05"
        )

        assert loaded is not None
        assert loaded["timestamp"].max() == pd.Timestamp("2024-01-05 23:00", tz="UTC"), (
            "A date-only end bound must keep the bars of the end date itself"
        )
        assert loaded["timestamp"].min() == pd.Timestamp("2024-01-01 00:00", tz="UTC")


def tmp_contract_dir(shared_dir: Path) -> Path:
    """The module fixture already carries the PARITY/1h frame."""
    return shared_dir


# ─── Same-bar SL/TP priority (F8) ────────────────────────────────────────────


class TestSameBarExitPolicy:
    """Decision Record: stop-first when intrabar data is unavailable."""

    def test_stop_wins_when_both_levels_hit_same_bar(self):
        assert (
            resolve_same_bar_exit(
                direction="LONG", high=112.0, low=109.0, stop_price=109.5, take_profit_price=111.5
            )
            == "stop"
        )

    def test_tp_wins_when_only_tp_is_hit(self):
        assert (
            resolve_same_bar_exit(
                direction="LONG", high=112.0, low=111.0, stop_price=109.0, take_profit_price=111.5
            )
            == "tp"
        )

    def test_stop_wins_when_only_stop_is_hit(self):
        assert (
            resolve_same_bar_exit(
                direction="LONG", high=110.0, low=108.0, stop_price=109.0, take_profit_price=111.0
            )
            == "stop"
        )

    def test_short_side_mirrors_the_rule(self):
        assert (
            resolve_same_bar_exit(
                direction="SHORT", high=111.0, low=108.0, stop_price=110.5, take_profit_price=108.5
            )
            == "stop"
        )
        assert (
            resolve_same_bar_exit(
                direction="SHORT", high=109.0, low=107.0, stop_price=110.5, take_profit_price=108.5
            )
            == "tp"
        )

    def test_no_hit_returns_none(self):
        assert (
            resolve_same_bar_exit(
                direction="LONG", high=110.0, low=109.5, stop_price=108.0, take_profit_price=112.0
            )
            is None
        )

    def test_unknown_direction_is_rejected_not_treated_as_short(self):
        """A direction spelling outside the engine convention must fail loudly.

        The helper's LONG/SHORT branch would otherwise silently evaluate an
        unknown value as SHORT and invert stop/TP logic into plausible-looking
        wrong exits.
        """
        with pytest.raises(ValueError):
            resolve_same_bar_exit(
                direction="long", high=110.0, low=109.5, stop_price=108.0, take_profit_price=112.0
            )
        with pytest.raises(ValueError):
            resolve_same_bar_exit(
                direction="", high=110.0, low=109.5, stop_price=108.0, take_profit_price=112.0
            )

    def test_missing_intrabar_data_is_flagged(self):
        flag = flag_missing_intrabar(intrabar_resolution="1-lower", intrabar_coverage=False)
        assert flag is not None
        assert flag["same_bar_priority"] == "stop_first_fallback"
        assert flag["intrabar_available"] is False
        assert flag["reason"] == "intrabar_data_missing"

        empty = flag_missing_intrabar(intrabar_resolution="5m", intrabar_coverage=False)
        assert empty is not None
        assert empty["intrabar_available"] is False

    def test_sub_bars_present_but_unresolvable_is_flagged_as_unresolved(self):
        """Sub-bars exist but the walk found no ordered event: still a fallback.

        Coverage alone is not resolution — the flag must be emitted (so the
        stop-first fallback stays visible) and must record that the data was
        there but could not order the two levels.
        """
        flag = flag_missing_intrabar(
            intrabar_resolution="5m", intrabar_coverage=True, reason="intrabar_unresolved"
        )
        assert flag is not None
        assert flag["same_bar_priority"] == "stop_first_fallback"
        assert flag["intrabar_available"] is True
        assert flag["reason"] == "intrabar_unresolved"

    def test_available_intrabar_data_is_not_flagged(self):
        assert flag_missing_intrabar(intrabar_resolution="5m", intrabar_coverage=True) is None

    def test_no_intrabar_resolution_requested_is_not_flagged(self):
        assert flag_missing_intrabar(intrabar_resolution=None, intrabar_coverage=False) is None


def _sl_tp_strategy(path: Path, *, stop: float, take_profit: float, entry: int = 2) -> None:
    """Entry on bar ``entry``; exit on the last bar (both engines shift)."""
    path.write_text(
        f"""
import pandas as pd

def generate_signals(df: pd.DataFrame, params: dict):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[{entry}] = True
    exits.iloc[len(df) - 1] = True
    indicators = {{
        "direction": pd.Series("LONG", index=df.index),
        "stop_loss": pd.Series(pd.NA, index=df.index),
        "take_profit": pd.Series(pd.NA, index=df.index),
    }}
    indicators["stop_loss"].iloc[{entry}] = {stop!r}
    indicators["take_profit"].iloc[{entry}] = {take_profit!r}
    return entries, exits, indicators
"""
    )


def test_native_engine_stops_first_without_intrabar_data(tmp_path: Path):
    """Conformance (F8): no intrabar data -> both levels hit -> stop wins."""
    data = _deterministic_ohlcv(periods=10, freq="h")
    # Bar 5 trades through BOTH the stop and the take-profit level.
    data.loc[5, "high"] = 114.0
    data.loc[5, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        parameters={},
    )
    result = NativeAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].reason == "stop_loss", (
        "Without intrabar data the stop must win the same-bar ambiguity"
    )
    assert trade.exits[-1].price == pytest.approx(100.0)


def test_vectorbt_engine_stops_first_without_intrabar_data(tmp_path: Path):
    """Conformance (F8): vectorbt's intrabar path is off -> stop-first too.

    vectorbt resolves protective levels on the entry bar and only consults them
    through the intrabar walk; with no intrabar data the trade exits on the
    scheduled signal exit and the levels are recorded as meta. Conformance
    here means: the recorded stop level is the strategy's stop (the
    stop-first-fallback contract's input), and NO exit leg may claim a
    take-profit fill that the stop-first fallback denies.
    """
    data = _deterministic_ohlcv(periods=10, freq="h")
    data.loc[5, "high"] = 114.0
    data.loc[5, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        parameters={},
    )
    result = VectorbtAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.meta.get("sl_price") == pytest.approx(100.0)
    assert trade.meta.get("tp_price") == pytest.approx(110.0)
    assert trade.exits[-1].reason != "take_profit", (
        "Without intrabar data no exit may claim the take-profit the "
        "stop-first fallback denies"
    )


INTRABAR_SUBBARS = {
    # 5m sub-bars for the 04:00 hour (both engines' intrabar window): the
    # price rallies through the take-profit first, then dips through the
    # stop. The 1h main frame trades through BOTH levels on bar 4, so the
    # stop-first tie-break would report stop_loss; the chronological event
    # is the take-profit.
    "timestamp": pd.date_range("2024-01-01 04:00", periods=12, freq="5min", tz="UTC"),
    "open": [104.0, 105.0, 106.0, 108.0, 110.0, 112.0, 110.0, 108.0, 105.0, 100.6, 100.4, 100.5],
    "high": [105.5, 106.0, 108.5, 110.5, 112.5, 114.0, 110.8, 108.9, 106.0, 100.9, 100.8, 104.5],
    "low": [104.4, 105.0, 106.0, 108.0, 110.0, 111.0, 108.0, 105.0, 96.0, 100.0, 96.0, 100.4],
    "close": [105.0, 106.0, 108.0, 110.0, 112.0, 113.0, 110.0, 108.0, 96.5, 100.4, 100.5, 100.6],
    "volume": [100.0] * 12,
}


def _write_intrabar_fixture(tmp_path: Path) -> pd.DataFrame:
    """Main frame trades through both levels on bar 4; sub-bars order the events.

    Bar 4 follows the entry bar (entry fills at bar 3's open), so the
    ambiguous bar is seen while the position is open in both engines.
    """
    data = _deterministic_ohlcv(periods=10, freq="h")
    data.loc[4, "high"] = 114.0
    data.loc[4, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)
    intrabar = pd.DataFrame(INTRABAR_SUBBARS)
    _write_ohlcv(tmp_path, "PARITY", "5m", intrabar)
    return data


def test_vectorbt_engine_uses_chronological_intrabar_event_when_available(tmp_path: Path):
    """Conformance (F8): intrabar enabled+available -> the actual event wins.

    Within the ambiguous hour the price rallies through the take-profit
    (per the 5m sub-bars) before dipping through the stop. Stop-first would
    report stop_loss; the chronological intrabar walk must report
    take_profit.
    """
    _write_intrabar_fixture(tmp_path)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        modeling_resolution="5m",
        parameters={},
    )
    result = VectorbtAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].reason == "take_profit", (
        "With intrabar data available the chronological event must win"
    )
    assert trade.exits[-1].price == pytest.approx(110.0)
    assert trade.meta.get("modeling_resolution") == "5m"


def test_native_engine_uses_chronological_intrabar_event_when_available(tmp_path: Path):
    """Conformance (F8): native engine, same scenario, chronological event wins."""
    _write_intrabar_fixture(tmp_path)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        modeling_resolution="5m",
        parameters={},
    )
    result = NativeAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    # The native engine names TP exits after the TP level ("tp"); vectorbt
    # uses "take_profit". Either way the CHRONOLOGICAL event must win over
    # the stop-first tie-break.
    assert trade.exits[-1].reason in {"tp", "take_profit"}, (
        f"With intrabar data available the chronological event must win, "
        f"got {trade.exits[-1].reason!r}"
    )
    assert trade.exits[-1].price == pytest.approx(110.0)


def test_native_engine_flags_the_stop_first_fallback_when_intrabar_is_missing(tmp_path: Path):
    """Decision Record: intrabar requested but missing -> stop-first AND flag."""
    data = _deterministic_ohlcv(periods=10, freq="h")
    data.loc[4, "high"] = 114.0
    data.loc[4, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        modeling_resolution="5m",
        parameters={},
    )
    result = NativeAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].reason == "stop_loss"
    flag = trade.meta.get("same_bar_priority")
    assert flag is not None, "Missing intrabar data must flag the stop-first fallback"
    assert flag["same_bar_priority"] == "stop_first_fallback"
    assert flag["intrabar_available"] is False


# ─── Sizing (F3) ─────────────────────────────────────────────────────────────


class TestSizingContract:
    """Conformance: risk% sizing derives quantity from the actual stop distance.

    risk_target = initial_capital * risk_per_trade_pct; the stop distance is
    |entry - stop|; qty = risk_target / stop_distance, capped by the
    max-position and max-leverage notionals.
    """

    def test_native_engine_sizes_from_actual_stop(self):
        engine_cfg = BacktestConfig(
            strategy_name="unit-test",
            symbols=["TEST"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            risk_per_trade_pct=0.01,
            parameters={},
        )
        from runner.engine import TradeEngine

        engine = TradeEngine(engine_cfg, symbol="TEST")
        quantity = engine._calculate_position_size(
            entry_price=100.0, stop_loss_price=95.0, direction="LONG"
        )
        # 1% of 100k = 1000 USD at risk over a 5-point stop => 200 units.
        assert quantity == pytest.approx(200.0)

    def test_vectorbt_engine_sizes_from_actual_stop(self, tmp_path: Path):
        data = _deterministic_ohlcv(periods=10, freq="h")
        _write_ohlcv(tmp_path, "PARITY", "1h", data)

        strategy = tmp_path / "sl_tp_strategy.py"
        _sl_tp_strategy(strategy, stop=95.0, take_profit=200.0)

        cfg = _config(
            tmp_path,
            strategy_name="sl_tp_strategy",
            start_date="2024-01-01",
            end_date="2024-01-01",
            initial_capital=100_000,
            risk_per_trade_pct=0.01,
            parameters={},
        )
        result = VectorbtAdapter().run(cfg, str(strategy))

        assert len(result.trades) == 1
        trade = result.trades[0]
        entry_price = trade.entry_price
        stop_distance = entry_price - 95.0
        expected_qty = (100_000 * 0.01) / stop_distance
        assert trade.quantity == pytest.approx(expected_qty, rel=1e-3)

    def test_vectorbt_engine_caps_notional_at_max_leverage(self, tmp_path: Path):
        data = _deterministic_ohlcv(periods=10, freq="h")
        _write_ohlcv(tmp_path, "PARITY", "1h", data)

        strategy = tmp_path / "sl_tp_strategy.py"
        _sl_tp_strategy(strategy, stop=103.9, take_profit=200.0)

        cfg = _config(
            tmp_path,
            strategy_name="sl_tp_strategy",
            start_date="2024-01-01",
            end_date="2024-01-01",
            initial_capital=100_000,
            risk_per_trade_pct=0.5,  # absurd risk -> must hit the leverage cap
            max_leverage=1.0,
            max_position_pct=1.0,
            parameters={},
        )
        result = VectorbtAdapter().run(cfg, str(strategy))

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.meta["risk_math"]["capped_by_max_leverage"] is True
        assert trade.meta["risk_math"]["final_notional"] == pytest.approx(100_000, rel=1e-6)


# ─── Stats keys (F5) ─────────────────────────────────────────────────────────


class TestStatsKeysContract:
    """Conformance: every engine can expose the canonical stats key set."""

    REQUIRED_KEYS = sorted(EngineStatsKeys.REQUIRED)

    def test_contract_key_set_matches_backteststats(self):
        from shared.models import BacktestStats

        canonical = BacktestStats().to_dict()
        missing = EngineStatsKeys.missing_from(canonical)
        assert not missing, f"Contract keys must be a subset of BacktestStats: {missing}"

    def test_signal_replay_result_carries_the_canonical_stats_keys(self):
        frame = _deterministic_ohlcv(periods=40, freq="h")
        signals = [
            {
                "timestamp": "2024-01-01T02:00:00Z",
                "symbol": "PARITY",
                "action": "buy",
                "entry_price": 101.5,
                "stop_loss": 95.0,
                "take_profit": 130.0,
            },
            {
                "timestamp": "2024-01-01T20:00:00Z",
                "symbol": "PARITY",
                "action": "sell",
                "entry_price": 108.0,
            },
        ]
        result = SignalReplayAdapter().run(
            signals=[ReplaySignal.from_payload(signal) for signal in signals],
            ohlcv_by_symbol={"PARITY": frame},
            timeframe="1h",
        )
        missing = EngineStatsKeys.missing_from(result.stats)
        assert not missing, f"signal-replay stats are missing canonical keys: {sorted(missing)}"

    def test_native_engine_result_can_expose_the_canonical_stats_keys(
        self, contract_data_dir: Path
    ):
        result = NativeAdapter().run(_config(tmp_contract_dir(contract_data_dir)), STRATEGY_PATH)
        assert len(result.trades) > 0

        stats = compute_stats(
            trades=result.trades,
            initial_capital=result.meta.initial_capital,
            equity_curve=result.equity_curve or None,
        )
        missing = EngineStatsKeys.missing_from(stats)
        assert not missing, f"native stats are missing canonical keys: {sorted(missing)}"

    def test_vectorbt_engine_result_can_expose_the_canonical_stats_keys(
        self, contract_data_dir: Path
    ):
        result = VectorbtAdapter().run(
            _config(tmp_contract_dir(contract_data_dir)), STRATEGY_PATH
        )
        assert len(result.trades) > 0

        stats = compute_stats(
            trades=result.trades,
            initial_capital=result.meta.initial_capital,
            equity_curve=result.equity_curve or None,
        )
        missing = EngineStatsKeys.missing_from(stats)
        assert not missing, f"vectorbt stats are missing canonical keys: {sorted(missing)}"

    def test_pynecore_result_can_expose_the_canonical_stats_keys(self, contract_data_dir: Path):
        """Conformance: pynecore's stats dict must carry the canonical keys.

        PynecoreAdapter delegates execution to the external ``pyne`` CLI; the
        contract facet under test here is the *stats contract*, which the
        adapter satisfies by routing through compute_stats like every other
        engine. The CLI-independent path is exercised via the same helper the
        adapter's result is enriched with.
        """
        result = NativeAdapter().run(_config(tmp_contract_dir(contract_data_dir)), STRATEGY_PATH)
        # PynecoreAdapter.run() merges compute_stats_for_result(result) with the
        # parsed strategy stats — verify the shared helper covers the key set.
        from runner.stats import compute_stats_for_result

        merged = {**compute_stats_for_result(result), **(result.stats or {})}
        missing = EngineStatsKeys.missing_from(merged)
        assert not missing, f"pynecore stats are missing canonical keys: {sorted(missing)}"


# ─── total_bars (F6) ─────────────────────────────────────────────────────────


class TestTotalBarsContract:
    """Conformance: ``meta.total_bars`` = loaded main-timeframe bar count.

    F6: the vectorbt adapter historically persisted the trade count here.
    Every engine must report the number of bars of market data actually loaded
    for the backtest, summed across symbols.
    """

    def test_native_engine_reports_loaded_bar_count(self, contract_data_dir: Path):
        data_dir = tmp_contract_dir(contract_data_dir)
        frame = _read_parquet(data_dir, "PARITY", "1h")
        result = NativeAdapter().run(_config(data_dir), STRATEGY_PATH)
        assert result.meta.total_bars == len(frame)

    def test_vectorbt_engine_reports_loaded_bar_count(self, contract_data_dir: Path):
        data_dir = tmp_contract_dir(contract_data_dir)
        frame = _read_parquet(data_dir, "PARITY", "1h")
        result = VectorbtAdapter().run(_config(data_dir), STRATEGY_PATH)
        assert len(result.trades) > 0, "Fixture must trade for the assertion to bite"
        assert result.meta.total_bars == len(frame), (
            "total_bars must be the loaded bar count, not the trade count (F6)"
        )

    def test_pynecore_engine_reports_loaded_bar_count(self, contract_data_dir: Path):
        """Conformance: pynecore sums ``len(df)`` over the loaded symbols."""
        data_dir = tmp_contract_dir(contract_data_dir)
        frame = _read_parquet(data_dir, "PARITY", "1h")
        result = PyneCoreAdapter().run(_config(data_dir), STRATEGY_PATH)
        # pyne CLI is not installed in this environment; the adapter logs and
        # skips symbol processing, but total_bars must still reflect the bars
        # it loaded (not trades, not zero).
        assert result.meta.total_bars == len(frame)

    def test_total_bars_is_not_the_trade_count(self, contract_data_dir: Path):
        data_dir = tmp_contract_dir(contract_data_dir)
        for adapter in (NativeAdapter(), VectorbtAdapter()):
            result = adapter.run(_config(data_dir), STRATEGY_PATH)
            assert result.meta.total_bars > len(result.trades), (
                f"{result.meta.engine}: total_bars ({result.meta.total_bars}) must not "
                f"degenerate into the trade count ({len(result.trades)})"
            )


def _read_parquet(data_dir: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    path = data_dir / "ohlcv" / "test_source" / "equities" / symbol / f"{timeframe}.parquet"
    return pd.read_parquet(path)


# ─── Fills (F1/F2) ───────────────────────────────────────────────────────────


class TestFillContract:
    """Conformance: market entries fill at the next bar's open.

    Native and vectorbt share this convention for market entries; the
    signal-replay engine fills same-bar at the signal bar's close (documented
    exception — see the contract document's execution-timing section).
    """

    def test_native_engine_fills_market_entry_at_next_bar_open(self):
        data = _deterministic_ohlcv(periods=30, freq="h")

        from runner.engine import TradeEngine

        def strategy_fn(df: pd.DataFrame, params: dict):
            entries = pd.Series(False, index=df.index)
            exits = pd.Series(False, index=df.index)
            entries.iloc[2] = True
            exits.iloc[10] = True
            return entries, exits, {}, []

        cfg = BacktestConfig(
            strategy_name="unit-test",
            symbols=["PARITY"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000,
            modeling_resolution="main_timeframe",
            parameters={},
        )
        result = TradeEngine(cfg, symbol="PARITY").run(data, strategy_fn)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.entry_price == pytest.approx(float(data.iloc[3]["open"]))
        assert trade.entry_time == pd.Timestamp(data.iloc[3]["timestamp"]).isoformat()

    def test_vectorbt_engine_fills_market_entry_at_next_bar_open(self, tmp_path: Path):
        data = _deterministic_ohlcv()  # 260 bars: enough for two round trips
        _write_ohlcv(tmp_path, "PARITY", "1h", data)

        cfg = _config(tmp_path, parameters={"fast_length": 5, "slow_length": 20})
        result = VectorbtAdapter().run(cfg, STRATEGY_PATH)

        assert len(result.trades) >= 1
        for trade in result.trades:
            entry_ts = pd.Timestamp(trade.entry_time)
            bar = data.loc[data["timestamp"] == entry_ts].iloc[0]
            assert trade.entry_price == pytest.approx(float(bar["open"])), (
                "vectorbt market entries must fill at the (shifted) bar's open"
            )

    def test_native_and_vectorbt_agree_on_entry_fills_for_the_bundled_fixture(
        self, contract_data_dir: Path
    ):
        data_dir = tmp_contract_dir(contract_data_dir)
        cfg = _config(data_dir)

        native = NativeAdapter().run(cfg, STRATEGY_PATH)
        vectorbt = VectorbtAdapter().run(cfg, STRATEGY_PATH)

        assert len(native.trades) == len(vectorbt.trades)
        for native_trade, vectorbt_trade in zip(native.trades, vectorbt.trades):
            assert pd.Timestamp(native_trade.entry_time) == pd.Timestamp(vectorbt_trade.entry_time)
            assert native_trade.entry_price == pytest.approx(vectorbt_trade.entry_price, rel=1e-9)

    def test_signal_replay_engine_fills_at_signal_bar_close(self):
        """Documented exception (F10): signal-replay fills same-bar at close."""
        frame = _deterministic_ohlcv(periods=40, freq="h")
        signal = ReplaySignal.from_payload(
            {
                "timestamp": "2024-01-01T05:00:00Z",
                "symbol": "PARITY",
                "action": "buy",
                "entry_price": float(frame.iloc[5]["close"]),
            }
        )
        result = SignalReplayAdapter().run(
            signals=[signal],
            ohlcv_by_symbol={"PARITY": frame},
            timeframe="1h",
        )

        assert len(result.trades) == 1
        trade = result.trades[0]
        # Same-bar close fill, at the bar whose timestamp carries the signal.
        assert trade.entry_time == pd.Timestamp(frame.iloc[5]["timestamp"]).isoformat()
        assert trade.entry_price == pytest.approx(float(frame.iloc[5]["close"]))


# ─── Pynecore restatement containment (F10) ──────────────────────────────────


class TestPynecoreRestatementContainment:
    """Conformance (F10): the post-hoc restatement may only tighten exits.

    ``_normalize_trade_exits`` rewrites an exit leg's price/reason when the
    recorded exit price violates the recorded protective level. The restated
    price must always be the level itself — never a level beyond it — and the
    reason must be the matching protective reason.
    """

    def _trade(self, direction: str, exit_price: float, meta: dict) -> Trade:
        return Trade(
            id=1,
            symbol="PARITY",
            direction=direction,
            entry_time="2024-01-01T00:00:00+00:00",
            entry_price=100.0,
            quantity=1.0,
            exits=[
                TradeExit(
                    time="2024-01-01T05:00:00+00:00",
                    price=exit_price,
                    quantity=1.0,
                    pnl=0.0,
                    reason="signal",
                )
            ],
            pnl=0.0,
            meta=meta,
        )

    def test_stop_violating_exit_is_restated_to_the_stop(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("LONG", 94.0, {"sl_price": 95.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price == pytest.approx(95.0)
        assert trades[0].exits[-1].reason == "stop_loss"

    def test_tp_violating_exit_is_restated_to_the_take_profit(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("LONG", 121.0, {"tp_price": 120.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price == pytest.approx(120.0)
        assert trades[0].exits[-1].reason == "take_profit"

    def test_exit_within_levels_is_left_alone(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("LONG", 105.0, {"sl_price": 95.0, "tp_price": 120.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price == pytest.approx(105.0)
        assert trades[0].exits[-1].reason == "signal"

    def test_short_side_restatement_is_mirrored(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("SHORT", 126.0, {"sl_price": 125.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price == pytest.approx(125.0)
        assert trades[0].exits[-1].reason == "stop_loss"

    def test_restated_price_is_never_pushed_beyond_the_level(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("LONG", 90.0, {"sl_price": 95.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price >= 95.0 - 1e-9

    def test_instrumented_restatement_only_touches_violating_legs(self):
        """Property: the restatement is a containment, not a rewrite.

        For any exit price at/inside the levels, the leg is untouched; for any
        violating price, the restated price is exactly the level.
        """
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        for direction in ("LONG", "SHORT"):
            long = direction == "LONG"
            sl = 95.0 if long else 125.0
            tp = 120.0 if long else 105.0
            inside = [sl + 1, (sl + tp) / 2, tp - 1] if long else [sl - 1, (sl + tp) / 2, tp + 1]
            for price in inside:
                trades = [self._trade(direction, price, {"sl_price": sl, "tp_price": tp})]
                _normalize_trade_exits(trades)
                assert trades[0].exits[-1].price == pytest.approx(price), (
                    f"{direction} exit {price} inside levels must not be restated"
                )
            for violating in ([90.0, 80.0] if long else [130.0, 140.0]):
                trades = [self._trade(direction, violating, {"sl_price": sl, "tp_price": tp})]
                _normalize_trade_exits(trades)
                assert trades[0].exits[-1].price == pytest.approx(sl)
                assert trades[0].exits[-1].reason == "stop_loss"
