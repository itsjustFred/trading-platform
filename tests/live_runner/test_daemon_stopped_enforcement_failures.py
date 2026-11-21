"""STOPPED enforcement: the failure paths that must never look like success.

``_enforce_stopped_deployments`` stamps a deployment as enforced only when
every sweep reports ``ControlOutcome.complete``. The happy paths and the
partial-sweep paths already had tests; these cover the ones that never ran:

* the account has no adapter yet - nothing was swept, so there is nothing to
  stamp, and the pass has to run again once an adapter exists;
* an account that can never get an adapter (it is not in the database) - the
  operator is told the broker must flatten it by hand;
* a cancel that raised, after which the flatten still runs;
* a flatten that raised, or an adapter that cannot flatten - the deployment is
  neither flat nor stamped.

Every test uses the throwaway SQLite sandbox from ``tests/conftest.py`` and a
fake adapter: no broker, network, service or production database is touched.
"""

from __future__ import annotations

import asyncio

import pytest

from live_runner import daemon
from shared.database import list_stopped_enforced_deployment_ids
from shared.models import LiveAccountConfig
from tests.test_daemon_control_plane import (
    ACCOUNT_ID,
    DEPLOYMENT_ID,
    _ControlAdapter,
    _deployment,
    _seed,
)
from tests.test_daemon_control_plane import (
    _enforce as _enforce_with_known_account,
)

_KNOWN_ACCOUNT = {ACCOUNT_ID: LiveAccountConfig(id=ACCOUNT_ID, broker="ctrader")}


@pytest.fixture(autouse=True)
def _clear_pending_cancel_passes():
    """The pending_cancel escalation counter is process state, not test state."""
    daemon._PENDING_CANCEL_PASSES.clear()
    yield
    daemon._PENDING_CANCEL_PASSES.clear()


def _enforce(
    database,
    adapter: _ControlAdapter | None,
    *,
    account_lookup: dict[str, LiveAccountConfig] | None,
    already: set[str] | None = None,
) -> set[str]:
    """One tick of STOPPED enforcement against ``database``'s single deployment."""
    ledger = set() if already is None else set(already)
    asyncio.run(
        daemon._enforce_stopped_deployments(
            [_deployment(database)],
            {ACCOUNT_ID: adapter} if adapter is not None else {},
            already_enforced=ledger,
            account_lookup=account_lookup,
        )
    )
    return ledger


def _stamped(database) -> bool:
    return DEPLOYMENT_ID in list_stopped_enforced_deployment_ids()


def test_a_deployment_with_no_adapter_yet_is_retried_not_stamped(
    isolated_db_sandbox, caplog
) -> None:
    """A stop observed before its account connected has swept nothing.

    Stamping it here would record an enforcement that never happened, and the
    next tick would skip it, so the open position and resting orders would stay.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database)

    with caplog.at_level("INFO"):
        ledger = _enforce(database, None, account_lookup=_KNOWN_ACCOUNT)

    assert ledger == set()
    assert not _stamped(database)
    retry = [
        record
        for record in caplog.records
        if "no active adapter yet" in record.getMessage()
        and "will retry" in record.getMessage()
    ]
    assert retry, "a missing adapter must say the enforcement is being retried"
    assert retry[0].levelname == "INFO"
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_a_missing_account_is_refused_and_reported_for_manual_flatten(
    isolated_db_sandbox, caplog
) -> None:
    """An account no longer in the database can never get an adapter.

    The cancel/flatten can therefore never run, and the only safe outcome is
    an ERROR telling the operator to flatten at the broker - never a stamp that
    says the account is flat.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database)

    with caplog.at_level("WARNING"):
        ledger = _enforce(database, None, account_lookup={})

    assert ledger == set()
    assert not _stamped(database)
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "can never get an adapter" in message
    assert "flatten it at the broker by hand" in message
    assert "account is not in the database" in message
    assert errors[0].data["deployment_id"] == DEPLOYMENT_ID
    assert errors[0].data["account_id"] == ACCOUNT_ID


def test_a_retry_after_the_adapter_appeared_stamps_the_deployment(
    isolated_db_sandbox,
) -> None:
    """The whole point of not stamping: the next tick really does enforce it."""
    database, _, _ = isolated_db_sandbox
    _seed(database)

    first = _enforce(database, None, account_lookup=_KNOWN_ACCOUNT)
    assert first == set()

    adapter = _ControlAdapter()
    ledger = _enforce(database, adapter, account_lookup=_KNOWN_ACCOUNT)

    assert adapter.calls == ["cancel", "flatten", "orphan"]
    assert ledger == {DEPLOYMENT_ID}
    assert _stamped(database)


