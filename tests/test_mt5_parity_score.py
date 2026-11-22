"""score_parity.py: MT5 tester trades vs platform artifact, paired on the server clock."""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "strategy-comparisons"
    / "intraday_volatility_breakout"
    / "audit"
    / "score_parity.py"
)


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("score_parity_under_test", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_ref(path: pathlib.Path, rows: list[str]) -> None:
    header = "entry_time,exit_time,dir,vol,entry,exit,sl,lots,pnl,reason"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def _trade(direction, entry_time, entry, exit_time, exit_price, qty):
    return {
        "direction": direction,
        "entry_time": entry_time,
        "entry_price": entry,
        "quantity": qty,
        "meta": {"sl_price": None},
        "exits": [{"time": exit_time, "price": exit_price, "reason": "signal"}],
    }


def test_pairs_on_the_server_trading_day_and_direction(tmp_path):
    mod = _load()
    _write_ref(
        tmp_path / "mt5_trades_nas100.csv",
        [
            # 2023-07-05 is legacy index history (UTC+2): 17:45 server = 15:45 UTC.
            "2023.07.05 17:45:00,2023.07.05 22:55:00,LONG,2,100,110,90,2,20,time",
            # 2025-07-01 is New York close (UTC+3): 16:45 server = 13:45 UTC.
            "2025.07.01 16:45:00,2025.07.01 22:55:00,SHORT,1,200,190,210,1,10,time",
            "2025.07.02 17:00:00,2025.07.02 22:55:00,LONG,1,300,305,290,1,5,time",
        ],
    )
    artifact = tmp_path / "pf.json"
    artifact.write_text(
        json.dumps(
            {
                "trades": [
                    _trade(
                        "LONG",
                        "2023-07-05T15:45:00+00:00",
                        100.0,
                        "2023-07-05T20:55:00+00:00",
                        111.0,
                        2,
                    ),
                    _trade(
                        "SHORT",
                        "2025-07-01T13:30:00+00:00",
                        200.0,
                        "2025-07-01T19:55:00+00:00",
                        190.0,
                        1,
                    ),
                    _trade(
                        "SHORT",
                        "2025-07-02T14:00:00+00:00",
                        300.0,
                        "2025-07-02T19:55:00+00:00",
                        301.0,
                        1,
                    ),
                ]
            }
        ),
        encoding="utf-8",
    )

    mt5 = mod.load_mt5(tmp_path, "nas100")
    pf = mod.load_platform(artifact, "nas100", "dukascopy_mt5_index")
    summary, _merged = mod.score("nas100", mt5, pf)

    assert summary["matched"] == 2
    assert summary["mt5_only"] == 1  # 2025-07-02 LONG: the platform went SHORT that day
    assert summary["pf_only"] == 1
    assert (
        summary["matched_same_entry_minute"] == 1
    )  # 2023-07-05 only; 2025-07-01 differs by 15 min
    assert summary["mt5_pnl"] == pytest.approx(35.0)
    assert summary["pf_pnl"] == pytest.approx(22.0 + 10.0 - 1.0)
    assert summary["matched_pnl_delta"] == pytest.approx((22.0 - 20.0) + (10.0 - 10.0))


def test_platform_side_requires_a_broker_clock(tmp_path):
    mod = _load()
    artifact = tmp_path / "pf.json"
    artifact.write_text(json.dumps({"trades": []}), encoding="utf-8")
    with pytest.raises(SystemExit, match="broker clock is required"):
        mod.load_platform(artifact, "eurusd", "")


def _single_trade_artifact(tmp_path, *, direction="LONG", entry=1.0, exit_=1.001, qty=1.0):
    artifact = tmp_path / "pf.json"
    artifact.write_text(
        json.dumps(
            {
                "trades": [
                    _trade(
                        direction,
                        "2024-01-02T10:00:00+00:00",
                        entry,
                        "2024-01-02T12:00:00+00:00",
                        exit_,
                        qty,
                    )
                ]
            }
        ),
        encoding="utf-8",
    )
    return artifact


def test_eurusd_pnl_uses_the_100k_contract(tmp_path):
    mod = _load()
    artifact = _single_trade_artifact(tmp_path, entry=1.0, exit_=1.001, qty=1.0)
    pf = mod.load_platform(artifact, "eurusd", "dukascopy_mt5_forex")
    assert pf["pnl_px"].iloc[0] == pytest.approx(100.0)


def test_nas100_pnl_is_unit_contract_and_signed_by_direction(tmp_path):
    mod = _load()
    artifact = _single_trade_artifact(tmp_path, direction="short", entry=100.0, exit_=90.0, qty=2.0)
    pf = mod.load_platform(artifact, "nas100", "dukascopy_mt5_index")
    assert pf["dir"].tolist() == ["SHORT"]
    assert pf["pnl_px"].iloc[0] == pytest.approx(20.0)


def test_unknown_trade_direction_fails_instead_of_scoring_nan(tmp_path):
    mod = _load()
    artifact = _single_trade_artifact(tmp_path, direction="BOTH")
    with pytest.raises(SystemExit, match="direction"):
        mod.load_platform(artifact, "nas100", "dukascopy_mt5_index")


def test_summary_pnl_delta_is_platform_minus_mt5(tmp_path):
    mod = _load()
    _write_ref(
        tmp_path / "mt5_trades_nas100.csv",
        ["2024.01.02 12:45:00,2024.01.02 14:00:00,LONG,1,100,110,90,1,7,time"],
    )
    artifact = _single_trade_artifact(tmp_path, entry=100.0, exit_=110.0, qty=1.0)
    mt5 = mod.load_mt5(tmp_path, "nas100")
    pf = mod.load_platform(artifact, "nas100", "dukascopy_mt5_index")
    summary, _ = mod.score("nas100", mt5, pf)
    assert summary["mt5_pnl"] == pytest.approx(7.0)
    assert summary["pf_pnl"] == pytest.approx(10.0)
    assert summary["pnl_delta"] == pytest.approx(3.0)


@pytest.mark.parametrize("item", ["gbpusd=x.json", "eurusd", "=x.json"])
def test_artifact_pair_arguments_are_validated(item):
    mod = _load()
    with pytest.raises(SystemExit, match="expected"):
        mod._pairs([item])


def test_artifact_pair_arguments_parse_known_symbols():
    mod = _load()
    assert mod._pairs(["eurusd=a.json", "sp500=b.json"]) == {"eurusd": "a.json", "sp500": "b.json"}
    assert mod._pairs(None) == {}
