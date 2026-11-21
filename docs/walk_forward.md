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
> das ist nicht implementiert. Diese Datei beschreibt die Messung daher als Stabilitätsvalidierung mit
> Train/Test-Splits — nicht als Anti-Overfitting-Validierung.

**Ziel:** Sicherstellen, dass eine Strategie auch Out-of-Sample stabil profitabel ist, bevor sie live geht.

## CLI Usage

```bash
# Walk-forward analysis for one strategy (--symbols, --start and --end are required)
python -m runner.cli walkforward --strategy strategies/example_sma/example_sma.py --symbols AAPL --start 2024-01-01 --end 2025-12-31 --train-months 12 --test-months 3

# Mit Symbol-Liste
python -m runner.cli walkforward --strategy strategies/my_strat/my_strat.py --symbols EURUSD,GBPUSD,USDJPY --start 2024-01-01 --end 2025-12-31 --train-months 12 --test-months 3
```

`--strategy` takes a strategy file path; a bare name such as `example_sma` is not
resolved by the walk-forward path.

## Parameter

| Parameter | Beschreibung | Default |
|-----------|--------------|---------|
| `train_months` | Monate für Training | 12 |
| `test_months` | Monate für Test/OOS | 3 |
| `step_months` | Schrittweite | Test-Fenster (`test_months`) |

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

---

# Three-Way Optimization Split (Issue #164)

Since 2026-11, the parameter optimizer (`runner/optimizer.py`) uses a three-way
train/validation/test split instead of the previous two-way train/test split.
This section documents the new behaviour.

## Split Geometry

The full date range is divided into three segments plus embargo gaps:

```
[ Train ][ Embargo ][ Validation ][ Embargo ][ Test ]
```

- **Train** — parameter search (Optuna objective runs here)
- **Validation** — model/parameter selection; every completed trial is evaluated here; the winner is fixed by validation rank
- **Test** — untouched holdout; only the top fraction (default 10%) of trials by validation rank are evaluated here; test results are reported as a distribution and never change the winner

The default split is **60/20/20** (train/validation/test). The split is configurable via:
- `train_split_pct` (default 0.6)
- `validation_split_pct` (default 0.2)
- `test_split_pct` (default 0.2)

Old configs that only specify `train_split_pct` split the remainder equally between validation and test.

### Embargo Gaps

An embargo gap is inserted between each pair of segments:
- `embargo_train_val` between train and validation
- `embargo_val_test` between validation and test

The embargo size is `embargo_days` (fixed days) plus `embargo_pct` of the whole range (same semantics as the legacy two-way split). The gaps are taken **out of the train segment**, so validation and test keep their configured length. Whatever the configured embargo, consecutive segments are separated by at least 1 microsecond, because the date filter is inclusive on both ends and a bar on a shared boundary would otherwise be traded in two segments.

## Evaluation Flow

1. **Search** — Optuna objective runs on the train segment only.
2. **Validation evaluation** — Every completed trial is backtested on the validation segment.
3. **Selection** — The winner is fixed by validation rank (highest score for maximize targets, lowest for minimize). Tie-break: lower trial number wins. The top `top_fraction` (default 0.10) of trials by validation rank form the "top set".
4. **Test evaluation** — Only the top set is evaluated on the test segment. The test segment is read **once**, after the winner is fixed. Test results are reported as a distribution (count, share profitable, median target metric, Spearman rank correlation validation→test) and never change the winner.

This replaces the old "test touched once" wording: the acceptance test is that test is never read during search or selection.

## Result Payload

The optimization result JSON now carries the following keys (in addition to the legacy keys which are kept for backward compatibility):

### Legacy keys (kept for old readers; `test_*` changed meaning)
- `train_split_pct` — the train fraction (0.6 by default)
- `train_period`, `train_stats` — train window and winner's train metrics
- `test_period`, `test_stats` — **CHANGED MEANING** (parameter-search modes only; risk-parity keeps the holdout here, see below): now carries the validation window and the winner's validation metrics (the window the winner was selected on). Kept so existing readers keep comparing train against the held-out window used for selection.
- `oos_comparison`, `oos_status` — legacy OOS comparison against `test_stats`

