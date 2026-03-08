# Metrics Validation Report: `runner/stats.py` vs QuantStats

**Erstellt:** 2026-03-01  
**Scope:** Sharpe Ratio, Sortino Ratio, Calmar Ratio, Max Drawdown  
**Methode:** Side-by-side Vergleich mit QuantStats auf 4 synthetischen Szenarien  
**Risk-free Rate:** 4% p.a.

---

## Zusammenfassung

| Metric | Status | Befund |
|---|---|---|
| **Max Drawdown** | ✅ Korrekt | Perfekte Übereinstimmung mit QuantStats in allen Szenarien |
| **Sharpe Ratio** | ✅ Korrekt (mit Caveat) | Marginal abweichend (Population- vs. Sample-Varianz); Edge case bei σ=0 |
| **Sortino Ratio** | ⚠️ Andere Konvention | Systematisch niedriger: abweichende Downside-Denominator-Definition |
| **Calmar Ratio** | 🐛 Bug | `/100`-Fehler macht Calmar **~100× zu groß** |

---

## Test-Szenarien

| Szenario | Beschreibung | N Punkte |
|---|---|---|
| **A** – Steady Growth | +0.03%/Tag, kein Drawdown, σ≈0 | 252 |
| **B** – Realistic Volatile | µ=+0.05%/Tag, σ=1.2% (random seed 42) | 252 |
| **C** – Stress / Losses | µ=−0.1%/Tag, σ=2.5% (random seed 7) | 252 |
| **D** – Textbook Simple | Handliches 6-Punkte-Beispiel | 6 |

---

## Ergebnisse im Detail

### Szenario B – Realistic Volatile (Hauptreferenz)

| Metric | Unsere Impl. | QuantStats | Differenz |
|---|---|---|---|
| Sharpe Ratio | 0.3453 | 0.3489 | −0.0036 ✅ |
| Sortino Ratio | 0.3548 | 0.5178 | −0.1631 ⚠️ |
| Max Drawdown | 0.1640 | 0.1640 | 0.0000 ✅ |
| Calmar (buggy) | **54.94** | 0.5517 | **+54.39 🐛** |
| Calmar (fixed) | 0.5494 | 0.5517 | −0.0023 ✅ |

### Szenario C – Stress / Losses

| Metric | Unsere Impl. | QuantStats | Differenz |
|---|---|---|---|
| Sharpe Ratio | −1.3015 | −1.2969 | −0.0046 ✅ |
| Sortino Ratio | −1.2773 | −1.7175 | +0.4402 ⚠️ |
| Max Drawdown | 0.4393 | 0.4393 | 0.0000 ✅ |
| Calmar (buggy) | **−93.05** | −0.9333 | **−92.11 🐛** |
| Calmar (fixed) | −0.9305 | −0.9333 | +0.0028 ✅ |

### Szenario A – Steady Growth (σ = 0 Edge Case)

| Metric | Unsere Impl. | QuantStats | Bewertung |
|---|---|---|---|
| Sharpe Ratio | 0.0000 | ∞ | ⚠️ Edge case |
| Sortino Ratio | 0.0000 | NaN | ⚠️ Edge case |
| Max Drawdown | 0.0000 | 0.0000 | ✅ |
| Calmar | 0.0000 | ∞ | ⚠️ Edge case |

---

## Befund 1 – Max Drawdown ✅ Korrekt

```python
def _max_drawdown_pct(equities: list[float]) -> float:
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd  # Dezimalbruch, z.B. 0.164 für 16.4%
```

**Ergebnis:** Perfekte Übereinstimmung. Die Funktion gibt einen **Dezimalbruch** zurück (0.164, nicht 16.4). Der Bezeichner `max_drawdown_pct` ist damit irreführend – er sollte `max_drawdown` heißen.

---

## Befund 2 – Sharpe Ratio ✅ Korrekt (mit Caveat)

### Formel (unsere Implementierung)
```
excess_i = r_i − (rf_annual / 252)
μ_excess  = mean(excess)
σ_excess  = sqrt(Σ(excess_i − μ)² / N)          # Population-Varianz
Sharpe    = (μ_excess / σ_excess) × √252
```

### Formel (QuantStats)
```
excess_i = r_i − deannualize(rf_annual, 252)
μ_excess  = mean(excess)
σ_excess  = std(excess, ddof=1)                  # Sample-Varianz (N−1)
Sharpe    = (μ_excess / σ_excess) × √252
```

### Unterschiede

**a) Population- vs. Sample-Varianz**  
Bei N=251 beträgt der Korrekturfaktor √(N/(N−1)) = √(251/250) ≈ 1.002 → **0.2% Abweichung**. In der Praxis vernachlässigbar.

**b) Edge case σ=0 (Szenario A)**  
Wenn alle Returns identisch sind (kein Drawdown), ist σ=0.  
- Unsere Implementierung: gibt `0.0` zurück  
- QuantStats: gibt `inf` zurück (mathematisch korrekt: unendliche Sharpe bei null Risiko)

Die aktuelle Behandlung `if std == 0: return 0.0` ist defensiv, aber mathematisch nicht präzise. Für Backtests ohne Drawdown kann dies irreführend sein.

**Empfehlung:** Bei σ=0 sollte `float('inf')` oder `None` zurückgegeben werden, nicht `0.0`.

---

## Befund 3 – Sortino Ratio ⚠️ Andere Konvention

### Formel (unsere Implementierung)
```python
downside = [r for r in returns if r < 0]
downside_var   = sum(r² for r in downside) / len(downside)   # N_neg im Nenner
downside_std   = sqrt(downside_var)
excess_mean    = mean(returns) − (rf / 252)
Sortino        = (excess_mean / downside_std) × √252
```

