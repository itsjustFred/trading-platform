"""cTrader deployment planning from persisted backtest performance."""
from __future__ import annotations

import math
from typing import Any

from shared.models import LiveAccountConfig, LiveDeploymentConfig


def _metric(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(key, default))
    except (TypeError, ValueError):
        return default
    return default if math.isnan(value) else value


def _has_oos_evidence(row: dict[str, Any]) -> bool:
    """Whether a backtest row carries out-of-sample evidence (C5).

    Accepts a positive `oos_sharpe` or an explicit `oos_status == "available"`
    (the optimizer's vocabulary). In-sample-only rows have no business on a
    live shortlist.
    """
    try:
        if float(row.get("oos_sharpe") or 0.0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return str(row.get("oos_status") or "").strip().lower() == "available"


def score_backtest_for_live(row: dict[str, Any]) -> float:
    """Rank backtests for live deployment using robust performance metrics.

    C5: every term is unit-normalized (Sharpe/3, CAGR clamped to [-1, 1],
    PSR already 0..1, drawdown penalty 0..1) so no single raw scale
    dominates the way unnormalized CAGR did.
    """
    if row.get("ruined") is True:
        # #132: equity hit zero, so Sharpe/PSR are undefined (null). Reading
        # them as 0.0 would still let a blown-up run be ranked; never deploy it.
        return float("-inf")
    sharpe = _metric(row, "sharpe_ratio")
    cagr = min(max(_metric(row, "cagr"), -1.0), 1.0)
    psr = min(max(_metric(row, "psr"), 0.0), 1.0)
    max_drawdown = abs(_metric(row, "max_drawdown"))
    trade_count = _metric(row, "trade_count")
    if trade_count <= 0:
        return float("-inf")
    drawdown_penalty = min(max_drawdown, 100.0) / 100.0
    return (sharpe / 3.0) + cagr + psr - drawdown_penalty


def top_backtests_for_ctrader(
    backtests: list[dict[str, Any]],
    *,
    limit: int = 5,
    require_oos_evidence: bool = True,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in backtests:
        symbols = [str(symbol).strip().upper() for symbol in row.get("symbols", []) if str(symbol).strip()]
        if not symbols:
            continue
        # C5: in-sample top-N is overfit-friendly — OOS evidence required
        # unless explicitly opted out.
        if require_oos_evidence and not _has_oos_evidence(row):
            continue
        score = score_backtest_for_live(row)
        if score == float("-inf"):
            continue
        payload = dict(row)
        payload["live_deployment_score"] = round(score, 6)
        payload["symbols"] = symbols
        candidates.append(payload)
    candidates.sort(
        key=lambda item: (
            _metric(item, "live_deployment_score"),
            _metric(item, "sharpe_ratio"),
            _metric(item, "trade_count"),
            str(item.get("created_at") or ""),
        ),
        reverse=True,
    )
    return candidates[: max(1, int(limit))]


def build_ctrader_deployment_plan(
    *,
    backtests: list[dict[str, Any]],
    accounts: list[LiveAccountConfig],
    limit: int = 5,
    status: str = "PAUSED",
    require_oos_evidence: bool = True,
) -> list[LiveDeploymentConfig]:
    ctrader_accounts = [account for account in accounts if account.broker == "ctrader"]
    if not ctrader_accounts:
        return []
    account = ctrader_accounts[0]
    deployments: list[LiveDeploymentConfig] = []
    for row in top_backtests_for_ctrader(
        backtests, limit=limit, require_oos_evidence=require_oos_evidence
    ):
        strategy_name = str(row.get("strategy_name") or "").strip() or str(row.get("id") or "")
        timeframe = str(row.get("timeframe") or "").strip() or "1h"
        deployments.append(
            LiveDeploymentConfig(
                strategy_name=strategy_name,
                account_id=account.id,
                symbols=list(row["symbols"]),
                timeframe=timeframe,
                status=status if status in {"ACTIVE", "PAUSED", "STOPPED"} else "PAUSED",
                signal_source_type="internal",
                signal_source="internal",
                signal_config={
                    "source_backtest_id": str(row.get("id") or ""),
                    "live_deployment_score": row.get("live_deployment_score"),
                },
                settings={
                    "auto_selected_for": "ctrader_top5",
                    "source_backtest_id": str(row.get("id") or ""),
                    "selection_metrics": {
                        "sharpe_ratio": row.get("sharpe_ratio"),
                        "cagr": row.get("cagr"),
                        "psr": row.get("psr"),
                        "max_drawdown": row.get("max_drawdown"),
                        "trade_count": row.get("trade_count"),
                    },
                },
            )
        )
    return deployments
