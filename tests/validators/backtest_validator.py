from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class BacktestExpectation:
    required_stats_keys: list[str] = field(
        default_factory=lambda: [
            "total_pnl",
            "sharpe_ratio",
            "cagr",
            "max_drawdown_pct",
            "win_rate",
            "total_trades",
        ]
    )
    min_trades: int = 0
    sharpe_min: float = -2.0
    sharpe_max: float = 10.0


class BacktestResultValidator:
    def validate(self, result: dict[str, Any], expectation: BacktestExpectation) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []

        stats = result.get("stats")
        if not isinstance(stats, dict):
            errors.append("Missing or invalid 'stats' object.")
            stats = {}

        errors.extend(
            f"Missing required stats key: {key}"
            for key in expectation.required_stats_keys
            if key not in stats
        )

        for key, value in stats.items():
            if isinstance(value, bool):
                continue
            # String-typed "NaN"/"Infinity" must not bypass the numeric
            # checks: coerce every scalar stat through float().
            if isinstance(value, str):
                try:
                    value = float(value.strip())
                except ValueError:
                    errors.append(f"Stat '{key}' is not a finite number: {value!r}")
                    continue
            if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                errors.append(f"Stat '{key}' contains NaN/Inf.")

        total_trades = self._as_int(stats.get("total_trades", result.get("trades", [])))
        if total_trades < expectation.min_trades:
            errors.append(
                f"Trade count below expectation: {total_trades} < {expectation.min_trades}"
            )

        sharpe = self._as_float(stats.get("sharpe_ratio"))
        if sharpe is None:
            errors.append("Missing or invalid sharpe_ratio.")
        elif sharpe < expectation.sharpe_min:
            errors.append(f"Sharpe ratio out of range: {sharpe} < {expectation.sharpe_min}")
        elif sharpe > expectation.sharpe_max:
            warnings.append(
                f"Suspicious Sharpe ratio {sharpe} > {expectation.sharpe_max}; "
                "possible lookahead bias."
            )

        trades = result.get("trades")
        if isinstance(trades, list):
            for idx, trade in enumerate(trades):
                if not isinstance(trade, dict):
                    continue
                entry_time = self._parse_time(trade.get("entry_time"))
                exit_time = self._parse_time(trade.get("exit_time"))
                if entry_time is None or exit_time is None:
                    continue
                if exit_time < entry_time:
                    errors.append(f"Trade[{idx}] has exit_time before entry_time.")
        else:
            errors.append("Missing or invalid 'trades' list.")

        score = max(0.0, min(1.0, round(1.0 - (0.2 * len(errors)) - (0.05 * len(warnings)), 3)))
        return {
            "passed": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "score": score,
        }

    @staticmethod
    def _as_int(value: Any) -> int:
        if isinstance(value, list):
            return len(value)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
