"""Acceptance tests for #125: one live level resolver, trusted pip value,
entry-deviation gate against the live broker quote.

Maintainer decisions pinned here (issue #125 comments):

* Entry reference for sizing is metadata ``entry_price``, else
  ``theoretical_price``. SL/TP come from the same metadata aliases. One
  resolver (``risk.live.resolve_live_levels``) feeds both the daemon's sizing
  and the levels it hands to the broker, so the two can never disagree.
* A missing TP is not a resolver error; a malformed TP (non-numeric or
  ``<= 0``) is rejected.
* ``pip_value`` comes from trusted callers only, never from signal metadata.
* Before submitting, the signal entry is compared with the live broker quote
  (ask for buys, bid for sells). The deviation is a fraction of the SL
  distance. Revised decision: a move beyond
  ``max_entry_deviation_sl_fraction`` (per deployment, default 0.25) rejects
  in EITHER direction (toward the SL: setup possibly invalid; away from it:
  chasing, more real risk with a fixed stop). A same-side quote already at or
  beyond the TP (when one is set) or at or beyond the SL also rejects. The
  quote is a gate only, never a sizing input. A quote that cannot be fetched
  or is unusable rejects.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

import live_runner.daemon as daemon
from live_runner.base_adapter import BaseLiveAdapter
from risk.live import resolve_live_levels
from shared.models import LiveDeploymentConfig, LiveSignal

# ── fixtures ──────────────────────────────────────────────────────────────────


def _quote(bid: float, ask: float, **kwargs: Any):
    from live_runner.base_adapter import LiveQuote

    # A fresh broker timestamp unless a test says otherwise: the gate rejects
    # missing and stale quote times (#125 review follow-up).
    kwargs.setdefault("timestamp", datetime.now(timezone.utc))
    return LiveQuote(bid=bid, ask=ask, **kwargs)


@dataclass
class _GateAdapter(BaseLiveAdapter):
    """Flat broker account with a configurable quote."""

    quote: Any = None
    quote_error: Exception | None = None
    positions: list[dict[str, Any]] = field(default_factory=list)
    quote_calls: list[str] = field(default_factory=list)
    submitted: list[tuple[LiveSignal, float | None]] = field(default_factory=list)

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted.append((signal, qty))
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)

    async def get_quote(self, symbol: str):
        self.quote_calls.append(symbol)
        if self.quote_error is not None:
            raise self.quote_error
        return self.quote


@dataclass
class _QuotelessAdapter(BaseLiveAdapter):
    """An adapter that never implemented quotes (inherits the base default)."""

    submitted: list[LiveSignal] = field(default_factory=list)

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted.append(signal)
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def get_positions(self) -> list[dict[str, Any]]:
        return []


def _deployment(**settings: Any) -> LiveDeploymentConfig:
    # max_notional_per_trade=1.0 keeps the notional cap out of the way so the
    # quantity is a direct read of the stop distance and the pip value.
    base = {
        "max_risk_per_trade": 0.01,
        "max_total_exposure": 0.05,
        "max_notional_per_trade": 1.0,
    }
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings={**base, **settings},
    )


def _signal(
    deployment: LiveDeploymentConfig,
    metadata: dict[str, Any],
    *,
    action: str = "buy",
    theoretical_price: float = 100.0,
) -> LiveSignal:
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action=action,
        theoretical_price=theoretical_price,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


def _prepare(adapter, deployment, signal, **kwargs):
    return asyncio.run(daemon.prepare_order_with_risk(adapter, deployment, signal, **kwargs))


def _at_entry(entry: float = 100.0):
    return _quote(bid=entry, ask=entry)


# ── single resolver: entry / SL / TP precedence ───────────────────────────────


def test_daemon_has_no_own_level_lookup():
    """Acceptance: daemon.py no longer carries its own SL/TP alias lookup."""
    assert not hasattr(daemon, "_first_metadata_float")
    assert daemon.resolve_live_levels is resolve_live_levels


def test_daemon_sizes_from_metadata_entry_price_not_theoretical_price():
    """Regression (#125): the daemon sized off theoretical_price and ignored
    the metadata entry_price the shared resolver uses.

    Risk budget 1000 (1% of 100k). Resolver entry 101, stop 97 -> distance 4
    -> 250. The old daemon path used theoretical 100 -> distance 3 -> 333.33.
    """
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry(101.0))
    signal = _signal(
        deployment,
        {"entry_price": 101.0, "stop_loss": 97.0, "take_profit": 110.0},
        theoretical_price=100.0,
    )

    prepared, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(250.0)
    assert prepared.metadata["qty"] == pytest.approx(250.0)
    # Submission carries the same entry the sizing used, so adapters that place
    # relative SL/TP distances (cTrader) see the sized distance too.
    assert prepared.theoretical_price == pytest.approx(101.0)
    assert prepared.metadata["entry_price"] == pytest.approx(101.0)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        # entry: metadata entry_price wins; SL/TP primary keys.
        (
            {"entry_price": 102.0, "stop_loss": 98.0, "take_profit": 110.0},
            (102.0, 98.0, 110.0),
        ),
        # entry falls back to theoretical_price; SL/TP secondary aliases.
        ({"sl_price": 97.0, "tp_price": 108.0}, (100.0, 97.0, 108.0)),
        # None primary falls through to the next alias, in the resolver's order.
        (
            {"stop_loss": None, "sl_price": None, "stop_price": 96.0, "take_profit": None,
             "tp_price": 109.0},
            (100.0, 96.0, 109.0),
        ),
        # The first present alias wins even if a later one disagrees.
        (
            {"sl_price": 95.0, "stop_price": 90.0, "resolved_stop_loss": 85.0,
             "take_profit": 112.0, "tp_price": 150.0},
            (100.0, 95.0, 112.0),
        ),
        ({"resolved_stop_loss": 94.0, "take_profit": 111.0}, (100.0, 94.0, 111.0)),
        # entry_price None falls back to theoretical_price.
        ({"entry_price": None, "stop_loss": 99.0, "take_profit": 104.0}, (100.0, 99.0, 104.0)),
    ],
)
def test_sizing_and_submission_share_entry_sl_tp_precedence(metadata, expected):
    """Acceptance: entry, SL and TP precedence is the same for sizing and for
    the order handed to the broker, and it is the shared resolver's."""
    deployment = _deployment()
    signal = _signal(deployment, dict(metadata))
    entry, stop, take = resolve_live_levels(signal)
    assert (entry, stop, take) == pytest.approx(expected)
    adapter = _GateAdapter(quote=_at_entry(entry))

    prepared, qty = _prepare(adapter, deployment, signal)

    # Sizing used the resolver's distance (risk budget 1000, pip 1.0).
    assert qty == pytest.approx(1000.0 / abs(entry - stop))
    # Submission carries the resolver's levels.
    assert prepared.metadata["stop_loss"] == pytest.approx(stop)
    assert prepared.metadata["take_profit"] == pytest.approx(take)
    assert prepared.theoretical_price == pytest.approx(entry)


def test_daemon_sizing_and_submission_both_go_through_the_shared_resolver(monkeypatch):
    """Spy on the resolver the daemon imported: when it answers, both the size
    and the broker levels follow it, so there is no second lookup."""
    calls: list[str] = []

    def _resolver(signal):
        calls.append(str(signal.id))
        return 100.0, 90.0, 130.0

    monkeypatch.setattr(daemon, "resolve_live_levels", _resolver)
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    # Metadata disagrees with the spy on every level; the spy must win.
    signal = _signal(
        deployment, {"entry_price": 120.0, "stop_loss": 99.0, "take_profit": 101.0}
    )

    prepared, qty = _prepare(adapter, deployment, signal)

    assert calls, "the daemon never called the shared resolver"
    assert qty == pytest.approx(1000.0 / 10.0)
    assert prepared.metadata["stop_loss"] == pytest.approx(90.0)
    assert prepared.metadata["take_profit"] == pytest.approx(130.0)


@pytest.mark.parametrize("bad_tp", [0.0, -5.0, "not-a-price", {"bad": 1}, True])
@pytest.mark.parametrize("key", ["take_profit", "tp_price"])
def test_daemon_rejects_malformed_take_profit(key, bad_tp):
    """Decision: a malformed TP (non-numeric or <= 0) is rejected. The old
    daemon lookup let a TP of 0 or -5 through to the broker."""
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(deployment, {"stop_loss": 95.0, key: bad_tp})

    with pytest.raises(ValueError):
        _prepare(adapter, deployment, signal)

    assert adapter.submitted == []


@pytest.mark.parametrize("bad_stop", [0.0, -1.0, "nope", True])
@pytest.mark.parametrize("key", ["stop_loss", "sl_price", "stop_price", "resolved_stop_loss"])
def test_daemon_rejects_malformed_stop_alias(key, bad_stop):
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(deployment, {key: bad_stop, "take_profit": 110.0})

    with pytest.raises(ValueError):
        _prepare(adapter, deployment, signal)

    assert adapter.submitted == []


@pytest.mark.parametrize("bad_entry", [0.0, -1.0, "not-a-number", float("nan"), True])
def test_daemon_rejects_malformed_entry_price(bad_entry):
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(
        deployment, {"entry_price": bad_entry, "stop_loss": 95.0, "take_profit": 110.0}
    )

    with pytest.raises(ValueError, match="entry_price"):
        _prepare(adapter, deployment, signal)

    assert adapter.submitted == []


def test_resolver_passes_missing_take_profit_through_as_none():
    """Decision: a missing TP is not a resolver error."""
    deployment = _deployment()
    signal = _signal(deployment, {"stop_loss": 95.0})

    assert resolve_live_levels(signal) == (100.0, 95.0, None)


# ── pip value: trusted only ───────────────────────────────────────────────────


def test_daemon_ignores_metadata_pip_value():
    """Acceptance: a metadata pip_value is ignored. With pip 10 from the
    payload the old daemon path sized 1000 / (5 * 10) = 20 instead of 200."""
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(
        deployment, {"stop_loss": 95.0, "take_profit": 110.0, "pip_value": 10.0}
    )

    _, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(200.0)


@pytest.mark.parametrize("payload_pip", ["garbage", -1.0, 0.0, float("inf")])
def test_daemon_does_not_even_parse_metadata_pip_value(payload_pip):
    """An untrusted pip_value cannot fail (or steer) sizing either."""
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(
        deployment, {"stop_loss": 95.0, "take_profit": 110.0, "pip_value": payload_pip}
    )

    _, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(200.0)


def test_daemon_trusted_pip_value_still_applies():
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(
        deployment, {"stop_loss": 95.0, "take_profit": 110.0, "pip_value": 1000.0}
    )

    _, qty = _prepare(adapter, deployment, signal, pip_value=2.0)

    assert qty == pytest.approx(100.0)


def test_size_live_signal_for_submission_ignores_metadata_pip_value():
    deployment = _deployment()
    signal = _signal(deployment, {"stop_loss": 95.0, "pip_value": 10.0})

    sized = daemon.size_live_signal_for_submission(
        signal, deployment, account_balance=100_000.0
    )

    assert sized.metadata["qty"] == pytest.approx(200.0)


# ── trust boundary carried over from test_live_risk_sizing to the daemon path ──


def test_daemon_ignores_metadata_risk_limits():
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(
        deployment,
        {
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "max_risk_per_trade": 1.0,
            "max_total_exposure": 1.0,
            "max_positions": 999,
            "max_notional_per_trade": 50.0,
        },
    )

    prepared, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(200.0)
    assert prepared.metadata["risk_amount"] == pytest.approx(1000.0)


def test_daemon_ignores_metadata_position_count_when_broker_cannot_report():
    """Payload current_positions must not feed max_positions, even when the
    broker exposes no positions (the old path forwarded the whole payload to
    the engine, so a payload count of 999 rejected and a 0 bypassed)."""

    class _NoPositions(_GateAdapter):
        get_positions = None  # type: ignore[assignment]

    deployment = _deployment(max_positions=1)
    adapter = _NoPositions(quote=_at_entry())
    signal = _signal(
        deployment, {"stop_loss": 95.0, "take_profit": 110.0, "current_positions": 999}
    )

    _, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(200.0)


def test_daemon_broker_position_count_binds_over_payload():
    deployment = _deployment(max_positions=1)
    adapter = _GateAdapter(quote=_at_entry(), positions=[{"symbol": "MSFT", "qty": 3.0}])
    signal = _signal(
        deployment, {"stop_loss": 95.0, "take_profit": 110.0, "current_positions": 0}
    )

    with pytest.raises(ValueError, match="zero-size"):
        _prepare(adapter, deployment, signal)

    assert adapter.submitted == []


def test_daemon_ignores_metadata_qty_override():
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(
        deployment,
        {"stop_loss": 95.0, "take_profit": 110.0, "qty": 5000, "explicit_qty": 5000},
    )

    prepared, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(200.0)
    assert prepared.metadata["explicit_qty"] == pytest.approx(200.0)


def test_daemon_rejects_wrong_side_stop():
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(deployment, {"stop_loss": 105.0, "take_profit": 110.0})

    with pytest.raises(ValueError, match="zero-size"):
        _prepare(adapter, deployment, signal)

    assert adapter.submitted == []


def test_daemon_fallback_stop_is_submitted_and_used_for_sizing():
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(deployment, {"take_profit": 110.0})

    prepared, qty = _prepare(adapter, deployment, signal)

    # fallback_stop_pct 0.02 -> stop 98, distance 2 -> 500.
    assert qty == pytest.approx(500.0)
    assert prepared.metadata["stop_loss"] == pytest.approx(98.0)


# ── entry-deviation gate ──────────────────────────────────────────────────────

# Buy: entry 100, SL 96, TP 112 -> SL distance 4. 25 % of it is 1.0, so the
# ask may sit anywhere in [99.0, 101.0].
_BUY = {"stop_loss": 96.0, "take_profit": 112.0}
# Sell: entry 100, SL 104, TP 88. The bid may sit anywhere in [99.0, 101.0].
_SELL = {"stop_loss": 104.0, "take_profit": 88.0}

_TOWARD = "deviation_toward_stop_loss"
_AWAY = "deviation_away_from_stop_loss"
_AT_TP = "quote_at_or_beyond_take_profit"
_AT_SL = "quote_at_or_beyond_stop_loss"


def _short_deployment(**settings: Any) -> LiveDeploymentConfig:
    deployment = _deployment(**settings)
    return deployment.model_copy(update={"signal_config": {"allow_short": True}})


@pytest.mark.parametrize(
    ("ask", "rule"),
    [
        (100.0, None),
        (99.5, None),
        (99.0, None),  # exactly 0.25 toward the SL: not "more than"
        (98.99, _TOWARD),
        (97.0, _TOWARD),
        (100.5, None),
        (101.0, None),  # exactly 0.25 away from the SL
        (101.01, _AWAY),  # chasing: fixed stop now further away than sized
        (104.0, _AWAY),
        (96.0, _AT_SL),  # at the stop
        (95.0, _AT_SL),  # through the stop
        (112.0, _AT_TP),  # at the target
        (130.0, _AT_TP),
    ],
)
def test_buy_gate_limits_both_directions(ask, rule):
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=ask - 0.01, ask=ask))
    signal = _signal(deployment, dict(_BUY))

    if rule is None:
        prepared, _ = _prepare(adapter, deployment, signal)
        assert prepared.metadata["entry_quote_price"] == pytest.approx(ask)
        assert prepared.metadata["entry_gate_rejection_rule"] is None
    else:
        with pytest.raises(ValueError, match=f"entry deviation gate.*{rule}"):
            _prepare(adapter, deployment, signal)
        assert adapter.submitted == []
    assert adapter.quote_calls == ["AAPL"]


