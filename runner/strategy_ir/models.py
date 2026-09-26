"""
IR models for strategy conversion.

The StrategyIR schema is the typed intermediate representation between
intent extraction and deterministic engine code generation.
"""

from __future__ import annotations

from enum import Enum
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ParameterValueType(str, Enum):
    """Supported value categories for tunable parameters."""

    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "str"
    CHOICE = "choice"
    ANY = "any"


class ConditionSide(str, Enum):
    """Direction scope for a condition."""

    LONG = "long"
    SHORT = "short"
    BOTH = "both"


class ConditionWhen(str, Enum):
    """Evaluation stage of a condition."""

    ENTRY = "entry"
    EXIT = "exit"
    FILTER = "filter"
    RISK = "risk"


class ExitSignalKind(str, Enum):
    """Semantic type for an exit signal."""

    CLOSE = "close"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_EXIT = "time_exit"
    CUSTOM = "custom"


class DrawingKind(str, Enum):
    """Supported viewer drawing primitives."""

    LINE = "line"
    BAND = "band"
    MARKER = "marker"
    LABEL = "label"
    BOX = "box"
    HISTOGRAM = "histogram"
    SCATTER = "scatter"
    CUSTOM = "custom"


class DrawingPanel(str, Enum):
    """Target panel for drawing output."""

    OVERLAY = "overlay"
    PANEL = "panel"


class RawExpressionPurpose(str, Enum):
    """Role of an unstructured escape-hatch expression."""

    ENTRY = "entry"
    EXIT = "exit"
    INDICATOR = "indicator"
    RISK = "risk"
    DRAWING = "drawing"
    OTHER = "other"


class RawExpressionLanguage(str, Enum):
    """Source language of a raw expression payload."""

    PYTHON = "python"
    PINE = "pine"
    CSHARP = "csharp"
    PSEUDO = "pseudo"
    OTHER = "other"


class EngineHint(str, Enum):
    """Preferred target engine for deterministic codegen."""

    VECTORBT = "vectorbt"
    PYNECORE = "pynecore"
    LEAN = "lean"
    UNKNOWN = "unknown"


class ParameterSpec(BaseModel):
    """Tunable strategy parameter in typed IR form."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    value_type: ParameterValueType = ParameterValueType.ANY
    default: Any = None
    required: bool = False
    description: str = ""
    min_value: float | None = None
    max_value: float | None = None
    choices: list[Any] = Field(default_factory=list)
    tags: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Parameter name must not be empty.")
        return normalized

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ParameterSpec":
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError("Parameter min_value must be <= max_value.")
        if self.value_type == ParameterValueType.CHOICE and not self.choices:
            raise ValueError("Choice parameters must declare at least one option.")
        return self


class IndicatorRef(BaseModel):
    """Indicator reference discovered from source logic."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    source: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    output: str | None = None
    role: str = "aux"
    timeframe: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Indicator name must not be empty.")
        return normalized


class Condition(BaseModel):
    """Boolean condition for entry/exit/filter/risk logic."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str | None = None
    expression: str
    description: str = ""
    side: ConditionSide = ConditionSide.BOTH
    when: ConditionWhen = ConditionWhen.ENTRY
    references: list[str] = Field(default_factory=list)
    raw: str | None = None

    @field_validator("expression")
    @classmethod
    def _validate_expression(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Condition expression must not be empty.")
        return normalized


class ExitSignal(BaseModel):
    """Structured exit rule extracted from source strategy logic."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    condition: str
    kind: ExitSignalKind = ExitSignalKind.CUSTOM
    side: ConditionSide = ConditionSide.BOTH
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "condition")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Exit signal text fields must not be empty.")
        return normalized


class Drawing(BaseModel):
    """Viewer-aligned drawing descriptor inferred from strategy logic."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    kind: DrawingKind = DrawingKind.CUSTOM
    panel: DrawingPanel = DrawingPanel.OVERLAY
    series: str | None = None
    color: str | None = None
    width: int = Field(default=1, ge=1)
    style: dict[str, Any] = Field(default_factory=dict)
    source_expression: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Drawing name must not be empty.")
        return normalized


class StateTransition(BaseModel):
    """Transition edge inside a strategy state machine."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    from_state: str
    to_state: str
    condition: str

    @field_validator("from_state", "to_state", "condition")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("State transition fields must not be empty.")
        return normalized


class StateMachine(BaseModel):
    """Optional finite-state model for complex strategy flows."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    states: list[str] = Field(default_factory=list)
    initial_state: str
    transitions: list[StateTransition] = Field(default_factory=list)

    @field_validator("states")
    @classmethod
    def _validate_states(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if not normalized:
            raise ValueError("StateMachine must contain at least one state.")
        if len(set(normalized)) != len(normalized):
            raise ValueError("StateMachine states must be unique.")
        return normalized

    @model_validator(mode="after")
    def _validate_transitions(self) -> "StateMachine":
        if self.initial_state not in self.states:
            raise ValueError("StateMachine initial_state must exist in states.")
        allowed = set(self.states)
        for transition in self.transitions:
            if transition.from_state not in allowed:
                raise ValueError("StateMachine transition from_state must exist in states.")
            if transition.to_state not in allowed:
                raise ValueError("StateMachine transition to_state must exist in states.")
        return self


class RawExpression(BaseModel):
    """Escape hatch for logic not fully representable in typed IR yet."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    expression: str
    purpose: RawExpressionPurpose = RawExpressionPurpose.OTHER
    language: RawExpressionLanguage = RawExpressionLanguage.PYTHON
    notes: str = ""

    @field_validator("name", "expression")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("RawExpression name and expression must not be empty.")
        return normalized


class StrategyIR(BaseModel):
    """Typed intermediate representation for strategy conversion."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    indicators: list[IndicatorRef] = Field(default_factory=list)
    entry_conditions: list[Condition] = Field(default_factory=list)
    exit_signals: list[ExitSignal] = Field(default_factory=list)
    drawings: list[Drawing] = Field(default_factory=list)
    parameters: list[ParameterSpec] = Field(default_factory=list)
    state_machine: StateMachine | None = None
    raw_expressions: list[RawExpression] = Field(default_factory=list)
    # C6: presence heuristic (0.25 per populated part), NOT a calibrated
    # probability — 0.0 with empty parts is the explicit-unknown state.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    fallback_code: str = ""
    engine_hint: EngineHint = EngineHint.UNKNOWN
    source_summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("StrategyIR name must not be empty.")
        return normalized

    @model_validator(mode="after")
    def _validate_parameter_names(self) -> "StrategyIR":
        names = [parameter.name for parameter in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("StrategyIR parameter names must be unique.")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize StrategyIR as JSON-compatible dictionary."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StrategyIR":
        """Parse StrategyIR from dictionary payload."""
        return cls.model_validate(payload)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize StrategyIR to JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> "StrategyIR":
        """Parse StrategyIR from JSON string."""
        return cls.from_dict(json.loads(payload))
