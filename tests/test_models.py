"""Tests for shared.models — universal trade format."""

import tempfile
from pathlib import Path

import pytest

from shared.models import (
    BacktestMeta,
    BacktestStats,
    PortfolioStats,
    BacktestResult,
    EquityPoint,
    StrategyMeta,
    Trade,
    TradeExit,
)


def _make_simple_trade() -> Trade:
    return Trade(
        id=1,
        symbol="AAPL",
        direction="LONG",
        entry_time="2021-03-15T10:30:00Z",
        entry_price=121.50,
        quantity=82,
        exits=[
            TradeExit(
                time="2021-03-18T15:45:00Z",
                price=124.80,
                quantity=82,
                reason="rsi_target",
                fees=1.64,
            )
        ],
        pnl=270.60,
        pnl_pct=2.72,
        fees=1.64,
        net_pnl=268.96,
    )


def _make_partial_exit_trade() -> Trade:
    return Trade(
        id=7,
        symbol="CRWD",
        direction="LONG",
        entry_time="2026-02-23T14:35:02Z",
        entry_price=376.37,
        quantity=138,
        exits=[
                TradeExit(
                    time="2026-02-23T15:20:00Z",
                    price=387.22,
                    quantity=69,
                    reason="tp1",
                    fees=0.82,
                    tags=["tp1"],
                ),
                TradeExit(
                    time="2026-02-23T16:45:00Z",
                    price=382.10,
                    quantity=69,
                    reason="supertrend_flip",
                    fees=0.82,
                    tags=["runner"],
                ),

        ],
        pnl=1143.67,
        pnl_pct=2.20,
        fees=1.64,
        net_pnl=1142.03,
        tags=["tp1_reached"],
        meta={"sl_price": 369.14, "tp1_price": 387.22},
    )


def _make_result() -> BacktestResult:
    return BacktestResult(
        meta=BacktestMeta(
            id="test-run-20260224",
            created_at="2026-02-24T14:30:22Z",
            engine="vectorbt",
            strategies=["rsi_mean_reversion"],
            backtest_start="2021-01-01",
            backtest_end="2025-06-30",
            initial_capital=100_000.0,
            data={
                "timeframes": ["1h"],
                "symbols": ["AAPL", "CRWD"],
                "universe": "",
                "source": "yfinance",
            },
            parameters={"rsi_period": 2},
        ),
        trades=[_make_simple_trade(), _make_partial_exit_trade()],
        equity_curve=[
            EquityPoint(time="2021-01-01", equity=100_000.0),
            EquityPoint(time="2021-01-02", equity=100_150.30),
        ],
        stats={"total_trades": 2, "win_rate": 100.0},
        drawings=[
            {
                "type": "marker",
                "time": "2021-03-15T10:30:00Z",
                "position": "belowBar",
                "shape": "arrowUp",
                "color": "#3fb950",
                "text": "Buy",
            },
            {
                "type": "line",
                "start_time": "2021-03-15T10:30:00Z",
                "end_time": "2021-03-18T15:45:00Z",
                "val1": 121.5,
                "val2": 124.8,
                "color": "#58a6ff",
            },
        ],
    )


