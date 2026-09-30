"""The pandas context helper must mirror the strategy script's ET-session logic.

These five tests live here rather than in `test_pynecore_orb.py` because of *where* they
would otherwise be, not what they do.

`tests/strategies/conftest.py` installs an `autouse`, module-scoped fixture that skips an
entire module when its `TEST_SYMBOL`/`TEST_TIMEFRAME` dataset is missing. The sibling module
declares `TEST_SYMBOL = "US500"` / `TEST_TIMEFRAME = "30m"`, and this dataset is absent in
most environments - so **all eleven of its tests skip there**, including these five, which
build a hard-coded eight-row frame and need no market data whatsoever.

Measured: with this class in that module, the file reported `11 skipped, exit 0` on a
machine with no US500/30m data, and the pass count looked entirely healthy. Five guards
that never ran.

So they declare no `TEST_SYMBOL`/`TEST_TIMEFRAME`, which is what opts a module out of the
data gate - the same mechanism `test_pynecore_orb_interval_guard.py` and
`test_strategy_source_contracts.py` already use.

What they need from the sibling is small and is reproduced here: the loader, which imports
`pynecore_orb_context.py` by path. That module imports only pandas, so it loads fine with
no dataset present - which is the whole point.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_PATH = str(PROJECT_ROOT / "strategies" / "pynecore_orb" / "pynecore_orb_context.py")


def _load_context_module():
    """Load the strategy's pandas context helper by path.

    A missing or unimportable context module is a hard failure, not a skip: these guards
    exist to pin that file, so its absence is the thing they were written to report.
    """
    path = Path(CONTEXT_PATH)
    assert path.exists(), f"the guarded source is missing: {path}"
    spec = importlib.util.spec_from_file_location("pynecore_orb_context", CONTEXT_PATH)
    assert spec is not None and spec.loader is not None, f"not importable: {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOrbContextMirror:
    """Der pandas-Context-Helper muss die Skript-Logik auf ET-Sessions abbilden."""

    @staticmethod
    def _frame() -> pd.DataFrame:
        # 30m-Bars ueber zwei ET-Tage (Januar = EST, UTC-5).
        # Tag 1: OR 100/98 (Range 2, Long-Level 100.4), Breakout, dann
        # 1 %-Reversion ab HWM -> Trailing-Exit. Tag 2: kein Breakout.
        rows = [
            ("2024-01-02 14:00:00+00:00", 100, 100.2, 99.8, 100.0),  # 09:00 ET
            ("2024-01-02 14:30:00+00:00", 100, 100.0, 98.0, 99.0),  # 09:30 ET (OR)
            ("2024-01-02 15:00:00+00:00", 99, 101.0, 99.0, 101.0),  # 10:00 ET -> LONG
            ("2024-01-02 15:30:00+00:00", 101, 103.0, 100.5, 103.0),  # HWM 103
            ("2024-01-02 16:00:00+00:00", 103, 103.0, 101.0, 101.5),  # <101.97 -> Trail
            ("2024-01-02 16:30:00+00:00", 101.5, 102.0, 101.0, 101.8),
            ("2024-01-03 14:30:00+00:00", 100, 100.1, 99.9, 100.0),
            ("2024-01-03 15:00:00+00:00", 100, 100.1, 99.9, 100.0),
        ]
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
        df["volume"] = 1.0
        return df

    def test_breakout_levels_and_long_entry(self):
        ctx = _load_context_module().build_trade_context(self._frame(), {})
        assert ctx["breakout_long"].iloc[2] == pytest.approx(100.4)
        assert ctx["breakout_short"].iloc[2] == pytest.approx(97.6)
        assert ctx["direction"].iloc[2] == "LONG"

    def test_trailing_stop_exit(self):
        ctx = _load_context_module().build_trade_context(self._frame(), {})
        assert ctx["exit_reason"].iloc[4] == "trailing_stop"
        assert ctx["trailing_stop_at_exit"].iloc[4] == pytest.approx(103.0 * 0.99)

    def test_signal_plus_one_carry_stops_at_session_boundary(self):
        # B-M5: ein Signal auf der LETZTEN Bar von Tag 1 darf nicht auf die
        # erste Bar von Tag 2 uebertragen werden (andere OR-Range/Session).
        rows = [
            ("2024-01-02 14:30:00+00:00", 100, 100.0, 98.0, 99.0),  # OR Tag 1
            ("2024-01-02 15:00:00+00:00", 99, 101.0, 99.0, 101.0),  # LONG-Signal
            ("2024-01-03 14:30:00+00:00", 100, 100.1, 99.9, 100.0),  # Tag 2
            ("2024-01-03 15:00:00+00:00", 100, 100.1, 99.9, 100.0),
        ]
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
        df["volume"] = 1.0
        ctx = _load_context_module().build_trade_context(df, {})
        assert ctx["direction"].iloc[1] == "LONG"
        assert pd.isna(ctx["direction"].iloc[2]), (
            "Signal-Carry darf die Session-Boundary nicht ueberschreiten"
        )
        assert pd.isna(ctx["exit_reason"].iloc[2])

    def test_overnight_bars_do_not_pollute_range(self):
        df = self._frame().copy()
        df.loc[0, "high"] = 150.0  # 09:00-ET-Bar ausserhalb der OR
        df.loc[2, "high"] = 150.0  # 10:00-ET-Bar nach der OR
        ctx = _load_context_module().build_trade_context(df, {})
        assert ctx["OR_High"].iloc[2] == pytest.approx(100.0)
        assert ctx["OR_Low"].iloc[2] == pytest.approx(98.0)

    def test_day_change_flattens_open_position_without_forced_exit_bar(self):
        # B-H1-Mirror: Position ist zum Session-Ende noch offen (keine
        # 15:45-ET-Bar in Tag 1 -> kein time_exit), Tag 2 beginnt. Der Helper
        # muss den Exit auf der letzten Bar von Tag 1 als day_reset_flatten
        # buchen statt die Position still zu droppen (Skript-Seite: Close).
        rows = [
            ("2024-01-02 14:30:00+00:00", 100, 100.0, 98.0, 99.0),  # OR Tag 1
            ("2024-01-02 15:00:00+00:00", 99, 101.0, 99.0, 101.0),  # LONG-Signal
            ("2024-01-02 15:30:00+00:00", 101, 103.0, 100.5, 103.0),  # offen, HWM
            ("2024-01-03 14:30:00+00:00", 100, 100.1, 99.9, 100.0),  # Tag 2
            ("2024-01-03 15:00:00+00:00", 100, 100.1, 99.9, 100.0),
        ]
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
        df["volume"] = 1.0
        ctx = _load_context_module().build_trade_context(df, {})
        assert ctx["direction"].iloc[1] == "LONG"
        assert ctx["exit_reason"].iloc[2] == "day_reset_flatten"
