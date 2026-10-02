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


class TestStockkitRouteNormalisationAndAccountLabelMatch:
    """The normalisation that decides which broker account a StockKit signal routes to.

    Two mutations survived, and both are the same shape: **a normaliser whose whole job is to
    make two spellings of the same thing compare equal, tested only with spellings that already
    compare equal.**

    `_ALIASES` contains `ram_jet` and `rocket_fuel` with underscores. The route normaliser
    therefore does `str(value or "").strip().lower().replace("-", "_")` before the lookup - so
    `ram-jet` resolves only *because* the dash is replaced first. Remove that and `ram-jet` is a
    silent miss: no route, so no account number, so the signal is refused. The same for
    `rocket-fuel` and `rocketfuel-longsonly`.

    `_normalize_account_label` strips everything outside `[a-z0-9]`, which is what lets the env
    label `"Rocket Fuel"` match the route `rocket_fuel`. Remove it and the comparison becomes
    `"rocket fuel" != "rocketfuel"` - again a silent miss, and again in the direction of *not
    routing*, which is the safe direction to fail. That is worth stating plainly, because it is
    the same asymmetry as the `1M` collision: **this class of bug is easy to miss precisely
    because the failure is a refusal rather than a wrong order.**

    The fallback-table behaviour is asserted alongside, since the whole point of the label
    matching is that it beats a hardcoded account number when paper accounts are re-created.
    """

    def test_a_dashed_alias_resolves_because_the_dash_is_replaced_first(self) -> None:
        """The exact spelling that needs the replacement - and the ones that do not."""
        from live_runner.stockkit_routing import normalize_stockkit_route

        for spelling in ("ram-jet", "ram_jet", "ram jet", "RAM-JET", "  ram-jet  "):
            assert normalize_stockkit_route(spelling) == "ramjet", (
                f"{spelling!r} did not resolve to ramjet; the alias table only holds the "
                "underscore spelling, so this depends entirely on the dash replacement"
            )
        for spelling in ("rocket-fuel", "rocket_fuel", "rocket fuel", "rocketfuel-longsonly"):
            expected = "rocketfuel"
            assert normalize_stockkit_route(spelling) == expected, spelling

    def test_an_unrecognised_route_resolves_to_nothing_at_all(self) -> None:
        """`""`, not the input echoed back.

        The distinction is what makes an unrecognised label *refusable* downstream rather than
        a route that resolves to some account. Asserted on several near-misses.
        """
        from live_runner.stockkit_routing import normalize_stockkit_route

        for unknown in ("", "   ", None, "main-account", "a-b", "banana", "nitro-fast"):
            assert normalize_stockkit_route(unknown) == "", (
                f"{unknown!r} resolved to {normalize_stockkit_route(unknown)!r} rather than "
                "nothing, so an unrecognised label would look routable"
            )

    def test_the_account_label_is_compared_with_separators_and_case_removed(self) -> None:
        """Every spelling of one label must normalise to the same string.

        This is the property the function exists for, so it is asserted as a set rather than
        case by case - the point is that they are *equal*, not that each works.
        """
        from live_runner.stockkit_routing import _normalize_account_label

        spellings = ["Rocket Fuel", "rocket_fuel", "ROCKET-FUEL", "rocket.fuel", " rocket fuel "]
        normalised = {_normalize_account_label(v) for v in spellings}
        assert normalised == {"rocketfuel"}, normalised
        for absent in (None, "", "   ", 0, False):
            assert _normalize_account_label(absent) == "", repr(absent)

    def test_a_separated_env_label_still_matches_its_route(self) -> None:
        """**The end-to-end claim, and the one the mutation actually breaks.**

        The env label is written by a human - `"Rocket Fuel"` - and the route is a token
        - `rocket_fuel`. They only compare equal through the label normaliser, so this is the
        assertion that distinguishes a working label match from a silently-refused one.
        """
        from live_runner.stockkit_routing import expected_account_number_for_route

        for label in ("Rocket Fuel", "ROCKET-FUEL", "rocket_fuel", "  Rocket Fuel  "):
            env = {
                "ALPACA_ACCOUNT_NAME_ACC1": label,
                "ALPACA_ACCOUNT_NUMBER_ACC1": "ACCOUNT-NUMBER-1",
            }
            assert expected_account_number_for_route("rocket_fuel", env) == "ACCOUNT-NUMBER-1", (
                f"label {label!r} did not match route 'rocket_fuel'"
            )

    def test_the_label_match_beats_the_static_fallback_table(self) -> None:
        """The re-created-paper-account case the label match exists for.

        A hardcoded account number is invalidated silently when a paper account is
        re-created, so the env label match is the mechanism that keeps routing correct. Asserted
        by giving the label match a number that is obviously not the fallback one.
        """
        from live_runner.stockkit_routing import (
            STOCKKIT_ACCOUNT_ROUTES,
            expected_account_number_for_route,
        )

        env = {
            "ALPACA_ACCOUNT_NAME_ACC1": "Rocket Fuel",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "FROM-ENV",
        }
        assert expected_account_number_for_route("rocket_fuel", env) == "FROM-ENV"
        # With no env at all, the static table answers - and it is a different number.
        assert expected_account_number_for_route("rocket_fuel", {}) == (
            STOCKKIT_ACCOUNT_ROUTES["rocketfuel"]
        )
        assert STOCKKIT_ACCOUNT_ROUTES["rocketfuel"] != "FROM-ENV"

    def test_both_rocketfuel_variants_deliberately_share_one_account(self) -> None:
        """**My first version of this test asserted a separation the code does not make.**

        I assumed `rocketfuel_longsonly` and `rocketfuel_longandshort` were distinct routes
        resolving to distinct accounts, and wrote a test that a longer env label would not
        capture a shorter route. It failed - because the alias table maps **both** to
        `rocketfuel`:

            'rocketfuel_longsonly':  'rocketfuel'
            'rocketfuel_longandshort': 'rocketfuel'

        So the two strategy variants share one broker account, and any env label matching
        `rocketfuel` is the label for both. That looks deliberate, so it is pinned rather than
        "fixed" - and the name says why it matters: **if a second RocketFuel account is ever
        added, this table has no way to express it**, and the variants would keep sharing the
        first one. That is a real constraint to know before adding an account, not a bug to
        report now.

        The related non-collision is asserted too: a label that normalises to something no route
        ever produces cannot capture anything.
        """
        from live_runner.stockkit_routing import (
            _ALIASES,
            STOCKKIT_ACCOUNT_ROUTES,
            expected_account_number_for_route,
        )

        assert _ALIASES["rocketfuel_longsonly"] == "rocketfuel"
        assert _ALIASES["rocketfuel_longandshort"] == "rocketfuel"
        assert len(STOCKKIT_ACCOUNT_ROUTES) == len(set(_ALIASES.values())), (
            "the alias table now maps onto more accounts than exist; re-check what the "
            "shared-route constraint in this test means"
        )

        env = {
            "ALPACA_ACCOUNT_NAME_ACC1": "Rocket Fuel",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "ACCOUNT-1",
            # A label no route normalises to. It must capture nothing.
            "ALPACA_ACCOUNT_NAME_ACC2": "Rocket Fuel Long Only",
            "ALPACA_ACCOUNT_NUMBER_ACC2": "ACCOUNT-2",
        }
        for route in ("rocketfuel", "rocket_fuel", "rocket-fuel",
                      "rocketfuel_longsonly", "rocketfuel_longandshort"):
            assert expected_account_number_for_route(route, env) == "ACCOUNT-1", (
                f"route {route!r} did not resolve to the single RocketFuel account"
            )

    def test_the_first_matching_acc_index_wins(self) -> None:
        """Two accounts carrying the same label: ACC1 is taken, deterministically.

        Worth pinning because the alternative - last wins - depends on dict iteration order of
        the env scan, so routing would change with the shape of the env file rather than with
        its content.
        """
        from live_runner.stockkit_routing import expected_account_number_for_route

        env = {
            "ALPACA_ACCOUNT_NAME_ACC1": "Rocket Fuel",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "FIRST",
            "ALPACA_ACCOUNT_NAME_ACC2": "Rocket Fuel",
            "ALPACA_ACCOUNT_NUMBER_ACC2": "SECOND",
        }
        assert expected_account_number_for_route("rocket_fuel", env) == "FIRST"

    def test_an_env_entry_with_a_blank_number_does_not_win(self) -> None:
        """A label that matches but carries no number must not shadow the fallback.

        Otherwise the routing silently loses the account entirely - the label claims the
        route, and then supplies nothing.
        """
        from live_runner.stockkit_routing import (
            STOCKKIT_ACCOUNT_ROUTES,
            expected_account_number_for_route,
        )

        env = {
            "ALPACA_ACCOUNT_NAME_ACC1": "Rocket Fuel",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "   ",
        }
        assert expected_account_number_for_route("rocket_fuel", env) == (
            STOCKKIT_ACCOUNT_ROUTES["rocketfuel"]
        ), "a blank env number shadowed the static fallback"