class TestTradeExit:
    def test_roundtrip(self):
        e = TradeExit(
            time="2021-03-18T15:45:00Z",
            price=124.80,
            quantity=82,
            reason="tp1",
            fees=1.0,
        )
        d = e.to_dict()
        e2 = TradeExit.from_dict(d)
        assert e2.time == e.time
        assert e2.price == e.price
        assert e2.quantity == e.quantity
        assert e2.reason == e.reason
        assert e2.fees == e.fees

    def test_per_exit_pnl_survives_the_roundtrip(self):
        """A partial exit's own `pnl` must serialise.

        `reason` and `fees` are pinned by `test_roundtrip`, but `pnl` is not - that
        fixture omits it. Dropping the `pnl` branch from `to_dict` leaves every test
        green.

        Why it matters: `Trade.pnl` is the *gross* total and `TradeExit.pnl` is the
        per-leg figure. The per-leg number is what a partial exit is reconciled
        against, so losing it on serialisation means a stored backtest's legs no longer
        sum to the trade - a mismatch that surfaces much later as an unexplained
        discrepancy rather than as a missing key.

        Asserted both ways, so the guard cannot be satisfied by always writing the key:
        present serialises and round-trips, absent stays absent.
        """
        with_pnl = TradeExit(
            time="2021-03-18T15:45:00Z",
            price=124.80,
            quantity=40,
            pnl=120.00,
        )
        d = with_pnl.to_dict()
        assert d.get("pnl") == 120.00, (
            "a per-exit pnl was dropped on serialisation: " + repr(d)
        )
        restored = TradeExit.from_dict(d)
        assert restored.pnl == 120.00
        assert restored.quantity == 40

        without = TradeExit(time="2021-01-01T00:00:00Z", price=100.0, quantity=10)
        assert "pnl" not in without.to_dict(), (
            "an unset pnl was written as a key anyway: " + repr(without.to_dict())
        )

        # And through the parent, so the legs of a partial exit are preserved together.
        partial = Trade(
            id=3, symbol="AAPL", direction="LONG",
            entry_time="2021-03-18T00:00:00Z", entry_price=120.0,
            quantity=80,
            exits=[
                TradeExit(time="2021-03-18T15:45:00Z", price=124.80, quantity=40, pnl=192.0,
                          reason="tp1"),
                TradeExit(time="2021-03-19T10:00:00Z", price=126.00, quantity=40, pnl=240.0,
                          reason="tp2"),
            ],
            pnl=432.0,
        )
        payload = partial.to_dict()
        assert [e["pnl"] for e in payload["exits"]] == [192.0, 240.0], (
            "per-exit pnl was lost through the parent trade: " + repr(payload["exits"])
        )

    def test_minimal(self):
        e = TradeExit(time="2021-01-01T00:00:00Z", price=100.0, quantity=10)
        d = e.to_dict()
        assert "reason" not in d
        assert "fees" not in d
        assert "tags" not in d


