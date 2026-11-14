"""Differential tests: backtest engines vs tiny independent reference simulators.

Three textbook strategies are run on the native engine (``NativeAdapter``) and
the vectorbt engine (``VectorbtAdapter``) and compared, trade by trade, with a
reference implementation written in plain Python in THIS module. Nothing from
``runner/`` is imported for the reference (only the engines under test are
imported to *run* them). Data access is ``pandas.read_parquet`` for the synthetic
scenarios and ``pandas.read_csv`` for the one committed real-data slice.

Strategies
----------
(a) SMA(fast)/SMA(slow) crossover, long only, "all-in" sizing
    (``tests/strategies/diff_sma_cross.py``: tie-exact integer comparisons, because float rolling
    means flip the cross bar on real quotes where fast == slow).
(b) Donchian breakout with an ATR stop, ATR target and fixed-risk sizing
    (``tests/strategies/diff_donchian.py``).
(c) RSI mean reversion with a time exit and a sizing-only percent stop
    (``tests/strategies/diff_rsi_time_exit.py``).

Conventions (``docs/cross-engine-backtest-contract.md``, ``docs/sizing-semantics.md``)
-------------------------------------------------------------------------------------
* A signal on bar N fills at the OPEN of bar N+1; a signal on the last bar never
  fills.
* Signal exits fill at the OPEN of the bar AFTER the signal (owner decision
  2026-11-13, superseding the old trigger-bar-close rule); a signal on the last
  bar cannot fill and the position is closed at the last bar's close
  (``end_of_data``) like any other open position.
* LONG stop triggers when ``low <= stop``, take profit when ``high >= tp``.
  Both fill at the LEVEL on an intrabar touch, but at the bar's OPEN when the bar
  opens beyond the level (gap, 2026-11-13; same for take-profit and for SHORT).
  A gap is the first event of the bar, so it also wins over a parked signal
  exit's reason and over the other level. A stop and a target touched by the same
  bar resolve stop-first (no intrabar data). The fill bar itself can be stopped
  out (A-H1) but is never a gap (the position was opened at that very open).
* The reference is LONG-only (a short leg needs a direction-aware guard here first).
* A protective/sizing stop on the wrong side of the fill price (``stop >= fill``
  for a LONG) skips the trade.
* Risk sizing: ``qty = equity*risk / |fill - stop|`` capped by the position
  notional ``equity*min(max_position_exposure_pct, max_leverage)``. Equity is
  the running net equity (initial + sum of net pnl). The native engine also
  caps ``risk`` at the risk-engine aggregate default (5%).
* Without an explicit stop both engines fall back to a 2%-of-entry stop.
* Costs: ``fee = (entry + exit) * qty * fee_rate + fee_flat`` per round trip.
  The engines have no separate slippage knob; slippage is expressed through
  ``fee_rate``.
* vectorbt does not enforce protective levels without intrabar data (contract
  section 1): the reference is run with ``protective=False`` for it, which makes
  the levels sizing-only and exits happen on the scheduled signal exit. Where
  both reference variants coincide (no level was touched) native and vectorbt
  must also equal each other and the test asserts that.
* vectorbt rounds prices to 6dp, quantity to 4dp and pnl/fees to 2dp in its
  output; its tolerances reflect that rounding and nothing else.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

STRATEGIES_DIR = Path(__file__).resolve().parent / "strategies"
SMA_PATH = str(STRATEGIES_DIR / "diff_sma_cross.py")
DONCHIAN_PATH = str(STRATEGIES_DIR / "diff_donchian.py")
RSI_PATH = str(STRATEGIES_DIR / "diff_rsi_time_exit.py")

CAPITAL = 100_000.0
#: The native risk engine's aggregate-exposure default also bounds one trade's risk.
NATIVE_RISK_CEILING = 0.05
FALLBACK_STOP_PCT = 0.02
NAN = float("nan")


# ───────────────────────────── reference: signals ──────────────────────────────


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def _window(series: list[float], end: int, length: int) -> list[float] | None:
    """series[end-length+1 .. end] or None when it is out of range / has a NaN."""
    start = end - length + 1
    if start < 0:
        return None
    window = series[start : end + 1]
    return window if all(_finite(v) for v in window) else None


def _mean(window: list[float] | None) -> float:
    return NAN if window is None else math.fsum(window) / len(window)


def ref_sma_signals(
    close: list[float], fast: int, slow: int, scale: float = 100_000
) -> tuple[list[bool], list[bool]]:
    """Exact: sign(mean_fast - mean_slow) == sign(sum_fast*slow - sum_slow*fast) (int ticks)."""
    ticks = [round(v * scale) if _finite(v) else None for v in close]

    def window_sum(i: int, length: int) -> int | None:
        w = ticks[i - length + 1 : i + 1] if i - length + 1 >= 0 else None
        return None if w is None or any(v is None for v in w) else sum(w)

    n = len(close)
    diff: list[int | None] = []
    for i in range(n):
        f, s = window_sum(i, fast), window_sum(i, slow)
        diff.append(None if f is None or s is None else f * slow - s * fast)
    entries = [False] * n
    exits = [False] * n
    for i in range(1, n):
        if diff[i] is None or diff[i - 1] is None:
            continue
        entries[i] = diff[i] > 0 and diff[i - 1] <= 0
        exits[i] = diff[i] < 0 and diff[i - 1] >= 0
    return entries, exits


@dataclass
class Signals:
    entries: list[bool]
    exits: list[bool]
    #: protective stop / target published on the entry-signal bar (None = none)
    stop: list[float | None]
    take_profit: list[float | None]
    #: sizing-only stop published on the entry-signal bar (None = none)
    sizing_stop: list[float | None]


def ref_donchian_signals(
    high: list[float],
    low: list[float],
    close: list[float],
    *,
    entry_len: int = 10,
    exit_len: int = 5,
    atr_len: int = 10,
    stop_atr: float = 2.0,
    tp_atr: float = 4.0,
) -> Signals:
    n = len(close)
    tr = [NAN] * n
    for i in range(n):
        if i == 0:
            tr[i] = high[0] - low[0] if _finite(high[0], low[0]) else NAN
        elif _finite(high[i], low[i], close[i - 1]):
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    sig = Signals([False] * n, [False] * n, [None] * n, [None] * n, [None] * n)
    in_position = False
    for i in range(n):
        atr = _mean(_window(tr, i, atr_len))
        upper_window = _window(high, i - 1, entry_len)
        lower_window = _window(low, i - 1, exit_len)
        if in_position:
            if lower_window is not None and _finite(close[i]) and close[i] < min(lower_window):
                sig.exits[i] = True
                in_position = False
        elif upper_window is not None and _finite(close[i], atr) and close[i] > max(upper_window):
            sig.entries[i] = True
            sig.stop[i] = close[i] - stop_atr * atr
            sig.take_profit[i] = close[i] + tp_atr * atr
            in_position = True
    return sig


def ref_rsi_time_signals(
    close: list[float],
    *,
    rsi_len: int = 5,
    oversold: float = 30.0,
    hold_bars: int = 4,
    stop_pct: float = 0.03,
) -> Signals:
    n = len(close)
    alpha = 1.0 / rsi_len
    avg_gain = avg_loss = NAN
    observations = 0
    rsi = [NAN] * n
    for i in range(1, n):
        change = close[i] - close[i - 1]
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if observations == 0:
            avg_gain, avg_loss = gain, loss
        else:
            avg_gain = (1 - alpha) * avg_gain + alpha * gain
            avg_loss = (1 - alpha) * avg_loss + alpha * loss
        observations += 1
        if observations >= rsi_len:
            rsi[i] = 100.0 if avg_loss == 0.0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    sig = Signals([False] * n, [False] * n, [None] * n, [None] * n, [None] * n)
    entry_bar: int | None = None
    for i in range(n):
        if entry_bar is not None:
            if i - entry_bar >= hold_bars:
                sig.exits[i] = True
                entry_bar = None
        elif _finite(rsi[i], close[i]) and rsi[i] < oversold:
            sig.entries[i] = True
            entry_bar = i
            if stop_pct > 0:
                sig.sizing_stop[i] = close[i] * (1.0 - stop_pct)
    return sig


# ───────────────────────────── reference: execution ────────────────────────────


@dataclass(frozen=True)
class RefConfig:
    capital: float = CAPITAL
    risk_pct: float = 0.05
    #: min(max_position_exposure_pct, max_leverage): the notional cap in equity multiples
    notional_cap: float = 1.0
    fee_rate: float = 0.0
    fee_flat: float = 0.0
    #: apply the native 5% risk ceiling (the vectorbt engine has none)
    native_risk_ceiling: bool = True
    #: honour protective stop / target levels as exits (False = vectorbt, no intrabar)
    protective: bool = True
    #: skip a trade whose sizing stop is on the wrong side of the fill (LONG: stop >= fill);
    #: False reproduces the documented vectorbt sizing-only defect
    skip_wrong_side_stop: bool = True
    #: size an explicit-stop-less trade on the fill price (contract section 4)
    #: instead of the signal-bar close
    fallback_on_fill: bool = True


@dataclass
class RefTrade:
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    fees: float
    net: float
    reason: str
    side: str = "LONG"


@dataclass
class _Open:
    entry_i: int
    entry_price: float
    qty: float
    stop: float | None
    take_profit: float | None


def ref_execute(frame: pd.DataFrame, sig: Signals, cfg: RefConfig) -> list[RefTrade]:
    ts = list(pd.to_datetime(frame["timestamp"], utc=True))
    o, h, lo, c = (frame[k].astype(float).tolist() for k in ("open", "high", "low", "close"))
    n = len(frame)
    equity = cfg.capital
    trades: list[RefTrade] = []
    position: _Open | None = None
    pending: int | None = None  # signal bar index of the order waiting to fill
    exit_pending = False  # a signal exit parked on an earlier bar: fills at this open

    def close_position(i: int, price: float, reason: str) -> None:
        nonlocal position, equity
        assert position is not None
        pnl = (price - position.entry_price) * position.qty
        fees = (position.entry_price + price) * position.qty * cfg.fee_rate + cfg.fee_flat
        equity += pnl - fees
        trades.append(
            RefTrade(
                ts[position.entry_i],
                ts[i],
                position.entry_price,
                price,
                position.qty,
                pnl,
                fees,
                pnl - fees,
                reason,
            )
        )
        position = None

    def protective_exit(i: int, *, gap_allowed: bool = True) -> bool:
        """Close at a touched level; True when it closed the position."""
        if position is None or not cfg.protective:
            return False
        stop_hit = position.stop is not None and lo[i] <= position.stop
        tp_hit = position.take_profit is not None and h[i] >= position.take_profit
        stop_gap = gap_allowed and position.stop is not None and o[i] <= position.stop
        tp_gap = gap_allowed and position.take_profit is not None and o[i] >= position.take_profit
        if stop_gap:  # opens beyond the stop: fills at the open
            close_position(i, o[i], "stop_loss")
        elif tp_gap:  # opens beyond the target: fills at the open
            close_position(i, o[i], "take_profit")
        elif stop_hit:  # stop-first, also when the target is touched by the same bar
            close_position(i, position.stop, "stop_loss")
        elif tp_hit:
            close_position(i, position.take_profit, "take_profit")
        else:
            return False
        return True

    def size(fill: float, stop: float | None, signal_close: float) -> float:
        risk = min(cfg.risk_pct, NATIVE_RISK_CEILING) if cfg.native_risk_ceiling else cfg.risk_pct
        # The documented native defect prices the WHOLE no-stop order (risk distance and
        # notional cap) on the signal-bar close; the contract prices it on the fill.
        basis = fill if (stop is not None or cfg.fallback_on_fill) else signal_close
        distance = abs(fill - stop) if stop is not None else basis * FALLBACK_STOP_PCT
        qty = equity * risk / distance
        return min(qty, equity * cfg.notional_cap / basis)

    for i in range(n):
        if not _finite(o[i], h[i], lo[i], c[i]):
            # An unpriced bar cannot be traded on (see test_nan_bar_*), and an order
            # whose fill bar it is expires: the contract fills at the NEXT bar's open,
            # never at a later one.
            if pending is not None and pending < i:
                pending = None
            continue
        # The open comes first: a gap exit, then the parked signal exit, and only
        # then the rest of the bar (intrabar touches).
        if position is not None and cfg.protective:
            gapped = (position.stop is not None and o[i] <= position.stop) or (
                position.take_profit is not None and o[i] >= position.take_profit
            )
            if gapped:
                protective_exit(i)
        if exit_pending and position is not None:
            close_position(i, o[i], "signal")
        exit_pending = False
        protective_exit(i, gap_allowed=False)
        if pending is not None and pending < i:
            s = pending
            pending = None
            stop = sig.stop[s]
            sizing_stop = sig.sizing_stop[s] if stop is None else stop
            fill = o[i]
            wrong_side = sizing_stop is not None and sizing_stop >= fill
            if sizing_stop is None or not wrong_side or not cfg.skip_wrong_side_stop:
                position = _Open(i, fill, size(fill, sizing_stop, c[s]), stop, sig.take_profit[s])
                protective_exit(i, gap_allowed=False)  # the fill bar can already be stopped out
        if sig.entries[i] and position is None and pending is None:
            pending = i
        if sig.exits[i]:
            exit_pending = True
    if position is not None:
        close_position(n - 1, c[n - 1], "end_of_data")
    return trades


# ──────────────────────────── engine plumbing & compare ────────────────────────


def _write(
    data_dir: Path, frame: pd.DataFrame, *, symbol="SYN", timeframe="1d", cls="equities"
) -> None:
    target = data_dir / "ohlcv" / "test_source" / cls / symbol
    target.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target / f"{timeframe}.parquet", index=False)


def _config(
    data_dir: Path,
    *,
    timeframe="1d",
    risk=0.05,
    position_pct=1.0,
    leverage=1.0,
    fee_rate=0.0,
    fee_flat=0.0,
    params=None,
    start="2000-01-01",
    end="2100-01-01",
) -> BacktestConfig:
    parameters = dict(params or {})
    if fee_rate or fee_flat:
        parameters["fee_rate"] = fee_rate
        parameters["fee_flat_per_trade"] = fee_flat
    else:
        parameters["fees"] = "zero"
    return BacktestConfig(
        strategy_name="differential",
        symbols=["SYN"],
        timeframe=timeframe,
        start_date=start,
        end_date=end,
        initial_capital=CAPITAL,
        risk_per_trade_pct=risk,
        max_position_exposure_pct=position_pct,
        max_leverage=leverage,
        modeling_resolution="main_timeframe",
        data_dir=str(data_dir),
        parameters=parameters,
    )


def _engine_trades(result) -> list[RefTrade]:
    out = []
    for t in sorted(result.trades, key=lambda t: pd.Timestamp(t.entry_time)):
        last = t.exits[-1]
        out.append(
            RefTrade(
                pd.Timestamp(t.entry_time).tz_convert("UTC"),
                pd.Timestamp(last.time).tz_convert("UTC"),
                float(t.entry_price),
                float(last.price),
                float(t.quantity),
                float(t.pnl),
                float(t.fees or 0.0),
                float(t.net_pnl),
                str(t.meta.get("exit_reason") or last.reason or ""),
                t.direction,
            )
        )
    return out


@dataclass(frozen=True)
class Tol:
    price: float
    qty_rel: float
    qty_abs: float
    money: float
    money_rel: float


#: Native keeps full float precision: only operation-order noise is tolerated.
NATIVE_TOL = Tol(price=1e-9, qty_rel=1e-12, qty_abs=1e-12, money=1e-9, money_rel=1e-12)
#: vectorbt output rounding: price 6dp, quantity 4dp, pnl/fees 2dp.
VBT_TOL = Tol(price=5.1e-7, qty_rel=0.0, qty_abs=5.1e-5, money=5.1e-3, money_rel=0.0)


def _close(a: float, b: float, *, abs_tol: float, rel_tol: float = 0.0) -> bool:
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


def assert_trades_match(
    engine: list[RefTrade], ref: list[RefTrade], tol: Tol, *, check_reason: bool, label: str
) -> None:
    def brief(trades):
        return [
            (str(t.entry_ts), str(t.exit_ts), t.entry_price, t.exit_price, t.qty, t.reason)
            for t in trades
        ]

    assert len(engine) == len(ref), (
        f"{label}: trade count {len(engine)} != reference {len(ref)}\n"
        f"engine   : {brief(engine)}\nreference: {brief(ref)}"
    )
    for k, (e, r) in enumerate(zip(engine, ref)):
        ctx = f"{label} trade #{k}\nengine   : {e}\nreference: {r}"
        assert e.side == r.side, ctx
        assert e.entry_ts == r.entry_ts, ctx
        assert e.exit_ts == r.exit_ts, ctx
        assert _close(e.entry_price, r.entry_price, abs_tol=tol.price), ctx
        assert _close(e.exit_price, r.exit_price, abs_tol=tol.price), ctx
        assert _close(e.qty, r.qty, abs_tol=tol.qty_abs, rel_tol=tol.qty_rel), ctx
        assert _close(e.pnl, r.pnl, abs_tol=tol.money, rel_tol=tol.money_rel), ctx
        assert _close(e.fees, r.fees, abs_tol=tol.money, rel_tol=tol.money_rel), ctx
        assert _close(e.net, r.net, abs_tol=tol.money, rel_tol=tol.money_rel), ctx
        if check_reason:
            assert e.reason == r.reason or ({e.reason, r.reason} <= {"take_profit", "tp"}), ctx


def assert_final_equity(result, ref: list[RefTrade], cfg: RefConfig, tol: Tol, label: str) -> None:
    expected = cfg.capital + sum(t.net for t in ref)
    final = result.equity_curve[-1].equity
    # the adapters publish the curve rounded to 2dp (vectorbt: each trade's pnl is also 2dp)
    slack = 0.005 + (0.0 if tol is NATIVE_TOL else 0.0051 * len(ref))
    assert abs(final - expected) <= slack, f"{label}: final equity {final} != reference {expected}"


# ─────────────────────────────── synthetic data ────────────────────────────────


def make_frame(closes: list[float], *, start="2024-01-01", freq="B", wick=0.25) -> pd.DataFrame:
    """OHLC from closes: open = previous close, wicks beyond body. Quarter ticks keep
    every price exactly representable so rolling sums have no rounding noise."""
    opens = [closes[0]] + closes[:-1]
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range(start, periods=len(closes), freq=freq, tz="UTC"),
            "open": opens,
            "high": [max(a, b) + wick for a, b in zip(opens, closes)],
            "low": [min(a, b) - wick for a, b in zip(opens, closes)],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )
    return frame


def walk(seed: int, n: int, start: float = 100.0, step: float = 1.5) -> list[float]:
    rng = random.Random(seed)
    price, out = start, []
    for _ in range(n):
        price = max(10.0, price + round(rng.uniform(-step, step) * 4) / 4)
        out.append(price)
    return out


def set_bar(frame: pd.DataFrame, i: int, **values: float) -> pd.DataFrame:
    frame = frame.copy()
    for key, value in values.items():
        frame.loc[i, key] = value
    return frame


def lists(frame: pd.DataFrame):
    return tuple(frame[k].astype(float).tolist() for k in ("open", "high", "low", "close"))


# ───────────────────────────── run + compare drivers ───────────────────────────


def run_native(tmp_path: Path, frame, strategy: str, params: dict, **cfg_kwargs):
    data_dir = tmp_path / "native"
    _write(
        data_dir,
        frame,
        timeframe=cfg_kwargs.get("timeframe", "1d"),
        cls=cfg_kwargs.pop("cls", "equities"),
    )
    return NativeAdapter().run(_config(data_dir, params=params, **cfg_kwargs), strategy)


def run_vbt(tmp_path: Path, frame, strategy: str, params: dict, **cfg_kwargs):
    data_dir = tmp_path / "vbt"
    _write(
        data_dir,
        frame,
        timeframe=cfg_kwargs.get("timeframe", "1d"),
        cls=cfg_kwargs.pop("cls", "equities"),
    )
    return VectorbtAdapter().run(_config(data_dir, params=params, **cfg_kwargs), strategy)


@dataclass
class Case:
    name: str
    frame: pd.DataFrame
    strategy: str
    params: dict
    sig: Signals
    cfg_kwargs: dict = field(default_factory=dict)
    #: exit reasons the scenario must produce in the reference run; this guards
    #: that the scenario really exercises the edge case it is named after
    expect_reasons: tuple[str, ...] = ()
    min_trades: int = 1

    def ref_cfg(self, **overrides) -> RefConfig:
        kw = self.cfg_kwargs
        base = RefConfig(
            risk_pct=kw.get("risk", 0.05),
            notional_cap=min(kw.get("position_pct", 1.0), kw.get("leverage", 1.0)),
            fee_rate=kw.get("fee_rate", 0.0),
            fee_flat=kw.get("fee_flat", 0.0),
        )
        return replace(base, **overrides)

    def reference(self, *, vectorbt: bool = False, **overrides) -> list[RefTrade]:
        cfg = self.ref_cfg(
            protective=not vectorbt, native_risk_ceiling=not vectorbt, **overrides
        )
        return ref_execute(self.frame, self.sig, cfg)

    def has_protective_exit(self) -> bool:
        return any(t.reason in {"stop_loss", "take_profit"} for t in self.reference())


def _guard_scenario(case: Case) -> None:
    ref = case.reference()
    assert len(ref) >= case.min_trades, f"{case.name}: scenario produced {len(ref)} trades"
    reasons = {t.reason for t in ref}
    for wanted in case.expect_reasons:
        assert wanted in reasons, (
            f"{case.name}: reference never exits via {wanted!r} ({sorted(reasons)})"
        )


def check_native(tmp_path: Path, case: Case) -> None:
    _guard_scenario(case)
    ref = case.reference()
    result = run_native(tmp_path, case.frame, case.strategy, case.params, **dict(case.cfg_kwargs))
    assert_trades_match(
        _engine_trades(result), ref, NATIVE_TOL, check_reason=True, label=f"native/{case.name}"
    )
    assert_final_equity(result, ref, case.ref_cfg(), NATIVE_TOL, f"native/{case.name}")


def check_vectorbt(tmp_path: Path, case: Case) -> None:
    _guard_scenario(case)
    ref = case.reference(vectorbt=True)
    result = run_vbt(tmp_path, case.frame, case.strategy, case.params, **dict(case.cfg_kwargs))
    assert_trades_match(
        _engine_trades(result), ref, VBT_TOL, check_reason=False, label=f"vectorbt/{case.name}"
    )
    assert_final_equity(result, ref, case.ref_cfg(), VBT_TOL, f"vectorbt/{case.name}")


def check_engines_agree(tmp_path: Path, case: Case) -> None:
    """Native == vectorbt where the contract says they must (fills + sizing, no protective exit)."""
    _guard_scenario(case)
    assert not case.has_protective_exit(), "only applicable without protective exits"
    native = run_native(tmp_path, case.frame, case.strategy, case.params, **dict(case.cfg_kwargs))
    vbt = run_vbt(tmp_path, case.frame, case.strategy, case.params, **dict(case.cfg_kwargs))
    assert_trades_match(
        _engine_trades(native),
        _engine_trades(vbt),
        VBT_TOL,
        check_reason=False,
        label=f"native-vs-vectorbt/{case.name}",
    )


# Native and vectorbt both size a no-explicit-stop order (2% fallback stop) on
# the FILL price (contract section 4); the gap scenarios below pin that.


def _make_params(
    scenarios: dict,
    xfail: dict[str, pytest.MarkDecorator] | None = None,
    *,
    agree: bool = False,
) -> list:
    """Parametrisation; ``xfail`` maps scenario -> strict-xfail mark for a known,
    documented engine defect (none are open at the moment)."""
    xfail = xfail or {}
    out = []
    for name in sorted(scenarios):
        if agree and scenarios[name]().has_protective_exit():
            continue  # documented: vectorbt has no protective exits without intrabar data
        marks = [xfail[name]] if name in xfail else []
        out.append(pytest.param(name, id=name, marks=marks))
    return out


# ─────────────────────────── scenario builders: strategy (a) ───────────────────


SMA_PARAMS = {"fast_length": 5, "slow_length": 20}


def sma_case(name: str, frame: pd.DataFrame, **cfg_kwargs) -> Case:
    c = lists(frame)[3]
    entries, exits = ref_sma_signals(c, 5, 20)
    sig = Signals(entries, exits, [None] * len(c), [None] * len(c), [None] * len(c))
    return Case(name, frame, SMA_PATH, SMA_PARAMS, sig, cfg_kwargs)


def _first(flags: list[bool], start: int = 0) -> int:
    return flags.index(True, start)


def _nan_bar(frame: pd.DataFrame, i: int) -> pd.DataFrame:
    return set_bar(frame, i, open=NAN, high=NAN, low=NAN, close=NAN)


def _retime_to_friday(frame: pd.DataFrame, signal_bar: int) -> pd.DataFrame:
    """Same prices, timestamps shifted so that ``signal_bar`` is a Friday."""
    start = pd.Timestamp("2024-01-01") + pd.offsets.BDay((4 - signal_bar) % 5)
    out = frame.copy()
    out["timestamp"] = pd.date_range(start, periods=len(frame), freq="B", tz="UTC")
    assert out.loc[signal_bar, "timestamp"].weekday() == 4
    assert out.loc[signal_bar + 1, "timestamp"].weekday() == 0
    return out


SMA_BASE = make_frame(walk(seed=3, n=160))
_sma_entries, _sma_exits = ref_sma_signals(lists(SMA_BASE)[3], 5, 20)
SMA_S = _first(_sma_entries, 25)  # first entry-signal bar after warm-up
SMA_X = _first(_sma_exits, SMA_S)


def _sma_gap_up():
    close_s = SMA_BASE.loc[SMA_S, "close"]
    return sma_case(
        "sma/gap_up_at_fill",
        set_bar(SMA_BASE, SMA_S + 1, open=close_s + 4.0, high=close_s + 4.5, close=close_s + 4.0),
    )


def _sma_weekend_gap():
    frame = _retime_to_friday(SMA_BASE, SMA_S)
    close_s = frame.loc[SMA_S, "close"]
    return sma_case(
        "sma/weekend_gap_down",
        set_bar(frame, SMA_S + 1, open=close_s - 3.0, low=close_s - 3.25, close=close_s - 3.0),
    )


def _sma_zero_volume():
    frame = SMA_BASE.copy()
    frame.loc[[SMA_S + 1, SMA_X], "volume"] = 0.0
    return sma_case("sma/zero_volume_fill_and_exit_bars", frame)


def _sma_nan_flat():
    return sma_case("sma/nan_bar_while_flat", _nan_bar(SMA_BASE, SMA_S - 3))


def _sma_nan_in_position():
    return sma_case("sma/nan_bar_while_long", _nan_bar(SMA_BASE, SMA_S + 2))


def _sma_last_bar():
    c = sma_case("sma/entry_signal_on_last_bar", SMA_BASE.iloc[: SMA_S + 1].reset_index(drop=True))
    c.min_trades = 0
    return c


SMA_SCENARIOS = {
    "base": lambda: sma_case("sma/base", SMA_BASE),
    "costs": lambda: sma_case("sma/costs", SMA_BASE, fee_rate=0.0007, fee_flat=1.5),
    "last_bar_signal": _sma_last_bar,
    "fills_on_last_bar": lambda: sma_case(
        "sma/fills_on_last_bar", SMA_BASE.iloc[: SMA_S + 2].reset_index(drop=True)
    ),
    "gap_up_at_fill": _sma_gap_up,
    "weekend_gap_down": _sma_weekend_gap,
    "zero_volume": _sma_zero_volume,
    "nan_bar_flat": _sma_nan_flat,
    "nan_bar_in_position": _sma_nan_in_position,
}


@pytest.mark.parametrize("scenario", _make_params(SMA_SCENARIOS))
def test_sma_native_matches_reference(tmp_path, scenario):
    check_native(tmp_path, SMA_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(SMA_SCENARIOS))
def test_sma_vectorbt_matches_reference(tmp_path, scenario):
    check_vectorbt(tmp_path, SMA_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(SMA_SCENARIOS, agree=True))
def test_sma_native_and_vectorbt_agree(tmp_path, scenario):
    check_engines_agree(tmp_path, SMA_SCENARIOS[scenario]())


# ─────────────────────────── scenario builders: strategy (b) ───────────────────

DON_PARAMS = {"entry_len": 10, "exit_len": 5, "atr_len": 10, "stop_atr": 2.0, "tp_atr": 4.0}
#: 5% risk at ~2 ATR stops, position cap 50% of equity: both risk-limited and cap-limited trades
DON_CFG = {"risk": 0.01, "position_pct": 0.5, "leverage": 1.0}


def don_signals(frame: pd.DataFrame) -> Signals:
    o, h, lo, c = lists(frame)
    return ref_donchian_signals(h, lo, c)


def don_case(name: str, frame: pd.DataFrame, expect=(), min_trades=1, **cfg) -> Case:
    return Case(
        name,
        frame,
        DONCHIAN_PATH,
        DON_PARAMS,
        don_signals(frame),
        {**DON_CFG, **cfg},
        tuple(expect),
        min_trades,
    )


DON_BASE = make_frame(walk(seed=11, n=220, step=2.0))
_don_sig = don_signals(DON_BASE)
_don_entries = [i for i, e in enumerate(_don_sig.entries) if e]


def _quiet_fill(frame: pd.DataFrame, s: int) -> pd.DataFrame:
    """Make the fill bar (s+1) and the bar after it benign: no stop/target touch."""
    close_s = frame.loc[s, "close"]
    for j in (s + 1, s + 2):
        frame = set_bar(
            frame, j, open=close_s, high=close_s + 0.5, low=close_s - 0.25, close=close_s + 0.25
        )
    return frame


def _don_edge(name, mutate, *, expect, s_index=0, min_trades=1, **cfg):
    """Pick the s_index-th entry signal of the base walk, calm its next bars, then mutate."""
    s = _don_entries[s_index]
    frame = _quiet_fill(DON_BASE, s)
    sig = don_signals(frame)  # signal bars before s are unchanged; recompute for later ones
    assert sig.entries[s]
    stop, tp = sig.stop[s], sig.take_profit[s]
    frame = mutate(frame, s, stop, tp)
    return don_case(name, frame, expect=expect, min_trades=min_trades, **cfg)


def _bar(frame, i, o, h, lo, c):
    return set_bar(frame, i, open=o, high=h, low=lo, close=c)


DON_SCENARIOS = {
    "base": lambda: don_case("don/base", DON_BASE, min_trades=3),
    "costs": lambda: don_case("don/costs", DON_BASE, min_trades=3, fee_rate=0.0007, fee_flat=1.5),
    "gap_through_stop": lambda: _don_edge(
        "don/gap_through_stop",
        lambda f, s, S, T: _bar(f, s + 2, S - 3.0, S - 2.75, S - 3.25, S - 3.0),
        expect=("stop_loss",),
    ),
    "stop_and_target_same_bar": lambda: _don_edge(
        "don/stop_and_target_same_bar",
        lambda f, s, S, T: _bar(f, s + 2, f.loc[s, "close"], T + 1.0, S - 1.0, f.loc[s, "close"]),
        expect=("stop_loss",),
    ),
    "take_profit": lambda: _don_edge(
        "don/take_profit",
        lambda f, s, S, T: _bar(f, s + 2, f.loc[s, "close"], T + 0.5, f.loc[s, "close"] - 0.25, T),
        expect=("take_profit",),
    ),
    "fill_bar_stopped_out": lambda: _don_edge(
        "don/fill_bar_stopped_out",
        lambda f, s, S, T: _bar(
            f, s + 1, f.loc[s, "close"], f.loc[s, "close"] + 0.25, S - 0.5, f.loc[s, "close"] - 0.25
        ),
        expect=("stop_loss",),
    ),
    "gap_below_stop_at_fill_skips_trade": lambda: _don_edge(
        "don/gap_below_stop_at_fill",
        lambda f, s, S, T: _bar(f, s + 1, S - 1.0, S - 0.75, S - 1.25, S - 1.0),
        expect=(),
        min_trades=2,
    ),
    "weekend_gap_up_fill": lambda: _don_weekend(),
    "zero_volume": lambda: _don_zero_volume(),
    "nan_bar_while_long": lambda: _don_edge(
        "don/nan_bar_while_long", lambda f, s, S, T: _nan_bar(f, s + 2), expect=()
    ),
    "nan_bar_flat": lambda: don_case(
        "don/nan_bar_flat", _nan_bar(DON_BASE, _don_entries[0] - 4), min_trades=2
    ),
    "entry_signal_on_last_bar": lambda: don_case(
        "don/entry_signal_on_last_bar",
        DON_BASE.iloc[: _don_entries[1] + 1].reset_index(drop=True),
        min_trades=1,
    ),
    "fills_on_last_bar": lambda: don_case(
        "don/fills_on_last_bar",
        DON_BASE.iloc[: _don_entries[1] + 2].reset_index(drop=True),
        min_trades=2,
        expect=("end_of_data",),
    ),
}


def _don_weekend():
    s = _don_entries[0]
    frame = _retime_to_friday(_quiet_fill(DON_BASE, s), s)
    # Monday opens 1.5 above the Friday close (inside the stop/target range)
    close_s = frame.loc[s, "close"]
    frame = _bar(frame, s + 1, close_s + 1.5, close_s + 2.0, close_s + 1.25, close_s + 1.75)
    return don_case("don/weekend_gap_up_fill", frame, min_trades=2)


def _don_zero_volume():
    s = _don_entries[0]
    frame = _quiet_fill(DON_BASE, s)
    frame.loc[[s + 1, s + 2], "volume"] = 0.0
    return don_case("don/zero_volume", frame, min_trades=2)


# NaN edge cases: a bar whose OHLC is entirely NaN is untradable. The reference
# skips it (no fill, no protective check) and rolling windows that contain it are
# undefined, so no signal fires from them.
@pytest.mark.parametrize("scenario", _make_params(DON_SCENARIOS))
def test_donchian_native_matches_reference(tmp_path, scenario):
    check_native(tmp_path, DON_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(DON_SCENARIOS))
def test_donchian_vectorbt_matches_reference(tmp_path, scenario):
    check_vectorbt(tmp_path, DON_SCENARIOS[scenario]())


# ─────────────────────────── scenario builders: strategy (c) ───────────────────

RSI_PARAMS = {"rsi_len": 5, "oversold": 30.0, "hold_bars": 4, "stop_pct": 0.03}
RSI_CFG = {"risk": 0.01, "position_pct": 0.5, "leverage": 1.0}


def rsi_case(name, frame, *, stop_pct=0.03, min_trades=1, expect=(), **cfg) -> Case:
    params = {**RSI_PARAMS, "stop_pct": stop_pct}
    sig = ref_rsi_time_signals(lists(frame)[3], stop_pct=stop_pct)
    return Case(name, frame, RSI_PATH, params, sig, {**RSI_CFG, **cfg}, tuple(expect), min_trades)


RSI_BASE = make_frame(walk(seed=5, n=140, step=2.5))
_rsi_sig = ref_rsi_time_signals(lists(RSI_BASE)[3])
_rsi_entries = [i for i, e in enumerate(_rsi_sig.entries) if e]
_rsi_exits = [i for i, e in enumerate(_rsi_sig.exits) if e]


def _rsi_edge(name, mutate, *, stop_pct=0.03, s_index=0, **kw):
    s = _rsi_entries[s_index]
    frame = mutate(RSI_BASE, s)
    return rsi_case(name, frame, stop_pct=stop_pct, **kw)


def _rsi_gap_fill(stop_pct):
    def mutate(f, s):
        close_s = f.loc[s, "close"]
        return _bar(f, s + 1, close_s + 4.0, close_s + 4.25, close_s + 3.75, close_s + 4.0)

    return mutate


RSI_SCENARIOS = {
    "base": lambda: rsi_case("rsi/base", RSI_BASE, min_trades=3, expect=("signal",)),
    "costs": lambda: rsi_case("rsi/costs", RSI_BASE, min_trades=3, fee_rate=0.0007, fee_flat=1.5),
    "gap_up_at_fill": lambda: _rsi_edge("rsi/gap_up_at_fill", _rsi_gap_fill(0.03)),
    "gap_below_sizing_stop_skips_trade": lambda: _rsi_edge(
        "rsi/gap_below_sizing_stop_at_fill",
        lambda f, s: _bar(
            f,
            s + 1,
            f.loc[s, "close"] * 0.9,
            f.loc[s, "close"] * 0.9 + 0.25,
            f.loc[s, "close"] * 0.9 - 0.25,
            f.loc[s, "close"] * 0.9,
        ),
        min_trades=1,
    ),
    "crash_through_sizing_stop_does_not_exit": lambda: _rsi_edge(
        "rsi/crash_through_sizing_stop",
        lambda f, s: _bar(
            f,
            s + 2,
            f.loc[s, "close"],
            f.loc[s, "close"] + 0.25,
            f.loc[s, "close"] * 0.8,
            f.loc[s, "close"] - 0.5,
        ),
    ),
    "time_exit_cut_by_end_of_data": lambda: _rsi_truncated(
        "rsi/end_of_data_before_time_exit", _rsi_entries[1] + 3, ("end_of_data",)
    ),
    "entry_signal_on_last_bar": lambda: _rsi_truncated(
        "rsi/entry_signal_on_last_bar", _rsi_entries[1] + 1, (), min_trades=1
    ),
    "weekend_gap_fill": lambda: _rsi_weekend(),
    "zero_volume": lambda: _rsi_edge("rsi/zero_volume", lambda f, s: _zero_vol(f, [s + 1, s + 4])),
}


def _zero_vol(frame, rows):
    frame = frame.copy()
    frame.loc[rows, "volume"] = 0.0
    return frame


def _rsi_truncated(name, length, expect, min_trades=1):
    return rsi_case(
        name, RSI_BASE.iloc[:length].reset_index(drop=True), expect=expect, min_trades=min_trades
    )


def _rsi_weekend():
    s = _rsi_entries[0]
    frame = _retime_to_friday(RSI_BASE, s)
    close_s = frame.loc[s, "close"]
    frame = _bar(frame, s + 1, close_s - 1.0, close_s - 0.75, close_s - 1.25, close_s - 1.0)
    return rsi_case("rsi/weekend_gap_down_fill", frame)


RSI_FALLBACK_SCENARIOS = {
    # no stop published => both engines fall back to a 2% stop
    "fallback_stop_no_gap": lambda: rsi_case(
        "rsi/fallback_stop", RSI_BASE, stop_pct=0.0, min_trades=3
    ),
    "fallback_stop_gap_up_at_fill": lambda: _rsi_edge(
        "rsi/fallback_stop_gap_up", _rsi_gap_fill(0.0), stop_pct=0.0
    ),
}
# A LONG sizing-only stop left at/above the fill by a gap down has no risk
# distance: both engines skip the trade (native A-C2 guard, vectorbt inverted
# sizing-stop guard) -- pinned by ``gap_below_sizing_stop_skips_trade``.


@pytest.mark.parametrize("scenario", _make_params(RSI_SCENARIOS))
def test_rsi_time_exit_native_matches_reference(tmp_path, scenario):
    check_native(tmp_path, RSI_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(RSI_SCENARIOS))
def test_rsi_time_exit_vectorbt_matches_reference(tmp_path, scenario):
    check_vectorbt(tmp_path, RSI_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(RSI_SCENARIOS, agree=True))
def test_rsi_time_exit_native_and_vectorbt_agree(tmp_path, scenario):
    check_engines_agree(tmp_path, RSI_SCENARIOS[scenario]())


@pytest.mark.parametrize(
    "scenario",
    _make_params(RSI_FALLBACK_SCENARIOS),
)
def test_rsi_fallback_stop_native_matches_reference(tmp_path, scenario):
    check_native(tmp_path, RSI_FALLBACK_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(RSI_FALLBACK_SCENARIOS))
def test_rsi_fallback_stop_vectorbt_matches_reference(tmp_path, scenario):
    check_vectorbt(tmp_path, RSI_FALLBACK_SCENARIOS[scenario]())


# ───────────────── Donchian with levels that are never touched ─────────────────
# Wide stop/target keep every exit a scheduled channel exit, so the contract's
# "fills + sizing agree" clause applies and native == vectorbt is asserted too.

WIDE_PARAMS = {**DON_PARAMS, "stop_atr": 15.0, "tp_atr": 60.0}


def _wide_case(name, **cfg) -> Case:
    frame = make_frame(walk(seed=21, n=200, step=1.0))
    o, h, lo, c = lists(frame)
    sig = ref_donchian_signals(h, lo, c, stop_atr=15.0, tp_atr=60.0)
    return Case(name, frame, DONCHIAN_PATH, WIDE_PARAMS, sig, cfg, (), 2)


WIDE_SCENARIOS = {
    "risk_limited": lambda: _wide_case(
        "don/wide/risk_limited", risk=0.01, position_pct=1.0, leverage=1.0
    ),
    "cap_limited": lambda: _wide_case(
        "don/wide/cap_limited", risk=0.05, position_pct=0.2, leverage=1.0
    ),
    "leverage_limited": lambda: _wide_case(
        "don/wide/leverage_limited", risk=0.05, position_pct=1.0, leverage=0.3, fee_rate=0.001
    ),
}


@pytest.mark.parametrize("scenario", _make_params(WIDE_SCENARIOS))
def test_donchian_wide_levels_native_matches_reference(tmp_path, scenario):
    check_native(tmp_path, WIDE_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(WIDE_SCENARIOS))
def test_donchian_wide_levels_vectorbt_matches_reference(tmp_path, scenario):
    check_vectorbt(tmp_path, WIDE_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(WIDE_SCENARIOS, agree=True))
def test_donchian_wide_levels_native_and_vectorbt_agree(tmp_path, scenario):
    check_engines_agree(tmp_path, WIDE_SCENARIOS[scenario]())


# ───────────────────────────── NaN on the fill bar ─────────────────────────────
# Contract section 1: a fill on an unpriced (all-NaN) bar fails closed. Both
# engines raise; this differential check only demands "loud or finite" (silently
# producing NaN trades/equity would poison every metric). The exact native
# error is pinned in tests/test_native_engine_execution.py.


def _assert_finite_or_loud(run) -> None:
    try:
        result = run()
    except (ValueError, RuntimeError) as exc:
        # Failing closed is acceptable, but only with the engines' documented refusal of an
        # unpriced fill; any other failure (path drift, refactor, dependency) must surface.
        # native: "... fill price nan is not finite and > 0 (unpriced bar)";
        # vectorbt: "entry_price must be finite and > 0".
        message = str(exc)
        assert "must be finite" in message or "(unpriced bar)" in message, (
            f"not the NaN-fill refusal: {exc!r}"
        )
        return
    for t in result.trades:
        values = [t.entry_price, t.quantity, t.pnl, t.net_pnl, *(e.price for e in t.exits)]
        assert all(math.isfinite(float(v)) for v in values), f"non-finite trade: {t}"
    assert all(math.isfinite(p.equity) for p in result.equity_curve), "non-finite equity curve"


def _nan_fill_sma():
    return sma_case("sma/nan_fill_bar", _nan_bar(SMA_BASE, SMA_S + 1))


def _nan_fill_don():
    frame = _nan_bar(_quiet_fill(DON_BASE, _don_entries[0]), _don_entries[0] + 1)
    return don_case("don/nan_fill_bar", frame, min_trades=0)


@pytest.mark.parametrize("engine", ["native", "vectorbt"])
@pytest.mark.parametrize(
    "frame_case",
    [
        pytest.param(lambda: sma_case("sma/nan_control", SMA_BASE), id="sma"),
        pytest.param(
            lambda: don_case(
                "don/nan_control", _quiet_fill(DON_BASE, _don_entries[0]), min_trades=0
            ),
            id="donchian",
        ),
    ],
)
def test_nan_fill_control_runs_to_completion(tmp_path, engine, frame_case):
    """Control for the NaN-fill test: the same scenario WITHOUT the unpriced bar must run
    to completion, so a refusal in the mutated run is attributable to the NaN bar."""
    case = frame_case()
    runner = run_native if engine == "native" else run_vbt
    result = runner(tmp_path, case.frame, case.strategy, case.params, **dict(case.cfg_kwargs))
    assert all(math.isfinite(p.equity) for p in result.equity_curve)


@pytest.mark.parametrize(
    "engine",
    ["native", pytest.param("vectorbt", id="vectorbt")],
)
@pytest.mark.parametrize(
    "builder",
    [
        pytest.param(_nan_fill_sma, id="sma_fallback_stop"),
        pytest.param(_nan_fill_don, id="donchian_explicit_stop"),
    ],
)
def test_nan_fill_bar_never_yields_non_finite_results(tmp_path, engine, builder):
    case = builder()
    runner = run_native if engine == "native" else run_vbt
    _assert_finite_or_loud(
        lambda: runner(tmp_path, case.frame, case.strategy, case.params, **dict(case.cfg_kwargs))
    )


# ───────────────────────────── one short real-data slice ───────────────────────
# 600 EURUSD 15m bars (Dukascopy bid, resampled from the local 1m store; the
# fixture is committed so CI runs it): real gaps, a weekend, irregular ranges.

REAL_SLICE = Path(__file__).resolve().parent / "fixtures" / "differential" / "eurusd_15m_slice.csv"
REAL_CFG = {"timeframe": "15m", "cls": "forex", "position_pct": 0.5, "leverage": 1.0}


def _real_frame() -> pd.DataFrame:
    frame = pd.read_csv(REAL_SLICE)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def _real_sma(**cfg) -> Case:
    frame = _real_frame()
    case = sma_case("real/sma", frame, **{**REAL_CFG, "risk": 0.05, "position_pct": 1.0}, **cfg)
    case.min_trades = 3
    return case


def _real_don(**cfg) -> Case:
    frame = _real_frame()
    o, h, lo, c = lists(frame)
    params = {
        **DON_PARAMS,
        "entry_len": 20,
        "exit_len": 10,
        "atr_len": 14,
        "stop_atr": 2.0,
        "tp_atr": 3.0,
    }
    sig = ref_donchian_signals(
        h, lo, c, entry_len=20, exit_len=10, atr_len=14, stop_atr=2.0, tp_atr=3.0
    )
    return Case(
        "real/donchian", frame, DONCHIAN_PATH, params, sig, {**REAL_CFG, "risk": 0.01, **cfg}, (), 3
    )


def _real_rsi(**cfg) -> Case:
    frame = _real_frame()
    params = {**RSI_PARAMS, "rsi_len": 7, "oversold": 30.0, "hold_bars": 6, "stop_pct": 0.002}
    sig = ref_rsi_time_signals(
        lists(frame)[3], rsi_len=7, oversold=30.0, hold_bars=6, stop_pct=0.002
    )
    return Case(
        "real/rsi_time_exit", frame, RSI_PATH, params, sig, {**REAL_CFG, "risk": 0.01, **cfg}, (), 3
    )


REAL_SCENARIOS = {
    "sma_zero_cost": lambda: _real_sma(),
    "sma_with_costs": lambda: _real_sma(fee_rate=0.00002, fee_flat=0.5),
    "donchian_zero_cost": lambda: _real_don(),
    "donchian_with_costs": lambda: _real_don(fee_rate=0.00002, fee_flat=0.5),
    "rsi_time_exit_zero_cost": lambda: _real_rsi(),
    "rsi_time_exit_with_costs": lambda: _real_rsi(fee_rate=0.00002, fee_flat=0.5),
}


@pytest.mark.parametrize("scenario", _make_params(REAL_SCENARIOS))
def test_real_slice_native_matches_reference(tmp_path, scenario):
    check_native(tmp_path, REAL_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(REAL_SCENARIOS))
def test_real_slice_vectorbt_matches_reference(tmp_path, scenario):
    check_vectorbt(tmp_path, REAL_SCENARIOS[scenario]())


@pytest.mark.parametrize("scenario", _make_params(REAL_SCENARIOS, agree=True))
def test_real_slice_native_and_vectorbt_agree(tmp_path, scenario):
    check_engines_agree(tmp_path, REAL_SCENARIOS[scenario]())


# ───────────────────── reference / strategy NaN-bar handling (cubic #189) ──────────


def test_rsi_strategy_never_signals_on_an_unpriced_close():
    import importlib.util

    spec = importlib.util.spec_from_file_location("diff_rsi_time_exit", RSI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Falling prices keep rsi below 30. The first entry is on bar 5 and its time exit on
    # bar 9; bar 10 (flat again, rsi still < 30 because the average carries across the
    # NaN) is unpriced and must not re-enter.
    close = [100.0 - k for k in range(14)]
    close[10] = NAN
    frame = pd.DataFrame({"close": close})

    entries, _exits, indicators, _ = module.generate_signals(frame, {"rsi_len": 5})

    assert entries.iloc[5], "control: the decline signals on a priced bar"
    assert not entries.iloc[10], "an unpriced bar must not emit an entry"
    assert indicators["sizing_sl_price"][entries].notna().all()


def test_reference_expires_an_order_whose_fill_bar_is_unpriced():
    n = 6
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC"),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
        }
    )
    frame.loc[2, ["open", "high", "low", "close"]] = NAN
    sig = Signals(
        [i == 1 for i in range(n)], [False] * n, [None] * n, [None] * n, [None] * n
    )

    trades = ref_execute(frame, sig, RefConfig())

    assert trades == [], "an order must not slip to the bar after an unpriced fill bar"