@pytest.mark.parametrize(
    ("bid", "rule"),
    [
        (100.0, None),
        (100.5, None),
        (101.0, None),
        (101.01, _TOWARD),
        (103.0, _TOWARD),
        (99.0, None),
        (98.99, _AWAY),
        (96.0, _AWAY),
        (104.0, _AT_SL),
        (105.0, _AT_SL),
        (88.0, _AT_TP),
        (70.0, _AT_TP),
    ],
)
def test_sell_gate_limits_both_directions(bid, rule):
    deployment = _short_deployment()
    adapter = _GateAdapter(quote=_quote(bid=bid, ask=bid + 0.01))
    signal = _signal(deployment, dict(_SELL), action="sell")

    if rule is None:
        prepared, _ = _prepare(adapter, deployment, signal)
        assert prepared.metadata["entry_quote_price"] == pytest.approx(bid)
        assert prepared.metadata["entry_gate_rejection_rule"] is None
    else:
        with pytest.raises(ValueError, match=f"entry deviation gate.*{rule}"):
            _prepare(adapter, deployment, signal)
        assert adapter.submitted == []


@pytest.mark.parametrize(
    ("action", "levels", "quote_price", "rule"),
    [
        # A wide threshold isolates the TP/SL rules from the deviation limit.
        ("buy", _BUY, 111.99, None),
        ("buy", _BUY, 112.0, _AT_TP),
        ("buy", _BUY, 96.01, None),
        ("buy", _BUY, 96.0, _AT_SL),
        ("sell", _SELL, 88.01, None),
        ("sell", _SELL, 88.0, _AT_TP),
        ("sell", _SELL, 103.99, None),
        ("sell", _SELL, 104.0, _AT_SL),
    ],
)
def test_gate_rejects_a_quote_at_or_beyond_tp_or_sl_regardless_of_threshold(
    action, levels, quote_price, rule
):
    deployment = (
        _short_deployment(max_entry_deviation_sl_fraction=100.0)
        if action == "sell"
        else _deployment(max_entry_deviation_sl_fraction=100.0)
    )
    quote = (
        _quote(bid=quote_price - 0.01, ask=quote_price)
        if action == "buy"
        else _quote(bid=quote_price, ask=quote_price + 0.01)
    )
    adapter = _GateAdapter(quote=quote)
    signal = _signal(deployment, dict(levels), action=action)

    if rule is None:
        prepared, _ = _prepare(adapter, deployment, signal)
        assert prepared.metadata["entry_gate_rejection_rule"] is None
    else:
        with pytest.raises(ValueError, match=f"entry deviation gate.*{rule}"):
            _prepare(adapter, deployment, signal)
        assert adapter.submitted == []


