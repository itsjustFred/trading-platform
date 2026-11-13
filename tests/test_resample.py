"""Tests for data_tools/resample.py."""

from pathlib import Path

import pandas as pd
import pytest

from data_tools.resample import TIMEFRAME_MAP, resample_file, resample_ohlcv


@pytest.fixture
def sample_1m_df():
    """Create a sample 1-minute OHLCV DataFrame."""
    idx = pd.date_range("2024-01-02 09:00", periods=120, freq="min")
    import numpy as np
    np.random.seed(42)
    opens = 100 + np.cumsum(np.random.randn(120) * 0.1)
    df = pd.DataFrame({
        "timestamp": idx,
        "open": opens,
        "high": opens + np.abs(np.random.randn(120) * 0.2),
        "low": opens - np.abs(np.random.randn(120) * 0.2),
        "close": opens + np.random.randn(120) * 0.1,
        "volume": np.random.randint(100, 10000, 120),
    })
    return df


@pytest.fixture
def sample_1h_df():
    """Create a sample 1-hour OHLCV DataFrame."""
    idx = pd.date_range("2024-01-01", periods=500, freq="h")
    import numpy as np
    np.random.seed(42)
    opens = 100 + np.cumsum(np.random.randn(500) * 0.5)
    df = pd.DataFrame({
        "timestamp": idx,
        "open": opens,
        "high": opens + np.abs(np.random.randn(500) * 1.0),
        "low": opens - np.abs(np.random.randn(500) * 1.0),
        "close": opens + np.random.randn(500) * 0.5,
        "volume": np.random.randint(1000, 100000, 500),
    })
    return df


class TestResampleOHLCV:
    def test_1m_to_5m(self, sample_1m_df):
        result = resample_ohlcv(sample_1m_df, "5m")
        # 120 minutes / 5 = 24 bars
        assert len(result) == 24
        assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_1m_to_15m(self, sample_1m_df):
        result = resample_ohlcv(sample_1m_df, "15m")
        assert len(result) == 8  # 120 / 15

    def test_1h_to_4h(self, sample_1h_df):
        result = resample_ohlcv(sample_1h_df, "4h")
        assert len(result) > 0
        assert len(result) <= len(sample_1h_df) // 4 + 1

    def test_ohlcv_rules(self, sample_1m_df):
        """Verify OHLCV aggregation rules: O=first, H=max, L=min, C=last, V=sum."""
        result = resample_ohlcv(sample_1m_df, "5m")
        first_5 = sample_1m_df.iloc[:5]
        bar = result.iloc[0]
        assert bar["open"] == first_5["open"].iloc[0], "Open should be first"
        assert bar["high"] == first_5["high"].max(), "High should be max"
        assert bar["low"] == first_5["low"].min(), "Low should be min"
        assert bar["close"] == first_5["close"].iloc[-1], "Close should be last"
        assert bar["volume"] == first_5["volume"].sum(), "Volume should be sum"

    def test_preserves_timestamp_column(self, sample_1m_df):
        result = resample_ohlcv(sample_1m_df, "5m")
        assert "timestamp" in result.columns
        assert result["timestamp"].is_monotonic_increasing

    def test_invalid_timeframe(self, sample_1m_df):
        with pytest.raises(ValueError):
            resample_ohlcv(sample_1m_df, "99x")


class TestResampleFile:
    def test_resample_file(self, sample_1h_df, tmp_path):
        """Test resampling a parquet file."""
        src = tmp_path / "1h.parquet"
        sample_1h_df.to_parquet(src, index=False)

        outputs = resample_file(src, ["4h"])
        assert len(outputs) == 1
        assert outputs[0].exists()
        result = pd.read_parquet(outputs[0])
        assert len(result) > 0
        assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_resample_file_multiple_timeframes(self, sample_1h_df, tmp_path):
        """Test resampling to multiple timeframes."""
        src = tmp_path / "1h.parquet"
        sample_1h_df.to_parquet(src, index=False)

        outputs = resample_file(src, ["4h", "1d"])
        assert len(outputs) == 2
        for p in outputs:
            assert p.exists()
            df = pd.read_parquet(p)
            assert len(df) > 0