class TestTrade:
    def test_simple_roundtrip(self):
        t = _make_simple_trade()
        d = t.to_dict()
        t2 = Trade.from_dict(d)
        assert t2.id == t.id
        assert t2.symbol == t.symbol
        assert t2.pnl == t.pnl
        assert len(t2.exits) == 1
        assert t2.exits[0].reason == "rsi_target"

    def test_stop_levels_roundtrip(self):
        trade = Trade(
            id=1,
            symbol="X",
            direction="LONG",
            entry_time="2024-01-01T00:00:00Z",
            entry_price=100.0,
            quantity=1.0,
            exits=[TradeExit(time="2024-01-02T00:00:00Z", price=105.0, quantity=1.0)],
            pnl=5.0,
            stop_loss=95.0,
            take_profit=110.0,
        )
        payload = trade.to_dict()
        restored = Trade.from_dict(payload)
        assert payload["stop_loss"] == 95.0
        assert payload["take_profit"] == 110.0
        assert restored.stop_loss == 95.0
        assert restored.take_profit == 110.0

    def test_partial_exit_roundtrip(self):
        t = _make_partial_exit_trade()
        d = t.to_dict()
        t2 = Trade.from_dict(d)
        assert len(t2.exits) == 2
        assert t2.exits[0].reason == "tp1"
        assert t2.exits[1].reason == "supertrend_flip"
        assert t2.tags == ["tp1_reached"]
        assert t2.meta["sl_price"] == 369.14

    def test_is_winner(self):
        t = _make_simple_trade()
        assert t.is_winner is True
        t.pnl = -50.0
        assert t.is_winner is False

    def test_last_exit_time(self):
        t = _make_partial_exit_trade()
        assert t.last_exit_time == "2026-02-23T16:45:00Z"

    def test_direction_sign(self):
        t = _make_simple_trade()
        assert t.direction_sign == 1
        t.direction = "SHORT"
        assert t.direction_sign == -1

    def test_direction_survives_the_dict_roundtrip(self):
        """A SHORT trade must serialise as SHORT, not as a hardcoded LONG.

        Every `Trade` fixture in this file is a LONG - `_make_simple_trade`, the
        round-trip tests, the direction_sign test's first assertion - so hardcoding
        `"direction": "LONG"` in `to_dict` leaves all of them green. The only test that
        mentions SHORT sets the attribute and reads `direction_sign` without ever
        serialising.

        The failure is quiet and total: a stored backtest's SHORT trades come back as
        LONG, so direction-dependent PnL, MFE/MAE and R-multiples are wrong for exactly
        half the trades, while the record still looks complete. Round-tripping SHORT
        is also the only thing that would catch a `from_dict` that read the wrong key.
        """
        short = Trade(
            id=7, symbol="EURUSD", direction="SHORT",
            entry_time="2021-01-01T00:00:00Z", entry_price=1.2000,
            quantity=1000,
            exits=[TradeExit(time="2021-01-02T00:00:00Z", price=1.1900, quantity=1000)],
            pnl=100.0,
        )

        assert short.to_dict()["direction"] == "SHORT", (
            "to_dict did not carry the trade's own direction: "
            + repr(short.to_dict()["direction"])
        )

        restored = Trade.from_dict(short.to_dict())
        assert restored.direction == "SHORT", (
            "a SHORT trade came back as " + restored.direction + " after a round trip"
        )
        assert restored.direction_sign == -1

        # And LONG still round-trips, so the guard is not satisfied by always writing
        # whatever the input happened to be - the two must stay distinguishable.
        long_trade = Trade(
            id=8, symbol="AAPL", direction="LONG",
            entry_time="2021-01-01T00:00:00Z", entry_price=100.0,
            quantity=10,
            exits=[TradeExit(time="2021-01-02T00:00:00Z", price=105.0, quantity=10)],
            pnl=50.0,
        )
        assert long_trade.to_dict()["direction"] == "LONG"
        assert Trade.from_dict(long_trade.to_dict()).direction_sign == 1

    def test_optional_fields_omitted_when_none(self):
        t = Trade(
            id=1, symbol="X", direction="LONG",
            entry_time="2021-01-01T00:00:00Z", entry_price=100.0,
            quantity=10,
            exits=[TradeExit(time="2021-01-02T00:00:00Z", price=105.0, quantity=10)],
            pnl=50.0,
        )
        d = t.to_dict()
        assert "pnl_pct" not in d
        assert "fees" not in d
        assert "r_multiple" not in d
        assert "tags" not in d
        assert "meta" not in d


