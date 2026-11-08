from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from scripts.live_smoke import (
    SmokeCheck,
    SmokeReport,
    _ensure_ctrader_smoke_deployment,
    _position_qty_for_symbol,
    main,
    run_smoke,
)


def test_position_qty_for_symbol_matches_crypto_slash_alias() -> None:
    positions = [
        SimpleNamespace(symbol="ETHUSD", qty="0.5"),
        SimpleNamespace(symbol="BTCUSD", qty="0.000153092"),
    ]

    assert _position_qty_for_symbol(positions, "BTC/USD") == 0.000153092


def test_smoke_report_serializes_without_secret_fields() -> None:
    report = SmokeReport(
        mode="account-check",
        ok=True,
        started_at="2026-06-22T00:00:00+00:00",
        finished_at="2026-06-22T00:00:01+00:00",
        checks=[
            SmokeCheck(
                name="alpaca-paper-account",
                ok=True,
                details={"paper": True, "equity": 100000.0, "positions": 0},
            )
        ],
    )

    payload = asdict(report)

    assert payload["ok"] is True
    assert "api_key" not in str(payload).lower()
    assert "secret" not in str(payload).lower()


def test_live_smoke_help_exposes_ctrader_broker_minimum_default(capsys) -> None:
    try:
        main(["--help"])
    except SystemExit:
        pass

    output = capsys.readouterr().out
    assert "--ctrader-volume" in output
    assert "100000" in output
    assert "alpaca" in output
    assert "ctrader" in output


def test_live_smoke_accepts_broker_mode_aliases(tmp_path: Path, monkeypatch) -> None:
    seen_modes: list[str] = []

    async def fake_run_smoke(**kwargs):
        seen_modes.append(kwargs["mode"])
        return SmokeReport(
            mode=kwargs["mode"],
            ok=True,
            started_at="2026-07-13T00:00:00+00:00",
            finished_at="2026-07-13T00:00:01+00:00",
            checks=[SmokeCheck(name="alias", ok=True)],
        )

    monkeypatch.setattr("scripts.live_smoke.run_smoke", fake_run_smoke)

    assert main(["--mode", "alpaca", "--reports-dir", str(tmp_path)]) == 0
    assert main(["--mode", "ctrader", "--reports-dir", str(tmp_path)]) == 0
    assert seen_modes == ["alpaca-crypto-roundtrip", "ctrader-demo-roundtrip"]


def _account(database, account_id: str) -> None:
    with database.get_session() as session:
        session.add(
            database.LiveAccountModel(id=account_id, broker="ctrader", name=account_id)
        )
        session.commit()


def _smoke_rows(database) -> list:
    from sqlmodel import select

    with database.get_session() as session:
        return list(session.exec(select(database.LiveDeploymentModel)))


def test_the_smoke_deployment_is_paused_and_namespaced(isolated_db) -> None:
    """The parent row must never be tradable, and must carry the smoke convention's label.

    PAUSED is the whole reason a synthetic row is safe: a runner that picked this up would trade a
    fabricated signal. The label lives in `strategy_name` because the primary key is a UUID.
    """
    database, _engine, _data_dir = isolated_db
    database.init_db()
    _account(database, "44695443")

    deployment_id = _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="EURUSD")

    # The key is a UUID, not the descriptive label: LiveDeploymentConfig.id is typed uuid.UUID, and
    # a descriptive primary key made the daemon's whole deployment sync fail validation.
    uuid.UUID(deployment_id)
    rows = _smoke_rows(database)
    assert len(rows) == 1, rows
    assert rows[0].strategy_name == "ctrader-demo-smoke-44695443"
    assert rows[0].status == "PAUSED", (
        "an ACTIVE synthetic deployment is tradable, which is exactly what the PAUSED convention "
        "exists to prevent"
    )


def test_the_smoke_deployment_links_to_its_account(isolated_db) -> None:
    """`account_id` must point at the real account row, not a blank or invented one.

    `live_deployments.account_id` carries a foreign key, so blanking it either breaks the insert or
    leaves a row nobody can attribute to an account - the orphan class this work removes.
    """
    database, _engine, _data_dir = isolated_db
    database.init_db()
    _account(database, "44695443")

    _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="EURUSD")

    rows = _smoke_rows(database)
    assert [row.account_id for row in rows] == ["44695443"]


def test_the_smoke_deployment_records_the_upper_cased_symbol(isolated_db) -> None:
    """Symbols are stored upper-cased everywhere else, and a mixed-case one would not match."""
    database, _engine, _data_dir = isolated_db
    database.init_db()
    _account(database, "44695443")

    _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="eurusd")

    rows = _smoke_rows(database)
    assert rows[0].symbols_json == ["EURUSD"], rows[0].symbols_json


def test_the_smoke_deployment_is_idempotent_per_account(isolated_db) -> None:
    """A second smoke run must reuse the parent, not create a second row for the same label.

    Otherwise every run adds a deployment, and the signals of earlier runs point at a different
    parent than later ones - the orphans multiply instead of resolving.
    """
    database, _engine, _data_dir = isolated_db
    database.init_db()
    _account(database, "44695443")

    first = _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="EURUSD")
    second = _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="EURUSD")

    assert first == second
    assert len(_smoke_rows(database)) == 1, "a second deployment row was created for one label"


def test_discord_replay_smoke_routes_to_fake_order() -> None:
    report = asyncio.run(
        run_smoke(
            mode="discord-replay",
            accounts_env=Path(".accounts.env"),
            alpaca_symbol="BTC/USD",
            alpaca_notional=10.0,
            ctrader_symbol="EURUSD",
            ctrader_volume=100000,
            timeout_seconds=1,
        )
    )

    assert report.ok is True
    assert report.checks[0].name == "discord-replay-routing"
    assert report.checks[0].details["symbol"] == "AAPL"
    assert report.checks[0].details["broker_order_id"] == "discord-smoke-order"