def test_gate_without_take_profit_skips_only_the_tp_rule():
    """Explicit unbounded TP: no TP rule, but the deviation limit still binds."""
    unbounded = {"stop_loss": 96.0, "take_profit_policy": "unbounded_exit_on_opposite_signal"}
    wide = _deployment(max_entry_deviation_sl_fraction=100.0)
    adapter = _GateAdapter(quote=_quote(bid=149.99, ask=150.0))
    prepared, _ = _prepare(adapter, wide, _signal(wide, dict(unbounded)))
    assert prepared.metadata["entry_gate_rejection_rule"] is None

    default = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=101.49, ask=101.5))
    with pytest.raises(ValueError, match=f"entry deviation gate.*{_AWAY}"):
        _prepare(adapter, default, _signal(default, dict(unbounded)))


def test_buy_gate_uses_the_ask_not_the_bid():
    deployment = _deployment()
    # Bid alone would read as 0.5 of the SL distance toward the stop.
    adapter = _GateAdapter(quote=_quote(bid=98.0, ask=99.6))
    signal = _signal(deployment, dict(_BUY))

    prepared, _ = _prepare(adapter, deployment, signal)

    assert prepared.metadata["entry_quote_side"] == "ask"
    assert prepared.metadata["entry_quote_price"] == pytest.approx(99.6)
    assert prepared.metadata["entry_deviation_sl_fraction"] == pytest.approx(0.1)