class TestStatsExtrasRoundtrip:
    """`BacktestStats.extras` is the forward-compatibility bucket, and it was dropped.

    `BacktestStats.from_dict` splits a payload into known dataclass fields and an
    `extras` dict holding everything else. Emptying that dict loses every metric a newer
    producer wrote, on read-back only — and nothing raises, because the reader succeeded.

    The mutation survives because the existing round-trip test only uses metrics that
    *are* dataclass fields, so the extras bucket is exercised empty in both directions.
    That is the shape of fixture blind spot this whole sweep has been about.

    Two directions are asserted, because they fail differently:

    - producer writes a custom metric, reader must preserve it
    - a payload can also carry a field the *class* no longer has, and that is exactly
      the case `extras` exists for
    """

    def test_a_custom_metric_survives_the_roundtrip(self):
        original = BacktestStats(sharpe_ratio=1.5, cagr=0.2)
        original.extras["deflated_sharpe"] = 1.42
        original.extras["custom_engine_flag"] = "v2"

        restored = BacktestStats.from_dict(original.to_dict())

        assert restored.extras == {"deflated_sharpe": 1.42, "custom_engine_flag": "v2"}, (
            "custom metrics were dropped on the way back in: " + repr(restored.extras)
        )
        # The known fields are unaffected, so the fix cannot be "keep everything raw".
        assert restored.sharpe_ratio == 1.5
        assert restored.cagr == 0.2

    def test_an_unknown_field_from_a_newer_producer_is_kept_in_extras(self):
        payload = {
            "sharpe_ratio": 2.0,
            "psr": 0.9,
            "a_metric_from_the_future": [1, 2, 3],
        }
        restored = BacktestStats.from_dict(payload)

        assert restored.extras == {"a_metric_from_the_future": [1, 2, 3]}, (
            "a field this class does not know about was discarded rather than kept in "
            "extras: " + repr(restored.extras)
        )
        assert restored.sharpe_ratio == 2.0
        assert restored.psr == 0.9

    def test_portfolio_correlation_matrix_survives_the_roundtrip(self):
        """`PortfolioStats.correlation_matrix` must not serialise as an empty dict.

        It is the one field in the class that is a nested structure rather than a
        scalar, so replacing it with `{}` is a plausible-looking mutation that no
        scalar-field test notices. The correlation tests in
        `test_portfolio_optimizer_oos.py` exercise `_compute_correlation_matrix`, the
        *producer*; nothing checked the matrix survives being read back.

        The consequence is the portfolio shortlist losing the input to its own
        diversification decision: a single-strategy portfolio and a correlated pair both
        come back as `{}`, so anything downstream that reads it sees no correlation
        rather than measured correlation.

        Two shapes, because a one-element matrix is the easy one to get wrong: a full
        2x2 and a single-strategy 1x1.
        """
        matrix = {"a": {"a": 1.0, "b": 0.42}, "b": {"a": 0.42, "b": 1.0}}
        original = PortfolioStats(
            sharpe=1.4,
            sortino=1.9,
            cagr=0.18,
            max_drawdown=-9.5,
            net_profit=18000.0,
            win_rate=0.55,
            profit_factor=1.6,
            calmar=1.1,
            total_return=0.18,
            psr=0.87,
            p_value=0.04,
            correlation_matrix=matrix,
        )

        payload = original.to_dict()
        assert payload["correlation_matrix"] == matrix, (
            "the correlation matrix was written as an empty dict: "
            + repr(payload["correlation_matrix"])
        )

        restored = PortfolioStats.from_dict(payload)
        assert restored.correlation_matrix == matrix, (
            "the correlation matrix was lost on the way back in: "
            + repr(restored.correlation_matrix)
        )
        # Known scalars are unaffected, so the guard is not satisfied by keeping the
        # whole payload raw.
        assert restored.sharpe == 1.4
        assert restored.psr == 0.87

        single = PortfolioStats(correlation_matrix={"only": {"only": 1.0}})
        assert single.to_dict()["correlation_matrix"] == {"only": {"only": 1.0}}
        assert (
            PortfolioStats.from_dict(single.to_dict()).correlation_matrix
            == {"only": {"only": 1.0}}
        )

    def test_extras_is_empty_when_there_is_nothing_unrecognised(self):
        restored = BacktestStats.from_dict(
            BacktestStats(sharpe_ratio=1.0).to_dict()
        )
        assert restored.extras == {}, repr(restored.extras)


