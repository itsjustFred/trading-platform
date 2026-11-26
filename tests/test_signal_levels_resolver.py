"""SL/TP level resolution from signal metadata: one allowlist, three copies.

Proposal G19 (improvement run W08): the accepted SL/TP alias keys and the
"first present value" resolver are copied in three places on the live path —

* ``risk.live.resolve_live_levels`` (money parsing, strict),
* ``live_runner.alpaca_adapter.AlpacaLiveAdapter._resolve_broker_sl_tp``
  plus ``_round_bracket_levels_in_place`` (bare ``float()``, lenient),
* ``live_runner.ctrader_adapter.CTraderLiveAdapter._build_new_order_request``
  (bare ``float()``, lenient) —

and the copies do not agree on strictness. This file pins the CURRENT behaviour
of every copy over a table of alias keys x values BEFORE any of them is
rewired onto the shared resolver (``risk.signal_levels``); the rewiring must be
behaviour-identical, so every test here must keep passing unchanged.

Two outcomes differ between the copies today and are pinned as such (do NOT
"fix" them here without the maintainer's decision):

* a **boolean** level is refused by the strict copy (``True`` is not a price)
  and silently coerced to ``1.0`` by both broker adapters;
* an **unparseable** value raises ``"<key>: unreadable money value: <value>"``
  in the strict copy and ``"Invalid <key>: <value>"`` in the adapters.

Both are operator-visible texts, so the shared resolver keeps both shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from live_runner import alpaca_adapter as alpaca_module
from live_runner.ctrader_adapter import CTraderLiveAdapter
from risk.live import resolve_live_levels
from shared.models import LiveSignal

# ── the allowlist, exercised rather than imported ─────────────────────────────

STOP_ALIASES = ("stop_loss", "sl_price", "stop_price", "resolved_stop_loss")
TAKE_ALIASES = ("take_profit", "tp_price")

# Outcomes used by the tables below.
ACCEPT = "accept"  # resolves to the value as a float
COERCE = "coerce"  # resolves to float(value) although value is not a number
REFUSE = "refuse"  # raises ValueError
ABSENT = "absent"  # resolves to None (the strict copy only)

#: (value, strict copy, alpaca copy, cTrader copy) for ONE stop alias, the
#: other level being a valid take profit.
STOP_VALUE_TABLE: list[tuple[Any, str, str, str]] = [
    (95.0, ACCEPT, ACCEPT, ACCEPT),
    ("95.0", ACCEPT, ACCEPT, ACCEPT),
    (" 95.0 ", ACCEPT, ACCEPT, ACCEPT),
    (95, ACCEPT, ACCEPT, ACCEPT),
    (Decimal("95.0"), ACCEPT, ACCEPT, ACCEPT),
    # The pinned difference: a boolean is not a price, but float() takes it.
    (True, REFUSE, COERCE, COERCE),
    (False, REFUSE, REFUSE, REFUSE),
    (float("nan"), REFUSE, REFUSE, REFUSE),
    ("nan", REFUSE, REFUSE, REFUSE),
    (float("inf"), REFUSE, REFUSE, REFUSE),
    ("inf", REFUSE, REFUSE, REFUSE),
    (-5.0, REFUSE, REFUSE, REFUSE),
    (0.0, REFUSE, REFUSE, REFUSE),
    ("not-a-price", REFUSE, REFUSE, REFUSE),
    ({"bad": 1}, REFUSE, REFUSE, REFUSE),
    (None, ABSENT, REFUSE, REFUSE),
]

#: The same table for ONE take-profit alias, the other level being a valid
#: stop. cTrader refuses the coerced ``True`` (1.0 below its sell-side
#: reference) through its own wrong-side guard, not through the resolver.
TAKE_VALUE_TABLE: list[tuple[Any, str, str, str]] = [
    (95.0, ACCEPT, ACCEPT, ACCEPT),
    ("95.0", ACCEPT, ACCEPT, ACCEPT),
    (" 95.0 ", ACCEPT, ACCEPT, ACCEPT),
    (95, ACCEPT, ACCEPT, ACCEPT),
    (Decimal("95.0"), ACCEPT, ACCEPT, ACCEPT),
    (True, REFUSE, COERCE, COERCE),
    (False, REFUSE, REFUSE, REFUSE),
    (float("nan"), REFUSE, REFUSE, REFUSE),
    ("nan", REFUSE, REFUSE, REFUSE),
    (float("inf"), REFUSE, REFUSE, REFUSE),
    ("inf", REFUSE, REFUSE, REFUSE),
    (-5.0, REFUSE, REFUSE, REFUSE),
    (0.0, REFUSE, REFUSE, REFUSE),
    ("not-a-price", REFUSE, REFUSE, REFUSE),
    ({"bad": 1}, REFUSE, REFUSE, REFUSE),
    (None, ABSENT, REFUSE, REFUSE),
]


#: cTrader refuses a symbol with no cached and no fallback scale.
CTRADER_SYMBOL = "EURUSD"


def _signal(
    metadata: dict[str, Any], *, action: str, price: float, symbol: str = "AAPL"
) -> LiveSignal:
    return LiveSignal(
        deployment_id="dep-1",
        symbol=symbol,
        action=action,
        theoretical_price=price,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


def _strict(metadata: dict[str, Any], *, action: str = "buy", price: float = 100.0):
    """The strict copy: ``risk.live.resolve_live_levels`` -> (entry, stop, take)."""
    return resolve_live_levels(_signal(metadata, action=action, price=price))


def _alpaca(metadata: dict[str, Any]):
    """The lenient Alpaca copy: ``_resolve_broker_sl_tp`` -> (stop, take)."""
    adapter = alpaca_module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    return adapter._resolve_broker_sl_tp(
        _signal(metadata, action="buy", price=100.0)
    )


def _ctrader_adapter() -> CTraderLiveAdapter:
    """A detached cTrader adapter with the proto stubs the request builder needs."""
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123"
    )

    class _NewOrderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.symbolId = 0
            self.orderType = 0
            self.tradeSide = 0
            self.volume = 0
            self.timeInForce = 0
            self.clientOrderId = ""
            self.stopLoss = 0
            self.takeProfit = 0

        def HasField(self, name: str) -> bool:
            # Proto-presence double: relativeStopLoss/relativeTakeProfit are
            # absent from __init__ so presence tracks assignment.
            return name in self.__dict__

    adapter._ProtoOANewOrderReq = _NewOrderReq
    adapter._ProtoOAOrderType = SimpleNamespace(MARKET=1)
    adapter._ProtoOATradeSide = SimpleNamespace(BUY=1, SELL=2)
    adapter._ProtoOATimeInForce = SimpleNamespace(IMMEDIATE_OR_CANCEL=1)
    return adapter


def _ctrader(metadata: dict[str, Any], *, action: str = "buy"):
    """The lenient cTrader copy: ``_build_new_order_request`` -> relative levels.

    The relative distances are 1/100000 price units away from
    ``theoretical_price`` (100.0), so the effective levels are read back off
    them: a coerced ``True`` stop is 99.0 away from the reference, not 5.0.
    """
    signal = _signal(
        {"qty": 1.0, **metadata}, action=action, price=100.0, symbol=CTRADER_SYMBOL
    )
    request = _ctrader_adapter()._build_new_order_request(signal, symbol_id=7)
    reference = 100.0
    scale = 100_000
    # cTrader sends distances, not prices: a BUY's stop is below its reference
    # and its take profit above, a SELL's the other way round.
    outward = 1.0 if action == "buy" else -1.0
    stop_level = reference - outward * request.relativeStopLoss / scale
    take_field = vars(request).get("relativeTakeProfit")
    take_level = None if take_field is None else reference + outward * take_field / scale
    return stop_level, take_level


def _outcome_of_read(level: Any, expected: str, value: Any) -> None:
    """Assert one read outcome: accepted, coerced, refused or absent."""
    if expected is REFUSE:
        with pytest.raises(ValueError):
            level()
        return
    if expected is ABSENT:
        assert level() is None
        return
    resolved = level()
    assert resolved == pytest.approx(float(value)), (
        f"expected {value!r} to resolve to {float(value)!r}, got {resolved!r}"
    )


@pytest.mark.parametrize("alias", STOP_ALIASES)
@pytest.mark.parametrize(
    "value,strict,alpaca,ctrader",
    STOP_VALUE_TABLE,
    ids=[row[0].__class__.__name__ + ":" + str(row[0])[:12] for row in STOP_VALUE_TABLE],
)
def test_every_stop_alias_resolves_the_same_way_in_every_copy(
    alias: str, value: Any, strict: str, alpaca: str, ctrader: str
) -> None:
    """Characterization: alias x value table, one row per copy (G19 step 2)."""
    metadata = {alias: value, "take_profit": 105.0}

    def _strict_stop():
        return _strict(metadata)[1]

    def _alpaca_stop():
        return _alpaca(metadata)[0]

    def _ctrader_stop():
        return _ctrader(metadata)[0]

    _outcome_of_read(_strict_stop, strict, value)
    _outcome_of_read(_alpaca_stop, alpaca, value)
    _outcome_of_read(_ctrader_stop, ctrader, value)


@pytest.mark.parametrize("alias", TAKE_ALIASES)
@pytest.mark.parametrize(
    "value,strict,alpaca,ctrader",
    TAKE_VALUE_TABLE,
    ids=[row[0].__class__.__name__ + ":" + str(row[0])[:12] for row in TAKE_VALUE_TABLE],
)
def test_every_take_profit_alias_resolves_the_same_way_in_every_copy(
    alias: str, value: Any, strict: str, alpaca: str, ctrader: str
) -> None:
    """Characterization for the take-profit aliases; the stop stays valid."""
    # SELL: every acceptable take profit sits BELOW the reference price.
    metadata = {"stop_loss": 105.0, alias: value}

    def _strict_take():
        return _strict(metadata, action="sell")[2]

    def _alpaca_take():
        return _alpaca(metadata)[1]

    def _ctrader_take():
        return _ctrader(metadata, action="sell")[1]

    _outcome_of_read(_strict_take, strict, value)
    _outcome_of_read(_alpaca_take, alpaca, value)
    _outcome_of_read(_ctrader_take, ctrader, value)


# ── precedence: the allowlist order decides, never the payload's key order ────


def test_first_present_alias_wins_in_every_copy() -> None:
    """`stop_loss` beats `sl_price` regardless of dict insertion order."""
    for metadata in (
        {"stop_loss": 90.0, "sl_price": 95.0, "take_profit": 105.0},
        {"sl_price": 95.0, "stop_loss": 90.0, "take_profit": 105.0},
    ):
        assert _strict(metadata)[1] == pytest.approx(90.0)
        assert _alpaca(metadata)[0] == pytest.approx(90.0)
        assert _ctrader(metadata)[0] == pytest.approx(90.0)


def test_an_absent_alias_falls_through_to_the_next_one() -> None:
    metadata = {"stop_loss": None, "sl_price": 95.0, "take_profit": 105.0}
    assert _strict(metadata)[1] == pytest.approx(95.0)
    assert _alpaca(metadata)[0] == pytest.approx(95.0)
    assert _ctrader(metadata)[0] == pytest.approx(95.0)


def test_a_broken_first_alias_does_not_fall_through() -> None:
    """A present but unreadable alias is an error, not a skip: the later alias
    must never silently take over (an unparseable SL must never mean "no SL")."""
    metadata = {"stop_loss": "not-a-price", "sl_price": 95.0, "take_profit": 105.0}
    for read in (
        lambda: _strict(metadata),
        lambda: _alpaca(metadata),
        lambda: _ctrader(metadata),
    ):
        with pytest.raises(ValueError):
            read()


def test_an_alias_outside_the_allowlist_is_never_read() -> None:
    """`stoploss`/`sl`/`tp1_price` are not in any allowlist: they resolve to
    "no level", so the copy's own guard refuses the order (fail closed)."""
    for extra in ({"stoploss": 95.0}, {"sl": 95.0}, {"stop": 95.0}):
        metadata = {**extra, "take_profit": 105.0}
        assert _strict(metadata)[1] is None
        with pytest.raises(ValueError, match="without stop_loss"):
            _alpaca(metadata)
        with pytest.raises(ValueError, match="without stop_loss"):
            _ctrader(metadata)