def test_sell_gate_uses_the_bid_not_the_ask():
    deployment = _short_deployment()
    # Ask alone would read as 0.5 of the SL distance toward the stop.
    adapter = _GateAdapter(quote=_quote(bid=100.4, ask=102.0))
    signal = _signal(deployment, dict(_SELL), action="sell")

    prepared, _ = _prepare(adapter, deployment, signal)

    assert prepared.metadata["entry_quote_side"] == "bid"
    assert prepared.metadata["entry_quote_price"] == pytest.approx(100.4)
    assert prepared.metadata["entry_deviation_sl_fraction"] == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("ask", "signed", "direction"),
    [
        (100.8, -0.2, "away_from_stop_loss"),
        (99.2, 0.2, "toward_stop_loss"),
        (100.0, 0.0, "none"),
    ],
)
def test_gate_persists_the_signed_deviation_and_its_direction(ask, signed, direction):
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=ask - 0.01, ask=ask))
    signal = _signal(deployment, dict(_BUY))

    prepared, _ = _prepare(adapter, deployment, signal)

    assert prepared.metadata["entry_deviation_sl_fraction"] == pytest.approx(signed)
    assert prepared.metadata["entry_deviation_direction"] == direction


@pytest.mark.parametrize(
    ("threshold", "ask", "rule"),
    [
        (0.5, 98.5, None),  # 0.375 toward <= 0.5
        (0.5, 97.9, _TOWARD),  # 0.525 > 0.5
        (0.5, 101.9, None),  # 0.475 away <= 0.5
        (0.5, 102.1, _AWAY),  # 0.525 away > 0.5
        (0.1, 99.5, _TOWARD),  # 0.125 > 0.1
        (0.1, 99.6, None),  # exactly 0.1 (float noise absorbed)
        (0.1, 100.4, None),  # exactly 0.1 away
        (0.1, 100.5, _AWAY),
        (0.0, 99.99, _TOWARD),  # zero tolerance: any move rejects
        (0.0, 100.0, None),
        (0.0, 100.01, _AWAY),
    ],
)
def test_gate_threshold_is_configurable_per_deployment(threshold, ask, rule):
    deployment = _deployment(max_entry_deviation_sl_fraction=threshold)
    adapter = _GateAdapter(quote=_quote(bid=ask - 0.01, ask=ask))
    signal = _signal(deployment, dict(_BUY))

    if rule is None:
        prepared, _ = _prepare(adapter, deployment, signal)
        assert prepared.metadata["max_entry_deviation_sl_fraction"] == pytest.approx(threshold)
    else:
        with pytest.raises(ValueError, match=f"entry deviation gate.*{rule}"):
            _prepare(adapter, deployment, signal)