class TestResampleMerge:
    def test_resample_preserves_stored_history_not_present_in_source(self, tmp_path):
        """Regression DV-B23: resampling must not drop older stored bars."""
        source = tmp_path / "1m.parquet"
        old_source = pd.DataFrame(
            {
                "timestamp": pd.date_range("2020-01-01", periods=3, freq="h", tz="UTC"),
                "open": [1.0, 2.0, 3.0],
                "high": [1.1, 2.1, 3.1],
                "low": [0.9, 1.9, 2.9],
                "close": [1.05, 2.05, 3.05],
                "volume": [10, 20, 30],
            }
        )
        old_source.to_parquet(source, index=False)

        outputs = resample_file(source, ["1h"])
        stored = pd.read_parquet(outputs[0])
        assert len(stored) == 3
        assert str(stored["timestamp"].iloc[0])[:10] == "2020-01-01"

        # The source file is later truncated to recent rows only (e.g. re-download).
        new_source = old_source.iloc[[2]].copy()
        new_source.to_parquet(source, index=False)

        outputs = resample_file(source, ["1h"])
        merged = pd.read_parquet(outputs[0])

        assert len(merged) == 3, f"history regressed: {len(merged)} rows"
        assert str(merged["timestamp"].iloc[0])[:10] == "2020-01-01"

    def test_resample_newer_values_win_on_duplicate_timestamps(self, tmp_path):
        source = tmp_path / "1m.parquet"
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2020-01-01", periods=2, freq="h", tz="UTC"),
                "open": [1.0, 2.0],
                "high": [1.1, 2.1],
                "low": [0.9, 1.9],
                "close": [1.05, 2.05],
                "volume": [10, 20],
            }
        ).to_parquet(source, index=False)
        resample_file(source, ["1h"])

        revised = pd.DataFrame(
            {
                "timestamp": pd.date_range("2020-01-01", periods=1, freq="h", tz="UTC"),
                "open": [9.0],
                "high": [9.1],
                "low": [8.9],
                "close": [9.05],
                "volume": [99],
            }
        )
        revised.to_parquet(source, index=False)

        outputs = resample_file(source, ["1h"])
        merged = pd.read_parquet(outputs[0])

        assert len(merged) == 2
        assert float(merged.loc[merged["timestamp"].dt.date == pd.Timestamp("2020-01-01").date(), "close"].iloc[0]) == 9.05

    def test_resample_write_is_atomic(self, tmp_path, monkeypatch):
        source = tmp_path / "1m.parquet"
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2020-01-01", periods=2, freq="h", tz="UTC"),
                "open": [1.0, 2.0],
                "high": [1.1, 2.1],
                "low": [0.9, 1.9],
                "close": [1.05, 2.05],
                "volume": [10, 20],
            }
        ).to_parquet(source, index=False)

        observed: dict[str, Path] = {}

        real_to_parquet = pd.DataFrame.to_parquet

        def spy(self, path, *args, **kwargs):
            observed["path"] = Path(path)
            return real_to_parquet(self, path, *args, **kwargs)

        monkeypatch.setattr(pd.DataFrame, "to_parquet", spy)

        outputs = resample_file(source, ["1h"])

        assert observed["path"] != outputs[0], "resample target must be written via temp file"
        assert len(pd.read_parquet(outputs[0])) == 2
        assert not list(tmp_path.rglob("*.tmp-*")), "no temp files left behind"


class TestResampleWithOrigin:
    """C5: explicit UTC buckets, extra-column retention, loud gap drops."""

    def test_buckets_align_to_utc_day(self, sample_1m_df):
        from data_tools.resample import resample_with_origin

        out = resample_with_origin(sample_1m_df, "1h")
        # 09:00 and 10:00 UTC boundaries, not pandas-default alignment drift.
        assert out["timestamp"].dt.minute.eq(0).all()

    def test_extra_columns_retained(self, sample_1m_df):
        from data_tools.resample import resample_with_origin

        frame = sample_1m_df.copy()
        frame["spread"] = 0.5
        out = resample_with_origin(frame, "5min")
        assert "spread" in out.columns
        assert (out["spread"] == 0.5).all()

    def test_empty_buckets_warn_loudly(self, sample_1m_df, caplog):
        import logging

        import pandas as pd

        from data_tools.resample import resample_with_origin

        # Bars on both sides of a fully missing 10:00 hour: the empty
        # bucket materializes (and warns) instead of vanishing silently.
        morning = sample_1m_df.iloc[:60].copy()
        late = sample_1m_df.iloc[:60].copy()
        late["timestamp"] = pd.date_range("2024-01-02 11:00", periods=60, freq="min")
        gapped = pd.concat([morning, late], ignore_index=True)
        with caplog.at_level(logging.WARNING, logger="data_tools.resample"):
            out = resample_with_origin(gapped, "1h")
        assert any("empty bucket" in record.message for record in caplog.records)
        assert len(out) == 2


