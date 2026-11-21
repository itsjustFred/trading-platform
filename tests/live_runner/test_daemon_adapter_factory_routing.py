"""The adapter factory decides which broker an account's orders reach.

``_create_adapter_for_account`` is the seam where an account row plus the
credential mapping becomes an adapter, so it is where a paper/demo mistake
would quietly send a real order. None of it ran before these tests.

Pinned here: which adapter class a broker gets, that ``paper`` (Alpaca) and
``demo`` (cTrader) come from the credential mapping and never from the
account's own fields, that the credentials are routed to the account that
configured them, and that a missing or unparsable credential set returns
``None`` (or raises) instead of building an adapter that will fail later.

Nothing here opens a socket: the adapters are constructed, never connected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from live_runner import daemon
from live_runner.alpaca_adapter import AlpacaLiveAdapter
from live_runner.ctrader_adapter import CTraderLiveAdapter
from shared.models import LiveAccountConfig

ACCOUNT_ID = "acc-1"

_ALPACA_KEYS = {
    "ALPACA_API_KEY": "alpaca-key",
    "ALPACA_SECRET_KEY": "alpaca-secret",
}
_CTRADER_KEYS = {
    "CTRADER_APP_ID": "app-id",
    "CTRADER_APP_SECRET": "app-secret",
    "CTRADER_ACCESS_TOKEN": "access-token",
    "CTRADER_REFRESH_TOKEN": "refresh-token",
}


def _alpaca_account(**overrides: object) -> LiveAccountConfig:
    values: dict[str, object] = {"id": ACCOUNT_ID, "broker": "alpaca"}
    values.update(overrides)
    return LiveAccountConfig(**values)  # type: ignore[arg-type]


def _ctrader_account(**overrides: object) -> LiveAccountConfig:
    values: dict[str, object] = {"id": ACCOUNT_ID, "broker": "ctrader"}
    values.update(overrides)
    return LiveAccountConfig(**values)  # type: ignore[arg-type]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelname in {"WARNING", "ERROR"}
        and record.name.endswith("live-runner")
    ]


# --------------------------------------------------------------------------- #
# Alpaca: the class, and paper mode from the credential mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "extra_env, expected_paper",
    [
        ({}, True),
        ({"ALPACA_PAPER": "true"}, True),
        ({"ALPACA_PAPER": "yes"}, True),
        ({"ALPACA_PAPER": "false"}, False),
        ({"ALPACA_PAPER": "off"}, False),
    ],
)
def test_alpaca_paper_mode_comes_from_the_credential_mapping(
    extra_env: dict[str, str], expected_paper: bool
) -> None:
    adapter = daemon._create_adapter_for_account(
        _alpaca_account(), accounts_env_values={**_ALPACA_KEYS, **extra_env}
    )

    assert isinstance(adapter, AlpacaLiveAdapter)
    # This is the flag the demo gate and the adapter read: a paper account must
    # never reach the live endpoint and a live one must never be paper.
    assert adapter.paper is expected_paper
    assert adapter.account_id == ACCOUNT_ID


def test_alpaca_credential_blocks_route_by_the_account_that_configured_them() -> None:
    """Two accounts, two key pairs: each must get its own keys and paper flag."""
    other = dict(_ALPACA_KEYS)
    other.update(
        {
            "ALPACA_API_KEY_ACC1": "key-acc1",
            "ALPACA_SECRET_KEY_ACC1": "secret-acc1",
            "ALPACA_ACCOUNT_ID_ACC1": ACCOUNT_ID,
            "ALPACA_PAPER_ACC1": "false",
            "ALPACA_API_KEY_ACC2": "key-acc2",
            "ALPACA_SECRET_KEY_ACC2": "secret-acc2",
            "ALPACA_ACCOUNT_ID_ACC2": "acc-2",
            "ALPACA_PAPER_ACC2": "true",
        }
    )

    adapter = daemon._create_adapter_for_account(_alpaca_account(), accounts_env_values=other)

    assert isinstance(adapter, AlpacaLiveAdapter)
    assert adapter.api_key == "key-acc1"
    assert adapter.secret_key == "secret-acc1"
    assert adapter.paper is False, "ACC2's paper flag must not govern ACC1's account"
    assert adapter.account_id == ACCOUNT_ID


def test_alpaca_credential_blocks_route_by_the_broker_login() -> None:
    env = dict(_ALPACA_KEYS)
    env.update(
        {
            "ALPACA_API_KEY_ACC1": "key-acc1",
            "ALPACA_SECRET_KEY_ACC1": "secret-acc1",
            "ALPACA_BROKER_LOGIN_ACC1": "login-1",
            "ALPACA_ENDPOINT_ACC1": "https://paper-api.alpaca.markets",
        }
    )

    adapter = daemon._create_adapter_for_account(
        _alpaca_account(broker_login="login-1"), accounts_env_values=env
    )

    assert isinstance(adapter, AlpacaLiveAdapter)
    assert adapter.api_key == "key-acc1"
    assert adapter.paper is True


@pytest.mark.parametrize("paper", [True, False])
def test_alpaca_endpoint_block_chooses_paper_from_the_endpoint(paper: bool) -> None:
    env = dict(_ALPACA_KEYS)
    env.update(
        {
            "ALPACA_API_KEY_ACC1": "key-acc1",
            "ALPACA_SECRET_KEY_ACC1": "secret-acc1",
            "ALPACA_ACCOUNT_ID_ACC1": ACCOUNT_ID,
            "ALPACA_ENDPOINT_ACC1": (
                "https://paper-api.alpaca.markets"
                if paper
                else "https://api.alpaca.markets"
            ),
        }
    )

    adapter = daemon._create_adapter_for_account(_alpaca_account(), accounts_env_values=env)

    assert isinstance(adapter, AlpacaLiveAdapter)
    assert adapter.paper is paper


def test_alpaca_ambiguous_credentials_return_no_adapter(caplog) -> None:
    """Two unidentified blocks: no unique mapping, so no adapter at all."""
    env = dict(_ALPACA_KEYS)
    env.update(
        {
            "ALPACA_API_KEY_ACC1": "key-acc1",
            "ALPACA_SECRET_KEY_ACC1": "secret-acc1",
            "ALPACA_API_KEY_ACC2": "key-acc2",
            "ALPACA_SECRET_KEY_ACC2": "secret-acc2",
        }
    )

    adapter = daemon._create_adapter_for_account(_alpaca_account(), accounts_env_values=env)

    assert adapter is None
    assert any("No unique Alpaca credential block mapping" in m for m in _warnings(caplog))


def test_alpaca_without_credentials_returns_no_adapter(caplog) -> None:
    with caplog.at_level("WARNING"):
        adapter = daemon._create_adapter_for_account(
            _alpaca_account(), accounts_env_values={}
        )

    assert adapter is None
    assert any("Missing Alpaca credentials" in m for m in _warnings(caplog))


def test_alpaca_an_unparsable_paper_flag_raises_so_the_account_is_skipped() -> None:
    """A typo must not silently pick a side.

    The daemon's caller catches this and skips the account, because an
    unparsable flag must not become the default (paper) either.
    """
    env = {**_ALPACA_KEYS, "ALPACA_PAPER": "maybe"}

    with pytest.raises(ValueError, match="Invalid boolean value for ALPACA_PAPER"):
        daemon._create_adapter_for_account(_alpaca_account(), accounts_env_values=env)


def test_alpaca_a_failing_constructor_returns_no_adapter(caplog, monkeypatch) -> None:
    def _explode(**_: object) -> None:
        raise RuntimeError("bad credentials object")

    monkeypatch.setattr(daemon, "AlpacaLiveAdapter", _explode)

    with caplog.at_level("WARNING"):
        adapter = daemon._create_adapter_for_account(
            _alpaca_account(), accounts_env_values=dict(_ALPACA_KEYS)
        )

    assert adapter is None
    assert any("Could not initialize alpaca adapter" in m for m in _warnings(caplog))


def test_alpaca_an_empty_mapping_does_not_fall_through_to_the_process_env(
    monkeypatch,
) -> None:
    """An explicitly passed mapping wins even when it is empty.

    A falsy-but-present mapping falling through to ``os.environ`` would let a
    developer's shell credentials build a real adapter for an account that
    configured none.
    """
    for name in [key for key in os.environ if key.startswith("ALPACA_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "process-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "process-secret")

    empty = daemon._create_adapter_for_account(_alpaca_account(), accounts_env_values={})
    assert empty is None, "an explicitly empty mapping must not read os.environ"

    inherited = daemon._create_adapter_for_account(_alpaca_account())
    assert isinstance(inherited, AlpacaLiveAdapter)
    assert inherited.api_key == "process-key"


# --------------------------------------------------------------------------- #
# cTrader: demo mode from the credential mapping, not the account
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "extra_env, expected_demo",
    [
        ({}, True),
        ({"CTRADER_DEMO": "true"}, True),
        ({"CTRADER_DEMO": "yes"}, True),
        ({"CTRADER_DEMO": "false"}, False),
        ({"CTRADER_DEMO": "0"}, False),
    ],
)
def test_ctrader_demo_mode_comes_from_the_credential_mapping(
    extra_env: dict[str, str], expected_demo: bool
) -> None:
    account = _ctrader_account(ctid="9000001", broker_login="login-1")

    adapter = daemon._create_adapter_for_account(
        account, accounts_env_values={**_CTRADER_KEYS, **extra_env}
    )

    assert isinstance(adapter, CTraderLiveAdapter)
    assert adapter.demo is expected_demo, (
        "the account row carries no demo flag of its own for cTrader: the "
        "mapping's CTRADER_DEMO is the only source"
    )
    # The broker account is the ctid; the local account id is kept alongside it
    # so the adapter can persist tokens for this row and no other.
    assert adapter.ctid_trader_account_id == 9_000_001
    assert adapter.account_config_id == ACCOUNT_ID


@pytest.mark.parametrize(
    "account_fields, expected_identifier",
    [
        ({"ctid": "9000001", "broker_login": "login-1"}, 9_000_001),
        ({"ctid": None, "broker_login": "9000002"}, 9_000_002),
    ],
)
def test_ctrader_routing_prefers_the_ctid_then_the_broker_login(
    account_fields: dict[str, str | None], expected_identifier: int
) -> None:
    adapter = daemon._create_adapter_for_account(
        _ctrader_account(**account_fields), accounts_env_values=dict(_CTRADER_KEYS)
    )

    assert isinstance(adapter, CTraderLiveAdapter)
    assert adapter.ctid_trader_account_id == expected_identifier


def test_ctrader_without_credentials_returns_no_adapter(caplog) -> None:
    with caplog.at_level("WARNING"):
        adapter = daemon._create_adapter_for_account(
            _ctrader_account(ctid="9000001"), accounts_env_values={}
        )

    assert adapter is None
    assert any("Missing cTrader credentials" in m for m in _warnings(caplog))


def test_ctrader_an_unroutable_account_id_returns_no_adapter(caplog) -> None:
    """A non-numeric identifier cannot address a broker account.

    The adapter refuses it, so the factory must report no adapter rather than
    leaving the deployment to fail at connect time.
    """
    account = _ctrader_account(id="local-id")

    with caplog.at_level("WARNING"):
        adapter = daemon._create_adapter_for_account(
            account, accounts_env_values=dict(_CTRADER_KEYS)
        )

    assert adapter is None
    assert any("Could not initialize ctrader adapter" in m for m in _warnings(caplog))


def test_ctrader_an_unparsable_demo_flag_raises_so_the_account_is_skipped() -> None:
    env = {**_CTRADER_KEYS, "CTRADER_DEMO": "perhaps"}

    with pytest.raises(ValueError, match="Invalid boolean value for CTRADER_DEMO"):
        daemon._create_adapter_for_account(
            _ctrader_account(ctid="9000001"), accounts_env_values=env
        )


@pytest.mark.parametrize("broker", ["mt5", "tradestation"])
def test_an_unimplemented_broker_builds_no_adapter(broker: str, caplog) -> None:
    """The factory refuses rather than guessing which adapter to build."""
    account = LiveAccountConfig.model_construct(
        id=ACCOUNT_ID, broker=broker, name="x", broker_login=None, ctid=None
    )

    with caplog.at_level("WARNING"):
        adapter = daemon._create_adapter_for_account(account, accounts_env_values={})

    assert adapter is None
    assert any("Broker not implemented" in m for m in _warnings(caplog))


def test_the_accounts_env_path_is_honoured(tmp_path: Path) -> None:
    """The adapter must persist refreshed tokens for this deployment's file."""
    accounts_env = tmp_path / "accounts.env"

    adapter = daemon._create_adapter_for_account(
        _ctrader_account(ctid="9000001"),
        accounts_env_values=dict(_CTRADER_KEYS),
        accounts_env_path=accounts_env,
    )

    assert isinstance(adapter, CTraderLiveAdapter)
    assert adapter.accounts_env_path == accounts_env


def test_the_default_accounts_env_path_is_the_daemon_default() -> None:
    adapter = daemon._create_adapter_for_account(
        _ctrader_account(ctid="9000001"), accounts_env_values=dict(_CTRADER_KEYS)
    )

    assert isinstance(adapter, CTraderLiveAdapter)
    assert adapter.accounts_env_path == daemon.DEFAULT_ACCOUNTS_ENV


def test_cTrader_an_empty_mapping_does_not_fall_through_to_the_process_env(
    monkeypatch,
) -> None:
    """The same precedence rule for cTrader: an explicit mapping wins."""
    for name in [key for key in os.environ if key.startswith("CTRADER_")]:
        monkeypatch.delenv(name, raising=False)
    account = _ctrader_account(ctid="9000001")

    empty = daemon._create_adapter_for_account(account, accounts_env_values={})
    assert empty is None, "an explicitly empty mapping must not read os.environ"

    monkeypatch.setenv("CTRADER_APP_ID", "app-id")
    monkeypatch.setenv("CTRADER_APP_SECRET", "app-secret")
    monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "access-token")
    inherited = daemon._create_adapter_for_account(account)
    assert isinstance(inherited, CTraderLiveAdapter)
    assert inherited.app_id == "app-id"
