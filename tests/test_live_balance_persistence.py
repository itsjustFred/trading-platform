"""The live-balance persistence layer: column mapping, read order, and the empty-account guard.

    Why this file exists

    2026-10-15. `test_money_paths_are_pinned.py` named `shared/database.py`'s live-balance functions in
    a `KNOWN_UNSCANNED` list rather than scanning them, on the grounds that scanning all of `shared`
    would drag in fifteen backtest equity-curve functions that are not live money. Naming them was
    honest but left them unexamined, and "unexamined" is where the previous six gaps lived.

    Three mutations, against the full suite:

        M13  the persisted row writes cash into the equity column            SURVIVED - 4,982 passed
        M14  list_live_balances returns newest-first                          SURVIVED
        M15  get_latest_live_balance("") returns a zero record, not None     SURVIVED

    **M14 is the one with a real consumer.** `viewer/main.py:13043` reads the per-account history, and
    every viewer test monkeypatches `list_live_balances` - so the viewer's own tests could never have
    caught an ordering change in the function it replaces. Ascending order is the contract: the last
    element is the newest row. Flip it and `[-1]` silently becomes the *oldest* row, which reads as
    plausible data rather than as an error.

    **M13 is the same mistake as the one at L199**, one layer down. There, `live_balances.equity` was
    recorded as `balance` and nothing noticed. Here the swap happens in the mapper that every write
    and read goes through, so no consumer can detect it - and the production table already carries the
    column pair it is supposed to distinguish.

    All three tests run against a real SQLite file in `tmp_path`, not a stub, because the failure modes
    here are about column order and row order - precisely the things a stubbed session would agree
    with regardless.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def balance_db(monkeypatch, tmp_path: Path):
    """`shared.database` pointed at a throwaway SQLite file.

    `DATA_DIR` is the module's own resolved constant, so patching it and forcing re-init is enough;
    `DB_PATH` and the engine URL are rebuilt from it by `init_db`.
    """
    import shared.database as database

    # `reset_engine()` as well as the flags: the SQLAlchemy engine is a module-level cache, so
    # without this the fixture silently keeps using the *previous* test's database. The symptom is
    # baffling rather than obvious - this file's own ordering test saw four rows when it saved
    # three, because an earlier test's row had followed it into the wrong file.
    database.reset_engine()
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    database.init_db()
    yield database
    database.reset_engine()
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)


def _record(account_id: str, when: datetime, balance: float, equity: float):
    from shared.models import LiveBalanceRecord

    return LiveBalanceRecord(
        account_id=account_id,
        timestamp=when,
        balance=balance,
        equity=equity,
    )


# --- M13: the two columns must stay distinct through the mapper -----------------


def test_a_saved_balance_keeps_cash_and_equity_in_their_own_columns(balance_db) -> None:
    """Cash 3,000 and equity 4,500. A mapper that swaps them stores a row that is merely plausible."""
    record = _record(
        "acc-1",
        datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc),
        balance=3_000.0,
        equity=4_500.0,
    )
    balance_db.save_live_balance(record)

    stored = balance_db.list_live_balances("acc-1")
    assert len(stored) == 1
    assert stored[0].balance == pytest.approx(3_000.0)
    assert stored[0].equity == pytest.approx(4_500.0)


def test_the_live_balance_model_round_trips_without_swapping_columns(balance_db) -> None:
    """The mapper pair is used by the viewer as well as the poller, so pin it directly too."""
    from shared.database import LiveBalanceModel

    original = _record(
        "acc-1",
        datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc),
        balance=3_000.0,
        equity=4_500.0,
    )
    row = LiveBalanceModel.from_live_balance(original)
    assert row.balance == pytest.approx(3_000.0)
    assert row.equity == pytest.approx(4_500.0)

    back = row.to_live_balance()
    assert back.balance == pytest.approx(3_000.0)
    assert back.equity == pytest.approx(4_500.0)


# --- M14: the read order is the contract ---------------------------------------


def test_balance_history_is_oldest_first_so_the_last_row_is_the_newest(balance_db) -> None:
    base = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
    for offset in (2, 0, 1):  # deliberately out of order
        balance_db.save_live_balance(
            _record("acc-1", base + timedelta(hours=offset), 100.0 + offset, 200.0 + offset)
        )

    history = balance_db.list_live_balances("acc-1")
    # SQLite hands the timestamps back naive, so compare in naive terms rather than mixing the two.
    naive = [row.timestamp.replace(tzinfo=None) for row in history]
    expected = sorted((base + timedelta(hours=offset)).replace(tzinfo=None) for offset in (0, 1, 2))
    assert naive == expected
    # The viewer reads the last element as "now". If this is not the newest, every displayed balance
    # is the oldest row in the table and nothing raises.
    assert naive[-1] == expected[-1] == datetime(2026, 1, 3, 14, 0)
    assert history[-1].balance == pytest.approx(102.0)


# --- M15: no account is not an account with a zero balance ----------------------


def test_the_latest_balance_of_no_account_is_none_not_a_zero_record(balance_db) -> None:
    """A zero balance is a real, reportable state. Conflating it with 'no such account' hides both."""
    assert balance_db.get_latest_live_balance("") is None
    assert balance_db.get_latest_live_balance("   ") is None
    assert balance_db.get_latest_live_balance("never-seen") is None


def test_an_account_with_only_zero_rows_still_reports_that_zero(balance_db) -> None:
    """The guard is about *absent*, not about *zero* - so this must NOT come back as None."""
    balance_db.save_live_balance(
        _record("acc-zero", datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc), 0.0, 0.0)
    )
    found = balance_db.get_latest_live_balance("acc-zero")
    assert found is not None
    assert found.balance == pytest.approx(0.0)
    assert found.equity == pytest.approx(0.0)
