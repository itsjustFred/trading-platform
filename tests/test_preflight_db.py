"""Preflight check of the ACTIVE DB deployments the daemon actually trades (#184 finding 7)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from live_runner import preflight_db
from live_runner.preflight import main as preflight_main
from live_runner.preflight import run_live_preflight
from live_runner.preflight_db import resolve_db_path, run_db_preflight
from shared.models import LiveDeploymentConfig


def _url(path: Path) -> str:
    """A DATABASE_URL in the form shared.database._sqlite_url builds."""
    return f"sqlite:///{path.as_posix()}"


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


# Numeric and long enough to pass validate_discord_deployment_signal_config's
# placeholder/snowflake sanity check, so a Discord row can be seeded through the real chokepoint.
_DISCORD_CHANNEL = "123456789012345678"

_YAML_ONE = """
              - strategy_name: equity_momentum
                account_id: alpaca-paper
                symbols: [AAPL]
                timeframe: 1h
"""


def _yaml_one(signal_source: str = "internal", signal_source_id: str = "") -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        strategy_name="equity_momentum",
        account_id="alpaca-paper",
        symbols=["AAPL"],
        timeframe="1h",
        signal_source=signal_source,
        signal_source_id=signal_source_id,
        signal_config={"channel_id": _DISCORD_CHANNEL} if signal_source == "discord" else {},
    )


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
    signal_source="internal",
    signal_source_id="",
) -> str:
    database.ensure_live_account(account_id, "alpaca", name=account_id)
    created = database.create_live_deployment(
        LiveDeploymentConfig(
            strategy_name=strategy,
            account_id=account_id,
            symbols=list(symbols),
            timeframe=timeframe,
            status=status,
            signal_source=signal_source,
            signal_source_id=signal_source_id,
            signal_config={"channel_id": _DISCORD_CHANNEL} if signal_source == "discord" else {},
        )
    )
    return str(created.id)


def _db_path(database) -> Path:
    return Path(database.DB_PATH)


def _orphan_actives(database, *, account_id="ghost-account", row_id="orphan-1") -> None:
    """Insert an ACTIVE row whose account was never committed.

    ``create_live_deployment`` refuses an unknown account and the ``live_deployments.account_id``
    foreign key refuses the row through SQLAlchemy, so the integrity break this preflight has to
    catch (a deployment whose account is not in ``live_accounts``) can only be written through a
    connection that does not enforce foreign keys -- which is what a deleted account row leaves
    behind for the read-only check.
    """
    with sqlite3.connect(database.DB_PATH) as connection:
        connection.execute(
            "INSERT INTO live_deployments "
            "(id, strategy_name, account_id, symbols, timeframe, status, signal_source, "
            "signal_source_type, signal_source_id, symbol_filter, signal_config, settings, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'ACTIVE', 'internal', 'internal', '', '[]', '{}', '{}', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (row_id, "orphan_strategy", account_id, '["AAPL"]', "1h"),
        )


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

    assert report.errors == []
    assert report.warnings == []
    assert report.active_count == 1
    assert len(report.matched) == 1


def test_active_db_deployment_missing_from_yaml_is_an_error(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database, strategy="risk_parity")

    report = run_db_preflight([], db_path=_db_path(database))

    assert report.only_in_db
    assert "risk_parity" in report.only_in_db[0]
    assert any("absent from the YAML" in e for e in report.errors)


def test_active_db_deployment_without_symbols_is_an_error(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database, symbols=())

    report = run_db_preflight([], db_path=_db_path(database))

    assert any("no symbols" in e for e in report.errors)


def test_active_db_deployment_without_timeframe_is_an_error(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database, timeframe="")

    report = run_db_preflight([], db_path=_db_path(database))

    assert any("no timeframe" in e for e in report.errors)


def test_null_timeframe_in_a_foreign_db_is_an_error(tmp_path: Path) -> None:
    """A NULL timeframe must not serialize as the string "None" and pass as configured.

    The daemon's own schema forbids it (``live_deployments.timeframe`` is NOT NULL), but
    ``--db-path`` points the check at whatever file it is given, and a row read from a file this
    repo did not write must not slip through the empty-timeframe refusal.
    """
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE live_accounts (id VARCHAR PRIMARY KEY)")
        connection.execute("INSERT INTO live_accounts (id) VALUES ('alpaca-paper')")
        connection.execute(
            "CREATE TABLE live_deployments "
            "(id VARCHAR PRIMARY KEY, strategy_name VARCHAR, account_id VARCHAR, symbols VARCHAR, "
            "timeframe VARCHAR, signal_source VARCHAR, signal_source_id VARCHAR, status VARCHAR)"
        )
        connection.execute(
            "INSERT INTO live_deployments VALUES "
            "('abc', 'equity_momentum', 'alpaca-paper', '[\"AAPL\"]', NULL, 'internal', '', "
            "'ACTIVE')"
        )

    report = run_db_preflight([], db_path=path)

    assert any("no timeframe" in e for e in report.errors), report.errors
    assert not any("timeframe differs" in w for w in report.warnings), report.warnings


def test_active_db_deployment_with_unknown_account_is_an_error(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database)
    _orphan_actives(database)

    report = run_db_preflight([], db_path=_db_path(database))

    assert any("live_accounts" in e for e in report.errors)
    assert any("ghost-account" in e for e in report.errors)


def test_duplicate_active_db_deployments_fail_the_check(isolated_db) -> None:
    """The daemon dispatches every ACTIVE match, so two rows mean two orders per signal."""
    database = isolated_db[0]
    _seed(database)
    _seed(database)

    report = run_db_preflight([], db_path=_db_path(database))

    assert any("duplicate" in e for e in report.errors), report.errors


def test_signal_source_drift_between_db_and_yaml_is_reported(isolated_db) -> None:
    """The daemon gates Discord dispatch on the DB value, so the drift has to show up."""
    database = isolated_db[0]
    _seed(database, signal_source="discord")

    report = run_db_preflight([_yaml_one()], db_path=_db_path(database))

    assert report.errors == []
    assert any("signal_source" in w for w in report.warnings), report.warnings


def test_matching_signal_source_is_not_reported(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database, signal_source="discord", signal_source_id="news-svc")

    report = run_db_preflight([_yaml_one("discord", "news-svc")], db_path=_db_path(database))

    assert report.warnings == [], report.warnings


def test_empty_stored_signal_source_falls_back_to_the_type_like_the_daemon(isolated_db) -> None:
    """The daemon rehydrates ``signal_source or signal_source_type`` (to_live_deployment).

    A row with an empty ``signal_source`` but ``signal_source_type='discord'`` is traded as
    Discord, so a YAML ``internal`` entry must show as drift, not as a clean match.
    """
    database = isolated_db[0]
    _seed(database, signal_source="discord")
    with sqlite3.connect(database.DB_PATH) as connection:
        connection.execute(
            "UPDATE live_deployments SET signal_source = '', signal_source_type = 'discord'"
        )

    report = run_db_preflight([_yaml_one()], db_path=_db_path(database))

    assert any("signal_source differs (DB discord" in w for w in report.warnings), report.warnings


def test_signal_source_id_drift_between_db_and_yaml_is_reported(isolated_db) -> None:
    """signal_source_id selects the Discord credential a deployment is dispatched with."""
    database = isolated_db[0]
    _seed(database, signal_source="discord", signal_source_id="db-svc")

    report = run_db_preflight([_yaml_one("discord", "yaml-svc")], db_path=_db_path(database))

    assert report.errors == []
    assert any("signal_source_id" in w for w in report.warnings), report.warnings


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
    assert not missing.exists()
    assert not missing.parent.exists()


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

    assert result.ok is False, "an ACTIVE DB row absent from the YAML must fail the preflight"
    assert result.db_deployments is not None, "the --check-db report is missing from the result"
    assert any("surprise" in e for e in result.db_deployments["errors"]), result.db_deployments


def test_a_read_only_open_failure_names_the_wal_cause(isolated_db, monkeypatch) -> None:
    """A WAL database that needs recovery opens read-only as a write error, not as corruption.

    The permission-dependent variant of this (an unwritable data directory) is not asserted
    here: it only reproduces on a filesystem that honours the mode bits, and a test that
    silently passes as root would claim coverage it does not have.
    """
    database = isolated_db[0]
    _seed(database)

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(preflight_db, "read_active_db_deployments", refuse)

    report = run_db_preflight([], db_path=_db_path(database))

    assert any("attempt to write a readonly database" in e for e in report.errors), report.errors
    assert any("WAL" in e for e in report.errors), report.errors


def test_run_live_preflight_fails_on_duplicate_active_db_deployment(
    tmp_path: Path, isolated_db
) -> None:
    """Two ACTIVE rows for one deployment are not a warning: the daemon trades both."""
    database = isolated_db[0]
    _seed(database)
    _seed(database)

    result = run_live_preflight(
        _yaml(tmp_path, _YAML_ONE), env=_ENV, check_db=True, db_path=_db_path(database)
    )

    assert result.ok is False, "a duplicate ACTIVE row must fail the preflight"
    assert result.db_deployments is not None, "the --check-db report is missing from the result"
    assert any("duplicate" in e for e in result.db_deployments["errors"]), result.db_deployments


def test_db_check_is_opt_in(tmp_path: Path) -> None:
    result = run_live_preflight(_yaml(tmp_path, _YAML_ONE), env=_ENV)

    assert result.db_deployments is None, "no --check-db must mean no DB report at all"


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


def test_resolve_db_path_uses_the_file_part_of_a_plain_url(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "backtests.db"
    monkeypatch.setenv("DATABASE_URL", _url(path))

    assert resolve_db_path() == path


def test_resolve_db_path_drops_a_query_string_from_the_url(tmp_path: Path, monkeypatch) -> None:
    """SQLAlchemy reads the query as driver options; it is not part of the filename."""
    path = tmp_path / "backtests.db"
    monkeypatch.setenv("DATABASE_URL", f"{_url(path)}?timeout=30&mode=ro")

    assert resolve_db_path() == path


def test_resolve_db_path_decodes_a_percent_encoded_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path.as_posix()}/my%20db.db")

    assert resolve_db_path() == tmp_path / "my db.db"


def test_resolve_db_path_keeps_a_relative_url_relative(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///backtests.db")

    assert resolve_db_path() == Path("backtests.db")


def test_empty_explicit_db_path_is_not_the_default(tmp_path: Path, monkeypatch) -> None:
    """``--db-path ""`` names no file; it must fail, not silently check the DATABASE_URL file."""
    real = tmp_path / "real.db"
    real.touch()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{real.as_posix()}")

    report = run_db_preflight([], db_path="")

    assert any("not found" in e for e in report.errors), report.errors


def test_resolve_db_path_refuses_an_in_memory_url(monkeypatch) -> None:
    """An in-memory database is not a file the check can read, and it is not empty either."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    with pytest.raises(ValueError, match="in-memory"):
        resolve_db_path()


