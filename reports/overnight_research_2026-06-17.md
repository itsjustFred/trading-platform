# Overnight Research - Trading Platform

## Status (2026-06-17)

### Heute Erledigt
- Portfolio Optimizer SQLite Integration ✅
- Portfolio Optimizer OOS Support ✅
- Grid-Tester SQLite Integration ✅
- OOS Tests (8 tests passing) ✅

### Offene Punkte
1. Walk-Forward CLI Command (inspired by optimizer.py)
2. Dukascopy Downloader Parallelisierung
3. Viewer-UI Tests
4. Live-Risk-Konsistenz Tests

## Walk-Forward Pattern Research

Der `optimizer.py` hat bereits `train_split_pct` Parameter - kann als Inspiration dienen.

Walk-Forward Konzept:
- Rollierendes Fenster: z.B. 12 Monate Train, 3 Monate Test, Step 1 Monat
- Aggregiert: Durchschnittlicher OOS-Sharpe, Worst DD, %-Anteil profitable Folds

## Overnight Experiment Ideas

### 1. Auto-VectorBT Strategy Discovery
- Random Parameter Sampling für SMA/RSI/Keltner
- Speichere Top-10 Strategien mit >1.0 Sharpe in `data/discovered_strategies/`
- Minimaler Score-Threshold um Fehl-Positives zu vermeiden

### 2. Correlation Heatmap Generator
- Für alle Backtests in `backtests.db` eine Korrelationsmatrix
- Speichere als PNG via matplotlib/seaborn

### 3. Equity Curve Feature Extractor
- Technische Features aus Equity Curves (Trend, Volatility, Drawdown Duration)
- Nutze für Portfolio Optimizer Ranking

## Nächste Schritte

1. `runner/walkforward.py` erstellen (CLI: `python -m runner.cli walkforward --strategy ... --train-months 12 --test-months 3`)
2. `tests/test_walkforward.py` schreiben
3. `scripts/discover_strategies.py` für overnight experiments

---