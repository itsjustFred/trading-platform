"""INDEPENDENT reference implementations of three published strategies.

Plain Python lists and dicts.  Nothing here imports ``runner``, ``strategies``,
``risk`` or ``shared`` (guarded by ``test_reference_has_no_platform_imports``),
and it does not share any code with the ported strategies.

Conventions fixed by the platform contract (and stated in the report), applied
identically to every strategy:

* a decision is taken on the CLOSE of bar ``i``;
* an entry fills at the OPEN of bar ``i+1`` (no fill if ``i`` is the last bar);
* an exit fills at the CLOSE of bar ``i`` (``signal_exit_fill="close"``);
* quantity is ``cash / open`` at the fill (fully invested; the 100 % position cap is the binding sizing rule,
  measured on the FILL price; the fee is charged on top, not reserved);
* a position still open on the last bar is closed at that bar's close
  (the engine's ``end_of_data`` exit);
* proportional fee ``fee_rate`` on each side's notional.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Bar:
    day: str  # YYYY-MM-DD
    open: float
    close: float


@dataclass
class Trade:
    symbol: str
    entry_day: str
    entry_price: float
    qty: float
    exit_day: str
    exit_price: float
    fees: float = 0.0

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def net_pnl(self) -> float:
        return self.pnl - self.fees


@dataclass
class SimResult:
    trades: list[Trade] = field(default_factory=list)
    realized: dict[str, float] = field(default_factory=dict)  # day -> realized equity
    marked: dict[str, float] = field(default_factory=dict)  # day -> cash + qty*close
    final_equity: float = 0.0


def simulate(
    bars_by_symbol: dict[str, list[Bar]],
    target: dict[str, dict[str, bool]],
    *,
    capital: float = 100_000.0,
    fee_rate: float = 0.0,
) -> SimResult:
    """One-position-at-a-time long/flat simulator on shared cash.

    ``target[symbol][day]`` is the decision taken on that day's close
    (True = hold ``symbol`` from the next bar, False = flat in ``symbol``);
    days without an entry keep the previous decision.  Bars of all symbols
    must share one calendar (the tests use the fixture's own calendar).
    """
    res = SimResult()
    cash = capital
    holding: str | None = None
    qty = entry_price = entry_fees = 0.0
    entry_day = ""
    pending: tuple[str, float] | None = None  # (symbol, qty) filled at next open
    calendars = {s: [b.day for b in bars] for s, bars in bars_by_symbol.items()}
    days = sorted({d for cal in calendars.values() for d in cal})
    index = {s: {b.day: b for b in bars} for s, bars in bars_by_symbol.items()}
    last_day = days[-1]
    for day in days:
        # 1. fill the entry decided on the previous bar at today's open
        if pending is not None:
            symbol, _ = pending
            pending = None
            bar = index[symbol].get(day)
            if bar is not None and holding is None:
                q = cash / bar.open  # fees are NOT reserved by the engine's sizing
                fee = fee_rate * q * bar.open
                cash -= q * bar.open + fee
                holding, qty, entry_price, entry_day, entry_fees = symbol, q, bar.open, day, fee
        # 2. decisions on today's close
        decisions = {s: target[s][day] for s in target if day in target[s]}
        if holding is not None and holding in decisions and not decisions[holding]:
            bar = index[holding][day]
            fee = fee_rate * qty * bar.close
            cash += qty * bar.close - fee
            res.trades.append(
                Trade(holding, entry_day, entry_price, qty, day, bar.close, entry_fees + fee)
            )
            holding = None
            qty = 0.0
        if holding is not None and day == last_day:
            bar = index[holding][day]
            fee = fee_rate * qty * bar.close
            cash += qty * bar.close - fee
            res.trades.append(
                Trade(holding, entry_day, entry_price, qty, day, bar.close, entry_fees + fee)
            )
            holding = None
            qty = 0.0
        if holding is None and day != last_day:
            for symbol, want in decisions.items():
                if want:
                    close = index[symbol][day].close
                    pending = (symbol, 0.0)  # quantity is fixed at the fill (cash / open)
                    break
        res.realized[day] = cash
        mark = cash + (qty * index[holding][day].close if holding and day in index[holding] else 0.0)
        res.marked[day] = mark
    res.final_equity = res.marked[last_day]
    return res


# ───────────────────────────── Faber (2007) ──────────────────────────────


def faber_target(bars: list[Bar], sma_months: int = 10) -> dict[str, bool]:
    """Month-end decision: hold iff month-end close > mean of last N month-end closes.

    Returns the decision on month-end days only (the position is held through
    the following month).  The SMA includes the current month-end.
    """
    month_ends = [i for i, b in enumerate(bars) if i + 1 == len(bars) or bars[i + 1].day[:7] != b.day[:7]]
    out: dict[str, bool] = {}
    for k, i in enumerate(month_ends):
        if k + 1 < sma_months:
            continue
        window = [bars[month_ends[j]].close for j in range(k - sma_months + 1, k + 1)]
        out[bars[i].day] = bars[i].close > sum(window) / sma_months
    return out


# ───────────────────────── Connors RSI(2) (2008) ─────────────────────────


def wilder_rsi(closes: list[float], period: int = 2) -> list[float | None]:
    """Wilder's RSI, SMA-seeded: first averages = mean of the first ``period`` changes."""
    out: list[float | None] = [None] * len(closes)
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    if len(closes) <= period:
        return out
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if i > period:
            ag = (ag * (period - 1) + gains[i - 1]) / period
            al = (al * (period - 1) + losses[i - 1]) / period
        if al == 0.0:
            out[i] = None if ag == 0.0 else 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ag / al)
    return out