def test_resolve_db_path_refuses_a_non_sqlite_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost/trading")

    with pytest.raises(ValueError, match="SQLite"):
        resolve_db_path()


def test_resolve_db_path_refuses_a_url_with_no_path(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")

    with pytest.raises(ValueError, match="no SQLite file path"):
        resolve_db_path()


def test_a_query_bearing_database_url_is_read(isolated_db, monkeypatch) -> None:
    """The daemon accepts the query string form, so --check-db must not call it missing."""
    database = isolated_db[0]
    _seed(database)
    monkeypatch.setenv("DATABASE_URL", f"{_url(_db_path(database))}?timeout=30")

    report = run_db_preflight([_yaml_one()], db_path=None)

    assert report.active_count == 1
    assert report.errors == [], report.errors


def test_an_in_memory_database_url_fails_the_check(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    report = run_db_preflight([], db_path=None)

    assert any("in-memory" in e for e in report.errors), report.errors
    assert "not found" not in report.errors[0]


def test_a_duplicated_yaml_deployment_is_reported(isolated_db) -> None:
    """A YAML that lists one (strategy, account) twice compares only one of the two entries."""
    database = isolated_db[0]
    _seed(database)

    report = run_db_preflight([_yaml_one(), _yaml_one()], db_path=_db_path(database))

    assert any("listed twice" in w for w in report.warnings), report.warnings


def test_a_paused_yaml_deployment_is_not_duplicated(isolated_db) -> None:
    """Two entries with different statuses are two deployments, not a duplicate."""
    database = isolated_db[0]
    paused = _yaml_one().model_copy(update={"status": "PAUSED"})

    report = run_db_preflight([_yaml_one(), paused], db_path=_db_path(database))

    assert not any("listed twice" in w for w in report.warnings), report.warnings
    assert any("will not trade it" in w for w in report.warnings), report.warnings


def test_db_check_reports_when_the_config_never_let_it_run(tmp_path: Path, isolated_db) -> None:
    """An invalid YAML fails preflight either way; the DB report must not look like a pass."""
    database = isolated_db[0]
    _seed(database)
    broken = tmp_path / "broken.yaml"
    broken.write_text("- not\n- a mapping\n", encoding="utf-8")

    result = run_live_preflight(broken, env=_ENV, check_db=True, db_path=_db_path(database))

    assert result.ok is False, "an unparseable config must fail the preflight"
    assert result.db_deployments is not None, "a requested but unrun check must not read as clean"
    assert any(
        "not checked" in e for e in result.db_deployments["errors"]
    ), result.db_deployments


def test_cosmetic_spelling_is_not_drift(isolated_db) -> None:
    """Both sides are normalized the way the writers normalize them before they are compared."""
    database = isolated_db[0]
    _seed(database)

    report = run_db_preflight(
        [
            LiveDeploymentConfig(
                strategy_name="equity_momentum",
                account_id="alpaca-paper",
                symbols=[" aapl "],
                timeframe="H1",
            )
        ],
        db_path=_db_path(database),
    )

    assert report.errors == []
    assert report.warnings == [], report.warnings
    assert len(report.matched) == 1


def test_a_lowercase_stored_symbol_is_not_drift(isolated_db) -> None:
    """The daemon matches a stored symbol with ``.upper()``, so ``aapl`` trades as ``AAPL``.

    The runbook promises a case difference is not reported as drift; the DB side has to be
    normalized the same way as the YAML side, or this row warns about a spelling the daemon
    never mismatches.
    """
    database = isolated_db[0]
    _seed(database, symbols=("aapl",))

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
    assert report.warnings == [], report.warnings
    assert len(report.matched) == 1


def test_a_lock_timeout_is_not_blamed_on_the_wal(isolated_db, monkeypatch) -> None:
    """Only the write-refusal form carries the WAL hint; a lock is a lock."""
    database = isolated_db[0]
    _seed(database)

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(preflight_db, "read_active_db_deployments", refuse)

    report = run_db_preflight([], db_path=_db_path(database))

    assert any("database is locked" in e for e in report.errors), report.errors
    assert not any("WAL" in e for e in report.errors), report.errors


def test_crashed_run_with_check_db_reports_the_db_as_not_checked(tmp_path: Path, capsys) -> None:
    """A requested --check-db that crashed before the DB was read must not report null."""
    env_file = tmp_path / ".accounts.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in _ENV.items()), encoding="utf-8")

    code = preflight_main(
        [
            "--config",
            str(tmp_path / "missing.yaml"),
            "--accounts-env",
            str(env_file),
            "--check-db",
            "--db-path",
            str(tmp_path / "backtests.db"),
        ]
    )

    assert code == 1
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])
    assert payload["db_deployments"] is not None, "a requested check must not read as never run"
    assert any("crashed" in e for e in payload["db_deployments"]["errors"]), payload[
        "db_deployments"
    ]


