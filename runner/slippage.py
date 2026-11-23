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
tools, data outside the ``ohlcv/{source}/{asset_class}/...`` layout) or
that map to no slippage model REFUSE the run under the default ``auto``
resolution — fail closed like fees (#84, maintainer decision 2026-11-22).
A deliberate gross run must say so explicitly: ``slippage="none"`` (the
parity opt-out) records ``ZeroSlippage`` with
``reason=explicit_opt_out``; an explicit cost comes from ``'bps'``,
``'spread'`` or ``slippage_bps``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

import pandas as pd

__all__ = [
    "BarSpreadSlippage",
    "FixedBpsSlippage",
    "SLIPPAGE_CAPABLE_ENGINES",
    "SlippageModel",
    "ZeroSlippage",
    "assert_slippage_supported",
    "resolve_slippage_model",
    "slippage_declared",
    "DEFAULT_SLIPPAGE_BPS",
    "FX_SPREAD_ASSET_CLASSES",
]


class SlippageModel(Protocol):
    """Adjusts one fill price for adverse slippage.

    ``side`` is ``"buy"`` or ``"sell"`` from the trader's perspective:
    a buy fills higher, a sell fills lower. ``bar`` is the OHLCV row the
    fill was priced from (``None`` when unavailable — spread models then
    use their configured bps fallback). ``fill_side`` is ``"ask"`` when the
    fill price is already denominated on the ask side (an ``ask_ohlc``
    ask-triggered SHORT exit), ``None`` otherwise; models that price off
    the bar spread use it to avoid charging the spread twice.
    """

    def slip_price(
        self,
        price: float,
        side: str,
        bar: Any = None,
        *,
        fill_side: str | None = None,
    ) -> float:
        ...

    def describe(self) -> dict[str, Any]:
        """JSON-safe description recorded in run metadata."""
        ...


#: Asset classes whose instruments trade on a quoted two-sided market
#: (FX, CFDs, metals, indices, energies): the per-bar bid/ask spread is
#: the dominant cost, so the bar-spread model applies where the data
#: carries ``ask_*`` columns. In practice only feeds that retain their
#: quote columns qualify — the repo's grid downloader normalises them
#: away, so most FX runs take the fixed-bps fallback instead.
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
        # Dukascopy volatility-index CFDs (VOLIDXUSD/DOLLARIDXUSD) are
        # bid/ask instruments of the same class family.
        "volatility",
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
    "volatility": 3.0,
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
#: param) already price the quoted spread for the fills they cover, so
#: when it is configured the auto model must not add the bar spread a
#: second time and degrades to the fixed-bps leg. ``ask_ohlc`` does NOT
#: degrade the model: it only moves SHORT stop/TP *triggers* onto the ask
#: side, and ask-triggered exits pass ``fill_side="ask"`` so the spread is
#: not charged twice on those legs either.
_EXTERNAL_SPREAD_PARAMS = ("spread_price_col",)

#: Engines that apply a fill-price slippage model (#130 scope: the native
#: engine and signal replay — the engines #318 keeps). Every other engine
#: must reject an explicit slippage declaration instead of running gross
#: while the user believes slippage was applied.
SLIPPAGE_CAPABLE_ENGINES = frozenset({"native", "signal_replay", "signal-replay"})


def _normalize_asset_class(asset_class: Any) -> str:
    return str(asset_class or "").strip().lower()


def _positive_float(value: Any, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc
    if math.isnan(parsed) or parsed in (float("inf"), float("-inf")):
        raise ValueError(f"Invalid {field}: {value!r}")
    return parsed


def _bar_numeric_values(bar: Any) -> dict[str, float]:
    """Lowercased column name -> float value for every numeric bar field."""
    if bar is None:
        return {}
    try:
        items = bar.items() if hasattr(bar, "items") else []
    except (AttributeError, TypeError):
        return {}
    values: dict[str, float] = {}
    for key, raw in items:
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        try:
            values[str(key).lower()] = float(raw)
        except (TypeError, ValueError):
            continue
    return values


def _median_sorted(values: list[float]) -> float:
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


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
    values = _bar_numeric_values(bar)
    if not values:
        return None
    diffs: list[float] = []
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
    return _median_sorted(diffs)


@dataclass(frozen=True)
class ZeroSlippage:
    """Explicit no-slippage declaration (the ``none`` parity opt-out)."""

    asset_class: str = ""
    reason: str = "explicit_opt_out"

    def slip_price(
        self,
        price: float,
        side: str,
        bar: Any = None,
        *,
        fill_side: str | None = None,
    ) -> float:
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

    def slip_price(
        self,
        price: float,
        side: str,
        bar: Any = None,
        *,
        fill_side: str | None = None,
    ) -> float:
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

    MT5-tester-like and matching the engine's existing ``spread_price_col``
    convention: canonical OHLC is the BID series, so a buy fills at the ask
    (``price + spread``) while a sell fills at the bid (``price`` unchanged)
    — the spread is charged once per round trip, on the buy side only. When
    the bar carries no usable ``ask_*`` columns the configured
    ``default_bps`` applies symmetrically per side instead.
    """

    default_bps: float
    asset_class: str = ""
    spread_source: str = "bar_bid_ask_else_fixed_bps"

    def __post_init__(self) -> None:
        parsed = _positive_float(self.default_bps, field="slippage default bps")
        if parsed < 0:
            raise ValueError("Slippage default bps must be non-negative.")
        object.__setattr__(self, "default_bps", parsed)

    def slip_price(
        self,
        price: float,
        side: str,
        bar: Any = None,
        *,
        fill_side: str | None = None,
    ) -> float:
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        spread = _bar_spread_units(bar)
        if spread is not None:
            # Bid-referenced bar: buys lift to the ask, sells stay on the
            # bid — the quoted spread is paid once, by the buy side. An
            # ask-triggered fill (``fill_side="ask"``: an ``ask_ohlc``
            # SHORT stop/TP whose level already sits on the ask) must not
            # pay it a second time.
            if side == "buy":
                if fill_side == "ask":
                    return float(price)
                return float(price) + spread
            return float(price)
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


def _normalized_slippage_defaults(params: dict[str, Any]) -> dict[str, Any]:
    """``slippage_defaults`` with asset-class keys normalized.

    Config names are normalized the same way ``resolve_bar_spread``
    normalises data columns (strip + lowercase), so ``{"Equities": 20}``
    applies to ``equities``. A key that matches no known asset class is a
    typo that would silently never apply — rejected. Duplicate keys that
    collide after normalization must agree.
    """
    overrides = params.get("slippage_defaults")
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ValueError("slippage_defaults must be a JSON object of asset_class -> bps")
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in overrides.items():
        key = _normalize_asset_class(raw_key)
        if not key:
            raise ValueError("slippage_defaults keys must be non-empty asset class names")
        if key in normalized and normalized[key] != raw_value:
            raise ValueError(
                f"slippage_defaults has conflicting entries for asset class {key!r}"
            )
        normalized[key] = raw_value
    unknown = sorted(key for key in normalized if key not in DEFAULT_SLIPPAGE_BPS)
    if unknown:
        raise ValueError(
            f"slippage_defaults keys match no known asset class: {unknown}; "
            f"known classes: {sorted(DEFAULT_SLIPPAGE_BPS)}"
        )
    return normalized


def _class_default_bps(asset_class: str, params: dict[str, Any]) -> float:
    """Configured fallback bps for ``asset_class`` (most specific wins)."""
    overrides = _normalized_slippage_defaults(params)
    raw = overrides.get(_normalize_asset_class(asset_class))
    if raw is not None:
        return _positive_float(raw, field="slippage_defaults entry")
    configured = params.get("slippage_default_bps")
    if configured is not None:
        return _positive_float(configured, field="slippage_default_bps")
    fallback = DEFAULT_SLIPPAGE_BPS.get(_normalize_asset_class(asset_class))
    if fallback is None:
        raise ValueError(
            f"No slippage default for asset_class={asset_class!r}; "
            "pass slippage_bps, slippage_default_bps or slippage_defaults."
        )
    return float(fallback)


def _auto_model(params: dict[str, Any], asset_class: str) -> SlippageModel:
    """Per-asset-class default: bar spread for FX/CFD, fixed bps else."""
    ac = _normalize_asset_class(asset_class)
    if params.get("slippage_bps") is not None:
        raise ValueError(
            "slippage_bps cannot be combined with slippage='auto' (or the "
            "default per-class resolution): the auto model picks the "
            "per-class cost itself. Pass slippage='bps' to force a fixed "
            "bps model, or slippage_default_bps/slippage_defaults to tune "
            "the auto fallback."
        )
    if not ac:
        raise ValueError(
            "slippage: the asset class could not be determined, so no "
            "per-class cost model applies. Refusing to run gross — pass an "
            "explicit slippage: 'none' for a deliberate parity run, or "
            "'bps'/'spread'/slippage_bps for a costed run."
        )
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
    # Recognized-but-unmapped class: no per-class cost exists, so the run
    # refuses rather than silently filling gross (maintainer decision
    # 2026-11-22, fail closed like fees #84). An explicit declaration —
    # ``slippage='none'`` for a deliberate parity run, or ``'bps'`` /
    # ``'spread'`` / ``slippage_bps`` for a costed run — never reaches
    # this point. A value that is no known class at all may be a symbol
    # segment from a legacy ``ohlcv/{source}/{symbol}/...`` layout, so the
    # message says so rather than blaming an invented class name.
    raise ValueError(
        f"slippage: asset_class={ac!r} is not mapped to a slippage model. "
        "If the run reads a legacy ohlcv/{source}/{symbol}/ layout, this "
        "value is the symbol and the asset class is genuinely "
        "undetermined. Refusing to run gross — pass an explicit slippage: "
        "'none' for a deliberate parity run, or 'bps'/'spread'/"
        "slippage_bps for a costed run."
    )


def _spread_fallback_bps(
    params: dict[str, Any], asset_class: str, bps_override: Any
) -> float:
    """Fallback bps for ``slippage='spread'`` — never a silent default."""
    if bps_override is not None:
        return _positive_float(bps_override, field="slippage_bps")
    if asset_class:
        return _class_default_bps(asset_class, params)
    configured = params.get("slippage_default_bps")
    if configured is not None:
        return _positive_float(configured, field="slippage_default_bps")
    raise ValueError(
        "slippage='spread' with an unknown asset class requires an "
        "explicit fallback: pass slippage_bps or slippage_default_bps."
    )


def _spread_model(
    params: dict[str, Any], asset_class: str, bps_override: Any
) -> SlippageModel:
    fallback = _spread_fallback_bps(params, asset_class, bps_override)
    # Same double-count guard as the auto model: a run that already prices
    # the spread through spread_price_col / ask_ohlc must not pay it twice.
    if any(params.get(key) for key in _EXTERNAL_SPREAD_PARAMS):
        return FixedBpsSlippage(bps=fallback, asset_class=asset_class)
    return BarSpreadSlippage(default_bps=fallback, asset_class=asset_class)


def _bps_model(bps_override: Any, asset_class: str, params: dict[str, Any]) -> SlippageModel:
    resolved = bps_override
    if resolved is None:
        if not asset_class:
            raise ValueError(
                "slippage='bps' requires slippage_bps when the asset "
                "class is unknown."
            )
        resolved = _class_default_bps(asset_class, params)
    return FixedBpsSlippage(
        bps=_positive_float(resolved, field="slippage_bps"), asset_class=asset_class
    )


def _fallback_key_present(params: dict[str, Any], key: str) -> bool:
    """A fallback key that actually configures something.

    ``slippage_defaults: {}`` declares no overrides, so it is absent for
    every dead-config and cost-declaration check.
    """
    value = params.get(key)
    return value is not None and value != {}


def _reject_dead_fallback_keys(params: dict[str, Any]) -> None:
    """Reject fallback keys a fixed ``slippage_bps`` makes unreachable.

    ``slippage_default_bps``/``slippage_defaults`` are fallbacks consulted
    only when no fixed bps is declared; beside ``slippage_bps`` they would
    be silently ignored — the same config lie ``slippage=<number>`` already
    rejects.
    """
    stray = [
        key
        for key in ("slippage_default_bps", "slippage_defaults")
        if _fallback_key_present(params, key)
    ]
    if stray:
        raise ValueError(
            "params['slippage_bps'] fixes the bps for every asset class; "
            f"{', '.join(stray)} would be silently ignored. Drop them or "
            "remove slippage_bps."
        )


def _numeric_slippage_or_raise(explicit: str, asset_class: str) -> SlippageModel:
    """A ``slippage`` string that is neither a spelling nor a number."""
    try:
        return FixedBpsSlippage(
            bps=_positive_float(explicit, field="slippage"), asset_class=asset_class
        )
    except ValueError:
        raise ValueError(
            f"Invalid slippage {explicit!r}: use 'none', 'spread', 'bps', "
            "'auto' or a fixed bps number."
        ) from None


def _resolve_string_slippage(
    explicit: str,
    params: dict[str, Any],
    asset_class: str,
    bps_override: Any,
) -> SlippageModel:
    spelling = explicit.strip().lower()
    if spelling in _NONE_SPELLINGS:
        return ZeroSlippage(asset_class=asset_class, reason="explicit_opt_out")
    if spelling == "spread":
        return _spread_model(params, asset_class, bps_override)
    if spelling in _BPS_SPELLINGS:
        return _bps_model(bps_override, asset_class, params)
    if spelling in _AUTO_SPELLINGS:
        return _auto_model(params, asset_class)
    return _numeric_slippage_or_raise(explicit, asset_class)


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
    # ``slippage_defaults`` keys are config, not data: a typo must raise on
    # EVERY resolution path. Validation used to live inside
    # ``_class_default_bps``, which gross/unclassified runs never reach —
    # ``{"slippage_defaults": {"equitys": 20}}`` resolved to ``none`` with
    # no error. Normalizing up front rejects bad keys (and non-dict values)
    # before any early return.
    _normalized_slippage_defaults(params)

    model = params.get("slippage_model")
    if model is not None:
        if not (hasattr(model, "slip_price") and hasattr(model, "describe")):
            raise ValueError(
                "params['slippage_model'] must implement slip_price() and describe()."
            )
        return model

    explicit = params.get("slippage")
    bps_override = params.get("slippage_bps")

    if explicit is not None and not isinstance(explicit, str):
        if isinstance(explicit, bool) or not isinstance(explicit, (int, float)):
            raise ValueError(
                "params['slippage'] must be a model spelling "
                "('none', 'spread', 'bps', 'auto') or a fixed bps number, "
                f"got {explicit!r}."
            )
        # A numeric slippage IS the fixed bps — combining it with any
        # tuning key would silently drop one of the two declarations.
        stray = [
            key
            for key in ("slippage_bps", "slippage_default_bps", "slippage_defaults")
            if _fallback_key_present(params, key)
        ]
        if stray:
            raise ValueError(
                f"params['slippage']={explicit!r} cannot be combined with "
                f"{', '.join(stray)}: the numeric spelling already fixes the "
                "bps. Drop the extra keys or use slippage='bps'."
            )
        return FixedBpsSlippage(
            bps=_positive_float(explicit, field="slippage"), asset_class=ac
        )

    if isinstance(explicit, str):
        spelling = explicit.strip().lower()
        if bps_override is not None and spelling not in _BPS_SPELLINGS and spelling != "spread":
            raise ValueError(
                f"params['slippage_bps'] cannot be combined with "
                f"slippage={explicit!r}: only 'bps'/'spread' spellings consume "
                "it (as the fixed value / the spread fallback)."
            )
        if spelling in _NONE_SPELLINGS:
            stray = [
                key
                for key in ("slippage_default_bps", "slippage_defaults")
                if _fallback_key_present(params, key)
            ]
            if stray:
                raise ValueError(
                    f"slippage={explicit!r} declares a gross run; "
                    f"{', '.join(stray)} would be silently ignored. Drop them."
                )
        if bps_override is not None:
            # Only the 'bps'/'spread' spellings reach this point with a
            # slippage_bps (the others raised above): the fixed bps wins for
            # every asset class, so the fallback keys are silently dead.
            _reject_dead_fallback_keys(params)
        return _resolve_string_slippage(explicit, params, ac, bps_override)

    if bps_override is not None:
        _reject_dead_fallback_keys(params)
        return FixedBpsSlippage(
            bps=_positive_float(bps_override, field="slippage_bps"), asset_class=ac
        )

    return _auto_model(params, ac)


def _slippage_params_declare_cost(params: dict[str, Any]) -> bool:
    """True when the params ask for anything other than an explicit gross run."""
    explicit = params.get("slippage")
    if "slippage_model" in params and params.get("slippage_model") is not None:
        model = params["slippage_model"]
        describe = getattr(model, "describe", None)
        if callable(describe):
            try:
                if describe().get("model") == "none":
                    return False
            except Exception:
                pass
        return True
    if isinstance(explicit, str):
        if explicit.strip().lower() in _NONE_SPELLINGS:
            # ``none`` plus tuning keys is rejected by resolve_slippage_model;
            # treat the declaration as gross only when it is clean.
            return any(
                _fallback_key_present(params, key)
                or (key == "slippage_bps" and params.get(key) is not None)
                for key in ("slippage_bps", "slippage_default_bps", "slippage_defaults")
            )
        return True
    if explicit is not None:
        return True
    return any(
        _fallback_key_present(params, key)
        or (key == "slippage_bps" and params.get(key) is not None)
        for key in ("slippage_bps", "slippage_default_bps", "slippage_defaults")
    )


def slippage_declared(params: dict[str, Any] | None) -> bool:
    """True when ``params`` explicitly declare a non-``none`` slippage cost."""
    return _slippage_params_declare_cost(dict(params or {}))


def assert_slippage_supported(engine_name: str, params: dict[str, Any] | None) -> None:
    """Fail closed when an engine cannot honour a declared slippage model.

    #130 scopes fill-price slippage to the native engine and signal replay.
    vectorbt/pynecore (and LEAN, which is import-only per #318) have no
    slippage code path, so a declared model there would be silently
    ignored — the run must refuse instead. ``slippage="none"`` declarations
    are allowed everywhere: they assert the gross behaviour the engine
    already has.
    """
    if str(engine_name) in SLIPPAGE_CAPABLE_ENGINES:
        return
    if slippage_declared(params):
        raise ValueError(
            f"engine '{engine_name}' does not model slippage (#130 scope: "
            "native + signal replay); the declared slippage params would be "
            "silently ignored. Re-run with --engine native or drop the "
            "slippage flags."
        )