class TestCtraderRoutingRejectionAndRiskSettingsPrecedence:
    """The routing rejection guard, and the one place two risk-setting spellings coexist.

    Most of `validate_live_account_routes` was already covered - duplicate ids, the
    non-positive cTID and two local accounts sharing a cTID were all killed on the first
    attempt. The gap is narrower than it looks: **a non-numeric cTID** was unmeasured, and that
    is the one that matters most, because a non-numeric identifier is exactly what a
    mis-configured account looks like and it would otherwise route *somewhere*.

    `_risk_config_for_deployment` reads `max_risk_per_trade` with a fallback to the legacy
    `risk_per_trade_pct`. The precedence between them was unmeasured, and precedence here is a
    **risk** decision rather than a naming convention: swapping the two would let a stale legacy
    value override the one the operator actually set.

    **The current spelling wins.** Asserted with both present, because that is the only case that
    distinguishes the branches - a test with just one set passes under either order. And the
    legacy spelling still works on its own, so an existing config is not silently ignored.

    The defaults are asserted in full, since a default that silently changed would be a risk
    posture change with no error: **1% risk per trade, 5% total exposure, 5 positions, 20%
    notional, fallback stop allowed, missing stop not rejected.**
    """

    @staticmethod
    def _account(account_id="a1", broker="ctrader", **kwargs):
        from shared.models import LiveAccountConfig

        base = {"id": account_id, "name": "n", "broker": broker}
        base.update(kwargs)
        return LiveAccountConfig(**base)

    @pytest.mark.parametrize(
        "identifier", ["abc", "", "1.5", "12 345", "0x10", "  ", "one-two"]
    )
    def test_a_non_numeric_ctrader_identifier_is_refused(self, identifier: str) -> None:
        """**The one that matters most, and the one the suite did not see.**

        A non-numeric cTID is what a mis-configured account looks like. The guard's own comment
        says a missing or non-numeric identifier must fail *here* rather than surface at
        preflight or, worse, route to the wrong account - so it is asserted at this layer.
        """
        from live_runner.account_routing import validate_live_account_routes

        with pytest.raises(ValueError, match="no numeric broker identifier"):
            validate_live_account_routes(
                [self._account(ctid=identifier, broker_login=None)]
            )

    def test_a_ctrader_account_with_no_identifier_at_all_is_refused(self) -> None:
        """Absent, not just malformed.

        The identifier falls back through `ctid`, then `broker_login`, then `id` - so a
        genuinely blank account still resolves to *something* from `id`, and that is what makes
        the non-numeric check rather than a presence check the operative one.
        """
        from live_runner.account_routing import validate_live_account_routes

        account = self._account(ctid="", broker_login="", id="not-a-number")
        with pytest.raises(ValueError, match="no numeric broker identifier"):
            validate_live_account_routes([account])

    def test_a_numeric_ctrader_identifier_is_accepted(self) -> None:
        """The pass case, so the guard is not simply always-firing.

        A numeric string is the normal form cTrader uses, and the identifier is compared as an
        *int* for the duplicate check - so `"00123"` and `123` are the same routing target.
        """
        from live_runner.account_routing import validate_live_account_routes

        validate_live_account_routes([self._account(id="a1", ctid="12345")])
        validate_live_account_routes([self._account(id="a1", broker_login="12345")])
        # And a non-cTrader account needs no identifier at all.
        validate_live_account_routes([self._account(id="a1", broker="alpaca")])

    def test_the_current_risk_setting_wins_over_the_legacy_spelling(self) -> None:
        """**Precedence, asserted with both present because that is the only distinguishing case.**

        A test with only one setting passes under either order, which is exactly why the swap
        mutation survived. Here the operator's current setting must win over a stale legacy value
        left in the same config.
        """
        from live_runner.daemon import _risk_config_for_deployment
        from shared.models import LiveDeploymentConfig
        from uuid import uuid4

        def _config(**settings):
            return LiveDeploymentConfig(
                id=str(uuid4()), strategy_name="s", account_id="a", symbols=["X"],
                timeframe="1h", status="ACTIVE", settings=settings,
            )

        both = _risk_config_for_deployment(
            _config(max_risk_per_trade=0.02, risk_per_trade_pct=0.09)
        )
        assert both.max_risk_per_trade == pytest.approx(0.02), (
            f"the legacy risk_per_trade_pct overrode max_risk_per_trade: "
            f"{both.max_risk_per_trade}"
        )

        # And each spelling still works on its own, so an older config is not ignored.
        assert _risk_config_for_deployment(
            _config(risk_per_trade_pct=0.09)
        ).max_risk_per_trade == pytest.approx(0.09)
        assert _risk_config_for_deployment(
            _config(max_risk_per_trade=0.02)
        ).max_risk_per_trade == pytest.approx(0.02)

    def test_the_default_risk_posture_is_pinned_in_full(self) -> None:
        """**A default that changed silently would be a risk change with no error anywhere.**

        Stated as a set rather than one at a time, because the posture is the point: 1% risk per
        trade, 5% total exposure, 5 positions, a 2% fallback stop, 20% notional per trade,
        fallback stop *allowed* and a missing stop *not* rejected.

        The last two are booleans and the easy ones to flip in a refactor - and
        `allow_fallback_stop=False` would refuse orders outright while
        `reject_missing_stop=True` would refuse them too, so both directions are asserted by
        the full-set check rather than individually.
        """
        from live_runner.daemon import _risk_config_for_deployment
        from shared.models import LiveDeploymentConfig
        from uuid import uuid4

        config = _risk_config_for_deployment(
            LiveDeploymentConfig(
                id=str(uuid4()), strategy_name="s", account_id="a", symbols=["X"],
                timeframe="1h", status="ACTIVE",
            )
        )
        assert config.max_risk_per_trade == pytest.approx(0.01)
        assert config.max_total_exposure == pytest.approx(0.05)
        assert config.max_positions == 5
        assert config.fallback_stop_pct == pytest.approx(0.02)
        assert config.max_notional_per_trade == pytest.approx(0.20)
        assert config.allow_fallback_stop is True, (
            "the default no longer allows a fallback stop, which would refuse orders outright"
        )
        assert config.reject_missing_stop is False, (
            "the default now rejects a missing stop, which would refuse orders outright"
        )

    def test_an_explicit_setting_overrides_its_default(self) -> None:
        """The non-boolean case, so the default-set test above is not the only thing asserted."""
        from live_runner.daemon import _risk_config_for_deployment
        from shared.models import LiveDeploymentConfig
        from uuid import uuid4

        config = _risk_config_for_deployment(
            LiveDeploymentConfig(
                id=str(uuid4()), strategy_name="s", account_id="a", symbols=["X"],
                timeframe="1h", status="ACTIVE",
                settings={
                    "max_total_exposure": 0.5,
                    "max_positions": 2,
                    "max_notional_per_trade": 0.9,
                    "allow_fallback_stop": False,
                    "reject_missing_stop": True,
                },
            )
        )
        assert config.max_total_exposure == pytest.approx(0.5)
        assert config.max_positions == 2
        assert config.max_notional_per_trade == pytest.approx(0.9)
        assert config.allow_fallback_stop is False
        assert config.reject_missing_stop is True
