"""
Strategy: Antonacci Global Equity Momentum (dual momentum)

Faithful port of the "Global Equity Momentum" (GEM) allocation from Gary
Antonacci, "Dual Momentum Investing" (Oxford University Press, 2014) and
the equities dual-momentum module of Gary Antonacci, "Risk Premia
Harvesting Through Dual Momentum" (Journal of Management &
Entrepreneurship 2017, 2(1), 27-55; SSRN 2042750; working-paper version of
January 2013):

    At each month end, look at the trailing 12-month returns of
      us     = US equities            (the book: SPY)
      non_us = non-US equities         (the book: VEU; EFA in earlier notes)
      bond   = US aggregate bonds      (the book: AGG)
      cash   = the cash/T-bill hurdle  (the module's alternative asset)

    RELATIVE leg  : if us beats non_us over 12 months -> hold us
                    else                              -> hold non_us
    ABSOLUTE leg  : the equity leg only survives if it beats cash;
                    otherwise fall back to bonds.
    Result        : exactly one slot is held:
                        us     if us > non_us and us > cash
                        non_us if non_us >= us and non_us > cash
                        bond   otherwise

Source fidelity
---------------
- 12-month lookback evaluated on MONTH-END closes ("For consistency
  reasons, we adjust all our positions monthly without skipping a month",
  Antonacci 2013 §2). The most recent month is NOT skipped.
- Relative + absolute momentum exactly as above; one slot at a time, 100%
  in that slot.
- Monthly rebalance: the decision is taken on the month-end bar. This
  platform fills the entry on the NEXT bar's open and the signal exit at
  the decision bar's CLOSE (``signal_exit_fill="close"``).
- No stop-loss, no take-profit, no leverage; the module holds one asset.

Multi-symbol limitation and how this port works around it
--------------------------------------------------------
The platform calls ``generate_signals(df, params)`` once per symbol with
ONE symbol's OHLCV frame, so a cross-asset decision cannot rank symbols
inside a single call. The rule therefore lives in pure pandas functions
that work on any aligned frame set:

- :func:`gem_allocation`  — the pure allocation rule over a slot x month
  matrix of month-end closes (single source of truth for the rule);
- :func:`attach_peer_closes` — a MECHANICAL helper that copies the other
  slots' closes into a symbol's frame as ``peer_close_<SYMBOL>`` columns
  and names the frame's own slot in ``slot`` (no trading logic);
- :func:`generate_signals` — the platform entry point: it reads the peer
  columns, runs the same allocation on that frame and emits the long/flat
  signals for THE SLOT the frame carries.

Run the platform once per slot on peer-attached frames, or once with all
slots in ``symbols``; the adapters then replay the three mutually
exclusive slots onto one shared portfolio equity (contract §4, D5/A-M4).

Scope notes (documented, NOT implemented)
-----------------------------------------
- The book's full GEM adds defensive trend/bond legs and discusses
  volatility targeting and CRDB/TAA variants; this port implements the
  published "equities momentum" slot decision only.
- The paper's series are total-return indices (MSCI US, EAFE+/ACWI ex US,
  Barclays Aggregate). Run on a total-return dataset to compare with the
  published numbers; ``tags.data_basis`` documents that requirement.
- The paper reports no transaction costs ("There have been no deductions
  for transaction costs"); this port leaves costs to the engine
  configuration and the parity report separates the gross run.
"""

