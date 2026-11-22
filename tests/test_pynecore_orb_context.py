"""Data-independent regression tests for the ``pynecore_orb`` context helper.

``strategies/pynecore_orb/pynecore_orb_context.py`` is the pure-pandas mirror of
the ``pynecore_orb`` script's ET-session state machine: it replays the
opening-range accumulation, the one-trade-per-day gate, the HWM/LWM trailing
stop and the forced exit over bar closes so the adapter can annotate trades.

Those mirror cases used to live in ``tests/strategies/test_pynecore_orb.py``,
whose whole module sits behind the US500/30m data gate
(``tests/strategies/conftest.py``). With no local dataset — CI included — every
test in that file skips, so the mirror was never executed there and a change to
the state machine it mirrors regressed unnoticed. This module carries the same
cases plus a SHORT-side case against synthetic 30m frames, so the state machine
is covered on every CI run: no dataset, no ``pyne`` subprocess, no broker.

The frames below are hand-built, not sampled: every number a test asserts on is
derived in the comment next to its bar.
"""

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTEXT_PATH = PROJECT_ROOT / "strategies" / "pynecore_orb" / "pynecore_orb_context.py"


def _load_context_module():
    """Load the helper by path: ``pynecore_orb`` is a slug folder, not a package."""
    spec = importlib.util.spec_from_file_location("pynecore_orb_context_under_test", CONTEXT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame(rows: list[tuple]) -> pd.DataFrame:
    """Build a frame from ``(utc_timestamp, open, high, low, close)`` rows."""
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    df["volume"] = 1.0
    return df


def _long_breakout_frame() -> pd.DataFrame:
    """Two ET days: day 1 opens a long and trails out, day 2 does nothing.

    Day 1: OR 100/98 (range 2, buffer 0.2) -> long level 100.4, short level
    97.6. The 10:00 ET close (101.0) breaks the long level, so the HWM starts at
    101.0, rises to 103.0 and the 11:00 ET close (101.5) is below the 1%
    reversion level 103.0 * 0.99 = 101.97 -> trailing-stop exit. The 12:00 ET
    close (90.0) sits below the short level, which must NOT open a second trade
    on the same day. Day 2 never leaves its own opening range.

    The 09:00 ET bar carries an extreme 150/50 range on purpose: a range
    accumulation that stopped respecting the 09:30 window would swallow it and
    widen the OR past every breakout level, so these frames fail loudly.
    """
    return _frame(
        [
            ("2024-01-02 14:00:00+00:00", 100.0, 150.0, 50.0, 100.0),  # 09:00 ET, before the OR
            ("2024-01-02 14:30:00+00:00", 100.0, 100.0, 98.0, 99.0),  # 09:30 ET, OR bar
            ("2024-01-02 15:00:00+00:00", 99.0, 101.0, 99.0, 101.0),  # 10:00 ET -> LONG
            ("2024-01-02 15:30:00+00:00", 101.0, 103.0, 100.5, 103.0),  # HWM 103.0
            ("2024-01-02 16:00:00+00:00", 103.0, 103.0, 101.0, 101.5),  # < 101.97 -> trail exit
            ("2024-01-02 16:30:00+00:00", 101.5, 102.0, 101.0, 90.0),  # below 97.6, no 2nd trade
            ("2024-01-03 14:30:00+00:00", 100.0, 100.1, 99.9, 100.0),  # day 2, OR bar
            ("2024-01-03 15:00:00+00:00", 100.0, 100.1, 99.9, 100.0),  # day 2, no breakout
        ]
    )


def _short_breakout_frame() -> pd.DataFrame:
    """The mirror image of the long case, on the SHORT side of the state machine.

    Day 1: same OR 100/98 -> long level 100.4, short level 97.6. The 10:00 ET
    close (97.0) breaks the short level, so the LWM starts at 97.0, falls to
    96.0 and the 11:00 ET close (97.5) is above the 1% reversion level
    96.0 * 1.01 = 96.96 -> trailing-stop exit. The 11:30 ET close (101.0)
    breaks the OPPOSITE (long) level, which must not re-enter anything - it is
    there so the one-trade-per-day gate is exercised on the short side too: a
    close that stayed inside the old range would leave nothing for a cleared
    `traded` flag to open, and that half of the pin would be vacuous.
    Day 2: a fresh OR (100.5/99.5, range 1) whose 10:00 ET close (101.0)
    breaks the new long level 100.7, proving the traded flag and the OR reset
    with the ET day.

    The 09:00 ET bar is extreme for the same reason as in the long frame.
    """
    return _frame(
        [
            ("2024-01-02 14:00:00+00:00", 100.0, 150.0, 50.0, 100.0),  # 09:00 ET, before the OR
            ("2024-01-02 14:30:00+00:00", 100.0, 100.0, 98.0, 99.0),  # 09:30 ET, OR bar
            ("2024-01-02 15:00:00+00:00", 99.0, 97.2, 96.8, 97.0),  # 10:00 ET -> SHORT
            ("2024-01-02 15:30:00+00:00", 96.5, 96.6, 95.9, 96.0),  # LWM 96.0
            ("2024-01-02 16:00:00+00:00", 96.2, 97.6, 96.0, 97.5),  # > 96.96 -> trail exit
            ("2024-01-02 16:30:00+00:00", 97.5, 101.0, 97.4, 101.0),  # > 100.4, no 2nd trade
            ("2024-01-03 14:30:00+00:00", 100.0, 100.5, 99.5, 100.0),  # day 2, fresh OR
            ("2024-01-03 15:00:00+00:00", 100.0, 101.0, 99.8, 101.0),  # day 2 -> LONG
        ]
    )


class TestOrbContextMirror:
    """The pandas context helper must mirror the script's ET-session state machine."""

    def test_breakout_levels_and_long_entry(self):
        ctx = _load_context_module().build_trade_context(_long_breakout_frame(), {})
        assert ctx["breakout_long"].iloc[2] == pytest.approx(100.4)
        assert ctx["breakout_short"].iloc[2] == pytest.approx(97.6)
        assert ctx["direction"].iloc[2] == "LONG"

    def test_breakout_levels_and_short_entry(self):
        """SHORT side: the same OR must break out to the downside as well."""
        ctx = _load_context_module().build_trade_context(_short_breakout_frame(), {})
        assert ctx["breakout_long"].iloc[1] == pytest.approx(100.4)
        assert ctx["breakout_short"].iloc[1] == pytest.approx(97.6)
        # No signal while the opening range is still being accumulated.
        assert pd.isna(ctx["direction"].iloc[1])
        assert ctx["direction"].iloc[2] == "SHORT"
        assert ctx["OR_High"].iloc[2] == pytest.approx(100.0)
        assert ctx["OR_Low"].iloc[2] == pytest.approx(98.0)

    def test_short_trailing_stop_exit(self):
        ctx = _load_context_module().build_trade_context(_short_breakout_frame(), {})
        assert ctx["exit_reason"].iloc[4] == "trailing_stop"
        assert ctx["trailing_stop_at_exit"].iloc[4] == pytest.approx(96.0 * 1.01)

    def test_trailing_stop_exit(self):
        ctx = _load_context_module().build_trade_context(_long_breakout_frame(), {})
        assert ctx["exit_reason"].iloc[4] == "trailing_stop"
        assert ctx["trailing_stop_at_exit"].iloc[4] == pytest.approx(103.0 * 0.99)

    def test_no_second_trade_after_the_first_one_closed(self):
        """One trade per ET day: a bar that breaks the opposite level later the
        same day must not open a second position."""
        for frame, direction in (
            (_long_breakout_frame(), "LONG"),
            (_short_breakout_frame(), "SHORT"),
        ):
            ctx = _load_context_module().build_trade_context(frame, {})
            assert set(ctx["direction"].iloc[2:6].dropna()) == {direction}

    def test_opening_range_and_traded_flag_reset_with_the_et_day(self):
        """Day 2 gets a fresh OR and a fresh trade budget."""
        ctx = _load_context_module().build_trade_context(_short_breakout_frame(), {})
        assert ctx["OR_High"].iloc[6] == pytest.approx(100.5)
        assert ctx["OR_Low"].iloc[6] == pytest.approx(99.5)
        assert ctx["breakout_long"].iloc[6] == pytest.approx(100.7)
        assert ctx["direction"].iloc[7] == "LONG"
        # The previous day's SHORT marker must not survive the session reset.
        assert pd.isna(ctx["direction"].iloc[6])
        assert pd.isna(ctx["exit_reason"].iloc[6])

    def test_protective_stop_series_matches_reversion_multiple(self):
        ctx = _load_context_module().build_trade_context(_short_breakout_frame(), {})
        close = _short_breakout_frame()["close"].astype(float)
        assert ctx["sl_price_long"].tolist() == pytest.approx((close * 0.99).tolist())
        assert ctx["sl_price_short"].tolist() == pytest.approx((close * 1.01).tolist())

    def test_forced_time_exit_books_the_open_position(self):
        # The 15:45 ET bar flattens an open position and stamps the exit.
        frame = _frame(
            [
                ("2024-01-02 14:30:00+00:00", 100.0, 100.0, 98.0, 99.0),  # OR
                ("2024-01-02 15:00:00+00:00", 99.0, 101.0, 99.0, 101.0),  # 10:00 ET -> LONG
                ("2024-01-02 20:15:00+00:00", 101.0, 105.0, 101.0, 105.0),  # 15:15 ET
                ("2024-01-02 20:45:00+00:00", 105.0, 105.0, 104.0, 105.0),  # 15:45 ET
            ]
        )
        ctx = _load_context_module().build_trade_context(frame, {})
        assert ctx["direction"].iloc[1] == "LONG"
        assert ctx["exit_reason"].iloc[3] == "time_exit"

    def test_no_entries_after_the_forced_exit(self):
        # A real long is open when the 15:45 ET bar flattens it, and the
        # breakout-shaped close that follows (below the short level, so a fresh
        # entry would show up as its own direction marker rather than the
        # carried one) must not trade either.
        frame = _frame(
            [
                ("2024-01-02 14:30:00+00:00", 100.0, 100.0, 98.0, 99.0),  # OR
                ("2024-01-02 15:00:00+00:00", 99.0, 101.0, 99.0, 101.0),  # 10:00 ET -> LONG
                ("2024-01-02 20:45:00+00:00", 101.0, 105.0, 101.0, 105.0),  # 15:45 ET
                ("2024-01-02 21:00:00+00:00", 99.0, 101.0, 97.0, 97.0),  # 16:00 ET, < 97.6
            ]
        )
        ctx = _load_context_module().build_trade_context(frame, {})
        assert ctx["direction"].iloc[1] == "LONG"
        assert ctx["exit_reason"].iloc[2] == "time_exit"
        # Exactly one direction marker for the session: the carried first
        # entry. A second one means the forced exit left the gate open.
        assert set(ctx["direction"].iloc[1:4].dropna()) == {"LONG"}
        # No exit reason other than the forced exit's own (carried) one.
        assert set(ctx["exit_reason"].iloc[2:4].dropna()) == {"time_exit"}

    def test_flat_opening_range_trades_nothing(self):
        # A zero-width opening range has no level to break: no entry, and the
        # breakout columns stay empty instead of collapsing onto the close.
        frame = _frame(
            [
                ("2024-01-02 14:30:00+00:00", 100.0, 100.0, 100.0, 100.0),  # flat OR
                ("2024-01-02 15:00:00+00:00", 100.0, 100.5, 99.5, 100.5),  # 10:00 ET
                ("2024-01-02 15:30:00+00:00", 100.5, 101.0, 100.4, 101.0),  # 10:30 ET
            ]
        )
        ctx = _load_context_module().build_trade_context(frame, {})
        assert ctx["OR_High"].iloc[0] == pytest.approx(100.0)
        assert ctx["OR_Low"].iloc[0] == pytest.approx(100.0)
        assert ctx["OR_Range"].iloc[2] == pytest.approx(0.0)
        assert pd.isna(ctx["breakout_long"].iloc[2])
        assert pd.isna(ctx["breakout_short"].iloc[2])
        assert pd.isna(ctx["direction"].iloc[2])

    def test_opening_range_accumulates_across_bars_of_a_finer_timeframe(self):
        # 30m bars put exactly one bar inside the 30-minute window, so the
        # high/low update branch is dead code for this timeframe but live for
        # the finer bars the script also allows. The mirror must take the
        # running high and the running low, not the latest bar's extremes.
        frame = _frame(
            [
                ("2024-01-02 14:15:00+00:00", 100.0, 100.5, 99.5, 100.0),  # 09:15 ET, before OR
                ("2024-01-02 14:30:00+00:00", 100.0, 100.0, 99.0, 99.5),  # 09:30 ET, OR opens
                ("2024-01-02 14:45:00+00:00", 99.5, 99.5, 98.0, 99.0),  # 09:45 ET, low extends
                ("2024-01-02 15:00:00+00:00", 99.0, 101.0, 99.0, 101.0),  # 10:00 ET -> LONG
                ("2024-01-02 15:15:00+00:00", 101.0, 103.0, 100.5, 103.0),  # HWM 103.0
                ("2024-01-02 15:30:00+00:00", 103.0, 103.0, 101.0, 101.5),  # trail exit
            ]
        )
        ctx = _load_context_module().build_trade_context(frame, {})
        assert ctx["OR_High"].iloc[2] == pytest.approx(100.0)
        assert ctx["OR_Low"].iloc[2] == pytest.approx(98.0)
        assert ctx["breakout_long"].iloc[2] == pytest.approx(100.4)
        assert ctx["direction"].iloc[3] == "LONG"
        assert ctx["exit_reason"].iloc[5] == "trailing_stop"

    def test_signal_plus_one_carry_stops_at_session_boundary(self):
        # B-M5: a signal on the LAST bar of day 1 must not be carried onto the
        # first bar of day 2 (different OR range, different session state).
        frame = _frame(
            [
                ("2024-01-02 14:30:00+00:00", 100.0, 100.0, 98.0, 99.0),  # OR day 1
                ("2024-01-02 15:00:00+00:00", 99.0, 101.0, 99.0, 101.0),  # LONG signal
                ("2024-01-03 14:30:00+00:00", 100.0, 100.1, 99.9, 100.0),  # day 2
                ("2024-01-03 15:00:00+00:00", 100.0, 100.1, 99.9, 100.0),
            ]
        )
        ctx = _load_context_module().build_trade_context(frame, {})
        assert ctx["direction"].iloc[1] == "LONG"
        assert pd.isna(ctx["direction"].iloc[2]), (
            "the signal carry must not cross the session boundary"
        )
        assert pd.isna(ctx["exit_reason"].iloc[2])

    def test_overnight_bars_do_not_pollute_range(self):
        frame = _long_breakout_frame().copy()
        frame.loc[0, "high"] = 150.0  # 09:00 ET bar, outside the opening range
        frame.loc[2, "high"] = 150.0  # 10:00 ET bar, after the opening range
        ctx = _load_context_module().build_trade_context(frame, {})
        assert ctx["OR_High"].iloc[2] == pytest.approx(100.0)
        assert ctx["OR_Low"].iloc[2] == pytest.approx(98.0)

    def test_day_change_flattens_open_position_without_forced_exit_bar(self):
        # B-H1 mirror: the position is still open when day 1 ends (no 15:45 ET
        # bar, so no time_exit) and day 2 begins. The helper must book the exit
        # on the last bar of day 1 as day_reset_flatten instead of silently
        # dropping the open position (script side: close).
        frame = _frame(
            [
                ("2024-01-02 14:30:00+00:00", 100.0, 100.0, 98.0, 99.0),  # OR day 1
                ("2024-01-02 15:00:00+00:00", 99.0, 101.0, 99.0, 101.0),  # LONG signal
                ("2024-01-02 15:30:00+00:00", 101.0, 103.0, 100.5, 103.0),  # open, HWM
                ("2024-01-03 14:30:00+00:00", 100.0, 100.1, 99.9, 100.0),  # day 2
                ("2024-01-03 15:00:00+00:00", 100.0, 100.1, 99.9, 100.0),
            ]
        )
        ctx = _load_context_module().build_trade_context(frame, {})
        assert ctx["direction"].iloc[1] == "LONG"
        assert ctx["exit_reason"].iloc[2] == "day_reset_flatten"


class TestOrbContextInputContract:
    """The helper refuses a frame it cannot derive ET sessions from."""

    def test_missing_session_clock_is_refused(self):
        frame = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]})
        build_trade_context = _load_context_module().build_trade_context
        with pytest.raises(ValueError, match="timestamp"):
            build_trade_context(frame, {})

    def test_derives_sessions_from_a_datetime_index(self):
        # The same state machine must come out of a tz-aware index, not only of
        # a 'timestamp' column (no constant-day fallback that trades nothing).
        index = pd.date_range("2024-01-02 14:30", periods=4, freq="30min", tz="UTC")
        frame = pd.DataFrame(
            {
                "open": [100.0, 99.0, 101.0, 103.0],
                "high": [100.0, 101.0, 103.0, 103.0],
                "low": [98.0, 99.0, 100.5, 101.0],
                "close": [99.0, 101.0, 103.0, 101.5],
            },
            index=index,
        )
        ctx = _load_context_module().build_trade_context(frame, {})
        assert ctx["OR_High"].iloc[0] == pytest.approx(100.0)
        assert ctx["OR_Low"].iloc[0] == pytest.approx(98.0)
        assert ctx["breakout_long"].iloc[0] == pytest.approx(100.4)
        assert ctx["direction"].iloc[1] == "LONG"
        assert ctx["exit_reason"].iloc[3] == "trailing_stop"
        assert ctx["trailing_stop_at_exit"].iloc[3] == pytest.approx(103.0 * 0.99)
        assert list(ctx["OR_Range"].index) == list(index)
