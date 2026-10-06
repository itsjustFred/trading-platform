"""The cTrader smoke signal must have a parent deployment row.

Seven of seven production live_signals named a deployment that does not exist, so all three
executions hanging off them were invisible to every deployment-scoped query. The smoke script is
the only writer that can produce an orphan, which is why this pins its behaviour rather than the
database's current state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, create_engine, text

from scripts.live_smoke import _ensure_ctrader_smoke_deployment

ACCOUNT_ID = "44695443"
DEPLOYMENT_ID = f"ctrader-demo-smoke-{ACCOUNT_ID}"
SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "live_smoke.py"


@pytest.fixture
def strict_session(monkeypatch):
    """SQLite needs the pragma on every connection; the production engine sets it the same way."""
    import shared.database as db
    from sqlalchemy import event
    from sqlmodel import SQLModel

    from shared.database import LiveAccountModel

    eng = create_engine("sqlite://")

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(eng)
    with Session(eng) as session:
        session.add(
            LiveAccountModel(
                id=ACCOUNT_ID,
                broker="ctrader",
                name=f"cTrader {ACCOUNT_ID}",
                ctid=ACCOUNT_ID,
                is_demo=True,
            )
        )
        session.commit()

    monkeypatch.setattr(db, "get_session", lambda: Session(eng))
    return eng


def test_smoke_signal_gets_a_parent_deployment(strict_session):
    from shared.database import LiveDeploymentModel

    _ensure_ctrader_smoke_deployment(account_id=ACCOUNT_ID, symbol="XAUUSD")

    returned = _ensure_ctrader_smoke_deployment(account_id=ACCOUNT_ID, symbol="XAUUSD")

    import uuid as _uuid

    _uuid.UUID(returned), "the helper must return a UUID, since LiveDeploymentConfig.id is a UUID"
    with Session(strict_session) as session:
        from sqlmodel import select as _select

        row = session.exec(
            _select(LiveDeploymentModel).where(
                LiveDeploymentModel.strategy_name == DEPLOYMENT_ID
            )
        ).first()
        assert row is not None, "smoke signal would be orphaned again"
        assert str(row.id) == returned, "the signal must be pointed at the row the helper returned"
        assert row.strategy_name == DEPLOYMENT_ID
        # Mirrors the discord:: synthetic convention so the daemon never trades it.
        assert row.status == "PAUSED"
        assert row.symbols_json == ["XAUUSD"]


def test_deployment_creation_is_idempotent(strict_session, monkeypatch):
    monkeypatch.setattr("shared.database.get_session", lambda: Session(strict_session))
    for _ in range(3):
        _ensure_ctrader_smoke_deployment(account_id=ACCOUNT_ID, symbol="XAUUSD")

    with Session(strict_session) as session:
        count = session.exec(
            text("select count(*) from live_deployments where strategy_name = :n").bindparams(
                n=DEPLOYMENT_ID
            )
        ).one()
    assert tuple(count) == (1,), "repeated calls must not create a second deployment"


def test_missing_account_fails_closed_instead_of_orphaning(strict_session):
    """live_deployments.account_id has an FK to live_accounts, so an absent account must raise.

    Failing is correct. Writing the signal anyway would recreate the exact invisibility this
    function exists to prevent.
    """
    with pytest.raises(Exception):  # noqa: B017 - the driver error type is not portable
        _ensure_ctrader_smoke_deployment(account_id="99999999", symbol="XAUUSD")


def test_worker_calls_the_helper_before_writing_the_signal():
    """Pin the call site: a helper nobody invokes cannot prevent anything."""
    source = SOURCE.read_text(encoding="utf-8")
    signal_construction = (
        'signal = LiveSignal(\n        deployment_id=smoke_deployment_id'
    )

    assert source.count("_ensure_ctrader_smoke_deployment(") >= 2, (
        "the helper must be defined and called, or the signal is orphaned again"
    )
    assert source.count(signal_construction) == 1, (
        "the signal no longer takes the helper's return value; if it went back to building its own "
        "deployment_id it would point at the descriptive label rather than the UUID row id"
    )
    assert source.index("_ensure_ctrader_smoke_deployment(\n        account_id=account_id") < source.index(
        signal_construction
    ), "the deployment must be ensured before the signal that depends on it"


def test_orphan_check_would_now_be_clean_for_a_smoke_signal(strict_session):
    """The production query that found 7 of 7 orphans must return nothing for a fresh smoke run."""
    _ensure_ctrader_smoke_deployment(account_id=ACCOUNT_ID, symbol="XAUUSD")
    with Session(strict_session) as session:
        orphans = session.execute(
            text(
                "select count(*) from live_signals "
                "where deployment_id not in (select id from live_deployments)"
            )
        ).scalar_one()
    assert orphans == 0