def test_gate_default_threshold_is_a_quarter_of_the_stop_distance():
    deployment = _deployment()
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(deployment, dict(_BUY))

    prepared, _ = _prepare(adapter, deployment, signal)

    assert prepared.metadata["max_entry_deviation_sl_fraction"] == pytest.approx(0.25)


@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf"), "abc", True, None])
def test_gate_rejects_an_invalid_threshold(bad):
    deployment = _deployment(max_entry_deviation_sl_fraction=bad)
    adapter = _GateAdapter(quote=_at_entry())
    signal = _signal(deployment, dict(_BUY))

    with pytest.raises(ValueError, match="max_entry_deviation_sl_fraction"):
        _prepare(adapter, deployment, signal)

    assert adapter.submitted == []


@pytest.mark.parametrize("ask", [97.0, 103.0])
def test_gate_threshold_is_never_read_from_signal_metadata(ask):
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=ask - 0.1, ask=ask))
    signal = _signal(
        deployment, {**_BUY, "max_entry_deviation_sl_fraction": 10.0}
    )

    with pytest.raises(ValueError, match="entry deviation"):
        _prepare(adapter, deployment, signal)


def test_gate_measures_against_the_fallback_stop_sizing_used():
    """No signal stop: the engine's 2 % fallback (98) sets the SL distance."""
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=99.39, ask=99.4))
    signal = _signal(deployment, {"take_profit": 110.0})

    with pytest.raises(ValueError, match=f"entry deviation gate.*{_TOWARD}"):
        _prepare(adapter, deployment, signal)


