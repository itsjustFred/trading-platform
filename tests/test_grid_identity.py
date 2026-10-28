"""The grid result identity: what it must catch, and what it must refuse.

Every case here is a way a persisted grid cell can stop answering the question
it was computed for. The rule under test is the same in all of them: an
identity that cannot be established, or that no longer holds, is a refusal --
never a warning attached to numbers that are still served.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from runner.grid_identity import (
    GRID_IDENTITY_VERSION,
    cell_identity,
    data_fingerprint,
    data_staleness_reason,
    identity_matches,
    read_identity,
    strategy_source_sha256,
)


def _write_frame(path: Path, *, periods: int = 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC"),
            "close": [float(index + 1) for index in range(periods)],
        }
    ).to_parquet(path, index=False)
    return path


def _setup(tmp_path: Path) -> None:
    """Create the inputs once; rewriting them would change the identity."""
    _write_frame(tmp_path / "data" / "ohlcv" / "yfinance" / "equity" / "US" / "AAPL" / "1d.parquet")
    strategy = tmp_path / "demo.py"
    strategy.write_text("STRATEGY_META = {'name': 'demo'}\n", encoding="utf-8")


def _identity(tmp_path: Path, *, rewrite_inputs: bool = False, **overrides) -> dict:
    if rewrite_inputs:
        _setup(tmp_path)
    data_dir = tmp_path / "data"
    strategy = tmp_path / "demo.py"
    payload = {
        "engine": "native",
        "strategy": str(strategy),
        "symbol": "AAPL",
        "timeframe": "1d",
        "start": "2024-01-01",
        "end": "2024-01-05",
        "capital": 10_000.0,
        "risk_pct": 0.01,
        "data_dir": str(data_dir),
    }
    payload.update(overrides)
    return cell_identity(**payload)


def test_identical_requests_produce_identical_identities(tmp_path):
    _setup(tmp_path)
    assert _identity(tmp_path)["fingerprint"] == _identity(tmp_path)["fingerprint"]


@pytest.mark.parametrize(
    "override",
    [
        {"engine": "vectorbt"},
        {"symbol": "MSFT"},
        {"timeframe": "1h"},
        {"start": "2023-01-01"},
        {"end": "2024-06-01"},
        {"capital": 20_000.0},
        {"risk_pct": 0.02},
        {"max_leverage": 3.0},
        {"intrabar_resolution": "1-lower"},
        {"params": {"fast": 10}},
        {"max_position_exposure_pct": 1.0},
    ],
)
def test_every_config_knob_is_part_of_the_identity(tmp_path, override):
    """A knob that is not in the identity is a knob that cannot invalidate a cell."""
    assert _identity(tmp_path)["fingerprint"] != _identity(tmp_path, **override)["fingerprint"]


def test_the_strategy_source_is_part_of_the_identity(tmp_path):
    _setup(tmp_path)
    baseline = _identity(tmp_path)
    strategy = tmp_path / "demo.py"
    strategy.write_text(
        "STRATEGY_META = {'name': 'demo'}\n# edited\n", encoding="utf-8"
    )
    assert _identity(tmp_path)["fingerprint"] != baseline["fingerprint"]


def test_the_identity_is_stable_across_processes(tmp_path):
    """The recorded identity is compared in another process, on another run.

    A timestamp or an insertion-ordered dict would make every comparison a
    coin flip, so the whole gate would be noise.
    """
    _setup(tmp_path)
    identity = _identity(tmp_path)
    again = _identity(tmp_path)
    assert identity["config"] == again["config"]
    assert identity["data"] == again["data"]
    assert identity["fingerprint"] == again["fingerprint"]


def test_an_unreadable_strategy_is_not_a_wildcard(tmp_path):
    assert strategy_source_sha256(tmp_path / "missing.py") is None


def test_the_data_fingerprint_changes_when_a_frame_is_rewritten(tmp_path):
    baseline = _identity(tmp_path)
    parquet = tmp_path / "data" / "ohlcv" / "yfinance" / "equity" / "US" / "AAPL" / "1d.parquet"
    _write_frame(parquet, periods=9)
    assert _identity(tmp_path)["fingerprint"] != baseline["fingerprint"]


def test_data_fingerprint_is_none_when_a_file_cannot_be_read(tmp_path):
    """Unknown is not "unchanged": it is a refusal."""
    missing = tmp_path / "gone.parquet"
    assert data_fingerprint([missing]) is None


def test_identity_refuses_when_either_half_is_unknown(tmp_path):
    identity = _identity(tmp_path)
    unknown = {**identity, "data": None}

    assert identity_matches(identity, identity) == (True, "")
    ok, reason = identity_matches(identity, unknown)
    assert not ok and "data half" in reason
    ok, reason = identity_matches(unknown, identity)
    assert not ok and "data half" in reason


def test_identity_refuses_an_absent_or_older_recorded_identity(tmp_path):
    identity = _identity(tmp_path)

    ok, reason = identity_matches(None, identity)
    assert not ok and "no grid identity" in reason

    ok, reason = identity_matches({**identity, "version": GRID_IDENTITY_VERSION + 1}, identity)
    assert not ok and "version" in reason


def test_identity_names_the_config_fields_that_changed(tmp_path):
    identity = _identity(tmp_path)
    changed = _identity(tmp_path, capital=20_000.0)

    ok, reason = identity_matches(identity, changed)

    assert not ok
    assert "config changed" in reason
    assert "capital" in reason


def test_read_identity_ignores_anything_it_cannot_parse(tmp_path):
    broken = tmp_path / "broken.trades.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_identity(broken) is None
    assert read_identity(tmp_path / "absent.trades.json") is None
    (tmp_path / "no_meta.json").write_text("{}", encoding="utf-8")
    assert read_identity(tmp_path / "no_meta.json") is None


def test_data_staleness_reason_passes_a_healthy_identity(tmp_path):
    _setup(tmp_path)
    assert data_staleness_reason(_identity(tmp_path)) is None


def test_data_staleness_reason_reports_a_changed_frame(tmp_path):
    _setup(tmp_path)
    identity = _identity(tmp_path)
    _write_frame(
        tmp_path / "data" / "ohlcv" / "yfinance" / "equity" / "US" / "AAPL" / "1d.parquet",
        periods=9,
    )

    reason = data_staleness_reason(identity)

    assert reason is not None and "changed" in reason


def test_data_staleness_reason_reports_a_deleted_frame(tmp_path):
    _setup(tmp_path)
    identity = _identity(tmp_path)
    (
        tmp_path / "data" / "ohlcv" / "yfinance" / "equity" / "US" / "AAPL" / "1d.parquet"
    ).unlink()

    reason = data_staleness_reason(identity)

    assert reason is not None and "gone" in reason


def test_data_staleness_reason_detects_a_tampered_file_list(tmp_path):
    """A hand-edited identity must not certify data it does not describe."""
    _setup(tmp_path)
    identity = _identity(tmp_path)
    tampered = {**identity, "data": {**identity["data"], "fingerprint": "0" * 64}}

    reason = data_staleness_reason(tampered)

    assert reason is not None and "fingerprint" in reason


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {},
        {"version": 0, "data": {"files": []}},
        {"version": GRID_IDENTITY_VERSION, "data": None},
        {"version": GRID_IDENTITY_VERSION, "data": {"files": [{"path": ""}]}},
        {"version": GRID_IDENTITY_VERSION, "data": {"files": "not-a-list"}},
    ],
)
def test_data_staleness_reason_refuses_every_unusable_identity(identity):
    assert data_staleness_reason(identity) is not None
