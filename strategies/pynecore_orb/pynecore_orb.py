"""
@pyne
Opening Range Breakout (PyneCore).
Single-symbol intraday breakout of the first-30-minute range:

- Opening range (OR) = high/low of 09:30-10:00 ET (first 30 minutes).
- Long when close breaks above or_high + or_range * breakout_buffer (0.2).
- Short when close breaks below or_low - or_range * breakout_buffer.
- 1%-of-price trailing stop from the high/low water mark.
- Forced exit at 15:45 ET (flat AT the 15:45 open: the close order goes out on
  the bar before, because Pine fills at the next bar open). One trade per day.

Session times are derived from each bar's timestamp in America/New_York.
Faithful port of opening_range_breakout/algorithm.py (Variant 2,
price-percentage trailing stop) with the variant_2.py buffer default (0.2).
"""

STRATEGY_META = {
    "name": "Opening Range Breakout (PyneCore)",
    "version": "2.1.0",
    "source_url": "https://github.com/alexisbouley2/quantconnect/tree/main/strategies/opening_range_breakout",
    "notes": (
        "Single-symbol intraday ORB per opening_range_breakout/algorithm.py "
        "(Variant 2: 1%-of-price trailing stop from HWM/LWM, forced exit "
        "15:45 ET, one trade per day) with the variant_2.py buffer default "
        "0.2. Session logic derives 9:30-15:45 ET from bar timestamps "
        "(America/New_York). 2.1.0: the forced-exit order is raised on the bar "
        "before 15:45 so it fills AT the 15:45 open (next-open fills). "
        "Replaces the previous SMA-crossover implementation that was kept "
        "under this slug by mistake."
    ),
    "parameters": {
        "opening_range_minutes": 30,
        "breakout_buffer": 0.2,
        "reversion_multiple": 0.01,
        "exit_hour_ny": 15,
        "exit_minute_ny": 45,
    },
    "tags": {
        "type": ["breakout", "intraday", "momentum"],
        "source": "alexisbouley2-github",
        "link": "https://github.com/alexisbouley2/quantconnect/tree/main/strategies/opening_range_breakout",
        "asset_class": ["equities", "indices"],
        "complexity": "medium",
        "naming_note": (
            "slug retained as pynecore_orb for backtest-artifact compatibility; "
            "the 2.0.0 rewrite implements the opening-range breakout the slug "
            "always named (the 1.x implementation was an SMA crossover kept "
            "under this slug by mistake)"
        ),
    },
}

from pynecore import Series
from pynecore.lib import (
    dayofmonth,
    high,
    hour,
    input,
    low,
    minute,
    month,
    script,
    strategy,
    time,
    timeframe,
    year,
)

_TZ = "America/New_York"
_OR_START_MINUTES = 9 * 60 + 30  # 09:30 ET

# Session state. Module-level by design: the adapter runs one pyne CLI
# subprocess per symbol, so this is process-isolated (the pyne equivalent
# of Pine `var`); pynecore.lib offers no Persistent-fields API.
_STATE = {
    "day": None,
    "or_high": None,
    "or_low": None,
    "traded": False,
    "hwm": None,
    "lwm": None,
}


