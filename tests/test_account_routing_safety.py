from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import live_runner.daemon as daemon
from live_runner.config_parser import parse_live_deployments_config
from shared.models import LiveAccountConfig, LiveDeploymentConfig


def _write_config(path: Path, content: str) -> Path:
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def test_live_config_rejects_duplicate_internal_account_ids(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
accounts:
  - id: shared-account
    broker: alpaca
  - id: shared-account
    broker: ctrader
    ctid: "12345"

deployments: []
        """,
    )

    with pytest.raises(ValueError, match="duplicate account id.*shared-account"):
        parse_live_deployments_config(config_path)


def test_live_config_rejects_duplicate_ctrader_broker_identifier(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
accounts:
  - id: strategy-a-demo
    broker: ctrader
    ctid: "12345"
  - id: strategy-b-demo
    broker: ctrader
    ctid: "12345"

deployments: []
        """,
    )

    with pytest.raises(ValueError, match="cTrader broker account.*12345"):
        parse_live_deployments_config(config_path)


def test_live_config_rejects_numerically_equivalent_ctrader_broker_identifier(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
accounts:
  - id: strategy-a-demo
    broker: ctrader
    ctid: "00123"
  - id: strategy-b-demo
    broker: ctrader
    ctid: "123"

deployments: []
        """,
    )

    with pytest.raises(ValueError, match="cTrader broker account.*123"):
        parse_live_deployments_config(config_path)


def test_live_config_allows_distinct_identifiers_within_one_ctrader_account(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
accounts:
  - id: local-strategy-account
    broker: ctrader
    broker_login: "67890"
    ctid: "12345"

deployments: []
        """,
    )

    accounts, deployments = parse_live_deployments_config(config_path)

    assert deployments == []
    assert accounts == [
        LiveAccountConfig(
            id="local-strategy-account",
            broker="ctrader",
            broker_login="67890",
            ctid="12345",
        )
    ]


def test_live_config_rejects_non_positive_ctrader_identifier(tmp_path: Path) -> None:
    """Zero and negative cTIDs are rejected, and the message says why.

    The non-positive guard is separate from the non-numeric one: `"0"` and `"-7"`
    both parse as `int`, so they sail past the try/except and reach
    `numeric_identifier <= 0`. Removing that comparison leaves every other routing
    test green, because all of them use positive identifiers.

    Why it matters: a cTID is what the broker routes an order to. A zero or negative
    one is not a real account, and reaching the adapter means an order addressed to
    nothing — or, depending on the broker's handling, to whatever the API treats as
    the default account. The error names the value so the operator can see which
    field is wrong.
    """
    for identifier, described in (("0", "non-positive"), ("-7", "non-positive")):
        config_path = _write_config(
            tmp_path / "live_deployments.yaml",
            f"""
accounts:
  - id: broken-demo
    broker: ctrader
    ctid: "{identifier}"

deployments: []
            """,
        )
        with pytest.raises(ValueError, match=described) as excinfo:
            parse_live_deployments_config(config_path)
        assert identifier in str(excinfo.value), (
            "the error does not name the offending identifier: " + str(excinfo.value)
        )


def test_a_ctrader_account_with_no_identifier_at_all_is_rejected(tmp_path: Path) -> None:
    """Empty and whitespace-only identifiers fail rather than routing on a fallback.

    `ctrader_account_identifier` falls back `ctid -> broker_login -> id`. So an account
    with none of them set resolves to the *local* account id, which is a non-cTrader
    identifier — and if that local id happens to be numeric, it would route orders to
    a broker account number that was never configured.

    Two shapes: no identifiers at all, and a whitespace-only ctid that `int()` cannot
    parse.
    """
    for body, match in (
        (
            """
accounts:
  - id: no-identifier-demo
    broker: ctrader

deployments: []
            """,
            "no numeric broker identifier",
        ),
        (
            """
accounts:
  - id: blank-ctid-demo
    broker: ctrader
    ctid: "   "

deployments: []
            """,
            "no numeric broker identifier",
        ),
    ):
        config_path = _write_config(tmp_path / "live_deployments.yaml", body)
        with pytest.raises(ValueError, match=match):
            parse_live_deployments_config(config_path)


def test_daemon_fails_closed_before_adapter_creation_for_duplicate_ctrader_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = [
        LiveAccountConfig(id="strategy-a-demo", broker="ctrader", ctid="12345"),
        LiveAccountConfig(id="strategy-b-demo", broker="ctrader", ctid="12345"),
    ]
    deployments = [
        LiveDeploymentConfig(
            strategy_name="fx_breakout",
            account_id="strategy-a-demo",
            symbols=["EURUSD"],
            timeframe="15m",
        )
    ]
    adapter_creations: list[str] = []

    async def _load_state():
        return accounts, deployments

    def _create_adapter(account, **_kwargs):
        adapter_creations.append(account.id)
        return None

    async def _stop_after_iteration(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "_load_db_accounts_and_deployments", _load_state)
    monkeypatch.setattr(daemon, "_create_adapter_for_account", _create_adapter)
    monkeypatch.setattr(daemon.asyncio, "sleep", _stop_after_iteration)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            daemon.sync_deployments_loop(
                {},
                {},
                {},
                accounts_env=Path("/nonexistent/accounts.env"),
                interval_seconds=0,
            )
        )

    assert adapter_creations == []


def test_duplicate_account_ids_are_compared_after_stripping(tmp_path: Path) -> None:
    """Whitespace-padded ids are the same account, and the set uses the stripped value.

    Verified as an **equivalent mutant** rather than a gap: mutating the local to
    `str(account.id)` survives, because that single `strip()` feeds *both* the
    membership check and `account_ids.add`, so the set comparison is unchanged and
    only the error message would differ. Recorded here so the next audit does not
    re-report it as a gap.

    Asserted because the behaviour is load-bearing even though the mutation is not:
    `"a"` and `" a "` name one account, and two local entries for one account would
    each be routed separately.

    The cTrader routing key gets the same treatment for the same reason - a cTID
    differing only by whitespace is one broker account, and letting it through would
    leave two local rows claiming it.
    """
    exact = _write_config(
        tmp_path / "exact.yaml",
        """accounts:
  - id: shared-account
    broker: alpaca
  - id: shared-account
    broker: alpaca

deployments: []
""",
    )
    with pytest.raises(ValueError, match="duplicate account id.*shared-account"):
        parse_live_deployments_config(exact)

    padded = _write_config(
        tmp_path / "padded.yaml",
        """accounts:
  - id: shared-account
    broker: alpaca
  - id: '  shared-account  '
    broker: alpaca

deployments: []
""",
    )
    with pytest.raises(ValueError, match="duplicate account id"):
        parse_live_deployments_config(padded)

    padded_ctid = _write_config(
        tmp_path / "padded-ctid.yaml",
        """accounts:
  - id: strategy-a
    broker: ctrader
    ctid: "12345"
  - id: strategy-b
    broker: ctrader
    ctid: " 12345 "

deployments: []
""",
    )
    with pytest.raises(ValueError, match="assigned to multiple local accounts"):
        parse_live_deployments_config(padded_ctid)


def test_live_config_rejects_non_numeric_ctrader_identifier(tmp_path: Path) -> None:
    """B3: a ctrader account without numeric cTID fails at validation."""
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
accounts:
  - id: bad-ctrader
    broker: ctrader
    broker_login: "not-a-number"

deployments: []
        """,
    )

    with pytest.raises(ValueError, match="no numeric broker identifier"):
        parse_live_deployments_config(config_path)
