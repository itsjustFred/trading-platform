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

The lower half of this file pins the shared resolver itself: one allowlist per
level, one resolution order, one result type, and both parsings.
"""

from __future__ import annotations

import ast
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from live_runner import alpaca_adapter as alpaca_module
from live_runner.ctrader_adapter import CTraderLiveAdapter
from risk.live import resolve_live_levels
from risk.signal_levels import (
    SL_METADATA_KEYS,
    TP_METADATA_KEYS,
    LevelParsing,
    SignalLevel,
    resolve_signal_level,
)
from shared.models import LiveSignal

REPO_ROOT = Path(__file__).resolve().parents[1]

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



# ── the shared resolver: risk.signal_levels ───────────────────────────────────


def test_the_allowlist_is_one_ordered_tuple_per_level() -> None:
    """One definition, canonical spelling first, one tuple per level."""
    assert SL_METADATA_KEYS == (
        "stop_loss",
        "sl_price",
        "stop_price",
        "resolved_stop_loss",
    )
    assert TP_METADATA_KEYS == ("take_profit", "tp_price")
    # A stop alias must never resolve a take profit and vice versa.
    assert not set(SL_METADATA_KEYS) & set(TP_METADATA_KEYS)


LEVEL_KEYS = ((SL_METADATA_KEYS, STOP_ALIASES), (TP_METADATA_KEYS, TAKE_ALIASES))


@pytest.mark.parametrize(
    "keys,aliases", LEVEL_KEYS, ids=[aliases[0] for _, aliases in LEVEL_KEYS]
)
@pytest.mark.parametrize("alias", STOP_ALIASES + TAKE_ALIASES)
def test_the_resolver_names_the_key_a_level_came_from(alias, keys, aliases) -> None:
    """The result names the alias that carried the level, not just its price."""
    for parsing in LevelParsing:
        resolved = resolve_signal_level({alias: 95.0}, keys, parsing=parsing)

        if alias in aliases:
            assert resolved == SignalLevel(price=95.0, key=alias)
        else:
            # An alias of the OTHER level is not a level of this tuple.
            assert resolved == SignalLevel(None, None)


def test_absent_metadata_resolves_to_an_absent_level() -> None:
    for metadata in (None, {}, {key: None for key in SL_METADATA_KEYS}):
        for parsing in LevelParsing:
            assert resolve_signal_level(metadata, SL_METADATA_KEYS, parsing=parsing) == (
                SignalLevel(None, None)
            )


def test_an_alias_outside_the_allowlist_resolves_to_an_absent_level() -> None:
    """`stoploss`/`sl`/`tp1_price`/`sizing_sl_price` are in neither tuple: the
    payload carries no level and the caller's guard decides what to do."""
    for alias in ("stoploss", "stop", "sl", "tp1_price", "sizing_sl_price"):
        for parsing in LevelParsing:
            assert resolve_signal_level({alias: 95.0}, SL_METADATA_KEYS, parsing=parsing) == (
                SignalLevel(None, None)
            )


def test_the_first_alias_the_payload_carries_wins() -> None:
    """Precedence comes from the allowlist order, never from key insertion."""
    for parsing in LevelParsing:
        for published in (
            {"sl_price": 95.0, "stop_loss": 90.0},
            {"stop_loss": 90.0, "sl_price": 95.0},
        ):
            resolved = resolve_signal_level(published, SL_METADATA_KEYS, parsing=parsing)
            assert resolved == SignalLevel(price=90.0, key="stop_loss")


def test_an_absent_alias_falls_through_to_the_next_spelling() -> None:
    resolved = resolve_signal_level(
        {"stop_loss": None, "sl_price": 95.0}, SL_METADATA_KEYS, parsing=LevelParsing.MONEY
    )
    assert resolved == SignalLevel(price=95.0, key="sl_price")


@pytest.mark.parametrize("parsing", list(LevelParsing), ids=lambda parsing: parsing.value)
def test_a_present_but_unreadable_alias_never_falls_through(parsing) -> None:
    """An unparseable value is an error, not a skip onto the next alias."""
    with pytest.raises(ValueError):
        resolve_signal_level(
            {"stop_loss": "not-a-price", "sl_price": 95.0},
            SL_METADATA_KEYS,
            parsing=parsing,
        )


def test_money_parsing_refuses_a_boolean_and_a_non_finite_number() -> None:
    """`True` is not a price and `nan` poisons every comparison downstream.

    A negative or zero price resolves: whether a level is legal depends on the
    side of the trade, so that check belongs to the caller (the strict copy's
    ``resolve_live_levels``, the adapters' own guards), not to the allowlist.
    """
    for value in (True, False, float("nan"), float("inf"), "nan"):
        with pytest.raises(ValueError, match="stop_loss: "):
            resolve_signal_level(
                {"stop_loss": value}, SL_METADATA_KEYS, parsing=LevelParsing.MONEY
            )


