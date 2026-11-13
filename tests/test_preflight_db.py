"""Preflight check of the ACTIVE DB deployments the daemon actually trades (#184 finding 7)."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from live_runner.preflight import main as preflight_main
from live_runner.preflight import run_live_preflight
from live_runner.preflight_db import run_db_preflight
from shared.models import LiveDeploymentConfig


def _yaml(tmp_path: Path, deployments: str) -> Path:
    path = tmp_path / "live_deployments.yaml"
    path.write_text(
        dedent(
            f"""
            accounts:
              - id: alpaca-paper
                broker: alpaca
                broker_login: PA3EFBL4P7BU
            deployments:
            {deployments}
            """
        ),
        encoding="utf-8",
    )
    return path


_YAML_ONE = """
              - strategy_name: equity_momentum
                account_id: alpaca-paper
                symbols: [AAPL]
                timeframe: 1h
"""

_ENV = {
    "ALPACA_API_KEY_ACC1": "k",
    "ALPACA_SECRET_KEY_ACC1": "s",
    "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3EFBL4P7BU",
    "ALPACA_ENDPOINT_ACC1": "https://paper-api.alpaca.markets/v2",
}


def _seed(
    database,
    *,
    strategy="equity_momentum",
    symbols=("AAPL",),
    timeframe="1h",
    status="ACTIVE",
    account_id="alpaca-paper",
) -> str:
    database.ensure_live_account(account_id, "alpaca", name=account_id)
    created = database.create_live_deployment(
        LiveDeploymentConfig(
            strategy_name=strategy,
            account_id=account_id,
            symbols=list(symbols),
            timeframe=timeframe,
            status=status,
        )
    )
    return str(created.id)


def _db_path(database) -> Path:
    return Path(database.DB_PATH)


def test_matching_active_deployment_is_clean(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database)

    report = run_db_preflight(
        [
            LiveDeploymentConfig(
                strategy_name="equity_momentum",
                account_id="alpaca-paper",
                symbols=["AAPL"],
                timeframe="1h",
            )
        ],
        db_path=_db_path(database),
    )

    assert report.errors == [] and report.warnings == []
    assert report.active_count == 1
    assert len(report.matched) == 1


def test_active_db_deployment_missing_from_yaml_is_an_error(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database, strategy="risk_parity")

    report = run_db_preflight([], db_path=_db_path(database))

    assert report.only_in_db and "risk_parity" in report.only_in_db[0]
    assert any("absent from the YAML" in e for e in report.errors)


def test_yaml_only_deployment_is_a_warning_and_paused_rows_are_ignored(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database, strategy="paused_one", status="PAUSED")

    report = run_db_preflight(
        [
            LiveDeploymentConfig(
                strategy_name="equity_momentum",
                account_id="alpaca-paper",
                symbols=["AAPL"],
                timeframe="1h",
            )
        ],
        db_path=_db_path(database),
    )

    assert report.errors == []
    assert report.active_count == 0
    assert report.only_in_yaml == ["equity_momentum (alpaca-paper)"]
    assert any("will not trade it" in w for w in report.warnings)


def test_symbol_and_timeframe_differences_are_reported(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database, symbols=("AAPL", "MSFT"), timeframe="15m")

    report = run_db_preflight(
        [
            LiveDeploymentConfig(
                strategy_name="equity_momentum",
                account_id="alpaca-paper",
                symbols=["AAPL"],
                timeframe="1h",
            )
        ],
        db_path=_db_path(database),
    )

    assert report.errors == []
    assert any("symbols differ" in w for w in report.warnings)
    assert any("timeframe differs" in w for w in report.warnings)


def test_missing_database_is_an_error_and_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "backtests.db"

    report = run_db_preflight([], db_path=missing)

    assert any("not found" in e for e in report.errors)
    assert not missing.exists() and not missing.parent.exists()


def test_check_is_read_only(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database)
    path = _db_path(database)
    before = path.read_bytes()

    run_db_preflight([], db_path=path)

    assert path.read_bytes() == before


def test_unreadable_schema_is_an_error(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()

    report = run_db_preflight([], db_path=path)

    assert any("Cannot read deployments" in e for e in report.errors)


def test_run_live_preflight_fails_on_db_only_active_deployment(tmp_path: Path, isolated_db) -> None:
    database = isolated_db[0]
    _seed(database)
    _seed(database, strategy="surprise")

    result = run_live_preflight(
        _yaml(tmp_path, _YAML_ONE), env=_ENV, check_db=True, db_path=_db_path(database)
    )

    assert result.ok is False
    assert result.db_deployments is not None
    assert any("surprise" in e for e in result.db_deployments["errors"])


def test_db_check_is_opt_in(tmp_path: Path) -> None:
    result = run_live_preflight(_yaml(tmp_path, _YAML_ONE), env=_ENV)

    assert result.db_deployments is None


def test_cli_flag_prints_the_diff(tmp_path: Path, isolated_db, capsys) -> None:
    database = isolated_db[0]
    _seed(database)
    _seed(database, strategy="surprise")
    config = _yaml(tmp_path, _YAML_ONE)
    env_file = tmp_path / ".accounts.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in _ENV.items()), encoding="utf-8")

    code = preflight_main(
        [
            "--config",
            str(config),
            "--accounts-env",
            str(env_file),
            "--check-db",
            "--db-path",
            str(_db_path(database)),
        ]
    )

    assert code == 1
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :]) if out.lstrip().startswith("{") else None
    assert payload is not None, out[:200]
    assert payload["db_deployments"]["only_in_db"]