class TestBidAskAggregation:
    """C5/CodeRabbit: bid_/ask_ siblings aggregate like true ranges."""

    def test_bid_high_uses_max_not_last(self) -> None:
        import pandas as pd

        from data_tools.resample import resample_with_origin

        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=5, freq="min", tz="UTC"),
                "open": [1.0, 1.1, 1.2, 1.3, 1.4],
                "high": [1.1, 1.2, 1.3, 1.4, 1.5],
                "low": [0.9, 1.0, 1.1, 1.2, 1.3],
                "close": [1.05, 1.15, 1.25, 1.35, 1.45],
                "volume": [10, 20, 30, 40, 50],
                "bid_high": [1.05, 9.99, 1.25, 1.35, 1.45],
                "ask_low": [0.95, 1.05, 0.01, 1.25, 1.35],
                "bid_open": [1.0, 1.1, 1.2, 1.3, 1.4],
                "spread": [0.1, 0.2, 0.3, 0.4, 0.5],
            }
        )
        result = resample_with_origin(frame, "5min")
        assert len(result) == 1
        bar = result.iloc[0]
        assert bar["bid_high"] == 9.99
        assert bar["ask_low"] == 0.01
        assert bar["bid_open"] == 1.0
        assert bar["spread"] == 0.5

    def test_zero_volume_flag_recomputed_from_merged_volume(self) -> None:
        import pandas as pd

        from data_tools.resample import resample_with_origin

        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=5, freq="min", tz="UTC"),
                "open": [1.0, 1.1, 1.2, 1.3, 1.4],
                "high": [1.1, 1.2, 1.3, 1.4, 1.5],
                "low": [0.9, 1.0, 1.1, 1.2, 1.3],
                "close": [1.05, 1.15, 1.25, 1.35, 1.45],
                "volume": [0, 0, 0, 0, 10],
                "zero_volume_bar": [True, True, True, True, False],
            }
        )
        result = resample_with_origin(frame, "5min")
        # Merged volume is 10 (one live sub-bucket) → not zero-volume.
        assert bool(result.iloc[0]["zero_volume_bar"]) is False

        frame["volume"] = 0
        result = resample_with_origin(frame, "5min")
        assert bool(result.iloc[0]["zero_volume_bar"]) is True


