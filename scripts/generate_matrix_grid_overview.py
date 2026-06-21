from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev

BASE = Path('/mnt/block-volume-data/backtests/matrix_runner_v2')
REPORT_PATH = BASE / 'matrix_run_report.json'
OUT_PATH = Path('/home/ubuntu/Desktop/trading-platform/frontend/public/matrix-grid-overview.json')

TF_ORDER = {'1m': 1, '5m': 2, '15m': 3, '1h': 4, '4h': 5, '1d': 6}


def as_float(value, default=None):
    try:
        if value is None:
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def parse_minutes(started_at, finished_at):
    if not started_at or not finished_at:
        return None
    try:
        sd = datetime.fromisoformat(started_at.replace('+00:00', 'Z'))
        fd = datetime.fromisoformat(finished_at.replace('+00:00', 'Z'))
        return max(0, (fd - sd).total_seconds() / 60)
    except Exception:
        return None


def normal_cdf(x):
    sign = 1 if x >= 0 else -1
    ax = abs(x) / math.sqrt(2)
    t = 1 / (1 + 0.3275911 * ax)
    erf = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * math.exp(-ax * ax)
    return 0.5 * (1 + sign * erf)


def compute_psr_from_trades(output_path: str, capital: float | None) -> float | None:
    path = Path(output_path)
    if not path.exists():
        return None
    try:
        with path.open('r', encoding='utf-8') as fh:
            payload = json.load(fh)
    except Exception:
        return None
    trades = payload.get('trades') if isinstance(payload, dict) else None
    if not isinstance(trades, list):
        return None
    meta_value = payload.get('meta')
    meta = meta_value if isinstance(meta_value, dict) else {}
    base_capital = as_float(meta.get('initial_capital'), None) or as_float(capital, 100000.0) or 1.0
    returns = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        pnl = as_float(trade.get('pnl'), None)
        if pnl is None:
            continue
        returns.append(pnl / base_capital)
    n = len(returns)
    if n < 3:
        return None
    avg = mean(returns)
    std = pstdev(returns)
    if std == 0 or not math.isfinite(std):
        return None
    sharpe = avg / std * math.sqrt(252)
    skew = sum(((r - avg) / std) ** 3 for r in returns) / n
    kurt = sum(((r - avg) / std) ** 4 for r in returns) / n
    variance = (1 - skew * sharpe + ((kurt - 1) * sharpe * sharpe) / 4) / n
    if variance <= 0 or not math.isfinite(variance):
        return None
    psr = normal_cdf(sharpe / math.sqrt(variance))
    return psr if math.isfinite(psr) else None


def safe_round(value, digits=4):
    return round(value, digits) if value is not None and math.isfinite(value) else None


def build_summary(rows):
    if not rows:
        return None
    valid_sharpe = [r['sharpe'] for r in rows if r.get('sharpe') is not None and math.isfinite(r['sharpe'])]
    valid_cagr = [r['cagr'] for r in rows if r.get('cagr') is not None and math.isfinite(r['cagr'])]
    valid_psr = [r['psr'] for r in rows if r.get('psr') is not None and math.isfinite(r['psr'])]
    valid_dd = [r['max_drawdown_pct'] for r in rows if r.get('max_drawdown_pct') is not None and math.isfinite(r['max_drawdown_pct'])]
    valid_profit = [r['net_profit_pct'] for r in rows if r.get('net_profit_pct') is not None and math.isfinite(r['net_profit_pct'])]
    best_sharpe_row = max(rows, key=lambda r: r['sharpe'] if r['sharpe'] is not None else -1e99)
    best_cagr_row = max(rows, key=lambda r: r['cagr'] if r['cagr'] is not None else -1e99)
    best_psr_row = max(rows, key=lambda r: r['psr'] if r['psr'] is not None else -1e99)
    worst_dd_row = min(rows, key=lambda r: r['max_drawdown_pct'] if r['max_drawdown_pct'] is not None else 1e99)
    best_profit_row = max(rows, key=lambda r: r['net_profit_pct'] if r['net_profit_pct'] is not None else -1e99)
    return {
        'count': len(rows),
        'total_trades': sum(int(r.get('total_trades') or 0) for r in rows),
        'total_pnl': safe_round(sum(float(r.get('total_pnl') or 0) for r in rows), 2),
        'avg_sharpe': safe_round(mean(valid_sharpe), 4) if valid_sharpe else None,
        'best_sharpe': safe_round(max(valid_sharpe), 4) if valid_sharpe else None,
        'best_sharpe_combo': {'symbol': best_sharpe_row['symbol'], 'timeframe': best_sharpe_row['timeframe']},
        'avg_cagr': safe_round(mean(valid_cagr), 4) if valid_cagr else None,
        'best_cagr': safe_round(max(valid_cagr), 4) if valid_cagr else None,
        'best_cagr_combo': {'symbol': best_cagr_row['symbol'], 'timeframe': best_cagr_row['timeframe']},
        'avg_psr': safe_round(mean(valid_psr), 4) if valid_psr else None,
        'best_psr': safe_round(max(valid_psr), 4) if valid_psr else None,
        'best_psr_combo': {'symbol': best_psr_row['symbol'], 'timeframe': best_psr_row['timeframe']},
        'avg_max_drawdown_pct': safe_round(mean(valid_dd), 4) if valid_dd else None,
        'worst_max_drawdown_pct': safe_round(min(valid_dd), 4) if valid_dd else None,
        'worst_drawdown_combo': {'symbol': worst_dd_row['symbol'], 'timeframe': worst_dd_row['timeframe']},
        'avg_net_profit_pct': safe_round(mean(valid_profit), 4) if valid_profit else None,
        'best_net_profit_pct': safe_round(max(valid_profit), 4) if valid_profit else None,
        'best_profit_combo': {'symbol': best_profit_row['symbol'], 'timeframe': best_profit_row['timeframe']},
    }