STRATEGY_META = {
    "name": "Antonacci Global Equity Momentum (Dual Momentum)",
    "version": "1.0.0",
    "source_url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2042750",
    "notes": (
        "Antonacci dual momentum (GEM): at each month end rank the trailing "
        "12-month returns of US equities (SPY), non-US equities (VEU/EFA), "
        "bonds (AGG) and the cash hurdle; hold the US slot if it beats both "
        "non-US and cash, else the non-US slot if it beats cash, else bonds. "
        "One slot at a time, monthly rebalance, no costs in the source. "
        "Cross-asset decision lives in the pure functions gem_allocation / "
        "attach_peer_closes because the platform hands generate_signals one "
        "symbol's frame per call; peer closes are attached as "
        "peer_close_<SYMBOL> columns and the frame's slot comes from a 'slot' "
        "column. Requires a total-return (dividend adjusted) dataset to match "
        "the published numbers."
    ),
    "parameters": {
        "us_symbol": "SPY",
        "non_us_symbol": "VEU",
        "bond_symbol": "AGG",
        "cash_symbol": "BIL",
        "lookback_months": 12,
    },
    "tags": {
        "type": ["momentum", "dual-momentum", "rotation"],
        "source": "antonacci-dual-momentum",
        "link": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2042750",
        "asset_class": ["multi-asset", "etf"],
        "complexity": "medium",
        "data_basis": "total_return",
        "rebalance": "monthly",
    },
}

import numpy as np
import pandas as pd

#: Slot name -> role, in the order the rule is applied.
SLOT_ORDER = ("us", "non_us", "bond", "cash")

#: Column prefix used by :func:`attach_peer_closes` for a peer slot's closes.
PEER_CLOSE_PREFIX = "peer_close_"

#: Column naming the frame's own slot (written by :func:`attach_peer_closes`).
SLOT_COLUMN = "slot"


def slot_symbols(params: dict) -> dict[str, str]:
    """Return ``{slot: symbol}`` from the strategy parameters."""
    params = params or {}
    return {
        "us": str(params.get("us_symbol", "SPY")),
        "non_us": str(params.get("non_us_symbol", "VEU")),
        "bond": str(params.get("bond_symbol", "AGG")),
        "cash": str(params.get("cash_symbol", "BIL")),
    }


def peer_close_columns(params: dict) -> dict[str, str]:
    """Return ``{slot: peer column name}`` for every configured slot.

    Column names are LOWER-CASE: the engines lower-case every data column on
    load (``native_adapter._load_ohlcv``), so a mixed-case ``peer_close_SPY``
    would arrive as ``peer_close_spy`` and never be found.
    """
    return {slot: f"{PEER_CLOSE_PREFIX}{symbol.lower()}" for slot, symbol in slot_symbols(params).items()}


def _month_keys(timestamps: pd.Series) -> pd.PeriodIndex:
    """Calendar-month keys for a timestamp series (timezone dropped)."""
    parsed = pd.to_datetime(timestamps, utc=True, errors="coerce")
    return pd.PeriodIndex(parsed.dt.tz_localize(None), freq="M")


def month_end_flags(timestamps: pd.Series) -> pd.Series:
    """True on the last daily bar of each calendar month."""
    if timestamps is None or len(timestamps) == 0:
        return pd.Series(False, index=pd.RangeIndex(0), dtype=bool)
    months = _month_keys(timestamps)
    return pd.Series(months, index=timestamps.index).ne(
        pd.Series(months, index=timestamps.index).shift(-1)
    )


def _periods(timestamps: pd.Series) -> pd.PeriodIndex:
    """Backwards-compatible alias of :func:`_month_keys`."""
    return _month_keys(timestamps)


def monthly_slot_closes(closes_by_slot: dict[str, pd.Series], timestamps: pd.Series) -> pd.DataFrame:
    """Build the slot x month matrix of month-end closes.

    One row per calendar month in which EVERY configured slot has a bar; a
    month where any slot is missing is dropped instead of being decided on
    stale data. Indexed by month period, which is the frequency the
    allocation rule works on.
    """
    mask = month_end_flags(timestamps).to_numpy()
    if mask.size == 0:
        return pd.DataFrame(columns=list(SLOT_ORDER), index=pd.PeriodIndex([], freq="M"))
    index = _periods(timestamps)[mask]
    columns = {}
    for slot in SLOT_ORDER:
        close = closes_by_slot[slot].astype(float).reset_index(drop=True)
        columns[slot] = pd.Series(close.to_numpy()[mask], index=index, dtype=float)
    table = pd.DataFrame(columns)
    return table.dropna(how="any")


