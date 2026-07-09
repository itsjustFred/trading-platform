# Grid Strategy Tester

## Überblick

Der Grid-Tester führt automatisierte Backtest-Matrizen für jede Strategie aus.

**Ziel:** Für jede Strategie eine Matrix (Symbol × Timeframe × Parameter-Set) testen und im Viewer als Heatmap visualisieren.

## CLI Usage

```bash
# Grid-Run für eine Strategie
python -m runner.grid_tester --strategy example_sma --symbols EURUSD GBPUSD USDJPY --timeframes 1h 4h 1d --start 2023-01-01 --end 2024-12-31

# Mit mehreren Strategien/Timeframes und anderem Engine-Backend
python -m runner.grid_tester --strategy my_strategy --symbols BTCUSDT ETHUSDT --timeframes 1h 1d --start 2023-01-01 --end 2024-12-31 --engine vectorbt
```

## Output

- **SQLite Storage:** Ergebnisse werden in `backtests.db` (Tabelle `grid_results`) gespeichert
- **Heatmap-Daten:** Sharpe, CAGR, MaxDD, Win-Rate pro Kombination
- **API:** `GET /api/grid/{strategy_id}` liefert Grid-Ergebnisse als JSON

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

## Default Grid

- Top-20 liquideste Symbole
- Timeframes: 1h, 4h, 1d
- Heatmap-Rendering im Viewer mit Sortierbarkeit

## Tests

- `tests/test_grid_tester_sqlite.py` - 4 Tests für SQLite-Persistenz und API-Lesefunktionen