@script.strategy(
    title="Opening Range Breakout",
    shorttitle="ORB",
    overlay=True,
    pyramiding=0,
    default_qty_type=strategy.percent_of_equity,
    default_qty_value=100,
    initial_capital=100000,
)
def main(
    src: Series[float] = input.source("close", title="Source"),
    opening_range_minutes: int = input.int(30, title="Opening Range Minutes"),
    breakout_buffer: float = input.float(0.2, title="Breakout Buffer (x OR range)"),
    reversion_multiple: float = input.float(0.01, title="Trailing Reversion (x price)"),
    exit_hour_ny: int = input.int(15, title="Forced Exit Hour (ET)"),
    exit_minute_ny: int = input.int(45, title="Forced Exit Minute"),
):
    # Coarse bars cannot express the 30-minute opening range (a 1h bar spans
    # past the window end; daily bars have no session clock). Fail loudly
    # instead of backtesting silently wrong sessions. 30m is the coarsest
    # supported interval — one bar covers the window exactly.
    if not timeframe.isintraday() or timeframe.in_seconds() > 30 * 60:
        raise ValueError(
            "pynecore_orb requires 30m-or-finer intraday bars "
            f"(chart timeframe {timeframe.period!r})."
        )
    bar_time = time()
    bar_hour = hour(bar_time, _TZ)
    bar_minute = minute(bar_time, _TZ)
    day_key = (year(bar_time, _TZ), month(bar_time, _TZ), dayofmonth(bar_time, _TZ))

    if _STATE["day"] != day_key:
        if strategy.position_size != 0:
            # B-H1: day-reset flatten. No forced-exit bar fired in the prior
            # session (data gap) — never carry an intraday position overnight.
            strategy.close("Long", comment="day_reset_flatten")
            strategy.close("Short", comment="day_reset_flatten")
        _STATE["day"] = day_key
        _STATE["or_high"] = None
        _STATE["or_low"] = None
        _STATE["traded"] = False
        _STATE["hwm"] = None
        _STATE["lwm"] = None

    minutes_now = bar_hour * 60 + bar_minute
    or_end = _OR_START_MINUTES + opening_range_minutes
    exit_minutes = exit_hour_ny * 60 + exit_minute_ny
    bar_minutes = timeframe.in_seconds() / 60.0

    # Accumulate the opening range only inside its window; bars before 09:30
    # ET (e.g. overnight CFD bars) must not pollute it.
    if _OR_START_MINUTES <= minutes_now < or_end:
        bar_high = float(high)
        bar_low = float(low)
        if _STATE["or_high"] is None:
            _STATE["or_high"] = bar_high
            _STATE["or_low"] = bar_low
        else:
            if bar_high > _STATE["or_high"]:
                _STATE["or_high"] = bar_high
            if bar_low < _STATE["or_low"]:
                _STATE["or_low"] = bar_low

    position = strategy.position_size

    # Forced exit first: flat into 15:45 ET, no entries afterwards. Pine fills
    # strategy.close() at the NEXT bar's open, so the order is raised on the first
    # bar whose END reaches 15:45 (the bar before 15:45) to be flat AT the 15:45
    # open instead of one bar later (which holds overnight on RTH-only data).
    # Bar length comes from the chart timeframe, no later bar is looked at; a
    # missing bar before 15:45 just moves the signal to the first bar present.
    if minutes_now + bar_minutes >= exit_minutes:
        if position != 0:
            strategy.close("Long", comment="time_exit")
            strategy.close("Short", comment="time_exit")
        _STATE["traded"] = True
        _STATE["hwm"] = None
        _STATE["lwm"] = None
        return

    if position > 0:
        # HWM/LWM track close by design: QC Variant 2 watermarks
        # current_price = bar close (algorithm.py), not high/low.
        price = float(src)
        if _STATE["hwm"] is None or price > _STATE["hwm"]:
            _STATE["hwm"] = price
        reversion_level = _STATE["hwm"] - _STATE["hwm"] * reversion_multiple
        if price < reversion_level:
            strategy.close("Long", comment="trailing_stop")
            _STATE["traded"] = True
            _STATE["hwm"] = None
    elif position < 0:
        price = float(src)
        if _STATE["lwm"] is None or price < _STATE["lwm"]:
            _STATE["lwm"] = price
        reversion_level = _STATE["lwm"] + _STATE["lwm"] * reversion_multiple
        if price > reversion_level:
            strategy.close("Short", comment="trailing_stop")
            _STATE["traded"] = True
            _STATE["lwm"] = None
    elif minutes_now >= or_end and not _STATE["traded"] and _STATE["or_high"] is not None:
        or_range = _STATE["or_high"] - _STATE["or_low"]
        if or_range > 0:
            price = float(src)
            if price > _STATE["or_high"] + or_range * breakout_buffer:
                strategy.entry("Long", strategy.long)
                _STATE["hwm"] = price
                _STATE["traded"] = True
            elif price < _STATE["or_low"] - or_range * breakout_buffer:
                strategy.entry("Short", strategy.short)
                _STATE["lwm"] = price
                _STATE["traded"] = True