class TestEveryBarsTimestampIsItsIntervalStart:
    """`market_calendar.py` states the rule this repo works to; the resampler broke it for weekly.

    > A bar's timestamp is the *start* of the interval it summarises ... a weekly bar stamped on
    > Monday contains every session from Monday to Friday.

    `resample_with_origin` passed only `origin=`, so pandas' **per-frequency** defaults leaked
    through: intraday and daily offsets default to `label='left', closed='left'`, while `1W` is
    W-SUN **right-closed**. So one function labelled daily bars by their first day and weekly bars
    by their last - measured: the bar labelled Sun 2023-01-01 actually covered Mon 2022-12-26
    through Sun 2023-01-01. A daily bar and a weekly bar from the same frame could not be read
    with the same expression.

    `_next_candle_close` anchors weeks on Monday for the same documented reason, so the two
    disagreed by a day at the boundary. Three places stated Monday-and-start; the resampler was
    the only outlier, on all three counts.
    """

    @staticmethod
    def _daily_source(start: str = "2022-12-24", end: str = "2023-01-09"):
        import pandas as pd

        days = pd.date_range(start, end, freq="1D", tz="UTC")
        return pd.DataFrame({
            "timestamp": days,
            "open": 1.0,
            # day-of-month as the value, so a bucket reveals exactly which days it swallowed.
            "high": days.day.astype(float),
            "low": days.day.astype(float),
            "close": 1.0,
            "volume": 1.0,
        })

    def test_a_weekly_bar_is_stamped_monday_and_covers_sunday_to_saturday(self) -> None:
        """A week is [Sunday 00:00, next Sunday 00:00) UTC, stamped with its Monday.

        The Sunday start is what lets the FX week open (Sunday ~22:00 UTC) belong to the week it
        opens; a Sunday-midnight *daily* row therefore joins the following Monday's bar.
        """
        import pandas as pd

        from data_tools.resample import resample_ohlcv

        result = resample_ohlcv(self._daily_source(), "1w")
        result = result[result["volume"] > 0]
        stamps = pd.to_datetime(result["timestamp"])
        assert (stamps.dt.weekday == 0).all(), (
            f"weekly bars are not all Monday-stamped: "
            f"{[str(pd.Timestamp(t).date()) for t in stamps]}"
        )
        # Which days each bar swallows is the whole point, so check it directly rather than
        # through a marker range - a range is meaningless across a month boundary, where one
        # Mon-Sun week legitimately holds markers 26..31 and 1.
        stamps = pd.to_datetime(result["timestamp"])
        for stamp, volume in zip(stamps, result["volume"], strict=True):
            monday = stamp
            days_in_bucket = int(
                (
                    (self._daily_source()["timestamp"] >= monday - pd.Timedelta(days=1))
                    & (self._daily_source()["timestamp"] < monday + pd.Timedelta(days=6))
                ).sum()
            )
            assert int(volume) == days_in_bucket, (
                f"the bar stamped {monday:%Y-%m-%d} holds {int(volume)} rows but its Mon-Sun "
                f"Sun-Sat window contains {days_in_bucket}"
            )

    def test_the_first_bar_starts_on_the_monday_its_data_begins_within(self) -> None:
        """Left-closed means the bar's own stamp is a day the bar contains."""
        import pandas as pd

        from data_tools.resample import resample_ohlcv

        result = resample_ohlcv(self._daily_source("2023-01-03", "2023-01-18"), "1w")
        result = result[result["volume"] > 0]
        first = pd.Timestamp(result["timestamp"].iloc[0])
        assert first.weekday() == 0 and first.date() == pd.Timestamp("2023-01-02").date(), (
            f"the first weekly bar is stamped {first}, expected Mon 2023-01-02 - the Monday on or "
            "before the first source row, because the bucket is left-closed"
        )

    @pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "30m", "1h", "4h", "1d"])
    def test_forcing_left_closed_is_a_no_op_for_every_other_timeframe(self, timeframe) -> None:
        """The change must be confined to weekly, and that is checkable.

        Measured against the pre-change behaviour: all seven of these already defaulted to
        left/left, so pinning it explicitly cannot move them. If one of these starts failing, the
        pin has become a behaviour change rather than a clarification.
        """
        import pandas as pd

        from data_tools.resample import TIMEFRAME_MAP

        minute_index = pd.date_range("2023-01-02", "2023-01-02 05:59", freq="1min", tz="UTC")
        source = pd.DataFrame({
            "timestamp": minute_index, "open": 1.0, "high": 2.0,
            "low": 0.5, "close": 1.5, "volume": 1.0,
        })
        was = source.set_index("timestamp").resample(TIMEFRAME_MAP[timeframe]).agg(
            {"open": "first", "volume": "sum"}
        )
        now = source.set_index("timestamp").resample(
            TIMEFRAME_MAP[timeframe], label="left", closed="left"
        ).agg({"open": "first", "volume": "sum"})
        was, now = was[was["volume"] > 0], now[now["volume"] > 0]
        assert was.index.equals(now.index), (
            f"{timeframe} buckets moved when label/closed were pinned; the weekly fix was supposed "
            "to touch nothing else"
        )

    def test_the_live_weekly_close_agrees_with_a_monday_stamp(self) -> None:
        """The two halves have to agree, or the divergence just moves.

        `_next_candle_close` anchors weeks on Monday 00:00 UTC. Given the Monday stamp the
        resampler now emits, the bar must close exactly seven days later - the next Monday, which
        is where a Mon-Sun week ends.
        """
        from datetime import datetime

        from live_runner.signal_sources.execution_timing import _next_candle_close

        week = 7 * 86400
        for stamp in ("2022-12-26", "2023-01-02", "2023-01-09"):
            reference = datetime.fromisoformat(f"{stamp}T00:00:00+00:00")
            close = _next_candle_close(reference, week, timeframe="1w")
            assert close.weekday() == 0, f"a weekly close landed on {close:%A}, not Monday"
            assert (close - reference).days == 7, (
                f"a bar stamped {stamp} closes {close}, which is not seven days later"
            )

    def test_volume_and_ohlc_are_conserved_across_the_new_week_buckets(self) -> None:
        """Moving which days group together must not lose or invent anything."""
        import numpy as np

        from data_tools.resample import resample_ohlcv

        source = self._daily_source()
        result = resample_ohlcv(source, "1w")
        result = result[result["volume"] > 0]
        assert result["volume"].sum() == pytest.approx(source["volume"].sum()), (
            "weekly volume no longer sums to the source volume"
        )
        assert result["high"].max() == pytest.approx(source["high"].max())
        assert result["low"].min() == pytest.approx(source["low"].min())
        # Independent reference: floor each row to the Monday of its week, then group. Sunday
        # belongs to the week that follows it, so it is moved forward a day first. Note the
        # floor has to be `timestamp - weekday days`; `pd.offsets.Week(weekday=0)` rolls *forward*
        # to the next Monday, which silently produced a third grouping and a wrong bar count.
        moved = source["timestamp"] + pd.Timedelta(days=1)
        monday_of = moved - pd.to_timedelta(moved.dt.weekday, unit="D")
        expected = source.assign(monday=monday_of).groupby("monday").size()
        assert len(result) == len(expected), (
            f"{len(result)} weekly bars from resample_ohlcv but {len(expected)} from an "
            "independent Monday-anchored groupby"
        )
        assert np.array_equal(result["volume"].to_numpy(), expected.to_numpy())


    def test_the_zero_volume_else_branch_is_unreachable_and_why(self) -> None:
        """Records why a mutation of the `else` branch cannot be killed, rather than hiding it.

        `resample_with_origin` has a fallback for the case where `resampled` has no `volume`
        column, resampling the `zero_volume_bar` flag separately. That branch **cannot run**:
        `_aggregation_for` always includes `"volume"` in the aggregation dict, so a source frame
        without a volume column makes `.agg` raise before the branch is reached. Measured:

            KeyError: Label(s) ['volume'] do not exist

        So the honest position is not "the fallback is covered" - it is that the code is carrying a
        branch nothing can reach. The separate resample call there has been given the same
        `label="left", closed="left"` as the main one anyway, so that if the branch ever does
        become reachable it cannot silently stop aligning with Monday-labelled bars. This test
        pins the unreachability so that nobody later "fixes" the branch believing it is live.
        """
        import pandas as pd

        from data_tools.resample import _aggregation_for

        agg = _aggregation_for(["open", "high", "low", "close", "zero_volume_bar"])
        assert "volume" in agg, (
            "_aggregation_for no longer requests `volume` unconditionally; if it has become "
            "conditional then the `else` branch in resample_with_origin became reachable and "
            "needs a real test rather than this note"
        )

        days = pd.date_range("2022-12-26", "2023-01-09", freq="1D", tz="UTC")
        without_volume = pd.DataFrame({
            "timestamp": days, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
            "zero_volume_bar": [i % 2 == 0 for i in range(len(days))],
        })
        with pytest.raises(KeyError):
            from data_tools.resample import resample_with_origin

            resample_with_origin(without_volume, "W-MON")