with REPORT_PATH.open('r', encoding='utf-8') as fh:
    report = json.load(fh)

symbols_seen: set[str] = set()
strategy_summaries = {}
rows = []
blocked = []

for item in report.get('strategies', []):
    strategy = item.get('strategy')
    if not strategy:
        continue
    status = item.get('status')
    if status != 'done':
        blocked.append({
            'strategy': strategy,
            'status': status,
            'ok': item.get('ok'),
            'failed': item.get('failed'),
            'requested': item.get('requested'),
            'trades': item.get('trades'),
            'blocked_reason': item.get('blocked_reason'),
            'updated_at': item.get('updated_at'),
        })
        continue
    summary_path = BASE / strategy / 'grid_summary.json'
    if not summary_path.exists():
        blocked.append({
            'strategy': strategy,
            'status': 'missing_grid_summary',
            'ok': item.get('ok'),
            'failed': item.get('failed'),
            'requested': item.get('requested'),
            'trades': item.get('trades'),
            'blocked_reason': f'{summary_path} not found',
            'updated_at': item.get('updated_at'),
        })
        continue
    with summary_path.open('r', encoding='utf-8') as fh:
        grid = json.load(fh)
    capital = as_float(grid.get('capital'), 100000.0)
    strategy_rows = []
    for run in grid.get('runs', []):
        if not isinstance(run, dict):
            continue
        symbol = run.get('symbol') or ''
        tf = run.get('timeframe') or ''
        symbols_seen.add(symbol)
        duration = parse_minutes(run.get('started_at'), run.get('finished_at'))
        psr = compute_psr_from_trades(run.get('output') or '', capital)
        row = {
            'strategy': strategy,
            'status': run.get('status'),
            'symbol': symbol,
            'timeframe': tf,
            'run_id': run.get('run_id'),
            'output': run.get('output'),
            'started_at': run.get('started_at'),
            'finished_at': run.get('finished_at'),
            'duration_minutes': round(duration, 2) if duration is not None else None,
            'total_trades': int(run.get('total_trades') or 0),
            'total_pnl': safe_round(as_float(run.get('total_pnl'), 0), 2),
            'net_profit_pct': safe_round(as_float(run.get('net_profit_pct'), 0), 4),
            'sharpe': safe_round(as_float(run.get('sharpe'), None), 4),
            'cagr': safe_round(as_float(run.get('cagr'), None), 4),
            'max_drawdown_pct': safe_round(as_float(run.get('max_drawdown_pct'), None), 4),
            'win_rate': safe_round(as_float(run.get('win_rate'), None), 4),
            'psr': safe_round(psr, 4),
            'daily_return_count': run.get('daily_return_count'),
            'returncode': run.get('returncode'),
            'matrix_csv': grid.get('matrix_csv'),
        }
        rows.append(row)
        strategy_rows.append(row)
    summary = build_summary(strategy_rows)
    if summary:
        summary.update({
            'strategy': strategy,
            'status': status,
            'ok': item.get('ok'),
            'failed': item.get('failed'),
            'skipped_existing': item.get('skipped_existing'),
            'requested': item.get('requested'),
            'trades': item.get('trades'),
            'completed_at': item.get('completed_at'),
            'updated_at': item.get('updated_at'),
            'matrix_csv': strategy_rows[0].get('matrix_csv') if strategy_rows else None,
        })
    strategy_summaries[strategy] = summary

symbol_order = {symbol: idx for idx, symbol in enumerate(sorted(symbols_seen))}
strategy_order = [s['strategy'] for s in strategy_summaries.values()]
rows.sort(key=lambda r: (strategy_order.index(r['strategy']), symbol_order.get(r['symbol'], 999), TF_ORDER.get(r['timeframe'], 99), r['symbol'], r['timeframe']))
summaries = [strategy_summaries[k] for k in sorted(strategy_summaries, key=lambda s: strategy_order.index(s))]

overview = {
    'generated_at': datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
    'source': str(BASE),
    'report_started_at': report.get('started_at'),
    'report_updated_at': report.get('updated_at'),
    'counts': {
        'strategies_done': len(summaries),
        'strategies_blocked': len(blocked),
        'rows': len(rows),
        'ok_rows': sum(1 for r in rows if r.get('status') == 'ok'),
        'failed_rows': sum(1 for r in rows if r.get('status') == 'failed'),
        'symbols': sorted(symbols_seen),
    },
    'strategy_summaries': summaries,
    'blocked_strategies': blocked,
    'rows': rows,
}

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
with OUT_PATH.open('w', encoding='utf-8') as fh:
    json.dump(overview, fh, indent=2, ensure_ascii=False)

top = sorted(rows, key=lambda r: r['sharpe'] if r['sharpe'] is not None else -1e99, reverse=True)[:5]
print(json.dumps({'out': str(OUT_PATH), 'counts': overview['counts'], 'top_by_sharpe': top, 'blocked': blocked}, indent=2, ensure_ascii=False))