def test_gate_measures_from_the_resolved_entry():
    """Entry 102 (metadata), SL 98 -> SL distance 4. An ask of 102.9 is 0.225
    of it away from the SL and passes; measured from theoretical 100
    (distance 2) it would read as 1.45 away and reject."""
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=102.8, ask=102.9))
    signal = _signal(
        deployment, {"entry_price": 102.0, "stop_loss": 98.0, "take_profit": 110.0}
    )

    prepared, _ = _prepare(adapter, deployment, signal)

    assert prepared.metadata["entry_deviation_sl_fraction"] == pytest.approx(-0.225)


def test_quote_is_never_a_sizing_input():
    deployment = _deployment()
    quantities = []
    for ask in (100.0, 99.2, 100.8, 99.5):
        adapter = _GateAdapter(quote=_quote(bid=ask - 0.05, ask=ask))
        signal = _signal(deployment, dict(_BUY))
        prepared, qty = _prepare(adapter, deployment, signal)
        quantities.append(qty)
        assert prepared.metadata["stop_loss"] == pytest.approx(96.0)
        assert prepared.metadata["take_profit"] == pytest.approx(112.0)
        assert prepared.theoretical_price == pytest.approx(100.0)
    assert quantities == pytest.approx([250.0] * 4)


def test_gate_persists_quote_and_deviation_on_the_signal():
    deployment = _deployment()
    quote_time = datetime.now(timezone.utc).replace(microsecond=0)
    adapter = _GateAdapter(
        quote=_quote(bid=99.4, ask=99.5, timestamp=quote_time, source="test-broker")
    )
    signal = _signal(deployment, dict(_BUY))

    prepared, _ = _prepare(adapter, deployment, signal)

    metadata = prepared.metadata
    assert metadata["entry_quote_bid"] == pytest.approx(99.4)
    assert metadata["entry_quote_ask"] == pytest.approx(99.5)
    assert metadata["entry_quote_side"] == "ask"
    assert metadata["entry_quote_price"] == pytest.approx(99.5)
    assert metadata["entry_quote_time"] == quote_time.isoformat()
    assert metadata["entry_quote_source"] == "test-broker"
    assert metadata["entry_deviation_sl_fraction"] == pytest.approx(0.125)
    assert metadata["entry_deviation_direction"] == "toward_stop_loss"
    assert metadata["max_entry_deviation_sl_fraction"] == pytest.approx(0.25)
    assert metadata["entry_gate_rejection_rule"] is None
    # The persisted figures reach the broker adapter.
    submitted_signal, _ = asyncio.run(_submit_and_capture(deployment, adapter))
    assert submitted_signal.metadata["entry_quote_price"] == pytest.approx(99.5)


