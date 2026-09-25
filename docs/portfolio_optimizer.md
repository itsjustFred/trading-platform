# Portfolio Optimizer

## Überblick

Der Portfolio-Optimizer kombiniert mehrere Strategien zu einem Portfolio mit optimaler Gewichtung basierend auf Risk-Adjusted Returns.

**Ziel:** Maximierung des Sharpe Ratio unter Diversifikations- und Drawdown-Penalties.

## CLI Usage

```bash
# Portfolio-Optimierung mit bestimmten Backtest-IDs
python -m runner.portfolio_optimizer --backtest-ids bt_001 bt_002 bt_003

# Mit OOS-Split für Validierung
python -m runner.portfolio_optimizer --backtest-ids bt_001 bt_002 --train-months 12 --test-months 3
```

Hinweis: `--backtest-ids` nimmt eine oder mehrere IDs als getrennte Argumente entgegen.

## Datenquellen

- Der Optimizer liest Equity-Curves zuerst aus SQLite (`BacktestRecord.equity_curve`).
- Fallback: Legacy-JSON-Dateien unter `data/backtests/{backtest_id}.trades.json`.
- `--from-sqlite` ist aktuell kein separater CLI-Parameter; SQLite wird automatisch bevorzugt.

## Output

- **JSON Output:** Ergebnisse werden nach `data/optimizations/portfolio_*.json` geschrieben.
- **Metrics:** In-Sample Sharpe, OOS Sharpe bei `--test-months`, PSR-ready Korrelationsmatrix, Max Drawdown.
- **API:** Viewer-Endpunkte können die JSON-Dateien bzw. persistierte Backtest/Grid-Daten weiterverarbeiten.

## OOS Validation

Der Optimizer unterstützt Out-of-Sample Validierung:

- `test_months`: Anzahl der Monate für den Test/OOS-Bereich vom Ende der Equity-Curve aus.
- `train_months`: Für zukünftige Walk-Forward-Implementierung vorgesehen; die aktuelle Split-Funktion nutzt primär `test_months`.

Die Funktion `_split_equity_curves_by_time` splittet Equity-Kurven für OOS-Berechnung.

## Constraints

- Drawdown wird über eine Penalty-Funktion im Optuna-Ziel berücksichtigt.
- Min-Sharpe-/Max-Weight-Constraints können im Optimierungsziel ergänzt werden; die aktuelle CLI bietet noch keine harten Constraint-Flags.

## Tests

- `tests/test_portfolio_optimizer_oos.py` - 20 Tests für OOS-Validierungs-Workflow
- Enthält Tests für: Equity-Point-Erstellung, Zeitfenster-Split, Korrelationsmatrix, Portfolio-Sharpe-Berechnung