class TestBacktestResultEvidence:
    """The equity-evidence contract, on the model rather than the validation reference.

    Two separate guards, and the reference-level test does not reach either of them
    through `BacktestResult.__post_init__` on its own terms.

    **`available` requires a curve.** Without it, a result can claim its equity
    evidence is available while carrying no curve at all - the artifact says it can
    prove something and then cannot show it. `to_backtest_result()` raises for that
    shape, so the check exists at the reference boundary too; what is missing is the
    model saying so for itself, which is where every other construction path lands.

    **A curve implies `available`.** `__post_init__` promotes `unknown` to
    `available` when a curve is present, so the serialised record does not claim
    ignorance about evidence it is carrying. Removing that leaves a result whose
    status is `unknown` while `equity_curve` is populated.
    """

    def _result(self, *, curve=(), status="unknown"):
        return BacktestResult(
            meta=_make_result().meta,
            trades=[],
            equity_curve=list(curve),
            equity_evidence_status=status,
        )

    def test_available_status_without_a_curve_is_refused(self):
        with pytest.raises(ValueError, match="requires an equity curve"):
            self._result(status="available")

        # And the other direction is still allowed: a curve with status "available".
        curve = [EquityPoint(time="2024-01-01T00:00:00Z", equity=100000.0)]
        ok = self._result(curve=curve, status="available")
        assert ok.equity_curve == curve

    def test_a_curve_promotes_the_evidence_status_from_unknown(self):
        curve = [EquityPoint(time="2024-01-01T00:00:00Z", equity=100000.0)]
        result = self._result(curve=curve)
        assert result.equity_evidence_status == "available", (
            "a result carrying an equity curve still claims its evidence status is "
            "unknown: " + result.equity_evidence_status
        )

    def test_no_curve_and_unknown_stays_unknown(self):
        assert self._result().equity_evidence_status == "unknown"

    def test_missing_is_preserved_rather_than_promoted(self):
        # "missing" is a deliberate statement, so it must not be rewritten to
        # "available" by the presence of a curve - that would turn a recorded
        # absence into a claim.
        curve = [EquityPoint(time="2024-01-01T00:00:00Z", equity=1.0)]
        result = self._result(curve=curve, status="missing")
        assert result.equity_evidence_status == "missing"

    def test_the_status_survives_serialisation(self):
        curve = [EquityPoint(time="2024-01-01T00:00:00Z", equity=100000.0)]
        result = self._result(curve=curve)
        payload = result.to_dict()
        assert payload.get("equity_evidence_status") == "available", (
            "the evidence status was dropped on serialisation, so a reloaded result "
            "cannot tell whether its curve is trustworthy: " + repr(payload.keys())
        )
        assert BacktestResult.from_dict(payload).equity_evidence_status == "available"

        # A "missing" status is serialised explicitly rather than omitted as unknown.
        missing = self._result(status="missing").to_dict()
        assert missing["equity_evidence_status"] == "missing"

        # "unknown" is the default, so it stays out of the payload entirely.
        assert "equity_evidence_status" not in self._result().to_dict()


class TestBacktestResult:
    def test_json_roundtrip(self):
        r = _make_result()
        json_str = r.to_json()
        r2 = BacktestResult.from_json(json_str)
        assert r2.meta.id == r.meta.id
        assert r2.meta.strategies == ["rsi_mean_reversion"]
        assert len(r2.trades) == 2
        assert len(r2.equity_curve) == 2
        assert r2.stats["total_trades"] == 2
        assert len(r2.drawings) == 2
        assert r2.equity_evidence_status == "available"

    def test_file_roundtrip(self):
        r = _make_result()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_result.json"
            r.save(path)
            r2 = BacktestResult.load(path)
            assert r2.meta.id == r.meta.id
            assert len(r2.trades) == 2

    def test_save_preserves_existing_file_when_serialization_fails(self, tmp_path, monkeypatch):
        result = _make_result()
        path = tmp_path / "result.json"
        path.write_text("previous", encoding="utf-8")

        def fail_serialization():
            raise ValueError("serialization failed")

        monkeypatch.setattr(result, "to_json", fail_serialization)
        with pytest.raises(ValueError, match="serialization failed"):
            result.save(path)

        assert path.read_text(encoding="utf-8") == "previous"

    def test_filename_generation(self):
        r = _make_result()
        assert r.filename == "rsi_mean_reversion_vectorbt_20260224-143022.json"

    def test_meta_serialization(self):
        r = _make_result()
        d = r.to_dict()
        assert d["meta"]["backtest_start"] == "2021-01-01"
        assert d["meta"]["backtest_end"] == "2025-06-30"
        assert d["meta"]["strategies"] == ["rsi_mean_reversion"]
        assert d["meta"]["data"]["timeframes"] == ["1h"]
        assert d["drawings"][0]["type"] == "marker"


