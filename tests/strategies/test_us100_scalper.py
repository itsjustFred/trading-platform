"""
Tests fuer Strategie: US100 Intraday Scalper.

Deterministische Synthetic-Frame-Tests auf 1h-Bars (keine Marktdaten noetig;
Januar = EST, d.h. NY-Mitternacht = 05:00 UTC):
- Midnight-Referenz + Breakout-Schaerfung 00:00-08:00 ET
- Stundlicher Buy-Stop (prev-H1-Close + 30pt) mit genau einem Fill pro Tag
- SL-Exit (prev-H1-Close - 25pt) und EOD-Flatten 16:00 ET
- Code-gegen-infos.txt-Konflikt: implementiert sind die CODE-Werte (0.12).
"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent.parent
STRATEGY_PATH = PROJECT_ROOT / "strategies" / "us100_scalper" / "us100_scalper.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("us100_scalper", str(STRATEGY_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame(with_stop_hit: bool = False) -> pd.DataFrame:
    """Zwei ET-Tage 1h-Bars; Tag 1 mit Breakout + Entry, Tag 2 ohne Breakout."""
    stamps = pd.date_range("2024-01-02 04:00", periods=43, freq="h", tz="UTC")
    n = len(stamps)
    opens = [20000.0] * n
    highs = [20005.0] * n
    lows = [19995.0] * n
    closes = [20000.0] * n

    def at(ts: str) -> int:
        return stamps.get_loc(pd.Timestamp(ts, tz="UTC"))

    # Breakout-Fenster (05:00-13:00 UTC = 00:00-08:00 ET): ein High ueber Ref+30.
    highs[at("2024-01-02 10:00")] = 20050.0
    closes[at("2024-01-02 10:00")] = 20010.0
    # Entry-Fenster ab 13:00 UTC: prev-H1-Close (12:00) = 20000 -> Stop 20030.
    highs[at("2024-01-02 13:00")] = 20040.0
    closes[at("2024-01-02 13:00")] = 20035.0
    # Zweiter Stop-Touch am selben Tag -> trotzdem kein zweiter Fill.
    highs[at("2024-01-02 18:00")] = 20100.0
    closes[at("2024-01-02 18:00")] = 20090.0
    if with_stop_hit:
        lows[at("2024-01-02 15:00")] = 19900.0
        closes[at("2024-01-02 15:00")] = 19950.0
    else:
        for ts in ("2024-01-02 14:00", "2024-01-02 15:00", "2024-01-02 20:00"):
            lows[at(ts)] = 20020.0
            closes[at(ts)] = 20035.0
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [10.0] * n,
        }
    )


def test_returns_four_tuple_contract():
    module = _load_module()
    entries, exits, indicators, drawings = module.generate_signals(_frame(), {})
    assert isinstance(entries, pd.Series)
    assert isinstance(exits, pd.Series)
    assert isinstance(indicators, dict)
    assert isinstance(drawings, list)


def test_breakout_arms_and_single_fill_then_eod_flatten():
    module = _load_module()
    df = _frame()
    entries, exits, indicators, _ = module.generate_signals(df, {})
    entry_idx = entries[entries].index.tolist()
    assert len(entry_idx) == 1, f"genau ein Fill pro Tag erwartet, gefunden: {entry_idx}"
    entry_bar = df.loc[entry_idx[0], "timestamp"]
    assert entry_bar == pd.Timestamp("2024-01-02 13:00", tz="UTC")
    assert indicators["direction"].loc[entry_idx[0]] == "LONG"
    # SL = prev-H1-Close (12:00 -> 20000) minus 25 Punkte.
    assert indicators["sl_price"].loc[entry_idx[0]] == pytest.approx(19975.0)
    # EOD-Flatten an der 16:00-ET-Bar (21:00 UTC).
    exit_idx = exits[exits].index.tolist()
    assert exit_idx == [df.index[df["timestamp"] == pd.Timestamp("2024-01-02 21:00", tz="UTC")][0]]
    assert indicators["exit_reason"].loc[exit_idx[0]] == "time_exit_eod"


def test_no_breakout_no_trade():
    module = _load_module()
    df = _frame()
    df.loc[df["timestamp"] == pd.Timestamp("2024-01-02 10:00", tz="UTC"), "high"] = 20005.0
    entries, _, _, _ = module.generate_signals(df, {})
    assert not entries.any(), "ohne Breakout im 00:00-08:00-ET-Fenster kein Trade"


def test_stop_loss_exit():
    module = _load_module()
    df = _frame(with_stop_hit=True)
    entries, exits, indicators, _ = module.generate_signals(df, {})
    assert entries.sum() == 1
    exit_idx = exits[exits].index.tolist()
    assert len(exit_idx) == 1
    assert indicators["exit_reason"].loc[exit_idx[0]] == "stop_loss"
    assert indicators["exit_price"].loc[exit_idx[0]] == pytest.approx(19975.0)


def test_day_reset_flattens_without_eod_bar():
    """B-H1: offene Position wird am Day-Reset geflattet (kein Phantom-Trade).

    Fehlt jede Post-16:00-ET-Bar des Entry-Tages (Session-Data-Gap), feuert
    der time_exit_eod nie. Der Day-Rollover muss den Exit auf der letzten
    Bar des Vortages zum Close buchen statt die Position still zu droppen.
    """
    module = _load_module()
    df = _frame()
    ny = df["timestamp"].dt.tz_convert("America/New_York")
    et_day = ny.dt.date.astype(str)
    mins = ny.dt.hour * 60 + ny.dt.minute
    df_gap = df[~((et_day == "2024-01-02") & (mins >= 16 * 60))].reset_index(drop=True)
    entries, exits, indicators, _ = module.generate_signals(df_gap, {})
    assert entries.sum() == 1
    exit_idx = exits[exits].index.tolist()
    assert len(exit_idx) == 1, "Day-Reset muss genau einen Exit buchen"
    assert indicators["exit_reason"].loc[exit_idx[0]] == "day_reset_flatten"
    # Letzte Bar des ET-Tages 2024-01-02 = 20:00 UTC (15:00 ET), Close 20035.
    assert df_gap.loc[exit_idx[0], "timestamp"] == pd.Timestamp(
        "2024-01-02 20:00", tz="UTC"
    )
    assert indicators["exit_price"].loc[exit_idx[0]] == pytest.approx(20035.0)


def test_code_values_win_over_infos_txt():
    module = _load_module()
    params = module.STRATEGY_META["parameters"]
    assert params["entry_offset_percent"] == pytest.approx(0.12)
    assert params["stop_loss_percent"] == pytest.approx(0.10)
    assert params["breakout_buffer_percent"] == pytest.approx(0.12)
    # Der Konflikt ist dokumentiert (infos.txt-Wert wird zitiert) ...
    assert "infos.txt" in module.STRATEGY_META["notes"]
    assert "entry 0.5" in module.STRATEGY_META["notes"]
    # ... aber implementiert sind die CODE-Werte.
    assert "code values are implemented" in module.STRATEGY_META["notes"]


def test_index_only_frame_matches_timestamp_column():
    """Index-Pfad muss NY-Zeit behalten (kein .values-TZ-Strip → UTC-Shift).

    Ohne timestamp-Spalte leitet die Strategie ET-Sessions aus dem
    tz-aware UTC-DatetimeIndex ab; Ergebnis muss identisch zum
    timestamp-Spalten-Pfad sein (sonst +5h-Fenster-Shift im Winter).
    """
    module = _load_module()
    df = _frame()
    df_idx = df.drop(columns=["timestamp"]).copy()
    df_idx.index = pd.DatetimeIndex(df["timestamp"])
    entries_a, exits_a, ind_a, _ = module.generate_signals(df, {})
    entries_b, exits_b, ind_b, _ = module.generate_signals(df_idx, {})
    assert entries_a.to_numpy().tolist() == entries_b.to_numpy().tolist()
    assert exits_a.to_numpy().tolist() == exits_b.to_numpy().tolist()
    assert (
        ind_a["exit_reason"].fillna("").tolist()
        == ind_b["exit_reason"].fillna("").tolist()
    )
    assert entries_b.sum() == 1, "Index-Pfad muss denselben Fill finden"


def test_frame_end_flattens_open_position():
    """B-H1-Edge: Frame endet mid-day mit offener Position (kein EOD/Day-Reset).

    Entry 13:00 UTC, Frame endet 15:00 UTC (vor 16:00-ET-EOD, kein Tag 2):
    Exit muss auf der letzten Bar zum Close als day_reset_flatten gebucht
    werden statt die Position still zu droppen.
    """
    module = _load_module()
    df = _frame()
    end = df.index[df["timestamp"] == pd.Timestamp("2024-01-02 15:00", tz="UTC")][0]
    df_cut = df.iloc[: end + 1].reset_index(drop=True)
    entries, exits, indicators, _ = module.generate_signals(df_cut, {})
    assert entries.sum() == 1
    exit_idx = exits[exits].index.tolist()
    assert exit_idx == [df_cut.index[-1]], "Exit muss auf der letzten Bar liegen"
    assert indicators["exit_reason"].iloc[-1] == "day_reset_flatten"
    assert indicators["exit_price"].iloc[-1] == pytest.approx(
        float(df_cut["close"].iloc[-1])
    )


def test_frame_end_flatten_skips_same_bar_entry():
    """PM11: Entry auf der letzten Bar bekommt keinen Flatten-Exit auf derselben Bar.

    Der Frame endet exakt auf der Touch-Bar des Buy-Stops. Ein day_reset_flatten
    auf derselben Bar wuerde einen Phantom-Roundtrip buchen (Entry zum Stop-Level,
    Exit zum Close), obwohl die Position nie bestand. Ohne Flatten-Exit faengt
    der naechste Frame (Day-Reset oder EOD) die Position ab.
    """
    module = _load_module()
    df = _frame()
    last = df.index[df["timestamp"] == pd.Timestamp("2024-01-02 13:00", tz="UTC")][0]
    df_cut = df.iloc[: last + 1].reset_index(drop=True)
    entries, exits, indicators, _ = module.generate_signals(df_cut, {})
    assert entries.sum() == 1, "Entry-Touch auf der letzten Bar muss als Entry erkannt sein"
    assert not exits.any(), "Kein Exit auf derselben Bar wie der Entry"
    assert indicators["exit_reason"].isna().all()
