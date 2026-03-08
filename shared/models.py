"""
Shared data models for the universal trade format.

Defines the canonical JSON schema as Python dataclasses:
- TradeExit: A single (partial) exit from a position
- Trade: One position from entry to full close
- BacktestMeta: Metadata about a backtest run
- BacktestResult: Complete backtest output (meta + trades + equity + stats)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─── Strategy metadata ──────────────────────────────────────────────────────


@dataclass
class StrategyMeta:
    """Metadaten für eine Strategie (unabhängig vom Backtest)."""

    name: str
    version: str = "1.0.0"
    tags: dict = field(default_factory=dict)
    # Tags structure:
    # {
    #     "type": ["mean-reversion", "momentum", "breakout"],
    #     "source": "zenomtrader-discord",
    #     "link": "https://...",
    #     "asset_class": ["equities", "forex"],
    #     "complexity": "simple" | "medium" | "complex"
    # }

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.tags:
            d["tags"] = self.tags
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyMeta:
        return cls(
            name=d["name"],
            version=d.get("version", "1.0.0"),
            tags=d.get("tags", {}),
        )


# ─── Timeframe constants ────────────────────────────────────────────────────

VALID_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
VALID_DIRECTIONS = ("LONG", "SHORT")


# ─── Exit ────────────────────────────────────────────────────────────────────


@dataclass
class TradeExit:
    """A single (partial) exit from a position."""

    time: str  # ISO 8601 UTC
    price: float
    quantity: float
    pnl: float | None = None  # PnL for this partial exit
    reason: str | None = None  # e.g. "stop_loss", "tp1", "trailing_stop"
    fees: float | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"time": self.time, "price": self.price, "quantity": self.quantity}
        if self.pnl is not None:
            d["pnl"] = self.pnl
        if self.reason is not None:
            d["reason"] = self.reason
        if self.fees is not None:
            d["fees"] = self.fees
        if self.tags:
            d["tags"] = self.tags
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TradeExit:
        return cls(
            time=d["time"],
            price=d["price"],
            quantity=d["quantity"],
            pnl=d.get("pnl"),
            reason=d.get("reason"),
            fees=d.get("fees"),
            tags=d.get("tags", []),
        )


# ─── Trade ───────────────────────────────────────────────────────────────────


@dataclass
class Trade:
    """One position from entry to full close. Supports partial exits."""

    id: int
    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_time: str  # ISO 8601 UTC
    entry_price: float
    quantity: float  # total quantity at entry
    exits: list[TradeExit]  # at least 1 element
    pnl: float  # gross PnL (sum over all exits)

    # Optional fields
    pnl_pct: float | None = None
    fees: float | None = None  # total fees (entry + all exits)
    net_pnl: float | None = None
    r_multiple: float | None = None
    mae: float | None = None  # Max Adverse Excursion
    mfe: float | None = None  # Max Favorable Excursion
    holding_bars: int | None = None  # entry to last exit
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "exits": [e.to_dict() for e in self.exits],
            "pnl": self.pnl,
        }
        # Only include optional fields if set
        for fld in ("pnl_pct", "fees", "net_pnl", "r_multiple", "mae", "mfe", "holding_bars"):
            val = getattr(self, fld)
            if val is not None:
                d[fld] = val
        if self.tags:
            d["tags"] = self.tags
        if self.meta:
            d["meta"] = self.meta
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Trade:
        return cls(
            id=d["id"],
            symbol=d["symbol"],
            direction=d["direction"],
            entry_time=d["entry_time"],
            entry_price=d["entry_price"],
            quantity=d["quantity"],
            exits=[TradeExit.from_dict(e) for e in d["exits"]],
            pnl=d["pnl"],
            pnl_pct=d.get("pnl_pct"),
            fees=d.get("fees"),
            net_pnl=d.get("net_pnl"),
            r_multiple=d.get("r_multiple"),
            mae=d.get("mae"),
            mfe=d.get("mfe"),
            holding_bars=d.get("holding_bars"),
            tags=d.get("tags", []),
            meta=d.get("meta", {}),
        )

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0

    @property
    def last_exit_time(self) -> str:
        """Time of the final exit."""
        return self.exits[-1].time if self.exits else self.entry_time

    @property
    def direction_sign(self) -> int:
        return 1 if self.direction == "LONG" else -1


# ─── Equity point ────────────────────────────────────────────────────────────


@dataclass
class EquityPoint:
    time: str
    equity: float

    def to_dict(self) -> dict[str, Any]:
        return {"time": self.time, "equity": self.equity}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EquityPoint:
        return cls(time=d["time"], equity=d["equity"])


# ─── Backtest metadata ──────────────────────────────────────────────────────


@dataclass
class BacktestMeta:
    """Metadata about a backtest run."""

    id: str  # unique id, e.g. "mr-rsi2-sp500-20260224-143022"
    created_at: str  # ISO 8601 UTC
    engine: str  # "vectorbt", "lean", "pynecore"

    # Strategie(n) — kann eine oder mehrere sein
    strategies: list[str] = field(default_factory=list)  # Namen der Strategien

    # Backtest-Zeitraum
    backtest_start: str = ""  # "2021-01-01"
    backtest_end: str = ""  # "2025-06-30"
    duration_days: int = 0
    total_bars: int = 0

    # Daten-Info
    data: dict = field(default_factory=dict)
    # {
    #     "timeframes": ["1h"],         # oder ["1h", "1d"] für Multi-TF
    #     "symbols": ["AAPL", "MSFT"],
    #     "universe": "sp500" | "forex-majors" | "custom" | "",
    #     "source": "yfinance" | "dukascopy" | "huggingface",
    # }

    # Capital
    initial_capital: float = 100_000.0
    currency: str = "USD"

    # Parameter (strategie-spezifisch)
    parameters: dict = field(default_factory=dict)

    # Tags für den Backtest selbst (nicht Strategie)
    tags: list[str] = field(default_factory=list)
    # z.B. ["paper-test", "optimized", "walk-forward", "multi-strategy"]

    # Notizen
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "created_at": self.created_at,
            "engine": self.engine,
            "strategies": self.strategies,
            "backtest_start": self.backtest_start,
            "backtest_end": self.backtest_end,
            "initial_capital": self.initial_capital,
        }
        if self.duration_days:
            d["duration_days"] = self.duration_days
        if self.total_bars:
            d["total_bars"] = self.total_bars
        if self.data:
            d["data"] = self.data
        if self.currency != "USD":
            d["currency"] = self.currency
        if self.parameters:
            d["parameters"] = self.parameters
        if self.tags:
            d["tags"] = self.tags
        if self.notes:
            d["notes"] = self.notes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BacktestMeta:
        return cls(
            id=d["id"],
            created_at=d["created_at"],
            engine=d["engine"],
            strategies=d.get("strategies", []),
            backtest_start=d.get("backtest_start", ""),
            backtest_end=d.get("backtest_end", ""),
            duration_days=d.get("duration_days", 0),
            total_bars=d.get("total_bars", 0),
            data=d.get("data", {}),
            initial_capital=d.get("initial_capital", 100_000.0),
            currency=d.get("currency", "USD"),
            parameters=d.get("parameters", {}),
            tags=d.get("tags", []),
            notes=d.get("notes", ""),
        )


# ─── Indicator series (optional, for viewer overlays) ─────────────────────────


@dataclass
class IndicatorSeries:
    """Time-series indicator data for viewer display.

    - type "overlay": drawn on the main price chart (EMA, SMA, SuperTrend, …)
    - type "panel":   separate sub-panel below chart (RSI, MACD, …) — Phase 3 rendering

    ``style`` is a dict with optional keys:
        color  (str)  — CSS color, e.g. "#58a6ff"
        width  (int)  — line width in px, default 1
        dash   (str)  — "solid" | "dashed" | "dotted", default "solid"
    """

    name: str
    type: str  # "overlay" | "panel"
    data: list[dict[str, Any]]  # [{"time": "ISO8601", "value": float}, ...]
    style: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "data": self.data, "style": self.style}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IndicatorSeries:
        return cls(name=d["name"], type=d["type"], data=d.get("data", []), style=d.get("style", {}))


# ─── Complete backtest result ────────────────────────────────────────────────


@dataclass
class BacktestResult:
    """Complete backtest output: meta + trades + optional equity_curve + stats."""

    meta: BacktestMeta
    trades: list[Trade]
    equity_curve: list[EquityPoint] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    indicators: list[IndicatorSeries] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "meta": self.meta.to_dict(),
            "trades": [t.to_dict() for t in self.trades],
        }
        if self.equity_curve:
            d["equity_curve"] = [e.to_dict() for e in self.equity_curve]
        if self.stats:
            d["stats"] = self.stats
        if self.indicators:
            d["indicators"] = [i.to_dict() for i in self.indicators]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BacktestResult:
        return cls(
            meta=BacktestMeta.from_dict(d["meta"]),
            trades=[Trade.from_dict(t) for t in d.get("trades", [])],
            equity_curve=[EquityPoint.from_dict(e) for e in d.get("equity_curve", [])],
            stats=d.get("stats", {}),
            indicators=[IndicatorSeries.from_dict(i) for i in d.get("indicators", [])],
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> BacktestResult:
        return cls.from_dict(json.loads(text))

    def save(self, path: str | Path) -> Path:
        """Save to JSON file. Returns the path written to."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        logger.info("Saved backtest result to %s", p)
        return p

    @classmethod
    def load(cls, path: str | Path) -> BacktestResult:
        """Load from JSON file."""
        p = Path(path)
        return cls.from_json(p.read_text(encoding="utf-8"))

    @property
    def filename(self) -> str:
        """Generate filename: {strategy}_{engine}_{YYYYMMDD-HHmmss}.json"""
        try:
            dt = datetime.fromisoformat(self.meta.created_at.replace("Z", "+00:00"))
            ts = dt.strftime("%Y%m%d-%H%M%S")
        except (ValueError, AttributeError):
            ts = "unknown"
        strategy_name = self.meta.strategies[0] if self.meta.strategies else "unknown"
        return f"{strategy_name}_{self.meta.engine}_{ts}.json"