def test_db_path_without_check_db_implies_the_check(tmp_path: Path, isolated_db, capsys) -> None:
    """--db-path names a database to validate; ignoring it would be a silent green pass."""
    database = isolated_db[0]
    _seed(database)
    config = _yaml(tmp_path, _YAML_ONE)
    env_file = tmp_path / ".accounts.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in _ENV.items()), encoding="utf-8")

    code = preflight_main(
        [
            "--config",
            str(config),
            "--accounts-env",
            str(env_file),
            "--db-path",
            str(_db_path(database)),
        ]
    )

    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])
    assert payload["db_deployments"] is not None, "--db-path alone must run the DB check"
    assert payload["db_deployments"]["db_path"] == str(_db_path(database))
    assert code == 0, payload["db_deployments"]


def _yaml_equity_momentum() -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        strategy_name="equity_momentum",
        account_id="alpaca-paper",
        symbols=["AAPL"],
        timeframe="1h",
    )


def test_padded_stored_symbol_is_an_error_not_a_clean_match(isolated_db) -> None:
    """The daemon matches a stored symbol verbatim (upper, no strip): " AAPL " never matches AAPL.

    Normalizing both sides before comparing would call this row clean while it is dead.
    """
    database = isolated_db[0]
    _seed(database, symbols=(" AAPL ",))

    report = run_db_preflight(
        [_yaml_equity_momentum()],
        db_path=_db_path(database),
    )

    assert any('" AAPL "' in e and "match" in e for e in report.errors), report.errors
    assert not any("has no symbols" in e for e in report.errors), report.errors