# ── the two operator-visible differences, pinned as maintainer decisions ─────


def test_strict_copy_names_the_key_in_the_money_error() -> None:
    for alias in STOP_ALIASES + TAKE_ALIASES:
        metadata = {alias: "not-a-price"}
        if alias in STOP_ALIASES:
            metadata["take_profit"] = 105.0
        with pytest.raises(ValueError, match=f"{alias}: unreadable money value: 'not-a-price'"):
            _strict(metadata)
    # Non-finite values get the money parser's own reason, still naming the key.
    with pytest.raises(ValueError, match="stop_loss: non-finite money value: nan"):
        _strict({"stop_loss": float("nan"), "take_profit": 105.0})


def test_adapters_name_the_key_in_the_invalid_error() -> None:
    for alias in STOP_ALIASES + TAKE_ALIASES:
        metadata = {alias: "not-a-price"}
        if alias in STOP_ALIASES:
            metadata["take_profit"] = 105.0
        else:
            metadata["stop_loss"] = 95.0
        with pytest.raises(ValueError, match=f"Invalid {alias}: 'not-a-price'"):
            _alpaca(metadata)
        with pytest.raises(ValueError, match=f"Invalid {alias}: 'not-a-price'"):
            _ctrader(metadata, action="buy" if alias in STOP_ALIASES else "sell")


def test_adapters_coerce_a_boolean_stop_to_one() -> None:
    """Pinned difference (G19): the adapters read `True` as a stop at 1.0.

    The recommended direction is fail-closed — `True` is not a price — but that
    is a behaviour change on the money path and stays a maintainer decision, so
    the coercion is pinned here rather than removed.
    """
    metadata = {"stop_loss": True, "take_profit": 105.0}

    stop, take = _alpaca(metadata)
    assert (stop, take) == (1.0, 105.0)

    assert _ctrader(metadata)[0] == pytest.approx(1.0)
