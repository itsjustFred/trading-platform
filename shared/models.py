"""
Shared data models for the universal trade format.

Defines the canonical JSON schema:
- StrategyMeta: Pydantic model for strategy metadata (validated)
- TradeExit: A single (partial) exit from a position (dataclass)
- Trade: One position from entry to full close (dataclass)
- BacktestMeta: Metadata about a backtest run (dataclass)
- BacktestResult: Complete backtest output (meta + trades + equity + stats)

Also includes canonical v2 signal/drawing schemas used to standardize strategy
execution semantics and chart object rendering across runner and viewer layers.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ─── Strategy metadata ──────────────────────────────────────────────────────


class StrategyMeta(BaseModel):
    """Metadaten für eine Strategie (unabhängig vom Backtest)."""

    model_config = ConfigDict(frozen=False)

    name: str
    version: str = "1.0.0"
    parameters: dict[str, Any] = Field(default_factory=dict)
    tags: dict[str, Any] = Field(default_factory=dict)
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
        if self.parameters:
            d["parameters"] = self.parameters
        if self.tags:
            d["tags"] = self.tags
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyMeta:
        return cls(
            name=d["name"],
            version=d.get("version", "1.0.0"),
            parameters=d.get("parameters", {}),
            tags=d.get("tags", {}),
        )


# ─── Timeframe constants ────────────────────────────────────────────────────

VALID_TIMEFRAMES = ("tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
VALID_DIRECTIONS = ("LONG", "SHORT")
VALID_ACTION_EVENTS = ("OPEN", "ADD", "REDUCE", "CLOSE", "REVERSE")
VALID_ORDER_TYPES = ("MARKET", "LIMIT", "STOP", "STOP_LIMIT")
VALID_SIZE_MODELS = ("PCT_POSITION", "ABS_QTY", "NOTIONAL", "RISK_PCT")
VALID_EXIT_LEG_KINDS = ("TP", "SL", "TRAIL", "TIME", "SIGNAL", "CUSTOM")
VALID_DRAWING_TYPES = ("marker", "line", "box", "triangle")
VALID_LINE_STYLES = ("solid", "dashed", "dotted")
VALID_TRIANGLE_SIZES = ("sm", "md", "lg")
TRIANGLE_SIZE_FACTORS = {"sm": 0.75, "md": 1.0, "lg": 1.35}


# ─── Canonical signal/execution schema (v2) ───────────────────────────────


@dataclass
class SignalOrder:
    """Canonical execution order event.

    Example representation for "Long(Limit, 50%)":
      action="OPEN", direction="LONG", order_type="LIMIT",
      size_model="PCT_POSITION", size_value=0.5
    """

    action: str
    direction: str
    order_type: str
    size_model: str
    size_value: float
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str | None = None
    reduce_only: bool = False
    label: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.action = str(self.action or "").upper()
        self.direction = str(self.direction or "").upper()
        self.order_type = str(self.order_type or "").upper()
        self.size_model = str(self.size_model or "").upper()

        if self.action not in VALID_ACTION_EVENTS:
            raise ValueError(f"Unsupported action '{self.action}'.")
        if self.direction not in VALID_DIRECTIONS:
            raise ValueError(f"Unsupported direction '{self.direction}'.")
        if self.order_type not in VALID_ORDER_TYPES:
            raise ValueError(f"Unsupported order_type '{self.order_type}'.")
        if self.size_model not in VALID_SIZE_MODELS:
            raise ValueError(f"Unsupported size_model '{self.size_model}'.")

        try:
            self.size_value = float(self.size_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("size_value must be numeric.") from exc

        if self.size_value <= 0:
            raise ValueError("size_value must be greater than 0.")

        if self.order_type in {"LIMIT", "STOP_LIMIT"} and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT and STOP_LIMIT orders.")
        if self.order_type in {"STOP", "STOP_LIMIT"} and self.stop_price is None:
            raise ValueError("stop_price is required for STOP and STOP_LIMIT orders.")

        if self.limit_price is not None:
            self.limit_price = float(self.limit_price)
            if self.limit_price <= 0:
                raise ValueError("limit_price must be greater than 0.")
        if self.stop_price is not None:
            self.stop_price = float(self.stop_price)
            if self.stop_price <= 0:
                raise ValueError("stop_price must be greater than 0.")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "direction": self.direction,
            "order_type": self.order_type,
            "size_model": self.size_model,
            "size_value": self.size_value,
        }
        if self.limit_price is not None:
            payload["limit_price"] = self.limit_price
        if self.stop_price is not None:
            payload["stop_price"] = self.stop_price
        if self.time_in_force:
            payload["time_in_force"] = self.time_in_force
        if self.reduce_only:
            payload["reduce_only"] = True
        if self.label:
            payload["label"] = self.label
        if self.meta:
            payload["meta"] = self.meta
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SignalOrder":
        return cls(
            action=payload["action"],
            direction=payload["direction"],
            order_type=payload["order_type"],
            size_model=payload["size_model"],
            size_value=payload["size_value"],
            limit_price=payload.get("limit_price"),
            stop_price=payload.get("stop_price"),
            time_in_force=payload.get("time_in_force"),
            reduce_only=payload.get("reduce_only", False),
            label=payload.get("label"),
            meta=payload.get("meta", {}),
        )


@dataclass
class ExitLeg:
    """Canonical leg for TP/SL/Trail/Time exits and partial closes."""

    kind: str
    quantity_pct: float | None = None
    price: float | None = None
    trail_amount: float | None = None
    priority: int = 100
    label: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = str(self.kind or "").upper()
        if self.kind not in VALID_EXIT_LEG_KINDS:
            raise ValueError(f"Unsupported exit leg kind '{self.kind}'.")

        if self.quantity_pct is not None:
            self.quantity_pct = float(self.quantity_pct)
            if self.quantity_pct <= 0 or self.quantity_pct > 1:
                raise ValueError("quantity_pct must be within (0, 1].")

        if self.price is not None:
            self.price = float(self.price)
            if self.price <= 0:
                raise ValueError("price must be greater than 0.")

        if self.trail_amount is not None:
            self.trail_amount = float(self.trail_amount)
            if self.trail_amount <= 0:
                raise ValueError("trail_amount must be greater than 0.")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "priority": self.priority}
        if self.quantity_pct is not None:
            payload["quantity_pct"] = self.quantity_pct
        if self.price is not None:
            payload["price"] = self.price
        if self.trail_amount is not None:
            payload["trail_amount"] = self.trail_amount
        if self.label:
            payload["label"] = self.label
        if self.meta:
            payload["meta"] = self.meta
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExitLeg":
        return cls(
            kind=payload["kind"],
            quantity_pct=payload.get("quantity_pct"),
            price=payload.get("price"),
            trail_amount=payload.get("trail_amount"),
            priority=payload.get("priority", 100),
            label=payload.get("label"),
            meta=payload.get("meta", {}),
        )


@dataclass
class SignalExecutionSchema:
    """Canonical execution schema for strategy runtime outputs."""

    schema_version: str = "2.0"
    direction: str = "LONG"
    entry: SignalOrder | None = None
    add_orders: list[SignalOrder] = field(default_factory=list)
    reduce_orders: list[SignalOrder] = field(default_factory=list)
    close_orders: list[SignalOrder] = field(default_factory=list)
    tp_legs: list[ExitLeg] = field(default_factory=list)
    sl_leg: ExitLeg | None = None
    trail_leg: ExitLeg | None = None
    time_exit: ExitLeg | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.direction = str(self.direction or "").upper()
        if self.direction not in VALID_DIRECTIONS:
            raise ValueError(f"Unsupported schema direction '{self.direction}'.")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "direction": self.direction,
            "add_orders": [order.to_dict() for order in self.add_orders],
            "reduce_orders": [order.to_dict() for order in self.reduce_orders],
            "close_orders": [order.to_dict() for order in self.close_orders],
            "tp_legs": [leg.to_dict() for leg in self.tp_legs],
        }
        if self.entry is not None:
            payload["entry"] = self.entry.to_dict()
        if self.sl_leg is not None:
            payload["sl_leg"] = self.sl_leg.to_dict()
        if self.trail_leg is not None:
            payload["trail_leg"] = self.trail_leg.to_dict()
        if self.time_exit is not None:
            payload["time_exit"] = self.time_exit.to_dict()
        if self.meta:
            payload["meta"] = self.meta
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SignalExecutionSchema":
        return cls(
            schema_version=payload.get("schema_version", "2.0"),
            direction=payload.get("direction", "LONG"),
            entry=SignalOrder.from_dict(payload["entry"]) if payload.get("entry") else None,
            add_orders=[SignalOrder.from_dict(order) for order in payload.get("add_orders", [])],
            reduce_orders=[SignalOrder.from_dict(order) for order in payload.get("reduce_orders", [])],
            close_orders=[SignalOrder.from_dict(order) for order in payload.get("close_orders", [])],
            tp_legs=[ExitLeg.from_dict(leg) for leg in payload.get("tp_legs", [])],
            sl_leg=ExitLeg.from_dict(payload["sl_leg"]) if payload.get("sl_leg") else None,
            trail_leg=ExitLeg.from_dict(payload["trail_leg"]) if payload.get("trail_leg") else None,
            time_exit=ExitLeg.from_dict(payload["time_exit"]) if payload.get("time_exit") else None,
            meta=payload.get("meta", {}),
        )


# ─── Canonical drawing schema (definitions + instances) ────────────────────


@dataclass
class DrawingDefinition:
    """Reusable drawing template with style/geometry defaults."""

    id: str
    type: str
    style: dict[str, Any] = field(default_factory=dict)
    geometry: dict[str, Any] = field(default_factory=dict)
    size_preset: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = str(self.type or "").lower()
        if self.type not in VALID_DRAWING_TYPES:
            raise ValueError(f"Unsupported drawing type '{self.type}'.")

        if self.type == "triangle" and self.size_preset is not None:
            preset = str(self.size_preset).lower()
            if preset not in VALID_TRIANGLE_SIZES:
                raise ValueError(f"Unsupported triangle size_preset '{self.size_preset}'.")
            self.size_preset = preset

        line_style = self.style.get("line_style")
        if line_style is not None and str(line_style).lower() not in VALID_LINE_STYLES:
            raise ValueError(f"Unsupported line_style '{line_style}'.")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "style": self.style,
            "geometry": self.geometry,
        }
        if self.size_preset is not None:
            payload["size_preset"] = self.size_preset
        if self.meta:
            payload["meta"] = self.meta
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DrawingDefinition":
        return cls(
            id=payload["id"],
            type=payload["type"],
            style=payload.get("style", {}),
            geometry=payload.get("geometry", {}),
            size_preset=payload.get("size_preset"),
            meta=payload.get("meta", {}),
        )


@dataclass
class DrawingInstance:
    """Placement-only instance that references a drawing definition."""

    definition_id: str
    x: float
    y: float
    rotation: float = 0.0
    size: str | None = None
    scale_override: float | None = None
    symbol: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.x = float(self.x)
        self.y = float(self.y)
        self.rotation = float(self.rotation)
        if self.scale_override is not None:
            self.scale_override = float(self.scale_override)
            if self.scale_override <= 0:
                raise ValueError("scale_override must be greater than 0.")

        if self.size is not None:
            normalized = str(self.size).lower()
            if normalized not in VALID_TRIANGLE_SIZES:
                raise ValueError(f"Unsupported size '{self.size}'.")
            self.size = normalized

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "definition_id": self.definition_id,
            "x": self.x,
            "y": self.y,
            "rotation": self.rotation,
        }
        if self.size is not None:
            payload["size"] = self.size
        if self.scale_override is not None:
            payload["scale_override"] = self.scale_override
        if self.symbol:
            payload["symbol"] = self.symbol
        if self.overrides:
            payload["overrides"] = self.overrides
        if self.meta:
            payload["meta"] = self.meta
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DrawingInstance":
        return cls(
            definition_id=payload["definition_id"],
            x=payload["x"],
            y=payload["y"],
            rotation=payload.get("rotation", 0.0),
            size=payload.get("size"),
            scale_override=payload.get("scale_override"),
            symbol=payload.get("symbol"),
            overrides=payload.get("overrides", {}),
            meta=payload.get("meta", {}),
        )


@dataclass
class DrawingSchema:
    """Canonical drawing schema with reusable templates and placed instances."""

    schema_version: str = "2.0"
    definitions: list[DrawingDefinition] = field(default_factory=list)
    instances: list[DrawingInstance] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "definitions": [definition.to_dict() for definition in self.definitions],
            "instances": [instance.to_dict() for instance in self.instances],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DrawingSchema":
        return cls(
            schema_version=payload.get("schema_version", "2.0"),
            definitions=[
                DrawingDefinition.from_dict(definition)
                for definition in payload.get("definitions", [])
            ],
            instances=[
                DrawingInstance.from_dict(instance)
                for instance in payload.get("instances", [])
            ],
        )


def create_triangle_definition(
    definition_id: str,
    *,
    default_size: str = "md",
    line_style: str = "solid",
    line_width: int = 1,
    color: str = "#f59e0b",
    fill_color: str | None = None,
) -> DrawingDefinition:
    """Create a reusable normal-triangle template.

    Placement can later be done by x/y/rotation and size (sm/md/lg).
    """

    normalized_size = str(default_size).lower()
    if normalized_size not in VALID_TRIANGLE_SIZES:
        raise ValueError(f"Unsupported default_size '{default_size}'.")

    normalized_style = str(line_style).lower()
    if normalized_style not in VALID_LINE_STYLES:
        raise ValueError(f"Unsupported line_style '{line_style}'.")

    style = {
        "line_style": normalized_style,
        "line_width": int(line_width),
        "color": color,
    }
    if fill_color:
        style["fill_color"] = fill_color

    geometry = {
        # Unit triangle centered around origin; scaled at placement time.
        "points": [(0.0, 1.0), (-0.866, -0.5), (0.866, -0.5)],
    }

    return DrawingDefinition(
        id=definition_id,
        type="triangle",
        style=style,
        geometry=geometry,
        size_preset=normalized_size,
    )


def place_triangle_instance(
    definition_id: str,
    x: float,
    y: float,
    *,
    rotation: float = 0.0,
    size: str = "md",
    symbol: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> DrawingInstance:
    """Place a triangle by only passing x/y/rotation/size."""

    normalized_size = str(size).lower()
    if normalized_size not in VALID_TRIANGLE_SIZES:
        raise ValueError(f"Unsupported size '{size}'.")

    scale = TRIANGLE_SIZE_FACTORS[normalized_size]
    return DrawingInstance(
        definition_id=definition_id,
        x=float(x),
        y=float(y),
        rotation=float(rotation),
        size=normalized_size,
        scale_override=scale,
        symbol=symbol,
        overrides=overrides or {},
    )


def parse_signal_order_shorthand(
    text: str,
    *,
    action: str = "OPEN",
) -> SignalOrder:
    """Parse shorthand like "Long(Limit, 50, 101.25)" into SignalOrder.

    Supported forms:
      - Long(Market, 50)
      - Short(Market, 25%)
      - Long(Limit, 50, 101.25)
      - Short(Stop, 30, 98.4)
      - Long(StopLimit, 40, 99.5, 99.8)

    Size defaults to PCT_POSITION. Values > 1 are interpreted as percent points
    (e.g., 50 -> 0.5).
    """

    raw = str(text or "").strip()
    match = re.match(r"^(Long|Short)\s*\((.*)\)$", raw, flags=re.IGNORECASE)
    if not match:
        raise ValueError(
            "Invalid shorthand. Expected format like 'Long(Limit, 50, 101.25)'."
        )

    direction = match.group(1).upper()
    body = match.group(2).strip()
    parts = [part.strip() for part in body.split(",") if part.strip()]
    if len(parts) < 2:
        raise ValueError("Shorthand must include order type and size.")

    order_token = parts[0].replace("_", "").replace("-", "").upper()
    order_map = {
        "MARKET": "MARKET",
        "LIMIT": "LIMIT",
        "STOP": "STOP",
        "STOPLIMIT": "STOP_LIMIT",
    }
    order_type = order_map.get(order_token)
    if order_type is None:
        raise ValueError(f"Unsupported order type token '{parts[0]}'.")

    size_token = parts[1]
    is_percent = size_token.endswith("%")
    size_numeric = size_token[:-1] if is_percent else size_token
    try:
        size_value = float(size_numeric)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid size token '{size_token}'.") from exc

    if is_percent or size_value > 1:
        size_value = size_value / 100.0

    if size_value <= 0 or size_value > 1:
        raise ValueError("Parsed position size must be within (0, 1].")

    stop_price: float | None = None
    limit_price: float | None = None

    def _parse_price(idx: int, label: str) -> float:
        if len(parts) <= idx:
            raise ValueError(f"Missing {label} price for {order_type} order.")
        try:
            price = float(parts[idx])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid {label} price token '{parts[idx]}'.") from exc
        if price <= 0:
            raise ValueError(f"{label} price must be greater than 0.")
        return price

    if order_type == "LIMIT":
        limit_price = _parse_price(2, "limit")
    elif order_type == "STOP":
        stop_price = _parse_price(2, "stop")
    elif order_type == "STOP_LIMIT":
        stop_price = _parse_price(2, "stop")
        limit_price = _parse_price(3, "limit")

    return SignalOrder(
        action=action,
        direction=direction,
        order_type=order_type,
        size_model="PCT_POSITION",
        size_value=size_value,
        limit_price=limit_price,
        stop_price=stop_price,
        label=raw,
    )


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


@dataclass
class BacktestStats:
    """Canonical backtest statistics shared across runner and viewer layers."""

    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    profit_factor: float | None = None
    total_pnl: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_holding_bars: float | None = None
    max_drawdown_pct: float = 0.0
    max_drawdown_duration: int = 0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    cagr: float = 0.0
    total_return: float = 0.0
    psr: float | None = None
    p_value: float = 1.0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "total_trades": self.total_trades,
            "winners": self.winners,
            "losers": self.losers,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "total_pnl": self.total_pnl,
            "avg_winner": self.avg_winner,
            "avg_loser": self.avg_loser,
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_duration": self.max_drawdown_duration,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "calmar_ratio": self.calmar_ratio,
            "cagr": self.cagr,
            "total_return": self.total_return,
            "p_value": self.p_value,
        }
        if self.avg_holding_bars is not None:
            data["avg_holding_bars"] = self.avg_holding_bars
        if self.psr is not None:
            data["psr"] = self.psr
        data.update(self.extras)
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BacktestStats":
        field_names = {item.name for item in dataclass_fields(cls) if item.name != "extras"}
        payload = {key: value for key, value in d.items() if key in field_names}
        payload["extras"] = {key: value for key, value in d.items() if key not in field_names}
        return cls(**payload)


@dataclass
class PortfolioStats:
    """Canonical portfolio statistics for persisted combined portfolio metrics."""

    sharpe: float = 0.0
    sortino: float = 0.0
    cagr: float = 0.0
    max_drawdown: float = 0.0
    net_profit: float = 0.0
    win_rate: float = 0.0
    profit_factor: float | None = None
    calmar: float = 0.0
    total_return: float = 0.0
    max_drawdown_duration: int = 0
    psr: float | None = None
    p_value: float = 1.0
    correlation_matrix: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "cagr": self.cagr,
            "max_drawdown": self.max_drawdown,
            "net_profit": self.net_profit,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "calmar": self.calmar,
            "total_return": self.total_return,
            "max_drawdown_duration": self.max_drawdown_duration,
            "p_value": self.p_value,
            "correlation_matrix": self.correlation_matrix,
        }
        if self.psr is not None:
            data["psr"] = self.psr
        data.update(self.extras)
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PortfolioStats":
        field_names = {item.name for item in dataclass_fields(cls) if item.name != "extras"}
        payload = {key: value for key, value in d.items() if key in field_names}
        payload["extras"] = {key: value for key, value in d.items() if key not in field_names}
        return cls(**payload)


# ─── Complete backtest result ────────────────────────────────────────────────


@dataclass
class BacktestResult:
    """Complete backtest output: meta + trades + optional equity_curve + stats."""

    meta: BacktestMeta
    trades: list[Trade]
    equity_curve: list[EquityPoint] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    indicators: list[IndicatorSeries] = field(default_factory=list)
    drawings: list[dict[str, Any]] = field(default_factory=list)

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
        if self.drawings:
            d["drawings"] = self.drawings
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BacktestResult:
        return cls(
            meta=BacktestMeta.from_dict(d["meta"]),
            trades=[Trade.from_dict(t) for t in d.get("trades", [])],
            equity_curve=[EquityPoint.from_dict(e) for e in d.get("equity_curve", [])],
            stats=d.get("stats", {}),
            indicators=[IndicatorSeries.from_dict(i) for i in d.get("indicators", [])],
            drawings=d.get("drawings", []),
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