class TestOriginIsOnlyPassedWherePandasHonoursIt:
    """`origin` is a tick-only parameter, and the docstring claimed otherwise.

    The docstring used to say "Buckets align to UTC via `origin`", implying it governed every rule.
    Measured: it shifts buckets for `h`/`m`/`s`/`ms`/`us`/`ns` and is **silently ignored for
    everything else** — `D`, `W`, `M` and the fixed frequencies — while emitting a `RuntimeWarning`
    on every call. For half this module's table the parameter did nothing except make noise.

    So it is now passed only where it has effect. A warning that fires on every daily and weekly
    resample is a warning an operator learns to skip, which is the trade this module already avoids
    elsewhere by *counting* dropped buckets rather than passing over them in silence.
    """

    @staticmethod
    def _ragged_source():
        """Deliberately not starting on a bucket boundary, so an effective origin would show."""
        import numpy as np
        import pandas as pd

        index = pd.date_range("2023-01-02 05:17", "2023-01-16 22:40", freq="17min", tz="UTC")
        rng = np.random.default_rng(3)
        return pd.DataFrame({
            "timestamp": index, "open": 1.0, "high": 2.0 + rng.random(len(index)),
            "low": 0.5, "close": 1.5, "volume": 1.0,
        })

    @pytest.mark.parametrize("timeframe", list(TIMEFRAME_MAP))
    def test_origin_is_passed_exactly_for_the_tick_like_rules(self, timeframe) -> None:
        """Asserted on the rule, not on the absence of a warning.

        An earlier version of this test caught warnings with `simplefilter("always")` and
        `resetwarnings()` and still could not see the one it was looking for - pandas emits it
        somewhere `catch_warnings(record=True)` does not reliably capture, even though pytest's own
        warning summary showed it. A test that cannot see the failure is not a test.

        Which rules *receive* `origin` is a pure function of the rule, so that is what is asserted.
        """
        import pandas as pd

        from data_tools.resample import _resample_kwargs

        kwargs = _resample_kwargs(TIMEFRAME_MAP[timeframe], "start_day")
        is_tick = isinstance(
            pd.tseries.frequencies.to_offset(TIMEFRAME_MAP[timeframe]), pd.tseries.offsets.Tick
        )
        assert ("origin" in kwargs) is is_tick, (
            f"{timeframe}: origin {'was' if 'origin' in kwargs else 'was not'} passed but the rule "
            f"{'is' if is_tick else 'is not'} tick-like; pandas ignores `origin` for "
            "non-tick rules and warns, so passing it there is pure noise"
        )

    @pytest.mark.parametrize("timeframe", list(TIMEFRAME_MAP))
    def test_label_and_closed_are_always_pinned(self, timeframe) -> None:
        """The other half of the kwargs, and the one that actually changes bars."""
        from data_tools.resample import _resample_kwargs

        kwargs = _resample_kwargs(TIMEFRAME_MAP[timeframe], "start_day")
        assert kwargs["label"] == "left" and kwargs["closed"] == "left", (
            f"{timeframe}: pandas' per-frequency defaults leaked back in, which is how weekly bars "
            "came to be labelled by their interval end"
        )

    def test_omitting_origin_changes_nothing_for_any_rule(self) -> None:
        """The load-bearing claim: this is a warning fix, not a behaviour change.

        Compared bucket-for-bucket - index *and* aggregate - with `origin` supplied and omitted. If
        a future pandas starts honouring `origin` for daily or weekly, this fails rather than the
        change silently altering every daily and weekly bar.
        """
        import numpy as np

        source = self._ragged_source().set_index("timestamp")
        for key, rule in TIMEFRAME_MAP.items():
            def buckets(**extra):
                resampled = source.resample(rule, **extra).agg(  # noqa: B023 - buckets() is only called within this iteration
                    {"high": "max", "low": "min", "volume": "sum"}
                )
                return resampled[resampled["volume"] > 0]

            with_origin = buckets(origin="start_day", label="left", closed="left")
            without = buckets(label="left", closed="left")
            assert with_origin.index.equals(without.index), f"{key}: bucket boundaries differ"
            for column in ("high", "low"):
                assert np.array_equal(
                    with_origin[column].to_numpy(), without[column].to_numpy()
                ), f"{key}: {column} differs"

    def test_origin_still_shifts_an_hourly_series_where_it_should(self) -> None:
        """Guards the fix from over-correcting into "never pass origin"."""
        from data_tools.resample import resample_with_origin

        source = self._ragged_source()
        by_day = resample_with_origin(source, "1h", origin="start_day")
        by_start = resample_with_origin(source, "1h", origin="start")
        first_day = pd.Timestamp(by_day["timestamp"].iloc[0])
        first_start = pd.Timestamp(by_start["timestamp"].iloc[0])
        assert first_day.strftime("%H:%M") == "05:00", first_day
        assert first_start.strftime("%H:%M") == "05:17", first_start
        assert first_day != first_start, (
            "`origin` no longer shifts an hourly series at all, so it is being dropped everywhere "
            "rather than only where pandas ignores it"
        )

    def test_daily_and_weekly_ignore_origin_which_is_why_it_is_not_passed(self) -> None:
        """The measurement behind the change, recorded so the docstring cannot drift from it."""
        import pandas as pd

        from data_tools.resample import resample_with_origin

        source = self._ragged_source()
        for rule in ("1D", "1W"):
            by_day = resample_with_origin(source, rule, origin="start_day")
            by_epoch = resample_with_origin(source, rule, origin="epoch")
            assert by_day["timestamp"].tolist() == by_epoch["timestamp"].tolist(), (
                f"{rule} now honours `origin`, so passing it conditionally is wrong - "
                "either pass it everywhere or stop claiming pandas ignores it"
            )
        assert isinstance(
            pd.tseries.frequencies.to_offset("1D"), pd.tseries.offsets.Tick
        ) is False