def sma(closes: list[float], n: int) -> list[float | None]:
    return [None if i + 1 < n else sum(closes[i + 1 - n : i + 1]) / n for i in range(len(closes))]


def connors_target(
    bars: list[Bar], *, rsi_entry: float = 10.0, trend: int = 200, exit_n: int = 5
) -> dict[str, bool]:
    """State machine on closes: flat->long on RSI(2)<entry above SMA(trend); long->flat on close>SMA(exit)."""
    closes = [b.close for b in bars]
    rsi = wilder_rsi(closes, 2)
    slow = sma(closes, trend)
    fast = sma(closes, exit_n)
    out: dict[str, bool] = {}
    long = False
    for i, bar in enumerate(bars):
        if rsi[i] is None or slow[i] is None or fast[i] is None:
            continue
        if not long and bar.close > slow[i] and rsi[i] < rsi_entry:
            long = True
        elif long and bar.close > fast[i]:
            long = False
        out[bar.day] = long
    return out


# ───────────────────── Antonacci GEM (2013 / 2014) ───────────────────────


def gem_targets(
    bars_by_slot: dict[str, list[Bar]], lookback: int = 12
) -> dict[str, dict[str, bool]]:
    """Dual momentum on month-end closes: slots ``us``, ``non_us``, ``bond``, ``cash``.

    Holds ``us`` iff its 12-month return beats both ``non_us`` and ``cash``;
    else ``non_us`` iff it beats ``cash`` (ties go to ``non_us``); else ``bond``.
    A month is decided only when every slot has a bar in it.
    """
    month_end: dict[str, dict[str, Bar]] = {}
    for slot, bars in bars_by_slot.items():
        last: dict[str, Bar] = {}
        for b in bars:
            last[b.day[:7]] = b  # bars are chronological: the last one of a month wins
        month_end[slot] = last
    months = sorted(set.intersection(*[set(m) for m in month_end.values()]))
    calendar_months = sorted(set(month_end["us"]))
    out = {slot: {} for slot in ("us", "non_us", "bond")}
    for k, month in enumerate(months):
        if k < lookback:
            continue
        past = months[k - lookback]
        mom = {s: month_end[s][month].close / month_end[s][past].close - 1.0 for s in bars_by_slot}
        if mom["us"] > mom["non_us"] and mom["us"] > mom["cash"]:
            pick = "us"
        elif mom["non_us"] >= mom["us"] and mom["non_us"] > mom["cash"]:
            pick = "non_us"
        else:
            pick = "bond"
        for slot in out:
            # decision is dated on the slot's own last bar of that month
            out[slot][month_end[slot][month].day] = slot == pick
    del calendar_months
    return out
