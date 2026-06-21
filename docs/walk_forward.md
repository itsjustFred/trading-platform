# Walk-Forward Validierung

## Überblick

Walk-Forward (WF) Validierung ist ein Anti-Overfitting-Mechanismus, der eine Strategie über mehrere Trainings- und Test-Fenster hinweg bewertet.

**Ziel:** Sicherstellen, dass eine Strategie auch Out-of-Sample profitabel ist, bevor sie live geht.

## CLI Usage

```bash
# Walk-Forward-Analyse für eine Strategie
python -m runner.cli walkforward --strategy example_sma --train-months 12 --test-months 3

# Mit Symbol-Liste
python -m runner.cli walkforward --strategy my_strat --symbols EURUSD,GBPUSD,USDJPY --train-months 12 --test-months 3
```

## Parameter

| Parameter | Beschreibung | Default |
|-----------|--------------|---------|
| `train_months` | Monate für Training | 12 |
| `test_months` | Monate für Test/OOS | 3 |
| `step_months` | Schrittweite | 1 (relativ zum Test-Ende) |

## Metriken

- **Durchschnittlicher OOS-Sharpe:** Mittelwert über alle profitable Folds
- **Worst OOS-Drawdown:** Größter Drawdown im Test-Bereich
- **Profitable Fold-Anteil:** % der Folds mit positivem Sharpe

## Flag-Kriterien

Strategien mit:
- < 50% profitable OOS-Folds → **GEFLAGGT**
- Worst OOS-Drawdown > Max-DD-Limit → **GEFLAGGT**

## Output

- **JSON Report:** `walkforward_results/{strategy_id}_wf.json`
- **HTML Report:** `walkforward_results/{strategy_id}_wf.html`
- **API:** Wird im Viewer abgerufen und visualisiert

## Tests

- `tests/test_walkforward.py` - 13 Tests für CLI, Output-Struktur und Metriken
- Deckt Signature, File-Output und Summary-Berechnung ab