class TestBacktestMetaDefaults:
    """`BacktestMeta.to_dict` omits defaults, so `from_dict`'s defaults are never read.

    Six mutations survive the whole suite: changing the default for
    `allow_fallback_stop`, `reject_missing_stop`, `signal_exit_fill`,
    `modeling_resolution`, `initial_capital`, or `currency`.

    One root cause: `to_dict` writes a field only when it differs from its default, so
    a default-valued field never appears in the payload — and `from_dict` is therefore
    always exercised *with* the field present. The defaults are the code that decides
    what an **older or hand-written artifact** means, and nothing read one.

    That is the dangerous direction. A pre-2026 artifact, or any payload produced
    before a field existed, relies entirely on these fallbacks, and the four safety
    flags are not decorative:

    - `allow_fallback_stop` — whether a signal without a stop may still be taken.
    - `reject_missing_stop` — the opposite, stricter setting.
    - `signal_exit_fill` — `close` versus the next open. This changes backtest PnL.
    - `modeling_resolution` — how intra-bar moves were modelled.

    Each default is asserted against a payload that omits the key, and paired with the
    present case so a fix cannot be "always write it".
    """

    # Field name -> (default in from_dict, a non-default value).
    _DEFAULTS = {
        "allow_fallback_stop": (True, False),
        "reject_missing_stop": (False, True),
        "require_explicit_column_roles": (False, True),
        "signal_exit_fill": ("close", "next_open"),
        "modeling_resolution": ("main_timeframe", "1m"),
        "initial_capital": (100_000.0, 250_000.0),
        "currency": ("USD", "EUR"),
    }

    def _minimal_payload(self) -> dict:
        """The four fields `from_dict` reads with `d[...]`, so it cannot fail on them."""
        return {
            "id": "old-artifact",
            "created_at": "2026-01-01T00:00:00Z",
            "engine": "vectorbt",
        }

    def test_every_omittable_field_falls_back_to_its_documented_default(self):
        payload = self._minimal_payload()
        meta = BacktestMeta.from_dict(payload)

        for name, (expected, _) in self._DEFAULTS.items():
            assert getattr(meta, name) == expected, (
                f"an artifact omitting {name!r} resolved to "
                f"{getattr(meta, name)!r}, not the documented default {expected!r}"
            )

    def test_every_field_is_read_when_present(self):
        """Paired with the above: a fix cannot be "always write the key"."""
        payload = self._minimal_payload()
        for name, (_, non_default) in self._DEFAULTS.items():
            payload[name] = non_default
        meta = BacktestMeta.from_dict(payload)

        for name, (_, non_default) in self._DEFAULTS.items():
            assert getattr(meta, name) == non_default, (
                f"{name!r} was not read from the payload: "
                f"{getattr(meta, name)!r} != {non_default!r}"
            )

    def test_the_four_safety_flags_round_trip_at_both_settings(self):
        """The flags where a wrong default changes whether an order may be taken."""
        permissive = BacktestMeta.from_dict(self._minimal_payload())
        assert permissive.allow_fallback_stop is True
        assert permissive.reject_missing_stop is False
        assert permissive.require_explicit_column_roles is False

        strict_payload = self._minimal_payload()
        strict_payload.update(
            allow_fallback_stop=False,
            reject_missing_stop=True,
            require_explicit_column_roles=True,
        )
        strict = BacktestMeta.from_dict(strict_payload)
        assert strict.allow_fallback_stop is False
        assert strict.reject_missing_stop is True
        assert strict.require_explicit_column_roles is True

    def test_a_value_of_the_default_is_omitted_and_comes_back_identical(self):
        """The omit-on-default scheme must be lossless for a default value."""
        meta = BacktestMeta(
            id="defaults",
            created_at="2026-01-01T00:00:00Z",
            engine="vectorbt",
            **{
                name: value
                for name, (value, _) in self._DEFAULTS.items()
            },
        )
        payload = meta.to_dict()
        # `initial_capital` is in the always-written block, since a backtest with no
        # recorded capital is meaningless rather than defaulted. The rest are omitted.
        for name in self._DEFAULTS:
            if name == "initial_capital":
                assert payload.get(name) == 100_000.0
                continue
            assert name not in payload, (
                f"{name!r} holds its default and should be omitted, but was written: "
                + repr(payload.get(name))
            )
        restored = BacktestMeta.from_dict(payload)
        for name, (expected, _) in self._DEFAULTS.items():
            assert getattr(restored, name) == expected, (
                f"{name!r} did not survive an omit-and-restore round trip: "
                f"{getattr(restored, name)!r}"
            )