def test_unsorted_input_is_sorted_before_bucketing():
    """A downloader that appends a late batch can hand the resampler rows out of
    order. `resample_with_origin` sorts the index first; without that, pandas
    buckets by position and the resulting bars are wrong rather than merely
    reordered."""
    from data_tools.resample import resample_with_origin

    stamps = pd.to_datetime(
        [
            "2024-01-02 09:03",
            "2024-01-02 09:00",
            "2024-01-02 09:04",
            "2024-01-02 09:01",
            "2024-01-02 09:02",
        ],
        utc=True,
    )
    frame = pd.DataFrame(
        {
            "timestamp": stamps,
            "open": [3.0, 0.0, 4.0, 1.0, 2.0],
            "high": [3.0, 0.0, 4.0, 1.0, 2.0],
            "low": [3.0, 0.0, 4.0, 1.0, 2.0],
            "close": [3.0, 0.0, 4.0, 1.0, 2.0],
            "volume": [1.0] * 5,
        }
    )

    out = resample_with_origin(frame, "5min")

    assert list(out["timestamp"]) == [pd.Timestamp("2024-01-02 09:00", tz="UTC")]
    assert out["open"].iloc[0] == 0.0, "the 09:00 bar must open the bucket, not the 09:05 row"
    assert out["close"].iloc[0] == 4.0, "the 09:04 bar must close the bucket"
    assert out["high"].iloc[0] == 4.0
    assert out["volume"].iloc[0] == 5.0