def test_padded_stored_signal_source_is_an_error_not_a_clean_match(isolated_db) -> None:
    """The daemon gates Discord dispatch on ``str(signal_source).lower() == "discord"``, no strip.

    A stored `` discord`` is not Discord to the daemon, so a YAML that says ``discord`` would be
    silently dead. Stripping it before comparing would call the row clean.
    """
    database = isolated_db[0]
    _seed(database, signal_source="discord")
    with sqlite3.connect(database.DB_PATH) as connection:
        connection.execute("UPDATE live_deployments SET signal_source = ' discord'")

    report = run_db_preflight(
        [
            LiveDeploymentConfig(
                strategy_name="equity_momentum",
                account_id="alpaca-paper",
                symbols=["AAPL"],
                timeframe="1h",
                signal_source="discord",
                signal_config={"channel_id": _DISCORD_CHANNEL},
            )
        ],
        db_path=_db_path(database),
    )

    assert any("signal_source" in e and "' discord'" in e for e in report.errors), report.errors


def test_non_string_stored_symbols_are_not_manufactured_into_symbols(isolated_db) -> None:
    """A foreign row with [null] is an error naming the item, not the symbol "NONE"."""
    database = isolated_db[0]
    _seed(database)
    with sqlite3.connect(database.DB_PATH) as connection:
        connection.execute("UPDATE live_deployments SET symbols = '[null]'")

    report = run_db_preflight(
        [_yaml_equity_momentum()],
        db_path=_db_path(database),
    )

    assert any("null" in e and "not clean" in e for e in report.errors), report.errors
    assert not any("NONE" in w for w in report.warnings + report.errors), report.errors