def gem_allocation(monthly_closes: pd.DataFrame, params: dict) -> pd.Series:
    """Apply the dual-momentum rule to the month-end close matrix.

    ``monthly_closes`` carries one column per slot (``us``/``non_us``/
    ``bond``/``cash``), indexed by month. The returned Series names the
    slot to hold for the FOLLOWING month at each decision month (the
    decision is taken on that month-end's close). Months without a full
    lookback return no allocation (NaN) and are skipped by callers.
    """
    lookback = int((params or {}).get("lookback_months", 12))
    monthly = monthly_closes.sort_index()
    momentum = monthly / monthly.shift(lookback) - 1.0

    us = momentum["us"]
    non_us = momentum["non_us"]
    cash = momentum["cash"]

    hold_us = (us > non_us) & (us > cash)
    hold_non_us = (~hold_us) & (non_us >= us) & (non_us > cash)
    allocation = pd.Series(np.where(hold_non_us, "non_us", "bond"), index=monthly.index, dtype="object")
    allocation = allocation.where(~hold_us, "us")
    allocation = allocation.where(us.notna() & non_us.notna() & cash.notna())
    return allocation


def frame_slot_closes(frame: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Slot -> close Series, both aligned to one peer-attached frame's bars.

    The frame's own close is used for its own slot, the ``peer_close_*``
    columns for the others. Raises when a peer column is missing rather
    than silently deciding the allocation on a partial universe.
    """
    params = params or {}
    peer_columns = peer_close_columns(params)
    timestamps = pd.to_datetime(frame["timestamp"], utc=True)
    index = timestamps.to_numpy()
    own_slot = _own_slot(frame)
    columns: dict[str, pd.Series] = {}
    for slot in SLOT_ORDER:
        if slot == own_slot:
            columns[slot] = pd.Series(frame["close"].astype(float).to_numpy(), index=index)
            continue
        column = peer_columns[slot]
        if column not in frame.columns:
            raise ValueError(
                f"GEM slot '{own_slot}' needs the peer column '{column}' "
                f"(close of {slot_symbols(params)[slot]}); run "
                "attach_peer_closes() first"
            )
        columns[slot] = pd.Series(frame[column].astype(float).to_numpy(), index=index)
    return pd.DataFrame(columns, index=index)


def _own_slot(frame: pd.DataFrame) -> str:
    if SLOT_COLUMN not in frame.columns:
        raise ValueError(
            "GEM needs a 'slot' column in the frame (values: "
            f"{list(SLOT_ORDER)}); run attach_peer_closes() first. Refusing to "
            "guess a slot from prices."
        )
    slot = str(frame[SLOT_COLUMN].iloc[0])
    if slot not in SLOT_ORDER:
        raise ValueError(
            f"GEM frame declares slot {slot!r}, expected one of {list(SLOT_ORDER)}"
        )
    return slot


def attach_peer_closes(frames: dict[str, pd.DataFrame], params: dict) -> dict[str, pd.DataFrame]:
    """Mechanically add every other slot's close column to each frame.

    No trading logic here: the caller hands in one frame per configured
    symbol (``{symbol: frame}``) and gets one frame per symbol back with
    ``peer_close_<SYMBOL>`` columns for the other slots plus a ``slot``
    column naming the frame's own slot. Frames are aligned on their own
    timestamps, so a missing peer date stays NaN and that month is left
    out of the allocation (see :func:`monthly_slot_closes`).
    """
    params = params or {}
    symbols = slot_symbols(params)
    slots_by_symbol = {symbol: slot for slot, symbol in symbols.items()}
    for symbol in frames:
        if symbol not in slots_by_symbol:
            raise ValueError(
                f"{symbol} is not a configured GEM slot (configured: "
                f"{sorted(symbols.values())}); cannot attach its slot"
            )
    out: dict[str, pd.DataFrame] = {}
    for symbol, frame in frames.items():
        if "timestamp" not in frame.columns:
            raise ValueError(f"frame for {symbol} needs a 'timestamp' column")
        own_timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        attached = frame.copy()
        attached[SLOT_COLUMN] = slots_by_symbol[symbol]
        for peer_symbol, peer_frame in frames.items():
            if peer_symbol == symbol:
                continue
            peer_close = peer_frame["close"].astype(float)
            peer_timestamps = pd.to_datetime(peer_frame["timestamp"], utc=True)
            attached[f"{PEER_CLOSE_PREFIX}{peer_symbol.lower()}"] = pd.Series(
                peer_close.to_numpy(), index=peer_timestamps.to_numpy(), dtype=float
            ).reindex(own_timestamps.to_numpy()).to_numpy()
        out[symbol] = attached
    return out


def generate_signals(df: pd.DataFrame, params: dict):
    """Long/flat signals for THE SLOT carried by the passed frame."""
    close = df["close"].astype(float).reset_index(drop=True)
    timestamps = pd.to_datetime(df["timestamp"], utc=True).reset_index(drop=True)
    own_slot = _own_slot(df)

    slot_closes = frame_slot_closes(df, params)
    slot_closes.index = timestamps.to_numpy()
    monthly = monthly_slot_closes(
        {slot: slot_closes[slot] for slot in SLOT_ORDER}, timestamps
    )
    allocation = gem_allocation(monthly, params)

    # Map the monthly decision back to bars: only the month-end bar that
    # CLOSED the decision month carries a target. The decision applies from
    # the NEXT bar, so the entry fills at that bar's open.
    is_month_end = month_end_flags(timestamps).to_numpy()
    month_end_periods = _periods(timestamps)[is_month_end]
    allocation_at = pd.Series(np.nan, index=close.index, dtype="object")
    for position, period in enumerate(month_end_periods):
        if period in allocation.index:
            target = allocation.loc[period]
            if pd.notna(target):
                allocation_at.iloc[int(np.where(is_month_end)[0][position])] = str(target)

    entries = pd.Series(False, index=close.index, dtype=bool)
    exits = pd.Series(False, index=close.index, dtype=bool)
    exit_reason = pd.Series(index=close.index, dtype="object")
    target_at_exit = pd.Series(np.nan, index=close.index, dtype="object")
    target_indicator = allocation_at

    in_position = False
    previous_position = False
    for i in range(len(df)):
        target = allocation_at.iloc[i]
        if pd.isna(target):
            # No allocation for this month (universe gap): the position
            # carries on, the rule does not trade on missing data.
            continue
        in_position = bool(str(target) == own_slot)
        if in_position and not previous_position:
            entries.iloc[i] = True
        elif not in_position and previous_position:
            exits.iloc[i] = True
            exit_reason.iloc[i] = "gem_rotation_out"
            target_at_exit.iloc[i] = str(target)
        previous_position = in_position

    indicators_dict = {
        "Gem_Target": target_indicator,
        "Gem_Target_at_exit": target_at_exit,
        "Month_End": pd.Series(is_month_end, index=close.index, dtype=bool),
        "exit_reason": exit_reason,
    }
    drawings: list[dict] = []
    if "timestamp" in df.columns:
        entry_times = pd.to_datetime(
            df.loc[entries[entries].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        exit_times = pd.to_datetime(
            df.loc[exits[exits].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        for ts in entry_times.iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "belowBar",
                    "shape": "arrowUp",
                    "color": "#22c55e",
                    "text": f"GEM enter {own_slot}",
                }
            )
        for ts in exit_times.iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "aboveBar",
                    "shape": "arrowDown",
                    "color": "#ef4444",
                    "text": "GEM rotate",
                }
            )

    return entries, exits, indicators_dict, drawings