async def _submit_and_capture(deployment, adapter):
    signal = _signal(deployment, dict(_BUY))
    await daemon.submit_order_with_risk(adapter, deployment, signal)
    return adapter.submitted[-1]


def test_payload_cannot_spoof_the_persisted_quote():
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=99.4, ask=99.5))
    signal = _signal(
        deployment,
        {
            **_BUY,
            "entry_quote_price": 1.0,
            "entry_quote_bid": 1.0,
            "entry_quote_ask": 1.0,
            "entry_quote_side": "bid",
            "entry_quote_time": "1999-01-01T00:00:00+00:00",
            "entry_quote_source": "publisher",
            "entry_deviation_sl_fraction": 0.0,
            "entry_deviation_direction": "none",
            "entry_gate_rejection_rule": "publisher",
        },
    )

    prepared, _ = _prepare(adapter, deployment, signal)

    metadata = prepared.metadata
    assert metadata["entry_quote_price"] == pytest.approx(99.5)
    assert metadata["entry_quote_bid"] == pytest.approx(99.4)
    assert metadata["entry_quote_ask"] == pytest.approx(99.5)
    assert metadata["entry_quote_side"] == "ask"
    assert metadata["entry_quote_time"] != "1999-01-01T00:00:00+00:00"
    assert metadata["entry_quote_source"] != "publisher"
    assert metadata["entry_deviation_sl_fraction"] == pytest.approx(0.125)
    assert metadata["entry_deviation_direction"] == "toward_stop_loss"
    assert metadata["entry_gate_rejection_rule"] is None


# ── gate: fail closed when the quote is unavailable or unusable ───────────────


def test_gate_fails_closed_when_the_quote_cannot_be_fetched(caplog):
    deployment = _deployment()
    adapter = _GateAdapter(quote_error=RuntimeError("quote endpoint down"))
    signal = _signal(deployment, dict(_BUY))

    with caplog.at_level(logging.WARNING, logger="live-runner"):
        with pytest.raises(ValueError, match="quote"):
            _prepare(adapter, deployment, signal)

    assert adapter.submitted == []
    assert any("quote" in record.getMessage().lower() for record in caplog.records)


def test_gate_fails_closed_when_the_adapter_has_no_quote_support():
    deployment = _deployment()
    adapter = _QuotelessAdapter()
    signal = _signal(deployment, dict(_BUY))

    with pytest.raises(ValueError, match="quote"):
        _prepare(adapter, deployment, signal)

    assert adapter.submitted == []


@pytest.mark.parametrize(
    ("bid", "ask"),
    [
        (0.0, 100.0),
        (100.0, 0.0),
        (-1.0, 100.0),
        (float("nan"), 100.0),
        (100.0, float("inf")),
        (100.5, 100.0),  # crossed book
    ],
)
def test_gate_fails_closed_on_an_unusable_quote(bid, ask):
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=bid, ask=ask))
    signal = _signal(deployment, dict(_BUY))

    with pytest.raises(ValueError, match="quote"):
        _prepare(adapter, deployment, signal)

    assert adapter.submitted == []


def test_gate_fails_closed_when_the_adapter_returns_nothing():
    deployment = _deployment()
    adapter = _GateAdapter(quote=None)
    signal = _signal(deployment, dict(_BUY))

    with pytest.raises(ValueError, match="quote"):
        _prepare(adapter, deployment, signal)