def test_active_db_row_whose_yaml_twin_is_paused_names_the_paused_entry(isolated_db) -> None:
    """The YAML has an entry, just not an ACTIVE one: calling it absent misdirects the operator."""
    database = isolated_db[0]
    _seed(database)
    paused = LiveDeploymentConfig(
        strategy_name="equity_momentum",
        account_id="alpaca-paper",
        symbols=["AAPL"],
        timeframe="1h",
        status="PAUSED",
    )

    report = run_db_preflight([paused], db_path=_db_path(database))

    assert any("YAML lists it as PAUSED" in e for e in report.errors), report.errors
    assert not any("absent from the YAML" in e for e in report.errors), report.errors


def _stored_symbol_filter(database, deployment_id: str) -> list[str]:
    with sqlite3.connect(database.DB_PATH) as connection:
        raw = connection.execute(
            "SELECT symbol_filter FROM live_deployments WHERE id = ?", (deployment_id,)
        ).fetchone()[0]
    return json.loads(raw)


def test_symbol_filter_is_normalized_on_write_and_on_read(isolated_db) -> None:
    """A cosmetically dirty symbol_filter is stored clean and read back clean.

    The stored column is checked directly: the returned object is rehydrated through the read
    path, which would hide a write that stored the dirty spelling.
    """
    database = isolated_db[0]
    database.ensure_live_account("alpaca-paper", "alpaca", name="alpaca-paper")
    config = LiveDeploymentConfig(
        strategy_name="equity_momentum",
        account_id="alpaca-paper",
        symbols=["AAPL"],
        timeframe="1h",
        symbol_filter=[" aapl ", "MSFT", " "],
    )

    created = database.create_live_deployment(config)

    assert _stored_symbol_filter(database, str(created.id)) == ["AAPL", "MSFT"]
    model = database.LiveDeploymentModel.from_live_deployment(config)
    assert model.symbol_filter_json == ["AAPL", "MSFT"]

    with sqlite3.connect(database.DB_PATH) as connection:
        connection.execute(
            "UPDATE live_deployments SET symbol_filter = ? WHERE id = ?",
            ('[" aapl ", " "]', str(created.id)),
        )
    read_back = database.get_live_deployment(str(created.id))
    assert read_back is not None
    assert read_back.symbol_filter == ["AAPL"]


