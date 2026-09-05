# Backtest Grid Runner

## Überblick

Der Grid-Runner führt automatisierte Backtest-Matrizen für Strategien aus.

**Zwei Ebenen:**

- `runner/backtest_grid.py` — kanonische Grid-Execution-Engine: läuft eine Strategie über ein
  Symbol × Timeframe-Grid. Jede Zelle läuft über denselben Validierungs-, Statistik-, Risk-Sizing-
  und SQLite-Persistence-Pfad wie ein manueller Backtest.
- `runner/backtest_matrix.py` — Multi-Strategy-Orchestrierung/Aggregation über `backtest_grid`
  (alle Strategien, Matrix-CSVs, Daily-Equity-Export für Portfolio-Allocation).
  Hostet zusätzlich das Legacy-Schnell-Grid `run_strategy_grid` (einzelne Strategie,
  Ergebnis-Persistenz in SQLite via `GridRecord`).

## CLI Usage

```bash
# Grid-Run für eine Strategie (kanonische Engine)
python -m runner.backtest_grid --strategy strategies/example_sma.py --symbols EURUSD GBPUSD USDJPY --timeframes 1h 4h 1d --start 2023-01-01 --end 2024-12-31

# Mit anderem Engine-Backend
python -m runner.backtest_grid --strategy strategies/my_strategy.py --symbols BTCUSDT ETHUSDT --timeframes 1h 1d --start 2023-01-01 --end 2024-12-31 --engine vectorbt

# Alle Strategien über das Grid (Matrix-Runner)
python -m runner.backtest_matrix --symbols EURUSD GBPUSD --timeframes 1h 4h
```

## Output

- **SQLite Storage:** Ergebnisse werden in `backtests.db` (Tabelle `grid_results`) gespeichert
- **Heatmap-Daten:** Sharpe, CAGR, MaxDD, Win-Rate pro Kombination
- **CSV:** `grid_matrix.csv` pro Grid-Run; `matrix_matrix.csv` / `matrix_daily_equity.csv` im Matrix-Runner

## GridRecord Model

```python
class GridRecord(SQLModel, table=True):
    id: str  # Unique grid run ID
    strategy: str  # Strategy name
    created_at: str  # ISO timestamp
    symbols_json: list[str]  # Tested symbols
    timeframes_json: list[str]  # Tested timeframes
    results_json: dict[str, Any]  # {symbol: {timeframe: {metrics}}}
    engine: str  # Backtest engine used
```

## Tests

- `tests/test_backtest_grid.py` - Grid-Execution-Engine (Zellen, CLI-Kommandos, Risk-Konsistenz)
- `tests/test_backtest_matrix.py` - Matrix-Orchestrierung über mehrere Strategien
- `tests/test_grid_tester_sqlite.py` - SQLite-Persistenz der Grid-Ergebnisse
