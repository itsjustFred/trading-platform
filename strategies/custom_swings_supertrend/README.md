# Custom Swings + Supertrend

## Quelle
- Original: Pine Script v5 — Datei `Custom Swings + Supertrend.txt` (trotz `.txt`-Endung enthält die Datei vollständiges Pine Script mit `strategy()`-Deklaration)
- Strategie-Name im Pine Script: `"DTFX Structure Swings + Supertrend Strategie"`
- Konvertiert: 2026-03-11

## Kernlogik der Originalstrategie

### Entry
**Long:** Supertrend flippt von DOWN auf UP und der Entry wird zusätzlich durch mehrere Gates gefiltert:
- EMA-/Pullback-Logik
- Swing-/BOS-Struktur (`validSH` / `validSL`)
- Session-Filter
- HTF-/LTF-Bias via `request.security(...)`
- optionale ADX- und Konsolidierungsfilter

**Short:** Analog, wenn der Supertrend von UP auf DOWN flippt.

### Exit
Die Quelle kombiniert mehrere Exit-Mechaniken:
- swing-/strukturbezogene Stop-Loss-Logik
- feste oder strukturbezogene Take-Profits
- Breakeven
- optionales Trailing
- optionale Partial Exits
- Exit bei Gegen-Flip des Supertrends

**Fill-Konvention (Version 1.1.0):** Marktausstiege auf Close-Basis (Supertrend-Gegen-Flip) tragen kein `exit_price` mehr (kein Close-Fill) und fuellen am naechsten Bar-Open (`signal_exit_fill="next_open"`). Stop-Loss und fester RRR-Take-Profit bleiben explizite Level (`exit_price`).

### Stop-Loss
Im Original nicht rein statisch, sondern struktur- bzw. bandbasiert:

```text
Long:  letzter valider Swing / ST-Band als Referenz
Short: letzter valider Swing / ST-Band als Referenz
```

### Partial Exits
Vorhanden. Die Quelle unterstützt Teilgewinnmitnahmen und Runner-/Trailing-Verhalten.

## Projekt-Portierung in diesem Repository

Diese Repository-Portierung ist bewusst ein **projekttauglicher Core-Port** der Strategie und **keine vollständige 1:1-Abbildung** der Pine-Quelle.

### Umgesetzter Scope
Der Python-Port unter `strategies/custom_swings_supertrend/custom_swings_supertrend.py` bildet zuverlässig die Kernidee ab:
- Supertrend-Flip als Entry-Trigger
- EMA-Richtungsfilter
- Supertrend-Band als `stop_loss` für korrektes `risk_per_trade_pct`-Sizing
- optionaler fixer RRR-Exit
- Breakeven-Management
- optionales Supertrend-basiertes Trailing
- vollständige Overlay-/Meta-Indikatoren für Trade-Viewer und Validierung

### Bewusst nicht 1:1 portierte Originalbestandteile
Folgende Pine-spezifische bzw. stark zustandsbehaftete Teile sind **nicht vollständig abgebildet**:
- Session-Filter
- `validSH` / `validSL` / BOS-Gates
- HTF-/LTF-Bias via `request.security(...)`
- ADX-Filter
- Konsolidierungszonen
- Multi-Slot-/Pyramiding-Management
- One-Trade-per-Swing-Logik
- echte Partial-Exit-/Runner-Logik der Quelle
- exakte swing-basierte TP-Bestimmung

## Indikator-Einstellungen des Ports
| Indikator         | Parameter            | Default |
|-------------------|----------------------|---------|
| Supertrend / ATR  | `st_period`          | `12`    |
| Supertrend / ATR  | `st_multiplier`      | `3.0`   |
| EMA Fast          | `ema_fast`           | `21`    |
| EMA Slow          | `ema_slow`           | `120`   |
| EMA-Filter        | `ema_filter_enabled` | `True`  |
| Fester TP         | `use_rrr`            | `False` |
| RRR               | `risk_reward`        | `2.0`   |
| Breakeven         | `use_breakeven`      | `True`  |
| Breakeven-Level   | `breakeven_r`        | `1.0`   |
| Trailing          | `use_trailing_sl`    | `False` |
| Richtung          | `trade_direction`    | `both`  |

## Risk-Management
- Stop-Distanz: Supertrend-Band am Entry-Bar (`stop_loss`)
- Position Sizing: `risk_per_trade_pct`-basiert über den vectorbt-Adapter
- Empfohlenes `--risk-pct`: `0.01`
- Validierter Port-Lauf: `XAUUSD 1h`, `2023-01-01` bis `2023-04-01`

## Framework-Empfehlung
**Für diese Repository-Portierung:** `vectorbt`

Begründung:
- im Projekt sofort lauffähig
- saubere AST-parsbare Parametersignatur
- kompatibel mit Optuna-/Frontend-Parameter-UI
- liefert `indicators_dict` / `drawings` im aktuellen Adapter-Contract

**Für eine vollständige 1:1-Parität der Pine-Quelle wäre perspektivisch `lean` besser geeignet**, weil die Originalstrategie stark zustandsbehaftete MTF-/Order-Management-Logik enthält.
