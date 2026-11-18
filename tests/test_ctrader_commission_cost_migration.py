"""Existing cTrader ``live_executions.commission`` rows become a positive cost (#205).

Before the sign fix cTrader stored ``ProtoOADeal.commission`` raw, i.e. a charge as a NEGATIVE
number. The convention is now a positive cost for every broker, so ``init_db`` converts the
existing cTrader rows once (``abs`` of the raw value) and records that it did.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

_ROWS = [
    # (execution id, signal id, commission)
    ("ct-charge", "sig-ct", -0.02),
    ("ct-zero", "sig-ct", 0.0),
    ("ct-unknown", "sig-ct", None),
    ("ct-positive", "sig-ct", 1.5),
    ("alp-equity", "sig-alp", 0.0),
    ("alp-crypto-fee", "sig-alp", 0.4),
    ("alp-negative", "sig-alp", -0.3),
    # A broker stored with a different case is still cTrader.
    ("ct-mixed-case", "sig-ct-mixed", -0.04),
]


def _seed(engine) -> None:
    with engine.begin() as connection:
        for account_id, broker in (
            ("acc-alp", "alpaca"),
            ("acc-ct", "ctrader"),
            ("acc-ct-mixed", "CTrader"),
        ):
            connection.execute(
                text(
                    "INSERT INTO live_accounts (id, broker, name, is_demo) "
                    "VALUES (:id, :broker, :id, 0)"
                ),
                {"id": account_id, "broker": broker},
            )
            connection.execute(
                text(
                    "INSERT INTO live_deployments (id, strategy_name, account_id, symbols, "
                    "timeframe, status, created_at, updated_at) VALUES "
                    "(:id, :id, :account, '[]', '1m', 'ACTIVE', "
                    "'2026-11-01 00:00:00', '2026-11-01 00:00:00')"
                ),
                {"id": f"dep-{account_id}", "account": account_id},
            )
        for signal_id, deployment_id in (
            ("sig-ct", "dep-acc-ct"),
            ("sig-alp", "dep-acc-alp"),
            ("sig-ct-mixed", "dep-acc-ct-mixed"),
        ):
            connection.execute(
                text(
                    "INSERT INTO live_signals (id, deployment_id, symbol, action, "
                    "theoretical_price, timestamp, metadata, source) VALUES "
                    "(:id, :dep, 'EURUSD', 'buy', 1.0, '2026-11-03 15:00:00', '{}', 'internal')"
                ),
                {"id": signal_id, "dep": deployment_id},
            )
        for execution_id, signal_id, commission in _ROWS:
            connection.execute(
                text(
                    "INSERT INTO live_executions (id, signal_id, actual_price, filled_qty, "
                    "commission, timestamp, broker_order_id, dedupe_key) VALUES "
                    "(:id, :sig, 1.0, 1.0, :commission, '2026-11-03 15:00:00', 'bo', :id)"
                ),
                {"id": execution_id, "sig": signal_id, "commission": commission},
            )


def _commissions(engine) -> dict[str, float | None]:
    with engine.connect() as connection:
        return dict(
            connection.execute(text("SELECT id, commission FROM live_executions")).fetchall()
        )


def _database_with_old_ctrader_rows(isolated_db_no_schema):
    database, engine, _data_dir = isolated_db_no_schema
    database.SQLModel.metadata.create_all(engine)
    _seed(engine)
    return database, engine


def test_init_db_converts_existing_ctrader_commissions_to_a_positive_cost(
    isolated_db_no_schema,
):
    database, engine = _database_with_old_ctrader_rows(isolated_db_no_schema)

    database.init_db()

    commissions = _commissions(engine)
    assert commissions["ct-charge"] == pytest.approx(0.02)
    assert commissions["ct-mixed-case"] == pytest.approx(0.04), "broker match ignores case"
    assert commissions["ct-positive"] == 1.5, "an already positive value is left as it is"
    assert commissions["ct-zero"] == 0.0
    assert str(commissions["ct-zero"]) == "0.0", "a known zero must never become -0.0"
    assert commissions["ct-unknown"] is None, "unknown stays unknown"
    # Only cTrader rows are touched: Alpaca never stored the broker's signed cash flow.
    assert commissions["alp-equity"] == 0.0
    assert commissions["alp-crypto-fee"] == 0.4
    assert commissions["alp-negative"] == -0.3


def test_ctrader_commission_migration_runs_once_and_never_flips_new_rows(
    isolated_db_no_schema,
):
    database, engine = _database_with_old_ctrader_rows(isolated_db_no_schema)
    database.init_db()
    with engine.begin() as connection:
        # A rebate written by the fixed adapter is a NEGATIVE cost; a re-run must keep it.
        connection.execute(
            text("UPDATE live_executions SET commission = -0.5 WHERE id = 'ct-zero'")
        )

    with engine.begin() as connection:
        database._migrate_ctrader_commission_to_cost(connection)
    database._migrate_schema()

    commissions = _commissions(engine)
    assert commissions["ct-zero"] == -0.5
    assert commissions["ct-charge"] == pytest.approx(0.02)


def test_ctrader_commission_migration_is_recorded_on_a_fresh_database(
    isolated_db_no_schema,
):
    database, engine, _data_dir = isolated_db_no_schema

    database.init_db()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO live_accounts (id, broker, name, is_demo) "
                "VALUES ('acc-ct', 'ctrader', 'acc-ct', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO live_deployments (id, strategy_name, account_id, symbols, "
                "timeframe, status, created_at, updated_at) VALUES "
                "('dep', 'dep', 'acc-ct', '[]', '1m', 'ACTIVE', "
                "'2026-11-01 00:00:00', '2026-11-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO live_signals (id, deployment_id, symbol, action, "
                "theoretical_price, timestamp, metadata, source) VALUES "
                "('sig', 'dep', 'EURUSD', 'buy', 1.0, '2026-11-03 15:00:00', '{}', 'internal')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO live_executions (id, signal_id, actual_price, filled_qty, "
                "commission, timestamp, broker_order_id, dedupe_key) VALUES "
                "('rebate', 'sig', 1.0, 1.0, -0.25, '2026-11-03 15:00:00', 'bo', 'k')"
            )
        )

    database._migrate_schema()

    assert _commissions(engine)["rebate"] == -0.25