### New keys
- `validation_split_pct`, `test_split_pct` — the validation and test fractions
- `validation_period`, `validation_stats` — same content as legacy `test_period`/`test_stats`
- `final_test_period`, `final_test_stats` — the untouched test holdout and the winner's result on it (the headline out-of-sample number)
- `three_way` — structured object with:
  - `segments`: `{train, validation, test, embargo_train_val, embargo_val_test}` — date ranges
  - `winner`: `{trial_number, train_metrics, validation_metrics, test_metrics}` — per-segment metrics
  - `selection`: `{winner_trial_number, score_source, target_metric, study_direction, top_fraction, top_set_size, n_selectable_trials, n_unselectable_trials, unselectable_trials, tie_break, ranked_top_set}`
    A trial is unselectable (listed in `unselectable_trials` with its reason, and logged at INFO) when it was a no-trade/unrankable train trial, or on validation made no trades, was ruined, had an undefined target metric (even with a 0.0 placeholder), or failed to run. If every trial is unselectable the run fails with an error naming each reason.
  - `test_distribution`: `{count, share_profitable, median_target_metric, target_metric, spearman_validation_to_test, ranked_by_test, note}`
  - `overfitting`: `{passed, checks: {degradation, deflated_sharpe}, thresholds, reasons, degradation, deflated_sharpe, metric, train_metric_value, validation_metric_value, n_selectable_trials, var_sharpe_per_obs, note}`

### Migration
**No migration of old results.** Existing two-way optimization results are left as they are; they render exactly as before with no `three_way` section. All backtests and optimizations will be recomputed later.

## Overfitting Detection

Two measures are combined into a verdict:

1. **Train→Validation degradation** — relative drop `(train - validation) / |train|`. Threshold: `max_train_validation_degradation` (default 0.5 = 50%).
2. **Deflated Sharpe Ratio (DSR)** — probability that the selected Sharpe is real rather than the best of N unskilled trials (Bailey & Lopez de Prado 2014). Threshold: `min_deflated_sharpe` (default 0.90). This is an overfitting /
selection-luck hint, not a judgement of strategy quality: a low-Sharpe strategy
can still be a useful part of a portfolio with negatively correlated drawdowns.

The verdict is **passed** only if **both** checks are computable and pass. If any check fails or is uncomputable, the verdict is **flagged** (not blocked). Blocking happens at the promotion gate (#165), which uses the verdict as a criterion. The verdict is an overfitting hint, not a quality score: a flag means the result may be selection luck, not that the strategy is poor.

## Walk-Forward Folds

Walk-forward folds (if configured) cover **only the range before the test holdout**. The folds split the train+validation range into sequential train/validation pairs. No fold reaches into the embargo gap before test or into test itself.

## Risk-Parity Covariance Mode

The risk-parity covariance optimization mode has no trial search. Covariance is fitted on the train segment and the selected universe is reported on the test holdout.

Its windows are cut from the same three-way split as the other modes, so it reads only train and test. The validation segment is never read, and both embargo gaps (before validation and before test) are skipped. It does not produce an overfitting verdict. **Exception: in this mode `test_period`/`test_stats` keep their old meaning and hold the untouched test holdout** (there is no validation window and no `final_test_*` key), so a mode-agnostic reader must not treat `test_*` as a validation window.

Because the default `train_split_pct` is now 0.6 (it was 0.7), this mode's test holdout is also smaller by default: 0.2 of the range instead of the 0.3 of the old two-way split. Set `train_split_pct`, `validation_split_pct` and `test_split_pct` explicitly to choose a different holdout.

## Viewer Display

- **Old results (no `three_way`)**: render as before — two panels (In-Sample / Out-of-Sample) with legacy labels.
- **New results (with `three_way`)**: show three segment date ranges; winner metrics per segment (Train / Validation / Test) with existing metric components; Validation column labeled as "selection window", Test column as "untouched holdout"; overfitting verdict (passed/flagged, two checks with value and threshold, reasons) and test distribution (count, share profitable, median, rank correlation) compactly. A flagged verdict is clearly visible but does not hide the result.

## Configuration Example

```yaml
# In optimization request
train_split_pct: 0.6
validation_split_pct: 0.2
test_split_pct: 0.2
embargo_days: 5
embargo_pct: 0.01
test_top_fraction: 0.10
max_train_validation_degradation: 0.5
min_deflated_sharpe: 0.90
```

Defaults are shown above; only `train_split_pct` is required for backward compatibility (the remainder is split equally).