class TestWeeklyBucketsFollowTheVenueWeek:
    """Weekly bars: Sunday-evening FX/CFD opens belong to the week they open, equities unchanged."""

    @staticmethod
    def _hourly(start: str, end: str, tz="UTC"):
        stamps = pd.date_range(start, end, freq="1h", tz=tz)
        return pd.DataFrame({
            "timestamp": stamps,
            "open": range(len(stamps)),
            "high": [float(i) + 1 for i in range(len(stamps))],
            "low": [float(i) - 1 for i in range(len(stamps))],
            "close": [float(i) + 0.5 for i in range(len(stamps))],
            "volume": 1.0,
        })

    def test_fx_sunday_open_is_in_the_week_it_opens(self) -> None:
        # Sun 2024-01-07 22:00 UTC is the first bar of the week of Mon 2024-01-08.
        source = self._hourly("2024-01-05 20:00", "2024-01-12 21:00")
        # Drop the weekend closure like a real FX feed: Fri 22:00 -> Sun 22:00.
        stamps = source["timestamp"]
        closed = (stamps >= "2024-01-05 22:00+00:00") & (stamps < "2024-01-07 22:00+00:00")
        source = source[~closed].reset_index(drop=True)

        weekly = resample_ohlcv(source, "1w")

        assert [str(t.date()) for t in weekly["timestamp"]] == ["2024-01-01", "2024-01-08"]
        new_week = weekly.iloc[1]
        sunday_open = source[source["timestamp"] == pd.Timestamp("2024-01-07 22:00", tz="UTC")]
        assert new_week["open"] == sunday_open["open"].iloc[0]
        first_week = weekly.iloc[0]
        assert first_week["volume"] == (source["timestamp"] < "2024-01-07 22:00+00:00").sum()
        assert new_week["volume"] == (source["timestamp"] >= "2024-01-07 22:00+00:00").sum()
        assert weekly["volume"].sum() == len(source)

    def test_friday_post_close_bar_on_saturday_utc_stays_in_its_week(self) -> None:
        source = self._hourly("2024-01-08 00:00", "2024-01-13 00:00")  # Mon .. Sat 00:00
        weekly = resample_ohlcv(source, "1w")
        assert len(weekly) == 1
        assert pd.Timestamp(weekly["timestamp"].iloc[0]) == pd.Timestamp("2024-01-08", tz="UTC")
        assert weekly["volume"].iloc[0] == len(source)

    def test_us_equity_monday_to_friday_weeks_are_unchanged(self) -> None:
        days = pd.bdate_range("2024-01-01", "2024-01-19", tz="UTC")
        source = pd.DataFrame({
            "timestamp": days + pd.Timedelta(hours=14, minutes=30),
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
        })
        weekly = resample_ohlcv(source, "1w")
        assert [str(t.date()) for t in weekly["timestamp"]] == [
            "2024-01-01", "2024-01-08", "2024-01-15",
        ]
        assert weekly["volume"].tolist() == [5.0, 5.0, 5.0]

    def test_holiday_shortened_week_keeps_its_own_bar(self) -> None:
        # MLK Monday 2024-01-15 is closed: four sessions, still one Monday-stamped bar.
        days = pd.bdate_range("2024-01-08", "2024-01-26", tz="UTC")
        days = days[days != pd.Timestamp("2024-01-15", tz="UTC")]
        source = pd.DataFrame({
            "timestamp": days + pd.Timedelta(hours=14, minutes=30),
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
        })
        weekly = resample_ohlcv(source, "1w")
        assert [str(t.date()) for t in weekly["timestamp"]] == [
            "2024-01-08", "2024-01-15", "2024-01-22",
        ]
        assert weekly["volume"].tolist() == [5.0, 4.0, 5.0]

    def test_a_whole_empty_week_is_dropped_not_invented(self) -> None:
        days = pd.to_datetime(["2024-01-02", "2024-01-23"], utc=True)
        source = pd.DataFrame({
            "timestamp": days, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
        })
        weekly = resample_ohlcv(source, "1w")
        assert [str(t.date()) for t in weekly["timestamp"]] == ["2024-01-01", "2024-01-22"]

    def test_monday_label_trails_the_sunday_open_by_only_the_open_offset(self) -> None:
        source = self._hourly("2024-01-07 22:00", "2024-01-12 21:00")
        weekly = resample_ohlcv(source, "1w")
        label = pd.Timestamp(weekly["timestamp"].iloc[0])
        first_bar = source["timestamp"].min()
        assert label.weekday() == 0
        assert label - first_bar == pd.Timedelta(hours=2)
        assert label <= source["timestamp"].max()


