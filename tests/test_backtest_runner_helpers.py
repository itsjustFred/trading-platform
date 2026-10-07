"""Unit tests for the extracted backtest-runner helpers (pre-mortem hardening)."""

import pytest

from viewer.main import (
    _backtest_runner_stage,
    _consume_backtest_line,
)



@pytest.fixture(autouse=True)
def _hermetic_db(isolated_db_sandbox):
    """Keep these tests off the configured database.

    They import `viewer.main`, which initialises the schema on import, so without this they read -
    and migrate - whatever `DATA_DIR` points at. That is invisible while the schema migrations are
    inert, and it is the reason the `live_signals.deployment_id` foreign key broke them: the migration
    correctly refused to run against a database that still has orphaned rows, which turned a latent
    production read into a test failure.

    Being explicit here is the fix rather than a workaround: a unit test that parses a log line has
    no business opening the live trading database.
    """

def test_consume_backtest_line_tracks_latest_saved_to():
    out_path, backtest_id = "", ""
    outcome = _consume_backtest_line(
        "Saved to: /data/backtests/first.trades.json",
        output_path=out_path,
        backtest_id=backtest_id,
    )
    assert outcome is not None
    message, stage, out_path, backtest_id = outcome
    assert out_path.endswith("first.trades.json")
    assert backtest_id == "first"

    # A second saved-to line must win.
    outcome = _consume_backtest_line(
        "Saved to: /data/backtests/second.trades.json",
        output_path=out_path,
        backtest_id=backtest_id,
    )
    assert outcome is not None
    _message, _stage, out_path, backtest_id = outcome
    assert out_path.endswith("second.trades.json")
    assert backtest_id == "second"


def test_consume_backtest_line_ignores_unparseable_output():
    assert _consume_backtest_line("", output_path="", backtest_id="") is None
    outcome = _consume_backtest_line("PROGRESS:10.0:Loading", output_path="", backtest_id="")
    assert outcome is not None
    message, stage, out_path, backtest_id = outcome
    assert message == "PROGRESS:10.0:Loading"
    assert stage == (50.5, "Running backtest... 50%")
    assert out_path == ""
    assert backtest_id == ""


def test_runner_stage_progress_scaling():
    assert _backtest_runner_stage("progress:50.0:Running") == (
        72.5,
        "Running backtest... 72%",
    )
    assert _backtest_runner_stage("something unrelated") is None