def test_any_float_parsing_coerces_a_boolean_and_passes_a_non_finite_number() -> None:
    """Pinned as the adapters' current behaviour (maintainer decision, G19)."""
    for value, expected in ((True, 1.0), (False, 0.0)):
        resolved = resolve_signal_level(
            {"stop_loss": value}, SL_METADATA_KEYS, parsing=LevelParsing.ANY_FLOAT
        )
        assert resolved == SignalLevel(price=expected, key="stop_loss")
    non_finite = resolve_signal_level(
        {"stop_loss": float("nan")}, SL_METADATA_KEYS, parsing=LevelParsing.ANY_FLOAT
    )
    assert non_finite.key == "stop_loss"
    assert math.isnan(non_finite.price)
    with pytest.raises(ValueError, match="Invalid stop_loss"):
        resolve_signal_level(
            {"stop_loss": "not-a-price"}, SL_METADATA_KEYS, parsing=LevelParsing.ANY_FLOAT
        )


@pytest.mark.parametrize("parsing", list(LevelParsing), ids=lambda parsing: parsing.value)
def test_each_parsing_keeps_its_own_operator_visible_error_text(parsing) -> None:
    """The two texts the live path already raises; unifying them is its own change."""
    with pytest.raises(ValueError) as excinfo:
        resolve_signal_level({"stop_loss": "abc"}, SL_METADATA_KEYS, parsing=parsing)
    expected = (
        "stop_loss: unreadable money value: 'abc'"
        if parsing is LevelParsing.MONEY
        else "Invalid stop_loss: 'abc'"
    )
    assert str(excinfo.value) == expected


@pytest.mark.parametrize("alias", STOP_ALIASES + TAKE_ALIASES)
@pytest.mark.parametrize("parsing", list(LevelParsing), ids=lambda parsing: parsing.value)
def test_the_error_names_the_alias_that_could_not_be_read(alias, parsing) -> None:
    with pytest.raises(ValueError, match=alias):
        resolve_signal_level(
            {alias: "abc"}, SL_METADATA_KEYS + TP_METADATA_KEYS, parsing=parsing
        )


ALIAS_UNIVERSE = frozenset((*SL_METADATA_KEYS, *TP_METADATA_KEYS))

#: Alias vocabularies that stay outside the shared module, each with the reason
#: it is a different domain: a scrub list names keys to REMOVE and a leg plan
#: names broker order fields - neither resolves a level from signal metadata.
#: A new site fails the scan below until it is declared here.
EXEMPT_ALIAS_VOCABULARIES = {
    "live_runner/daemon.py": "the exit-path scrub list (keys an exit must not carry)",
    "live_runner/alpaca_adapter.py": (
        "the partial-exit re-protection plan: canonical level names against "
        "broker order leg fields, not signal metadata aliases"
    ),
}


def _alias_vocabulary_sites() -> set[str]:
    """``package/file.py:line`` for every literal spelling out two or more aliases.

    Matched on the AST, not on the text: a renamed or reformatted copy of the
    list still carries its aliases, while a single use of one alias (a field
    name, one key read, one popped key) is not a vocabulary and never matches.
    """
    found: set[str] = set()
    for package in ("risk", "live_runner"):
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            if path.name in {"signal_levels.py", "test_signal_levels_resolver.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                    continue
                aliases = {
                    element.value
                    for element in node.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                }
                if len(aliases & ALIAS_UNIVERSE) >= 2:
                    relative = path.relative_to(REPO_ROOT / package)
                    found.add(f"{package}/{relative}:{node.lineno}")
                    break
    return found


def test_no_module_spells_out_the_allowlist_outside_the_shared_module() -> None:
    """A fresh copy of the alias list is what this PR removes; keep it out.

    The pre-mortem finding behind G19 was not the allowlist itself but the
    second copy of it: a resolver written per caller drifts in strictness, and
    a key one caller accepts is silently ignored by another.
    """
    observed = _alias_vocabulary_sites()
    observed_files = {site.split(":")[0] for site in observed}
    missing = sorted(
        site
        for site in observed
        if site.split(":")[0] not in EXEMPT_ALIAS_VOCABULARIES
    )
    stale = sorted(set(EXEMPT_ALIAS_VOCABULARIES) - observed_files)
    assert not missing, (
        "These spell out an SL/TP alias vocabulary outside risk.signal_levels: "
        f"{missing}. Move the list there and call resolve_signal_level, or add "
        "the file to EXEMPT_ALIAS_VOCABULARIES with the reason it is a "
        "different domain."
    )
    assert not stale, (
        f"Sites removed: {stale}. Drop them from EXEMPT_ALIAS_VOCABULARIES, or "
        "restore the vocabulary they guarded."
    )