def test_a_cancel_that_raises_still_flattens_and_is_reported(
    isolated_db_sandbox, caplog
) -> None:
    """A failed cancel says nothing about whether positions can be closed.

    Leaving exposure open to protect an ordering rule is the wrong trade, so
    the flatten runs - and the cancel failure is what keeps the deployment
    unenforced.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter()

    async def _explode() -> object:
        adapter.calls.append("cancel")
        raise RuntimeError("order list unavailable")

    adapter.cancel_open_orders = _explode  # type: ignore[method-assign]

    with caplog.at_level("WARNING"):
        ledger = _enforce(database, adapter, account_lookup=_KNOWN_ACCOUNT)

    assert adapter.calls == ["cancel", "flatten", "orphan"]
    assert ledger == set()
    assert not _stamped(database)
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert any("Failed to cancel open orders" in record.getMessage() for record in errors)
    assert any(
        "cancel failed: RuntimeError('order list unavailable')" in record.getMessage()
        for record in errors
    ), "the unenforced reason must name the exception that caused it"


def test_a_flatten_that_raises_leaves_the_deployment_unenforced(
    isolated_db_sandbox, caplog
) -> None:
    """A flatten that never returned an outcome cannot be read as complete.

    The deployment must stay unenforced so the next tick retries it: the
    position is still open and the operator has to see the failure.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(flatten_error=RuntimeError("broker refused the close"))

    with caplog.at_level("WARNING"):
        ledger = _enforce(database, adapter, account_lookup=_KNOWN_ACCOUNT)

    assert adapter.calls == ["cancel", "flatten"], (
        "a flatten that raised cannot have swept the protections it detached"
    )
    assert ledger == set()
    assert not _stamped(database)
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1
    assert "Failed to flatten positions" in errors[0].getMessage()
    assert errors[0].data["error"] == "RuntimeError('broker refused the close')"
    assert errors[0].data["account_id"] == ACCOUNT_ID


def test_a_flatten_that_raises_is_retried_on_the_next_tick(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)

    first = _enforce(
        database,
        _ControlAdapter(flatten_error=RuntimeError("broker refused")),
        account_lookup=_KNOWN_ACCOUNT,
    )
    assert first == set()

    adapter = _ControlAdapter()
    ledger = _enforce(database, adapter, account_lookup=_KNOWN_ACCOUNT)

    assert adapter.calls == ["cancel", "flatten", "orphan"]
    assert ledger == {DEPLOYMENT_ID}
    assert _stamped(database)


def test_an_adapter_that_cannot_flatten_never_stamps(isolated_db_sandbox, caplog) -> None:
    """An adapter without a flatten leaves the account with open positions.

    The deployment stays unenforced, and the flatten is not attempted again
    against an adapter that has already said it cannot do it.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(supports_flatten=False)

    with caplog.at_level("WARNING"):
        ledger = _enforce(database, adapter, account_lookup=_KNOWN_ACCOUNT)

    assert adapter.calls == ["cancel"], (
        "the account is not flat, so there are no orphaned protections to sweep yet"
    )
    assert ledger == set()
    assert not _stamped(database)
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "Adapter cannot flatten positions" in message
    assert "is NOT flat" in message
    assert errors[0].data["deployment_id"] == DEPLOYMENT_ID


def test_a_flatten_that_is_not_implemented_never_stamps(isolated_db_sandbox) -> None:
    """Claiming a capability the adapter does not implement must fail closed.

    ``supports_flatten`` is the adapter's own declaration, so an adapter can
    declare flatten and still raise ``NotImplementedError`` (a base-class
    fallback). The outcome is the same as cannot-flatten: nothing to report,
    nothing to stamp.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(flatten_error=NotImplementedError("not supported"))

    ledger = _enforce(database, adapter, account_lookup=_KNOWN_ACCOUNT)

    assert adapter.calls == ["cancel", "flatten"]
    assert ledger == set()
    assert not _stamped(database)


def test_the_known_account_path_still_reports_an_enforceable_account(
    isolated_db_sandbox,
) -> None:
    """The shared helper's own account lookup: a stop with an adapter enforces."""
    database, _, _ = isolated_db_sandbox
    _seed(database)

    ledger = _enforce_with_known_account(database, _ControlAdapter())

    assert ledger == {DEPLOYMENT_ID}
    assert _stamped(database)