### Formel (QuantStats — nach Red Rock Capital Paper)
```python
downside_var   = sum(r² for r in negative_returns) / len(all_returns)  # N_total im Nenner
downside_std   = sqrt(downside_var)
Sortino        = (mean(excess_returns) / downside_std) × √252
```

### Warum weichen die Werte ab?

In Szenario B (252 Punkte, 251 Returns): 118 von 251 Returns sind negativ (47%).

| | Downside Std | Sortino |
|---|---|---|
| Unsere Impl. (N_neg = 118) | 0.01128 | 0.3548 |
| QuantStats (N_all = 251) | 0.00773 | 0.5178 |

Durch Division durch N_total (251) statt N_neg (118) ist der QuantStats-Downside-Std kleiner → Sortino-Ratio höher.

### Welche Definition ist "korrekt"?

Es gibt zwei etablierte Definitionen:
- **Morningstar-Stil** (unser Code): Nur negative Renditen zählen, `sum(r²)/N_neg`
- **Red Rock Capital Paper** (QuantStats): Alle Renditen zählen, `sum(r² für r<0)/N_total`

Das Red Rock Capital Paper (von Rollinger & Hoffman, 2013) ist die modernere und breiter verwendete Definition. Beide sind in der Praxis gebräuchlich, **aber die Werte sind nicht direkt vergleichbar**.

**Empfehlung:** Auf QuantStats-Konvention umstellen für bessere Vergleichbarkeit mit externen Tools.

```python
def _sortino(returns: list[float], risk_free_rate: float) -> float:
    """Annualized Sortino ratio (Red Rock Capital definition)."""
    if not returns:
        return 0.0
    daily_rf = risk_free_rate / 252
    mean_return = sum(returns) / len(returns)
    # Sum of squared negative excess returns / total count (Red Rock Capital Paper)
    downside_sq_sum = sum(r ** 2 for r in returns if r < 0)
    downside_var = downside_sq_sum / len(returns)
    downside_std = math.sqrt(downside_var) if downside_var > 0 else 0
    if downside_std == 0:
        return 0.0
    excess_mean = mean_return - daily_rf
    return (excess_mean / downside_std) * math.sqrt(252)
```

---

## Befund 4 – Calmar Ratio 🐛 Bug

### Der Bug

`_max_drawdown_pct()` gibt einen **Dezimalbruch** zurück (z.B. `0.164` für 16.4%).  
Der Calmar-Code in `compute_stats()` dividiert den Wert aber nochmals durch 100:

```python
# In compute_stats():
max_drawdown_pct = _max_drawdown_pct(equities)      # gibt 0.164 zurück (Dezimalbruch!)
# ...
calmar_ratio = annualized_return / abs(max_drawdown_pct / 100)  # ← BUG: dividiert 0.164 / 100 = 0.00164
```

Das `/100` impliziert, dass `max_drawdown_pct` eine Prozentzahl (z.B. 16.4) wäre. Da es aber bereits ein Dezimalbruch ist, wird der Calmar **100× zu groß**.

### Nachweis

Szenario B (Drawdown = 16.4%):

| | Wert |
|---|---|
| `max_drawdown_pct` (was die Funktion zurückgibt) | 0.1640 |
| Annualisierte Rendite | ~9.03% |
| **Calmar (buggy `/100`)** | **54.94** |
| **Calmar (fixed, kein `/100`)** | **0.5494** |
| **QuantStats Calmar** | **0.5517** |

### Fix

```python
# In compute_stats(), Zeile ~83:
# VORHER (buggy):
calmar_ratio = annualized_return / abs(max_drawdown_pct / 100) if max_drawdown_pct != 0 else 0

# NACHHER (korrekt):
calmar_ratio = annualized_return / abs(max_drawdown_pct) if max_drawdown_pct != 0 else 0
```

---

## Empfehlungen

### Priorität 1 — Bug fixen (Calmar)
```python
# runner/stats.py, compute_stats(), ~Zeile 83
calmar_ratio = annualized_return / abs(max_drawdown_pct) if max_drawdown_pct != 0 else 0
```

### Priorität 2 — Sortino auf Red Rock Capital Definition umstellen (optional)
Vorteil: Vergleichbarkeit mit QuantStats, pyfolio, etc.  
Nachteil: Alle bestehenden Backtesting-Ergebnisse werden sich ändern.

### Priorität 3 — Sharpe Edge Case verbessern (optional)
```python
# _sharpe(): statt return 0.0 bei std=0:
if std == 0:
    return float('inf') if mean_excess > 0 else (float('-inf') if mean_excess < 0 else 0.0)
```

### Priorität 4 — Variablennamen klären
- `max_drawdown_pct` → besser `max_drawdown` (es ist ein Dezimalbruch, kein Prozentsatz)
- Oder die Funktion umbenennen zu `_max_drawdown` und den gespeicherten Wert konsistent halten

---

## Anhang: QuantStats-Aufrufkonvention

**Wichtig:** QuantStats erwartet den Risk-free Rate als **Jahresrate**. Die Funktion deannualisiert intern:

```python
# RICHTIG:
qs.stats.sharpe(returns, rf=0.04, periods=252)   # rf=4% p.a.

# FALSCH (rf wird nochmals deannualisiert → effektiv ≈ 0):
qs.stats.sharpe(returns, rf=0.04/252, periods=252)
```

Das Vergleichsskript befindet sich unter: `tests/compare_metrics_quantstats.py`