class TestMergeRefusesLegacyWeeklyLabels:
    @staticmethod
    def _bars(stamps):
        return pd.DataFrame({
            "timestamp": pd.to_datetime(stamps, utc=True),
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
        })

    def test_old_sunday_labelled_file_is_not_merged_into(self, tmp_path) -> None:
        from data_tools.resample import LegacyWeeklyLabelError, merge_resampled_history

        stored = tmp_path / "1w.parquet"
        self._bars(["2024-01-07", "2024-01-14"]).to_parquet(stored, index=False)  # Sundays
        fresh = self._bars(["2024-01-08", "2024-01-15"])  # the same weeks, Monday-stamped

        with pytest.raises(LegacyWeeklyLabelError, match="Monday 00:00 UTC"):
            merge_resampled_history(stored, fresh, "1w")

    def test_resample_file_fails_closed_and_keeps_stored_file(self, tmp_path) -> None:
        from data_tools.resample import LegacyWeeklyLabelError

        source = tmp_path / "1d.parquet"
        days = pd.bdate_range("2024-01-01", "2024-01-12", tz="UTC")
        pd.DataFrame({
            "timestamp": days, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
        }).to_parquet(source, index=False)
        stored = tmp_path / "1w.parquet"
        self._bars(["2024-01-07"]).to_parquet(stored, index=False)
        before = stored.read_bytes()

        with pytest.raises(LegacyWeeklyLabelError):
            resample_file(source, ["1w"])

        assert stored.read_bytes() == before

    def test_monday_labelled_file_merges_without_duplicate_weeks(self, tmp_path) -> None:
        from data_tools.resample import merge_resampled_history

        stored = tmp_path / "1w.parquet"
        self._bars(["2024-01-01", "2024-01-08"]).to_parquet(stored, index=False)
        merged = merge_resampled_history(stored, self._bars(["2024-01-08", "2024-01-15"]), "1w")
        assert [str(t.date()) for t in merged["timestamp"]] == [
            "2024-01-01", "2024-01-08", "2024-01-15",
        ]

    def test_non_weekly_merge_is_not_affected(self, tmp_path) -> None:
        from data_tools.resample import merge_resampled_history

        stored = tmp_path / "1d.parquet"
        self._bars(["2024-01-07"]).to_parquet(stored, index=False)
        merged = merge_resampled_history(stored, self._bars(["2024-01-08"]), "1d")
        assert len(merged) == 2

    def test_midweek_monday_time_of_day_is_also_legacy(self, tmp_path) -> None:
        from data_tools.resample import LegacyWeeklyLabelError, assert_stored_weekly_file_current

        stored = tmp_path / "1w.parquet"
        self._bars(["2024-01-08 22:00"]).to_parquet(stored, index=False)
        with pytest.raises(LegacyWeeklyLabelError):
            assert_stored_weekly_file_current(stored)
        assert_stored_weekly_file_current(tmp_path / "missing.parquet")  # no file, no error