# ── ACTIVE rows nothing can trade (#242) ─────────────────────────────────────


def test_active_internal_db_row_without_yaml_twin_warns_no_runner(isolated_db) -> None:
    """A DB-only internal row gets the same no-runner warning the daemon logs."""
    database = isolated_db[0]
    _seed(database)

    report = run_db_preflight([], db_path=_db_path(database))

    assert any("no live strategy runner" in w for w in report.warnings), report.warnings


def test_active_discord_db_row_does_not_warn_no_runner(isolated_db) -> None:
    database = isolated_db[0]
    _seed(database, signal_source="discord", signal_source_id="news-svc")

    report = run_db_preflight(
        [
            LiveDeploymentConfig(
                strategy_name="equity_momentum",
                account_id="alpaca-paper",
                symbols=["AAPL"],
                timeframe="1h",
                signal_source="discord",
                signal_source_id="news-svc",
                signal_config={"channel_id": "123456789012345678"},
            )
        ],
        db_path=_db_path(database),
    )

    assert not any("no live strategy runner" in w for w in report.warnings), report.warnings


def test_internal_db_row_with_yaml_twin_is_not_double_warned(isolated_db) -> None:
    """The YAML-side check reports it once; the DB diff does not repeat it."""
    database = isolated_db[0]
    _seed(database)

    report = run_db_preflight([_yaml_one()], db_path=_db_path(database))

    assert not any("no live strategy runner" in w for w in report.warnings), report.warnings


def test_internal_db_row_with_a_tradeable_yaml_twin_warns(isolated_db) -> None:
    """The row is missed by both preflights unless the DB side reports it.

    ``_validate_deployments`` warns for an ACTIVE non-Discord *YAML* deployment,
    so a Discord twin it is silent about, and the DB diff used to skip the warning
    whenever a twin existed at all: the DB row's own value went unreported except
    as ``signal_source`` drift, which is a comparison, not a warning.
    """
    database = isolated_db[0]
    _seed(database)

    report = run_db_preflight([_yaml_one("discord", "news-svc")], db_path=_db_path(database))

    warnings = [warning for warning in report.warnings if "no live strategy runner" in warning]
    assert len(warnings) == 1, report.warnings
    assert "equity_momentum" in warnings[0]


def test_a_discord_db_row_with_a_discord_yaml_twin_stays_quiet(isolated_db) -> None:
    """The row above is a defect report, not a report on every row."""
    database = isolated_db[0]
    _seed(database, signal_source="discord", signal_source_id="news-svc")

    report = run_db_preflight([_yaml_one("discord", "news-svc")], db_path=_db_path(database))

    assert report.warnings == [], report.warnings