@pytest.mark.parametrize(
    ("ask", "signed", "direction", "rule"),
    [
        (98.0, 0.5, "toward_stop_loss", _TOWARD),
        (102.0, -0.5, "away_from_stop_loss", _AWAY),
        (113.0, -3.25, "away_from_stop_loss", _AT_TP),
        (95.0, 1.25, "toward_stop_loss", _AT_SL),
    ],
)
def test_gate_rejection_is_logged_with_quote_deviation_and_rule(
    caplog, ask, signed, direction, rule
):
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=ask - 0.1, ask=ask))
    signal = _signal(deployment, dict(_BUY))

    with caplog.at_level(logging.WARNING, logger="live-runner"):
        with pytest.raises(ValueError, match="entry deviation"):
            _prepare(adapter, deployment, signal)

    records = [r for r in caplog.records if "entry deviation" in r.getMessage().lower()]
    assert records, "the gate rejection was not logged"
    data = getattr(records[-1], "data", {})
    assert data["entry_quote_price"] == pytest.approx(ask)
    assert data["entry_deviation_sl_fraction"] == pytest.approx(signed)
    assert data["entry_deviation_direction"] == direction
    assert data["entry_gate_rejection_rule"] == rule
    assert data["max_entry_deviation_sl_fraction"] == pytest.approx(0.25)


def test_exit_only_orders_are_not_gated():
    """Exit-only closes reduce exposure; the deviation gate is for entries."""
    deployment = _deployment()
    adapter = _GateAdapter(
        quote_error=RuntimeError("must not be asked"),
        positions=[{"symbol": "AAPL", "qty": 4.0}],
    )
    signal = _signal(deployment, {"exit_only": True}, action="sell")

    prepared, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(4.0)
    assert adapter.quote_calls == []


# ── gate on the Discord submission path ───────────────────────────────────────


def _discord_trade_signal():
    from risk import TradeSignal

    return TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=112.0,
        timeframe="1h",
        metadata={
            "discord_channel_id": "channel-1",
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def test_discord_path_rejects_deviation_before_claiming(monkeypatch, caplog):
    claims: list[str] = []
    monkeypatch.setattr(
        daemon,
        "claim_live_deployment_submission",
        lambda deployment_id, token: claims.append(token) or True,
    )
    monkeypatch.setattr(daemon, "release_live_deployment_submission", lambda token: None)
    monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda deployment: True)
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=97.9, ask=98.0))

    with caplog.at_level(logging.WARNING, logger="live-runner"):
        asyncio.run(
            daemon._submit_normalized_discord_signal(
                "acc-1",
                adapter,
                deployment,
                _discord_trade_signal(),
                signal_timeframe="1h",
                wait_enabled=False,
            )
        )

    assert adapter.submitted == []
    assert claims == [], "a gate refusal must not take the submission lease"
    assert adapter.quote_calls == ["AAPL"]


def test_discord_path_submits_when_quote_is_close(monkeypatch):
    monkeypatch.setattr(
        daemon, "claim_live_deployment_submission", lambda deployment_id, token: True
    )
    monkeypatch.setattr(daemon, "release_live_deployment_submission", lambda token: None)
    monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda deployment: True)
    deployment = _deployment()
    adapter = _GateAdapter(quote=_quote(bid=99.8, ask=99.9))

    asyncio.run(
        daemon._submit_normalized_discord_signal(
            "acc-1",
            adapter,
            deployment,
            _discord_trade_signal(),
            signal_timeframe="1h",
            wait_enabled=False,
        )
    )

    assert len(adapter.submitted) == 1
    submitted, qty = adapter.submitted[0]
    assert qty == pytest.approx(250.0)
    assert submitted.metadata["entry_quote_price"] == pytest.approx(99.9)
    assert math.isclose(submitted.metadata["entry_deviation_sl_fraction"], 0.025, abs_tol=1e-9)


def test_quote_time_is_serialised_and_a_missing_time_rejects():
    """The broker quote time is persisted as ISO-8601; a quote without one is
    refused rather than stamped with a fabricated time."""
    deployment = _deployment()
    stamp = datetime.now(timezone.utc)
    adapter = _GateAdapter(quote=_quote(bid=99.9, ask=100.0, timestamp=stamp))
    signal = _signal(deployment, dict(_BUY))

    prepared, _ = _prepare(adapter, deployment, signal)

    assert prepared.metadata["entry_quote_time"] == stamp.isoformat()

    adapter = _GateAdapter(quote=_quote(bid=99.9, ask=100.0, timestamp=None))
    with pytest.raises(ValueError, match="quote_time_missing"):
        _prepare(adapter, deployment, _signal(deployment, dict(_BUY)))
    assert adapter.submitted == []