class TestStrategyMeta:
    def test_roundtrip(self):
        sm = StrategyMeta(
            name="Test Strategy",
            version="1.0.0",
            tags={"type": ["momentum"], "complexity": "simple"},
            source_url="https://example.com/strategy",
            notes="Known caveat: avoid low-volume sessions.",
        )
        d = sm.to_dict()
        sm2 = StrategyMeta.from_dict(d)
        assert sm2.name == sm.name
        assert sm2.version == sm.version
        assert sm2.tags == sm.tags
        assert sm2.source_url == sm.source_url
        assert sm2.notes == sm.notes

    def test_strategy_parameters_survive_the_roundtrip(self):
        """`StrategyMeta.parameters` must serialise, and the existing fixture omits it.

        The round-trip fixture sets `tags`, `source_url` and `notes` but leaves
        `parameters` at its empty default - which `to_dict` omits. So dropping the
        branch leaves every test green, the same single-branch-fixture pattern as the
        `TradeExit.pnl` finding.

        `parameters` is the strategy's own configuration: the indicator periods,
        thresholds and lookbacks that produced a backtest. Losing it means a stored
        artifact no longer says *why* it produced those results, so a re-run cannot be
        reproduced and a promoted strategy cannot be re-optimised from the record.

        Asserted both ways - populated serialises and round-trips, empty stays absent -
        so the guard cannot be satisfied by always writing the key.
        """
        configured = StrategyMeta(
            name="Configured",
            parameters={"rsi_period": 14, "atr_period": 20, "stop_atr": 2.5},
        )
        payload = configured.to_dict()
        assert payload.get("parameters") == {
            "rsi_period": 14,
            "atr_period": 20,
            "stop_atr": 2.5,
        }, "the strategy's parameters were dropped on serialisation: " + repr(payload)

        restored = StrategyMeta.from_dict(payload)
        assert restored.parameters["rsi_period"] == 14
        assert restored.parameters["stop_atr"] == 2.5

        # Empty stays absent, so the key is still omitted for a bare strategy.
        assert "parameters" not in StrategyMeta(name="Bare").to_dict()

    def test_an_artifact_without_a_version_reads_the_documented_default(self):
        """`from_dict`'s version default is the only exercise of it.

        `test_minimal` asserts `to_dict` writes `"1.0.0"` explicitly, so `version`
        always appears in a produced payload and the reader's fallback is never
        reached. Changing that fallback survives the suite.

        The dangerous direction again: a hand-written or pre-version artifact omits the
        key entirely, and the fallback is what decides what it claims to be. Getting it
        wrong does not crash - it silently mislabels a strategy's version, which is what
        a reader compares against when deciding whether a result is current.
        """
        legacy = StrategyMeta.from_dict({"name": "Legacy"})
        assert legacy.version == "1.0.0", (
            "an artifact with no version read as " + repr(legacy.version)
            + " rather than the documented default"
        )
        assert legacy.parameters == {}
        assert legacy.tags == {}
        assert legacy.source_url == ""
        assert legacy.notes == ""

        # And the present case still wins, so the default is not overriding anything.
        assert StrategyMeta.from_dict({"name": "X", "version": "2.5.0"}).version == "2.5.0"

    def test_minimal(self):
        sm = StrategyMeta(name="Minimal")
        d = sm.to_dict()
        assert d["name"] == "Minimal"
        assert d["version"] == "1.0.0"
        assert "tags" not in d
        assert "source_url" not in d
        assert "notes" not in d
