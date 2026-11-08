"""Universe provenance and survivorship-bias evidence (backlog item 70).

A backtest over a symbol list that was resolved at run time says nothing about
whether that list is point-in-time. These tests pin the three things a reader
needs before trusting such a result:

* the artifact records who determined the universe and when, and never
  promotes "undeclared" into "point-in-time";
* the survivorship diagnostic reports what the stored data supports and marks
  everything else unknown — in particular the magnitude, which no computation
  over the present universe can supply;
* the optimizer carries both onto its own artifact and reports per-sub-period
  parameter stability.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import runner.cli as cli
from runner.universe_provenance import (
    MEMBERSHIP_CURRENT,
    MEMBERSHIP_POINT_IN_TIME,
    MEMBERSHIP_UNDECLARED,
    PLATFORM_MEMBERSHIP_SOURCES,
    UNIVERSE_PROVENANCE_KEY,
    SymbolCoverage,
    observe_symbol_coverage,
    read_universe_provenance,
    survivorship_bias_diagnostic,
    universe_provenance,
    unknown_universe_provenance,
)

# ─── universe_provenance ─────────────────────────────────────────────────────


def test_provenance_records_who_determined_the_universe_and_when():
    provenance = universe_provenance(
        symbols=["aapl", "MSFT", "AAPL"],
        provider="sp500-constituents.csv",
        as_of="2026-09-01",
        membership=MEMBERSHIP_CURRENT,
        resolved_at="2026-09-27T10:00:00+00:00",
        window_start="2015-01-01",
        window_end="2026-09-01",
    )

    assert provenance["provider"] == "sp500-constituents.csv"
    assert provenance["as_of"] == "2026-09-01"
    assert provenance["as_of_declared"] is True
    assert provenance["resolved_at"] == "2026-09-27T10:00:00+00:00"
    # AAPL appears once: the count must describe the universe that was traded,
    # not the raw argument.
    assert provenance["symbol_count"] == 2
    assert provenance["membership_basis"] == MEMBERSHIP_CURRENT
    assert provenance["is_point_in_time"] is False
    assert provenance["window"] == {
        "start": pd.Timestamp("2015-01-01", tz="UTC").isoformat(),
        "end": pd.Timestamp("2026-09-01", tz="UTC").isoformat(),
    }
    # The count cannot be read apart from the list it describes: the digest is
    # order-independent but changes with membership.
    assert len(provenance["symbols_sha256"]) == 64
    assert universe_provenance(
        symbols=["MSFT", "AAPL"], membership=MEMBERSHIP_CURRENT
    )["symbols_sha256"] == provenance["symbols_sha256"]
    assert universe_provenance(
        symbols=["AAPL", "MSFT", "IBM"], membership=MEMBERSHIP_CURRENT
    )["symbols_sha256"] != provenance["symbols_sha256"]


def test_a_run_with_no_declared_membership_is_never_recorded_as_point_in_time():
    """The default must stay unknown. Defaulting to the reassuring answer is
    exactly the failure this whole record exists to prevent."""
    provenance = universe_provenance(symbols=["AAPL", "MSFT"])

    assert provenance["membership_basis"] == MEMBERSHIP_UNDECLARED
    assert provenance["is_point_in_time"] is None
    assert provenance["as_of"] is None
    assert provenance["as_of_declared"] is False
    assert "membership_basis" in provenance["undefined_metrics"]
    assert "as_of" in provenance["undefined_metrics"]
    assert provenance["provider"] == "cli:--symbols"


def test_the_disclosure_says_out_loud_that_todays_list_is_not_point_in_time():
    current = universe_provenance(
        symbols=["AAPL"],
        provider="sp500-constituents",
        as_of="2026-09-01",
        membership=MEMBERSHIP_CURRENT,
    )
    undeclared = universe_provenance(symbols=["AAPL"], resolved_at="2026-09-27T00:00:00+00:00")

    assert "absent from the entire backtest window" in current["disclosure"]
    assert "biased upward" in current["disclosure"]
    assert "not computable" in current["disclosure"]
    assert "2026-09-01" in current["disclosure"]

    # "Nobody said" is a different fact from "today's list", and the reader has
    # to be able to tell them apart.
    assert "makes no statement about when its membership was determined" in undeclared["disclosure"]
    assert "unknown, and is recorded as unknown" in undeclared["disclosure"]


def test_a_point_in_time_claim_records_its_own_residual_caveat():
    provenance = universe_provenance(
        symbols=["AAPL"],
        as_of="2024-01-01",
        membership=MEMBERSHIP_POINT_IN_TIME,
        point_in_time_source="vendor-pit-2024-01-01",
        window_start="2024-01-01",
        window_end="2024-12-31",
    )

    assert provenance["is_point_in_time"] is True
    assert provenance["point_in_time_source"] == "vendor-pit-2024-01-01"
    # A list fixed at one date is not daily membership, and says so.
    assert "Membership changes inside the window are NOT modelled" in provenance["disclosure"]
    assert "daily_membership" in provenance["undefined_metrics"]


def test_point_in_time_without_a_date_or_after_the_window_fails_closed():
    """Both are unfalsifiable or look-ahead claims, and a look-ahead universe is
    a worse failure than a biased one because it is silent."""
    with pytest.raises(ValueError, match="requires an as-of date"):
        universe_provenance(symbols=["AAPL"], membership=MEMBERSHIP_POINT_IN_TIME)

    with pytest.raises(ValueError, match="look-ahead"):
        universe_provenance(
            symbols=["AAPL"],
            as_of="2025-01-01",
            membership=MEMBERSHIP_POINT_IN_TIME,
            point_in_time_source="vendor",
            window_start="2024-01-01",
            window_end="2024-12-31",
        )


def test_provenance_rejects_a_source_the_declared_basis_cannot_support():
    with pytest.raises(ValueError, match="declare membership='point_in_time'"):
        universe_provenance(
            symbols=["AAPL"],
            as_of="2024-06-01",
            membership=MEMBERSHIP_CURRENT,
            point_in_time_source="vendor-pit",
        )

    with pytest.raises(ValueError, match="membership must be one of"):
        universe_provenance(symbols=["AAPL"], membership="best_guess")

    with pytest.raises(ValueError, match="not a readable date"):
        universe_provenance(symbols=["AAPL"], as_of="last tuesday")


def test_the_platform_ships_no_point_in_time_membership_source():
    """Not a test of behaviour but of a documented fact the disclosure rests
    on: nothing in data_tools can enumerate membership as of a past date, which
    is why a point-in-time claim can only ever be declared, never re-derived."""
    assert PLATFORM_MEMBERSHIP_SOURCES == ()
    provenance = universe_provenance(symbols=["AAPL"])
    assert provenance["platform_membership_sources"] == []


def test_provenance_round_trips_through_a_persisted_result_meta():
    from shared.models import BacktestMeta

    meta = BacktestMeta(id="bt", created_at="2026-01-01T00:00:00+00:00", engine="vectorbt")
    assert read_universe_provenance(meta) is None, "an artifact without a record says so"

    meta.data[UNIVERSE_PROVENANCE_KEY] = universe_provenance(symbols=["AAPL"])
    assert read_universe_provenance(meta)["symbol_count"] == 1


def test_unknown_provenance_names_itself_as_unrecorded():
    unknown = unknown_universe_provenance("base artifact is legacy")

    assert unknown["membership_basis"] == MEMBERSHIP_UNDECLARED
    assert unknown["is_point_in_time"] is None
    assert unknown["symbol_count"] is None
    assert "symbol_count" in unknown["undefined_metrics"]
    assert "legacy" in unknown["disclosure"]
    # Distinguishable from a real record: a real one has a version and a count.
    recorded = universe_provenance(symbols=["AAPL"])
    assert unknown["version"] is None
    assert recorded["version"] is not None


# ─── observe_symbol_coverage ─────────────────────────────────────────────────


def _write_bars(data_dir: Path, symbol: str, timestamps: list[str], *, timeframe: str = "1d") -> Path:
    path = data_dir / "ohlcv" / "test" / symbol / f"{timeframe}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0] * len(timestamps),
            "high": [101.0] * len(timestamps),
            "low": [99.0] * len(timestamps),
            "close": [100.5] * len(timestamps),
            "volume": [1000.0] * len(timestamps),
        }
    ).to_parquet(path, index=False)
    return path


def test_coverage_is_read_from_the_stored_bars(tmp_path: Path):
    _write_bars(tmp_path, "AAPL", ["2024-01-01T00:00:00+00:00", "2024-06-01T00:00:00+00:00"])
    _write_bars(tmp_path, "IPO", ["2024-05-01T00:00:00+00:00", "2024-06-01T00:00:00+00:00"])

    observed = observe_symbol_coverage(
        ["AAPL", "IPO", "GONE"], timeframe="1d", data_dir=tmp_path, data_source="test"
    )

    assert observed["AAPL"].first_bar == pd.Timestamp("2024-01-01", tz="UTC")
    assert observed["AAPL"].last_bar == pd.Timestamp("2024-06-01", tz="UTC")
    assert observed["IPO"].first_bar == pd.Timestamp("2024-05-01", tz="UTC")
    # No frame at all is "unknown", which is a different claim from "listed
    # later" and must never be rendered as the latter.
    assert observed["GONE"] is None
    assert observed["AAPL"].observed is True


# ─── survivorship_bias_diagnostic ────────────────────────────────────────────


def _coverage(symbol: str, first: str | None, last: str | None) -> SymbolCoverage:
    return SymbolCoverage(
        symbol=symbol,
        first_bar=None if first is None else pd.Timestamp(first),
        last_bar=None if last is None else pd.Timestamp(last),
    )


def test_survivorship_counts_what_the_data_shows_and_calls_the_rest_unknown():
    """The fixture separates the two cases a bare count would conflate: a name
    that joined the universe years in, and a name whose download simply starts
    four days late. Only the lag makes them distinguishable, so it is reported.
    """
    coverage = {
        "FULL": _coverage("FULL", "2015-01-01T00:00:00Z", "2024-06-01T00:00:00Z"),
        "IPO": _coverage("IPO", "2024-03-01T00:00:00Z", "2024-06-01T00:00:00Z"),
        "LATE_DOWNLOAD": _coverage(
            "LATE_DOWNLOAD", "2015-01-05T00:00:00Z", "2024-06-01T00:00:00Z"
        ),
        "STOPPED": _coverage("STOPPED", "2015-01-01T00:00:00Z", "2024-02-01T00:00:00Z"),
        "UNKNOWN": None,
    }
    provenance = universe_provenance(
        symbols=list(coverage),
        as_of="2024-06-01",
        membership=MEMBERSHIP_CURRENT,
        window_start="2015-01-01",
        window_end="2024-06-01",
    )

    report = survivorship_bias_diagnostic(
        symbols=list(coverage),
        coverage=coverage,
        window_start="2015-01-01",
        window_end="2024-06-01",
        provenance=provenance,
    )

    assert report["n_symbols"] == 5
    assert report["n_symbols_observed"] == 4
    assert report["n_symbols_unobserved"] == 1
    assert report["unobserved_symbols"] == ["UNKNOWN"]
    assert report["n_no_data_at_window_start"] == 2
    assert report["n_started_mid_window"] == 2
    assert report["started_mid_window_symbols"] == ["IPO", "LATE_DOWNLOAD"]
    assert report["n_no_data_anywhere_in_window"] == 0
    assert report["n_symbols_with_data_ending_before_window_end"] == 1
    assert report["symbols_with_data_ending_before_window_end"] == ["STOPPED"]
    assert report["n_with_data_at_window_start"] == 2
    assert report["fraction_no_data_at_window_start"] == pytest.approx(0.5)
    lag = {entry["symbol"]: entry["lag_days"] for entry in report["no_data_at_window_start"]}
    assert lag == {"IPO": pytest.approx(3347.0, abs=1.0), "LATE_DOWNLOAD": pytest.approx(4.0)}
    assert report["max_first_bar_lag_days"] == pytest.approx(3347.0, abs=1.0)
    # The coverage fraction is a proxy for membership drift, not a measure of it.
    assert "Neither is an estimate of the bias" in report["caveat"]


def test_a_symbol_with_no_bar_in_the_window_at_all_is_reported_separately():
    coverage = {
        "GHOST": _coverage("GHOST", "2030-01-01T00:00:00Z", "2030-06-01T00:00:00Z"),
    }

    report = survivorship_bias_diagnostic(
        symbols=["GHOST"],
        coverage=coverage,
        window_start="2024-01-01",
        window_end="2024-06-01",
    )

    assert report["n_no_data_at_window_start"] == 1
    assert report["n_started_mid_window"] == 0
    assert report["n_no_data_anywhere_in_window"] == 1
    assert report["no_data_anywhere_in_window_symbols"] == ["GHOST"]


def test_the_bias_magnitude_is_never_invented():
    """The symbols carrying the bias are the ones missing from the universe,
    so nothing computed over the universe can bound it."""
    coverage = {"AAPL": _coverage("AAPL", "2015-01-01T00:00:00Z", "2024-06-01T00:00:00Z")}

    report = survivorship_bias_diagnostic(
        symbols=["AAPL"],
        coverage=coverage,
        window_start="2015-01-01",
        window_end="2024-06-01",
        provenance=universe_provenance(
            symbols=["AAPL"], as_of="2024-06-01", membership=MEMBERSHIP_CURRENT
        ),
    )

    assert report["bias_magnitude"] is None
    assert report["bias_magnitude_status"] == "not_computable"
    assert "survivorship_bias_magnitude" in report["undefined_metrics"]
    assert "in_window_membership" in report["undefined_metrics"]
    assert report["bias_direction"] == "upward"


def test_bias_direction_follows_the_declared_membership_basis():
    coverage = {"AAPL": _coverage("AAPL", "2015-01-01T00:00:00Z", "2024-06-01T00:00:00Z")}
    window = {"window_start": "2015-01-01", "window_end": "2024-06-01", "coverage": coverage}

    current = survivorship_bias_diagnostic(
        symbols=["AAPL"], provenance=universe_provenance(symbols=["AAPL"], membership=MEMBERSHIP_CURRENT), **window
    )
    point_in_time = survivorship_bias_diagnostic(
        symbols=["AAPL"],
        provenance=universe_provenance(
            symbols=["AAPL"],
            as_of="2015-01-01",
            membership=MEMBERSHIP_POINT_IN_TIME,
            point_in_time_source="vendor",
        ),
        **window,
    )
    undeclared = survivorship_bias_diagnostic(symbols=["AAPL"], **window)

    assert current["bias_direction"] == "upward"
    assert point_in_time["bias_direction"] == "none_expected_from_membership"
    # Unknown membership means unknown direction — not the reassuring default.
    assert undeclared["bias_direction"] == "unknown"
    assert "membership_basis" in undeclared["undefined_metrics"]
    assert "the direction of the bias is unknown" in undeclared["statement"]


def test_an_unreadable_symbol_is_excluded_from_the_counts_and_says_so():
    coverage = {
        "AAPL": _coverage("AAPL", "2015-01-01T00:00:00Z", "2024-06-01T00:00:00Z"),
        "MYSTERY": None,
    }

    report = survivorship_bias_diagnostic(
        symbols=["AAPL", "MYSTERY"],
        coverage=coverage,
        window_start="2015-01-01",
        window_end="2024-06-01",
    )

    # The fraction describes the symbols that could be read, not the whole list.
    assert report["fraction_no_data_at_window_start"] == pytest.approx(0.0)
    assert report["n_symbols_observed"] == 1
    # An unreadable symbol is not evidence of anything, in either direction.
    assert report["n_with_data_at_window_start"] == 1
    assert report["n_no_data_at_window_start"] == 0
    assert report["n_started_mid_window"] == 0
    assert report["n_no_data_anywhere_in_window"] == 0
    assert "coverage_for_some_symbols" in report["undefined_metrics"]


def test_a_missing_window_reports_nothing_rather_than_counting_everything():
    report = survivorship_bias_diagnostic(
        symbols=["AAPL"],
        coverage={"AAPL": _coverage("AAPL", "2015-01-01T00:00:00Z", "2024-06-01T00:00:00Z")},
        window_start="",
        window_end="",
    )

    assert report["n_symbols_observed"] == 0
    assert report["n_no_data_at_window_start"] == 0
    assert report["fraction_no_data_at_window_start"] is None
    assert report["window"] == {"start": None, "end": None}


def test_the_diagnostic_states_both_what_it_sees_and_what_it_cannot():
    report = survivorship_bias_diagnostic(
        symbols=["AAPL"],
        coverage={"AAPL": _coverage("AAPL", "2015-01-01T00:00:00Z", "2024-06-01T00:00:00Z")},
        window_start="2015-01-01",
        window_end="2024-06-01",
    )

    assert any("no data covering the start" in item for item in report["detectable"])
    assert any("absent from this universe" in item for item in report["not_detectable"])
    assert any("listing and delisting dates" in item for item in report["not_detectable"])


# ─── CLI wiring: the backtest artifact records its own provenance ────────────


class _CaptureEngine:
    name = "vectorbt"

    def __init__(self) -> None:
        self.captured: dict[str, object] = {}

    def validate_config(self, config) -> list[str]:
        return []

    def run(self, config, strategy, progress_callback=None):
        from shared.models import BacktestMeta, BacktestResult

        self.captured["meta_data"] = None
        result = BacktestResult(
            meta=BacktestMeta(
                id="bt",
                created_at="2026-09-27T00:00:00+00:00",
                engine=self.name,
                strategies=["demo"],
                backtest_start=config.start_date,
                backtest_end=config.end_date,
                initial_capital=float(config.initial_capital),
                data={},
            ),
            trades=[],
        )
        self.captured["meta_data"] = result.meta.data
        return result


def _run_cli(monkeypatch, tmp_path: Path, *extra: str) -> dict:
    import runner.cli as runner_cli

    engine = _CaptureEngine()
    monkeypatch.setattr(runner_cli, "get_engine", lambda _: engine)
    monkeypatch.setattr(runner_cli, "ensure_backtest_data", lambda **_kwargs: [])
    monkeypatch.setattr(runner_cli, "save_result_to_sqlite", lambda _result: None)
    monkeypatch.setattr(runner_cli, "attach_daily_equity", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner_cli, "merge_canonical_stats", lambda result: setattr(result, "stats", {})
    )

    args = runner_cli.build_parser().parse_args(
        [
            "run",
            "--engine",
            "vectorbt",
            "--strategy",
            "strategies/demo.py",
            "--symbols",
            "AAPL",
            "MSFT",
            "--risk-pct",
            "0.01",
            "--start",
            "2024-01-01",
            "--end",
            "2024-06-01",
            "--data-dir",
            str(tmp_path),
            "--output",
            str(tmp_path / "out.trades.json"),
            "--skip-sqlite",
            *extra,
        ]
    )
    runner_cli.cmd_run(args)
    data = engine.captured["meta_data"]
    assert isinstance(data, dict)
    return data


def test_every_run_records_a_universe_provenance_record(monkeypatch, tmp_path: Path):
    data = _run_cli(monkeypatch, tmp_path)

    provenance = data[UNIVERSE_PROVENANCE_KEY]
    assert provenance["symbol_count"] == 2
    assert provenance["provider"] == "cli:--symbols"
    assert provenance["membership_basis"] == MEMBERSHIP_UNDECLARED
    assert provenance["is_point_in_time"] is None
    assert provenance["resolved_at"], "the run must say when it resolved the list"
    assert provenance["window"]["start"].startswith("2024-01-01")
    # It is a sibling of execution_context, so the exact execution_context
    # contract the optimizer relies on is untouched.
    assert set(data["execution_context"]) == {
        "engine",
        "data_dir",
        "data_source",
        "symbols",
        "timeframe",
        "initial_capital",
        "risk_config",
    }
    # And it must survive to disk: a record that only existed in memory would
    # not reach the reader of the artifact.
    persisted = json.loads(
        (tmp_path / "out.trades.json").read_text(encoding="utf-8")
    )["meta"]["data"]
    assert persisted[UNIVERSE_PROVENANCE_KEY]["symbol_count"] == 2
    assert persisted[UNIVERSE_PROVENANCE_KEY]["membership_basis"] == MEMBERSHIP_UNDECLARED


def test_a_declared_current_membership_is_recorded_as_such(monkeypatch, tmp_path: Path):
    data = _run_cli(
        monkeypatch,
        tmp_path,
        "--universe-source",
        "sp500-constituents.csv",
        "--universe-as-of",
        "2024-06-01",
        "--universe-membership",
        "current",
    )

    provenance = data[UNIVERSE_PROVENANCE_KEY]
    assert provenance["provider"] == "sp500-constituents.csv"
    assert provenance["as_of"] == "2024-06-01"
    assert provenance["membership_basis"] == MEMBERSHIP_CURRENT
    assert provenance["is_point_in_time"] is False
    assert provenance["disclosure"].startswith("The universe is the membership as of 2024-06-01")


def test_a_lookahead_point_in_time_claim_stops_the_run(monkeypatch, tmp_path: Path):
    with pytest.raises(ValueError, match="look-ahead"):
        _run_cli(
            monkeypatch,
            tmp_path,
            "--universe-membership",
            "point_in_time",
            "--universe-as-of",
            "2026-01-01",
            "--universe-source-name",
            "vendor",
        )


# ─── optimizer wiring ────────────────────────────────────────────────────────


def _base_result(path: Path, *, symbols: list[str], data_dir: Path, data: dict | None = None) -> None:
    from shared.models import BacktestMeta, BacktestResult

    meta_data = {
        "symbols": list(symbols),
        "timeframes": ["1d"],
        "execution_context": {
            "engine": "vectorbt",
            "data_dir": str(data_dir),
            "data_source": "test",
            "symbols": list(symbols),
            "timeframe": "1d",
            "initial_capital": 100000.0,
            "risk_config": {"risk_per_trade_pct": 0.01},
        },
        "data_dir": str(data_dir),
        **(data or {}),
    }
    result = BacktestResult(
        meta=BacktestMeta(
            id="base-demo",
            created_at="2026-09-27T00:00:00+00:00",
            engine="vectorbt",
            strategies=["example_sma"],
            backtest_start="2024-01-01",
            backtest_end="2024-06-01",
            initial_capital=100000.0,
            risk_per_trade_pct=0.01,
            data=meta_data,
            parameters={},
        ),
        trades=[],
    )
    result.save(path / "base-demo.trades.json")


def _patch_optimizer(monkeypatch, tmp_path: Path) -> Path:
    import runner.optimizer as opt

    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(opt, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(opt, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
    strategy_path = tmp_path / "strategies" / "example_sma.py"
    strategy_path.parent.mkdir(parents=True, exist_ok=True)
    strategy_path.write_text('"""demo"""\n', encoding="utf-8")
    monkeypatch.setattr(
        opt, "find_strategy_entry", lambda _: SimpleNamespace(file_path=str(strategy_path), slug="example_sma")
    )
    monkeypatch.setattr(opt, "ensure_backtest_data", lambda **kwargs: [])
    monkeypatch.setattr(opt, "has_sufficient_segment_coverage", lambda **kwargs: True)
    return backtests_dir


def _trial(config, *, metric: float, exits: list[tuple[str, float]] | None = None) -> "object":
    """A stub trial result whose trades realize at the given (time, pnl) points."""
    from shared.models import BacktestResult, Trade, TradeExit

    trades = [
        Trade(
            id=index + 1,
            symbol=config.symbols[0],
            direction="LONG",
            entry_time=config.start_date,
            entry_price=100.0,
            quantity=1.0,
            exits=[TradeExit(time=when, price=100.0 + pnl / 100.0, quantity=1.0, pnl=pnl)],
            pnl=pnl,
        )
        for index, (when, pnl) in enumerate(exits or [])
    ]
    return BacktestResult(
        meta=SimpleNamespace(
            initial_capital=config.initial_capital,
            start_date=config.start_date,
            end_date=config.end_date,
            strategy_name=config.strategy_name,
            symbols=config.symbols,
            timeframe=config.timeframe,
            parameters=config.parameters,
        ),
        trades=trades,
        equity_curve=[],
        stats={
            "sharpe_ratio": metric,
            "total_pnl": sum(pnl for _when, pnl in (exits or [])),
            "win_rate": 1.0 if trades else 0.0,
            "max_drawdown_pct": -0.1,
        },
    )


def _train_subperiods() -> list[dict[str, str]]:
    """The sub-periods the optimizer will derive, computed the same way.

    Uses the optimizer's own split and the public fold machinery, so the fake
    backtest can place its trades inside the sub-periods the diagnostic will
    then measure — without hard-coding dates that would silently rot.
    """
    import runner.optimizer as opt
    from runner.optimization_diagnostics import subperiods_from_walk_forward, walk_forward_splits

    train_period, _test_period = opt._split_backtest_period("2024-01-01", "2024-06-01", 0.7)
    folds = walk_forward_splits(
        train_period["start"], train_period["end"], n_splits=2, train_fraction=0.7
    )
    return subperiods_from_walk_forward(folds)


def _midpoint(subperiod: dict[str, str]) -> str:
    start = pd.Timestamp(subperiod["start"])
    end = pd.Timestamp(subperiod["end"])
    return (start + (end - start) / 2).isoformat()


def test_the_optimization_artifact_carries_provenance_survivorship_and_stability(
    monkeypatch, tmp_path: Path
):
    """End to end: what a reader of the optimization artifact gets.

    The study's winner (window=3) is the WORST parameter in every sub-period —
    the signature of a ranking that does not survive time — and the artifact has
    to say so rather than reporting the study's own verdict.
    """
    import optuna

    import runner.optimizer as opt

    backtests_dir = _patch_optimizer(monkeypatch, tmp_path)
    data_dir = tmp_path / "market-data"
    # OLD is present for the whole window; IPO only appears halfway through it.
    _write_bars(data_dir, "OLD", [f"2024-0{m}-{d:02d}T00:00:00+00:00" for m in (1, 2, 3) for d in (1, 15)])
    _write_bars(data_dir, "IPO", [f"2024-0{m}-15T00:00:00+00:00" for m in (4, 5, 6)])
    provenance = universe_provenance(
        symbols=["OLD", "IPO"],
        provider="sp500-constituents.csv",
        as_of="2024-06-01",
        membership=MEMBERSHIP_CURRENT,
        window_start="2024-01-01",
        window_end="2024-06-01",
    )
    _base_result(
        backtests_dir,
        symbols=["OLD", "IPO"],
        data_dir=data_dir,
        data={UNIVERSE_PROVENANCE_KEY: provenance},
    )

    subperiods = _train_subperiods()
    assert len(subperiods) == 2, "this fixture needs two sub-periods to be meaningful"
    # Realized PnL per sub-period: the early period favours window=2, the later
    # one favours window=1, and the study's overall winner (window=3) trails
    # both everywhere.
    realized_by_window = {
        1: [0.0, 300.0],
        2: [200.0, -50.0],
        3: [10.0, 20.0],
    }
    sharpe_by_window = {1: 0.1, 2: 0.2, 3: 0.9}

    def fake_run_backtest(config, strategy_path):
        window = int(config.parameters["window"])
        is_train = str(config.start_date).startswith("2024-01")
        exits = (
            [
                (_midpoint(subperiod), pnl)
                for subperiod, pnl in zip(subperiods, realized_by_window[window])
                if pnl
            ]
            if is_train
            else []
        )
        return _trial(config, metric=sharpe_by_window[window], exits=exits)

    # A grid sampler makes the study deterministic: without it Optuna may sample
    # one value three times and the whole stability view would collapse to
    # "no axis varied" by luck.
    real_create_study = optuna.create_study
    monkeypatch.setattr(
        opt.optuna,
        "create_study",
        lambda **kwargs: real_create_study(
            **kwargs, sampler=optuna.samplers.GridSampler({"window": [1, 2, 3]})
        ),
    )
    monkeypatch.setattr(opt, "_run_vectorbt_backtest", fake_run_backtest)
    monkeypatch.setattr(opt, "compute_stats_for_result", lambda result: {**result.stats})

    payload = opt.run_optimization(
        opt.OptimizationConfig(
            base_backtest_id="base-demo",
            target_metric="sharpe",
            n_trials=3,
            train_split_pct=0.7,
            walk_forward_splits=2,
            parameter_ranges={"window": {"type": "int", "min": 1, "max": 3, "step": 1}},
        )
    )

    # 1. Provenance survives onto the optimization artifact.
    assert payload["universe_provenance"]["membership_basis"] == MEMBERSHIP_CURRENT
    assert payload["universe_provenance"]["symbol_count"] == 2
    assert payload["universe_provenance"]["as_of"] == "2024-06-01"
    assert payload["best_params"] == {"window": 3}

    diagnostics = payload["diagnostics"]
    # 2. Survivorship: what the data shows, and that the size is unknown.
    survivorship = diagnostics["survivorship"]
    assert survivorship["n_symbols"] == 2
    assert survivorship["n_no_data_at_window_start"] == 1
    assert [
        entry["symbol"] for entry in survivorship["no_data_at_window_start"]
    ] == ["IPO"]
    assert survivorship["n_started_mid_window"] == 1
    assert survivorship["bias_direction"] == "upward"
    assert survivorship["bias_magnitude"] is None
    assert survivorship["membership_basis"] == MEMBERSHIP_CURRENT

    # 3. Per-sub-period stability of the chosen parameter.
    stability = diagnostics["subperiod_stability"]
    assert stability["status"] == "ok"
    assert stability["parameter"] == "window"
    assert stability["selected_value"] == 3
    assert stability["n_periods"] == 2
    assert stability["selected_wins_n_periods"] == 0
    assert stability["selected_win_rate"] == pytest.approx(0.0)
    assert stability["winner_agreement"] == "unstable"
    assert stability["distinct_period_winners"] == 2
    assert [row["best_value"] for row in stability["per_period"]] == [2, 1]
    by_value = {row["value"]: row for row in stability["per_parameter_value"]}
    assert by_value[3]["mean_metric"] == pytest.approx(15.0)
    assert by_value[2]["best_in_n_periods"] == 1
    assert by_value[1]["best_in_n_periods"] == 1
    assert [row["label"] for row in stability["subperiods"]] == ["fold_0", "fold_1"]

    saved = json.loads(
        next((tmp_path / "optimizations").glob("opt_*.json")).read_text(encoding="utf-8")
    )
    assert saved["universe_provenance"]["membership_basis"] == MEMBERSHIP_CURRENT
    assert saved["diagnostics"]["survivorship"]["bias_magnitude"] is None
    assert saved["diagnostics"]["subperiod_stability"]["winner_agreement"] == "unstable"


def test_a_legacy_base_artifact_reports_unknown_provenance_instead_of_a_clean_bill(
    monkeypatch, tmp_path: Path
):
    import runner.optimizer as opt

    backtests_dir = _patch_optimizer(monkeypatch, tmp_path)
    _base_result(backtests_dir, symbols=["AAPL"], data_dir=tmp_path / "market-data")
    monkeypatch.setattr(
        opt,
        "_run_vectorbt_backtest",
        lambda config, strategy_path: _trial(
            config, metric=1.0, exits=[("2024-02-01T00:00:00+00:00", 100.0)]
        ),
    )
    monkeypatch.setattr(opt, "compute_stats_for_result", lambda result: {**result.stats})

    payload = opt.run_optimization(
        opt.OptimizationConfig(
            base_backtest_id="base-demo",
            target_metric="sharpe",
            n_trials=1,
            train_split_pct=0.7,
            parameter_ranges={"window": {"type": "int", "min": 2, "max": 2}},
        )
    )

    provenance = payload["universe_provenance"]
    assert provenance["symbol_count"] is None
    assert provenance["is_point_in_time"] is None
    assert "symbol_count" in provenance["undefined_metrics"]
    assert payload["diagnostics"]["survivorship"]["bias_direction"] == "unknown"
    # Nothing to compare across sub-periods with a single trial and no trades:
    # unknown, not "stable".
    assert payload["diagnostics"]["subperiod_stability"]["status"] == "unknown"


class _RecordingEngine:
    """An engine that reports whether it was ever asked to process a bar."""

    name = "native"

    def __init__(self):
        self.run_calls = 0

    def validate_config(self, config):
        return []

    def run(self, *args, **kwargs):
        self.run_calls += 1
        raise AssertionError(
            "engine.run was called, so a bad universe flag cost a full backtest "
            "before it was rejected"
        )


def test_contradictory_universe_flags_fail_before_the_engine_runs(monkeypatch, tmp_path):
    """`--universe-membership point_in_time` without an as-of is contradictory.

    The provenance artifact exists so a reader can tell a point-in-time list from a
    current one. Claiming the former without the date that defines it is not a
    weaker claim, it is a false one, so it is rejected.

    What matters is *when*. Every input the check needs is known before a bar is
    processed, so it belongs with the other fail-fast validation. Rejecting it
    afterwards means the user pays for the whole backtest, gets an error, and is
    left with a saved run whose provenance was never recorded — the failure is
    expensive and still leaves a misleading artifact behind. Hence the assertion on
    run_calls: it fails if the ordering regresses, not merely if the value is wrong.
    """
    engine = _RecordingEngine()
    monkeypatch.setattr(cli, "get_engine", lambda name: engine)
    monkeypatch.setattr(cli, "require_generated_strategy_approval", lambda path: None)
    monkeypatch.setattr(cli, "ensure_backtest_data", lambda **kwargs: [])
    monkeypatch.setattr(
        cli,
        "_resolve_strategy_type",
        lambda path: argparse.Namespace(value="market_data"),
    )

    strategy = tmp_path / "gen_strategy.py"
    strategy.write_text("# generated\n", encoding="utf-8")

    args = cli.build_parser().parse_args(
        [
            "run",
            "--engine",
            "native",
            "--strategy",
            str(strategy),
            "--symbols",
            "TEST",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-31",
            "--universe-membership",
            "point_in_time",
        ]
    )

    with pytest.raises(ValueError, match="as-of|as_of"):
        cli.cmd_run(args)

    assert engine.run_calls == 0, "the engine ran before the flags were rejected"
