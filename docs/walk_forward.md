# Walk-Forward Validierung

## Überblick

Walk-Forward (WF) Validierung ist ein Stabilitäts-Mechanismus, der eine Strategie über mehrere Trainings- und Test-Fenster hinweg bewertet.

> **⚠️ Expectation Gap (Cleanup 16-D1, F31 — dokumentiert statt implementiert):**
> Der aktuelle Walk-Forward führt **keine echte In-Sample-Parameterauswahl**
> durch. Die "Trainings"-Folds laufen mit EINEM festen Parametersatz; es wird
> nichts auf IS-Daten optimiert. `is_sharpe` und `oos_sharpe` sind daher zwei
> Slices desselben Backtests — die Messung validiert die **Stabilität** der
> Strategie über verschiedene Zeiträume, **keine** Anti-Overfitting-Robustheit
> im Sinne einer Parametervalidierung. Für echte Anti-Overfitting-Validierung
> muss der Parametersatz vor dem Test-Fenster per Optimierung (z. B.
> `runner.optimizer` mit `train_period` der Fold) je Fold gewählt werden;
> das ist nicht implementiert. Die README-Beschreibung ("Anti-overfitting
> validation") ist diesbezüglich zu lesen als "Stabilitätsvalidierung mit
> Train/Test-Splits".

**Ziel:** Sicherstellen, dass eine Strategie auch Out-of-Sample stabil profitabel ist, bevor sie live geht.

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