# Single-Leg Z-Score Mean Reversion — slug `stat_arb_pairs`

> **Naming-Integrity (Cleanup #35 / Settlement S15):** Diese Strategie ist
> KEIN Pairs-Trading. Es gibt kein zweites Bein, keinen Spread zwischen zwei
> Instrumenten und keine Market-Neutral-Absicherung — der Z-Score wird auf
> den kumulierten Log-Returns des EINZELNEN Instruments berechnet. Der
> Verzeichnis-/Slug-Name `stat_arb_pairs` bleibt aus Kompatibilitaetsgruenden
> mit bestehenden Backtest-Artefakten erhalten; die Meta-Tags beschreiben
> das implementierte Single-Leg-Verhalten.

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `desenyon/quantconnect-strategies`
- Konvertiert: 2026-03-08
- Umbenannt/umetikettiert: 2026-09-02 (Cleanup #35)

## Kernlogik

### Entry
Long-Entry, wenn der Z-Score der kumulierten Log-Returns des Instruments stark negativ vom Mittelwert abweicht.
Signal: `ZScore <= -2.0`.
Der Z-Score wird auf kumulierten Log-Returns ueber ein `lookback` von 60 Bars berechnet.

### Exit
Vollausstieg bei einem von zwei quellenbasierten Events:
1. Mean-Reversion-Ziel: `ZScore >= -0.5` (Quelle: `exit_zscore`).
2. Statistischer Fail-Stop: `ZScore <= -4.0` (Quelle: `stop_zscore`).
Opt-in (default OFF, keine Quelllogik): `use_atr_stop=true` fuer zusaetzlichen Stop-Loss-Hit auf dem ATR-Schutzstop.

### Stop-Loss
Kein Stop-Loss im Standard (die Quelle nutzt einen z-Score-Fail-Stop, keinen ATR-Stop).
Opt-in via `use_atr_stop=true`: `sl_price = entry_close - ATR_Wilder(14) * 2.0`.
Position Sizing ohne Stop faellt auf die Engine-Fallbacks zurueck (2-%-Default).

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Z-Score Lookback | Bars | 60 |
| Entry Z-Score | Threshold | -2.0 |
| Exit Z-Score | Threshold | -0.5 |
| Stop Z-Score | Threshold | -4.0 |
| ATR Stop (Opt-in) | Wilder Period / Multiplier | 14 / 2.0 |

## Risk-Management
- Stop-Distanz: keine im Standard (Opt-in ATR)
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` (Faellt ohne `sl_price`/ATR auf den 2-%-Default zurueck)
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Hinweis zur Vereinfachung
Das Originalthema ist Pairs Trading. Die vorliegende Python-Strategie nutzt einen Single-Symbol-Proxy auf Basis kumulierter Log-Returns, weil im aktuellen Adapter keine echte Zwei-Bein-Ausfuehrung existiert.

## Framework-Empfehlung
vectorbt — die vereinfachte Spread-/Z-Score-Logik ist rein datengetrieben und benoetigt keine event-getriebene Multi-Leg-Engine.
