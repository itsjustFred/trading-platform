"""#121: the Discord parser must never turn an exit, report or condition into an entry.

Every row in the issue's table sent a wrong-side or wrong-symbol order to a real
broker account, or sized risk off the wrong entry price. The maintainer decisions
on the issue are encoded here:

1. Exit/close messages become an *exit-only* signal (``signal_intent == "exit"``);
   the close side comes from the position at dispatch, never from the message.
3. A partial exit closes the stated fraction; an unquantifiable partial is
   rejected, never guessed. A full close only when the message clearly says so.
4. The first ``@``/``at`` price is the entry; ``target``/``TP`` is the take
   profit and ``stop``/``SL`` the stop loss.

Report messages ("Took profit on ...") and conditional messages ("wait for
confirmation ...") are rejected outright.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from live_runner.signal_sources.discord_source import _parse_trade_signal_from_message


def _parse(content: str):
    message = SimpleNamespace(
        id="900000000000000121",
        content=content,
        channel=SimpleNamespace(id="100000000000000121"),
        author=SimpleNamespace(id="bot-121", bot=True),
        created_at=datetime.now(timezone.utc),
    )
    return _parse_trade_signal_from_message(message, default_timeframe="1h")


def _is_exit(signal) -> bool:
    return signal is not None and signal.metadata.get("signal_intent") == "exit"


# ─── Real message shapes, one per configured bot ─────────────────────────────
#
# The only real samples in the repository (see test_discord_ingestion_gates.py,
# scripts/live_smoke.py). They must keep parsing exactly as before.

NITRO_OVERSOLD = (
    "\U0001f4a5 **NITRO WATCHLIST — BUY SIGNAL**\n\n"
    "**BE** is down significantly\n\n"
    "Price:      $256.30\n"
    "RSI:        29.8\n\n"
    "**Triggered by:** \U0001f53b RSI Cross-Under\n"
    "• RSI crossed below 30 (now 29.8)\n"
)
NITRO_SUMMARY = (
    "\U0001f4a5 **Nitro Daily Summary** — 2026-09-25\n\n"
    "Today:\n• Alerts: 0 | Closed: 0\n\n"
    "All Time (since 2026-02-25):\n• Alerts: 853 | Closed: 249 | Open: 19\n"
)
ROCKETFUEL_OVERBOUGHT = (
    "⚡ **OVERBOUGHT ALERT**\n\n"
    "**MXL** is overbought\n\n"
    "Price: $93.80\n"
    "RSI: 71.4\n\n"
    "RSI has crossed above 70\n"
)
RAMJET_OVERSOLD = "**$AAPL** is now oversold — buy signal\nPrice: $190.50"
RAMJET_DIRECTIVE = "RAM JET BUY AAPL @ 100.0"
ROCKETFUEL_REVERSAL = "Rocket Fuel momentum reversal for **$TSLA** @ 180.25"


# (message, expected) where expected is None for a rejection, or a dict with
# action/symbol/entry/stop_loss/take_profit and, for exits, fraction/side.
_ENTRY = "entry"
_EXIT = "exit"

PARSER_TABLE: list[tuple[str, dict | None]] = [
    # ── The issue's reproduced cases ──
    ("Exit long AAPL @ 100", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 100.0, "fraction": 1.0, "side": "long",
    }),
    ("Closing long AAPL at $100", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 100.0, "fraction": 1.0, "side": "long",
    }),
    ("Took profit on long AAPL @ $105", None),
    ("Wait for confirmation before you buy AAPL @ 100", None),
    ("BUY AAPL @ 100 target $120", {
        "intent": _ENTRY, "action": "buy", "symbol": "AAPL", "entry": 100.0,
        "take_profit": 120.0, "stop_loss": None,
    }),
    # ── Real samples from each configured bot (unchanged behaviour) ──
    (NITRO_OVERSOLD, {
        "intent": _ENTRY, "action": "buy", "symbol": "BE", "entry": 256.30,
        "take_profit": None, "stop_loss": None,
    }),
    (NITRO_SUMMARY, None),
    (ROCKETFUEL_OVERBOUGHT, None),
    (RAMJET_OVERSOLD, {
        "intent": _ENTRY, "action": "buy", "symbol": "AAPL", "entry": 190.50,
        "take_profit": None, "stop_loss": None,
    }),
    (RAMJET_DIRECTIVE, {
        "intent": _ENTRY, "action": "buy", "symbol": "AAPL", "entry": 100.0,
        "take_profit": None, "stop_loss": None,
    }),
    # A momentum reversal is RocketFuel's sell; it is not a parsed exit intent
    # (the deployment variant decides whether it closes or shorts, unchanged).
    (ROCKETFUEL_REVERSAL, {
        "intent": _ENTRY, "action": "sell", "symbol": "TSLA", "entry": 180.25,
        "take_profit": None, "stop_loss": None,
    }),
    # ── Full exits: the message names the close, the side is only a cross-check ──
    ("Close short TSLA @ 50", {
        "intent": _EXIT, "symbol": "TSLA", "entry": 50.0, "fraction": 1.0, "side": "short",
    }),
    ("Exit AAPL @ 100", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 100.0, "fraction": 1.0, "side": None,
    }),
    ("Close **$AAPL** @ 101.5", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 101.5, "fraction": 1.0, "side": None,
    }),
    ("Exit the long position in AAPL @ 100", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 100.0, "fraction": 1.0, "side": "long",
    }),
    # A target/stop on an exit message is not an entry level and is ignored.
    ("Exit long AAPL @ 100, target was 120", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 100.0, "fraction": 1.0, "side": "long",
    }),
    # ── Partial exits: explicit fraction or percentage only ──
    ("Trim half AAPL @ 105", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 105.0, "fraction": 0.5, "side": None,
    }),
    ("Took partials 1/3 on AAPL @ 105", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 105.0, "fraction": 1 / 3, "side": None,
    }),
    ("Sell 1/3 AAPL @ 105", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 105.0, "fraction": 1 / 3, "side": None,
    }),
    ("Trim 25% of long AAPL @ 105", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 105.0, "fraction": 0.25, "side": "long",
    }),
    ("Scale out 50% AAPL @ 105", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 105.0, "fraction": 0.5, "side": None,
    }),
    ("Trim 100% AAPL @ 105", {
        "intent": _EXIT, "symbol": "AAPL", "entry": 105.0, "fraction": 1.0, "side": None,
    }),
    # Unquantifiable or impossible partials: rejected, never guessed.
    ("Took partials on AAPL @ 105", None),
    ("Trim AAPL @ 105", None),
    ("Take some profits on AAPL @ 105", None),
    ("Trim 150% AAPL @ 105", None),
    ("Trim 0% AAPL @ 105", None),
    ("Sell 3/0 AAPL @ 105", None),
    # Two different fractions in one message: ambiguous.
    ("Trim half AAPL @ 105, then 1/3 more", None),
    # An exit needs exactly one ticker.
    ("Exit long AAPL and MSFT @ 100", None),
    # ── Reports: an outcome, not an instruction ──
    ("AAPL stopped out @ 95", None),
    ("AAPL hit target $120", None),
    ("Target hit on AAPL @ 120", None),
    ("Closed AAPL @ 105 for +5%", None),
    ("Exited long AAPL @ 105", None),
    ("Profit taken on AAPL @ 105", None),
    # ── Conditional messages: not an entry (or an exit) yet ──
    ("If AAPL breaks $100 buy AAPL @ 100", None),
    ("Watch for a reclaim, buy AAPL @ 100", None),
    ("Buy AAPL @ 100 once it confirms", None),
    ("Waiting on AAPL, buy signal @ 100", None),
    ("Close long AAPL @ 100 if it loses VWAP", None),
    # ── Price levels: first @/at price is the entry ──
    ("BUY AAPL @ 100 TP 120 SL 95", {
        "intent": _ENTRY, "action": "buy", "symbol": "AAPL", "entry": 100.0,
        "take_profit": 120.0, "stop_loss": 95.0,
    }),
    ("BUY AAPL @ 100, stop loss $95, take profit $120", {
        "intent": _ENTRY, "action": "buy", "symbol": "AAPL", "entry": 100.0,
        "take_profit": 120.0, "stop_loss": 95.0,
    }),
    ("BUY AAPL at 100 target 120 stop 95", {
        "intent": _ENTRY, "action": "buy", "symbol": "AAPL", "entry": 100.0,
        "take_profit": 120.0, "stop_loss": 95.0,
    }),
    ("BUY AAPL @ 100 SL: $95", {
        "intent": _ENTRY, "action": "buy", "symbol": "AAPL", "entry": 100.0,
        "take_profit": None, "stop_loss": 95.0,
    }),
    # The target precedes the entry in the text: still not the entry.
    ("Target $120 - BUY AAPL @ 100", {
        "intent": _ENTRY, "action": "buy", "symbol": "AAPL", "entry": 100.0,
        "take_profit": 120.0, "stop_loss": None,
    }),
    # A target or stop alone is never an entry price.
    ("BUY AAPL target $120", None),
    ("BUY AAPL stop $95", None),
    # Two different targets (or stops): ambiguous, rejected.
    ("BUY AAPL @ 100 TP 110 TP 120", None),
    ("BUY AAPL @ 100 SL 95 SL 90", None),
    # A malformed level price is not dropped silently, the signal is.
    ("BUY AAPL @ 100 target $12,34", None),
    # An exit and an entry in one message: ambiguous, rejected.
    ("BUY AAPL @ 100 and close long MSFT", None),
]


@pytest.mark.parametrize(("content", "expected"), PARSER_TABLE, ids=[row[0][:60] for row in PARSER_TABLE])
def test_parser_table(content: str, expected: dict | None) -> None:
    signal = _parse(content)
    if expected is None:
        assert signal is None, (
            f"{content!r} must be rejected, got action={signal.action} "
            f"symbol={signal.symbol} entry={signal.entry_price} "
            f"metadata_intent={signal.metadata.get('signal_intent')}"
        )
        return
    assert signal is not None, f"{content!r} must parse"
    assert signal.symbol == expected["symbol"], content
    assert signal.entry_price == pytest.approx(expected["entry"]), content
    if expected["intent"] == _EXIT:
        assert _is_exit(signal), f"{content!r} must be an exit-only signal"
        assert signal.metadata["exit_only"] is True
        assert signal.metadata["exit_fraction"] == pytest.approx(expected["fraction"])
        assert signal.metadata.get("exit_position_side") == expected["side"]
        # An exit never carries entry levels: it closes, it does not bracket.
        assert signal.stop_loss is None
        assert signal.take_profit is None
    else:
        assert not _is_exit(signal), f"{content!r} must be an entry, not an exit"
        assert "exit_fraction" not in signal.metadata
        assert signal.action == expected["action"], content
        assert signal.take_profit == (
            pytest.approx(expected["take_profit"])
            if expected["take_profit"] is not None
            else None
        ), content
        assert signal.stop_loss == (
            pytest.approx(expected["stop_loss"])
            if expected["stop_loss"] is not None
            else None
        ), content


# ─── The acceptance invariant, stated over a corpus ──────────────────────────

_EXIT_REPORT_OR_CONDITIONAL = [
    "Exit long AAPL @ 100",
    "Closing long AAPL at $100",
    "Close long AAPL @ 100",
    "Close short AAPL @ 100",
    "Exiting AAPL @ 100",
    "Exit long $AAPL @ 100",
    "Took profit on long AAPL @ $105",
    "Took profits on AAPL @ $105",
    "Took partials on AAPL @ 105",
    "Trim half AAPL @ 105",
    "Sell 1/3 AAPL @ 105",
    "Take profit on long AAPL @ 105",
    "AAPL stopped out @ 95",
    "AAPL hit target $120",
    "Closed long AAPL @ 105",
    "Exited AAPL @ 105",
    "Wait for confirmation before you buy AAPL @ 100",
    "Wait for confirmation, BUY AAPL @ 100",
    "If AAPL holds $100, long AAPL @ 100",
    "Buy AAPL @ 100 when it reclaims VWAP",
    "Looking for a long AAPL @ 100",
    "Potential long AAPL @ 100",
]


@pytest.mark.parametrize("content", _EXIT_REPORT_OR_CONDITIONAL)
def test_no_exit_report_or_condition_produces_a_new_entry(content: str) -> None:
    """The issue's acceptance criterion: an exit may be an exit-only signal,
    everything else is rejected. Nothing in this corpus may open a position."""
    signal = _parse(content)
    assert signal is None or _is_exit(signal), (
        f"{content!r} produced a new {signal.action} entry on {signal.symbol}"
    )


def test_exit_signal_is_marked_exit_only_independently_of_its_action() -> None:
    """The close side comes from the position, so the exit flag - not the
    placeholder action - is what downstream code keys on. A deployment that
    allows shorts must still treat this as a close, never as a new short."""
    signal = _parse("Exit long AAPL @ 100")
    assert _is_exit(signal)
    assert signal.metadata["exit_only"] is True
    assert signal.metadata["risk_sizing_mode"] == "exit_only"


def test_long_word_after_on_is_not_a_ticker() -> None:
    """"Took profit on long AAPL" used to resolve the symbol LONG."""
    from live_runner.signal_sources.discord_source import _extract_symbol

    assert _extract_symbol("profit on long AAPL @ $105") != "LONG"


def test_a_long_word_is_not_truncated_into_a_ticker() -> None:
    """"for confirmation" used to be cut to the 10-letter ticker CONFIRMATI."""
    from live_runner.signal_sources.discord_source import _extract_labeled_symbol

    assert _extract_labeled_symbol("wait for confirmation") is None
    assert _extract_labeled_symbol("buy signal for AAPL") == "AAPL"


def test_entry_price_ignores_target_and_stop_clauses() -> None:
    from live_runner.signal_sources.discord_source import _extract_trade_levels

    levels = _extract_trade_levels("BUY AAPL @ 100 target $120 stop $95")
    assert levels is not None
    assert levels.entry_price == pytest.approx(100.0)
    assert levels.take_profit == pytest.approx(120.0)
    assert levels.stop_loss == pytest.approx(95.0)


def test_an_indicator_reading_after_at_is_not_an_entry() -> None:
    """"at" is a price marker for "BUY AAPL at 100", not for "RSI at 28":
    an oscillator value must never become the entry the risk is sized off."""
    signal = _parse("**AAPL** is now oversold — buy signal, RSI at 28, $190.50")
    assert signal is not None
    assert signal.entry_price == pytest.approx(190.50)
