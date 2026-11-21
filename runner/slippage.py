"""Pluggable slippage models applied to fill prices (#130).

Maintainer decision (2026-11-21, issue #130): slippage is applied to the
FILL PRICE in the adverse direction — never a separate cost line — and is
scoped to the native engine and signal replay (the engines #318 keeps;
LEAN stays import-only).

- FX/CFD asset classes: the spread comes from bid/ask bar data where
  available (MT5-tester-like: buys pay the ask, sells receive the bid),
  otherwise a configured fixed-bps fallback applies.
- Stocks/crypto: a configurable fixed bps default per asset class.
- ``slippage: "none"`` is the explicit parity opt-out that reproduces the
  pre-#130 gross-fill behaviour bit-for-bit.

Every run records the resolved model in ``meta.data["slippage"]`` — the
recording is mandatory even when the resolved model is ``none``.

Asset classes that cannot be classified (direct engine use in tests and
tools, data outside the ``ohlcv/{source}/{asset_class}/...`` layout)
resolve to :class:`ZeroSlippage` and say so in ``describe()``: adapters
always classify from the parquet path, so an unclassified ``none`` is a
deliberate, auditable outcome rather than a silent gross default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import pandas as pd

__all__ = [
    "BarSpreadSlippage",
    "FixedBpsSlippage",
    "SlippageModel",
    "ZeroSlippage",
    "resolve_slippage_model",
    "DEFAULT_SLIPPAGE_BPS",
    "FX_SPREAD_ASSET_CLASSES",
]


class SlippageModel(Protocol):
    """Adjusts one fill price for adverse slippage.

    ``side`` is ``"buy"`` or ``"sell"`` from the trader's perspective:
    a buy fills higher, a sell fills lower. ``bar`` is the OHLCV row the
    fill was priced from (``None`` when unavailable — spread models then
    use their configured bps fallback).
    """

    def slip_price(self, price: float, side: str, bar: Any = None) -> float:
        ...

    def describe(self) -> dict[str, Any]:
        """JSON-safe description recorded in run metadata."""
        ...


#: Asset classes whose instruments trade on a quoted two-sided market
#: (FX, CFDs, metals, indices, energies): the per-bar bid/ask spread is
#: the dominant cost, so the bar-spread model applies where the data
#: carries ``ask_*`` columns (Dukascopy ``--price-type both`` feeds).
FX_SPREAD_ASSET_CLASSES = frozenset(
    {
        "forex",
        "fx",
        "cfd",
        "metals",
        "indices",
        "index",
        "energy",
        "energies",
        "commodity",
        "commodities",
    }
)

#: Configurable fixed-bps defaults per asset class (decision: "a
#: configurable fixed bps default per asset class"). Overridable per run
#: via ``slippage_defaults`` or globally via ``slippage_bps`` /
#: ``slippage_default_bps`` params.
DEFAULT_SLIPPAGE_BPS: dict[str, float] = {
    # FX/CFD-like: fallback when the bar carries no bid/ask columns.
    "forex": 1.0,
    "fx": 1.0,
    "cfd": 1.0,
    "metals": 2.0,
    "indices": 2.0,
    "index": 2.0,
    "energy": 3.0,
    "energies": 3.0,
    "commodity": 3.0,
    "commodities": 3.0,
    # Stocks/ETFs: half-spread plus small impact allowance.
    "equity": 5.0,
    "equities": 5.0,
    "stock": 5.0,
    "stocks": 5.0,
    "etf": 5.0,
    "etfs": 5.0,
    # Crypto: wider quoted spreads and thinner books.
    "crypto": 10.0,
    "cryptocurrency": 10.0,
    "cryptocurrencies": 10.0,
}

_EQUITY_ASSET_CLASSES = frozenset(
    {"equity", "equities", "stock", "stocks", "etf", "etfs"}
)
_CRYPTO_ASSET_CLASSES = frozenset(
    {"crypto", "cryptocurrency", "cryptocurrencies"}
)

_NONE_SPELLINGS = frozenset({"none", "zero", "off", "parity", "no"})
_AUTO_SPELLINGS = frozenset({"auto", "default", "asset_class", "per_asset_class", ""})
_BPS_SPELLINGS = frozenset({"bps", "fixed", "fixed_bps"})

#: Legacy per-bar spread surcharge columns (``spread_price_col`` strategy
#: param) and the ask-side trigger flag (``ask_ohlc``) already price the
#: quoted spread for the fills they cover. When either is configured the
#: auto model must not add the bar spread a second time, so it degrades to
#: the fixed-bps leg and records ``spread_source`` accordingly.
_EXTERNAL_SPREAD_PARAMS = ("spread_price_col", "ask_ohlc")


def _normalize_asset_class(asset_class: Any) -> str:
    return str(asset_class or "").strip().lower()


def _positive_float(value: Any, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise ValueError(f"Invalid {field}: {value!r}")
    return parsed


def _bar_spread_units(bar: Any) -> float | None:
    """Per-bar ask-minus-bid spread in price units, or ``None``.

    Canonical OHLC is the bid-side series (``normalize_ohlcv_frame`` fills
    it from ``bid_*`` and the MT5-chart convention treats bid as the
    reference). For each canonical field with an ``ask_*`` counterpart the
    positive difference is a quoted spread observation; the median of the
    available observations is used so one stale column cannot dominate.
    Non-positive differences (ask-side canonical data where ``ask_*``
    merely repeats the canonical series) are ignored — they carry no
    spread information and must not produce a negative adjustment.
    """
    if bar is None:
        return None
    diffs: list[float] = []
    try:
        items = bar.items() if hasattr(bar, "items") else []
    except (AttributeError, TypeError):
        return None
    values: dict[str, float] = {}
    for key, raw in items:
        col = str(key).lower()
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        try:
            values[col] = float(raw)
        except (TypeError, ValueError):
            continue
    for field in ("open", "high", "low", "close"):
        ask = values.get(f"ask_{field}")
        bid = values.get(field)
        if ask is None or bid is None:
            continue
        diff = ask - bid
        if diff > 0 and pd.notna(diff):
            diffs.append(diff)
    if not diffs:
        return None
    diffs.sort()
    mid = len(diffs) // 2
    if len(diffs) % 2:
        return diffs[mid]
    return (diffs[mid - 1] + diffs[mid]) / 2.0


@dataclass(frozen=True)
class ZeroSlippage:
    """Explicit no-slippage declaration (parity opt-out or unclassified)."""

    asset_class: str = ""
    reason: str = "explicit_opt_out"

    def slip_price(self, price: float, side: str, bar: Any = None) -> float:
        return float(price)

    def describe(self) -> dict[str, Any]:
        return {
            "model": "none",
            "bps": 0.0,
            "asset_class": self.asset_class or None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FixedBpsSlippage:
    """Fixed adverse bps adjustment: buys pay ``price * (1 + bps/1e4)``,
    sells receive ``price * (1 - bps/1e4)``."""

    bps: float
    asset_class: str = ""

    def __post_init__(self) -> None:
        parsed = _positive_float(self.bps, field="slippage bps")
        if parsed < 0:
            raise ValueError("Slippage bps must be non-negative.")
        object.__setattr__(self, "bps", parsed)

    def slip_price(self, price: float, side: str, bar: Any = None) -> float:
        adjustment = abs(float(price)) * self.bps / 10_000.0
        if side == "buy":
            return float(price) + adjustment
        if side == "sell":
            return max(float(price) - adjustment, 1e-12)
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    def describe(self) -> dict[str, Any]:
        return {
            "model": "fixed_bps",
            "bps": self.bps,
            "asset_class": self.asset_class or None,
        }


@dataclass(frozen=True)
class BarSpreadSlippage:
    """Spread from the bar's bid/ask columns, fixed-bps fallback.

    MT5-tester-like: when the bar carries ``ask_*`` columns the quoted
    spread (ask − bid, in price units) is the adjustment — buys pay it,
    sells receive it back. Without usable ask data the configured
    ``default_bps`` applies to the fill price instead.
    """

    default_bps: float
    asset_class: str = ""
    spread_source: str = "bar_bid_ask_else_fixed_bps"

    def __post_init__(self) -> None:
        parsed = _positive_float(self.default_bps, field="slippage default bps")
        if parsed < 0:
            raise ValueError("Slippage default bps must be non-negative.")
        object.__setattr__(self, "default_bps", parsed)

    def slip_price(self, price: float, side: str, bar: Any = None) -> float:
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        spread = _bar_spread_units(bar)
        if spread is not None:
            adjustment = spread
        else:
            adjustment = abs(float(price)) * self.default_bps / 10_000.0
        if side == "buy":
            return float(price) + adjustment
        return max(float(price) - adjustment, 1e-12)

    def describe(self) -> dict[str, Any]:
        return {
            "model": "bar_spread",
            "default_bps": self.default_bps,
            "asset_class": self.asset_class or None,
            "spread_source": self.spread_source,
        }


def _class_default_bps(asset_class: str, params: dict[str, Any]) -> float:
    """Configured fallback bps for ``asset_class`` (most specific wins)."""
    overrides = params.get("slippage_defaults")
    if isinstance(overrides, dict):
        raw = overrides.get(asset_class)
        if raw is not None:
            return _positive_float(raw, field="slippage_defaults entry")
    configured = params.get("slippage_default_bps")
    if configured is not None:
        return _positive_float(configured, field="slippage_default_bps")
    fallback = DEFAULT_SLIPPAGE_BPS.get(asset_class)
    if fallback is None:
        raise ValueError(
            f"No slippage default for asset_class={asset_class!r}; "
            "pass slippage_bps, slippage_default_bps or slippage_defaults."
        )
    return float(fallback)


def _auto_model(params: dict[str, Any], asset_class: str) -> SlippageModel:
    """Per-asset-class default: bar spread for FX/CFD, fixed bps else."""
    ac = _normalize_asset_class(asset_class)
    if not ac:
        return ZeroSlippage(asset_class="", reason="unclassified_asset_class")
    if ac in _CRYPTO_ASSET_CLASSES or ac in _EQUITY_ASSET_CLASSES:
        bps = _class_default_bps(ac, params)
        return FixedBpsSlippage(bps=bps, asset_class=ac)
    if ac in FX_SPREAD_ASSET_CLASSES:
        bps = _class_default_bps(ac, params)
        # A run that already prices the spread through the legacy
        # spread_price_col / ask_ohlc parameters must not pay it twice.
        if any(params.get(key) for key in _EXTERNAL_SPREAD_PARAMS):
            return FixedBpsSlippage(bps=bps, asset_class=ac)
        return BarSpreadSlippage(default_bps=bps, asset_class=ac)
    # Recognized-but-unmapped class: still record an explicit none rather
    # than inventing a cost the decision did not specify.
    return ZeroSlippage(asset_class=ac, reason="unclassified_asset_class")


def resolve_slippage_model(
    params: dict[str, Any] | None, *, asset_class: str = ""
) -> SlippageModel:
    """Resolve the slippage model for one run, always returning a model.

    Accepted (in order):

    1. ``params["slippage_model"]`` — a ready-made :class:`SlippageModel`.
    2. ``params["slippage"]`` — ``"none"``/``"zero"`` (parity opt-out),
       ``"spread"`` (bar bid/ask with bps fallback), ``"bps"`` (fixed,
       needs ``slippage_bps``), ``"auto"``/``"default"`` (per-class), or a
       number (fixed bps).
    3. ``params["slippage_bps"]`` — fixed bps regardless of asset class.
    4. absent — the per-asset-class default (never silent: the resolved
       model is recorded in run metadata either way).
    """
    params = dict(params or {})
    ac = _normalize_asset_class(asset_class)

    model = params.get("slippage_model")
    if model is not None:
        if not (hasattr(model, "slip_price") and hasattr(model, "describe")):
            raise ValueError(
                "params['slippage_model'] must implement slip_price() and describe()."
            )
        return model

    explicit = params.get("slippage")
    bps_override = params.get("slippage_bps")

    if isinstance(explicit, str):
        spelling = explicit.strip().lower()
        if spelling in _NONE_SPELLINGS:
            return ZeroSlippage(asset_class=ac, reason="explicit_opt_out")
        if spelling == "spread":
            if bps_override is not None:
                fallback = _positive_float(bps_override, field="slippage_bps")
            elif ac:
                fallback = _class_default_bps(ac, params)
            else:
                fallback = _positive_float(
                    params.get("slippage_default_bps", 5.0),
                    field="slippage_default_bps",
                )
            return BarSpreadSlippage(default_bps=fallback, asset_class=ac)
        if spelling in _BPS_SPELLINGS:
            if bps_override is None:
                if not ac:
                    raise ValueError(
                        "slippage='bps' requires slippage_bps when the asset "
                        "class is unknown."
                    )
                bps_override = _class_default_bps(ac, params)
            return FixedBpsSlippage(
                bps=_positive_float(bps_override, field="slippage_bps"), asset_class=ac
            )
        if spelling in _AUTO_SPELLINGS:
            return _auto_model(params, ac)
        try:
            return FixedBpsSlippage(
                bps=_positive_float(explicit, field="slippage"), asset_class=ac
            )
        except ValueError:
            raise ValueError(
                f"Invalid slippage {explicit!r}: use 'none', 'spread', 'bps', "
                "'auto' or a fixed bps number."
            ) from None

    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return FixedBpsSlippage(
            bps=_positive_float(explicit, field="slippage"), asset_class=ac
        )

    if bps_override is not None:
        return FixedBpsSlippage(
            bps=_positive_float(bps_override, field="slippage_bps"), asset_class=ac
        )

    return _auto_model(params, ac)
