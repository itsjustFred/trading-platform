# Short Term Reversal

## Quelle
- Original: `desenyon/quantconnect-strategies` — `short_term_reversal_strategy.py` (cross-sectional long/short-Universum, 20/20)
- Version: 1.2.0 (Market-Exits ohne exit_price=close; 5-Bar-Hold dokumentiert)
- Konvertiert: 2026-03-08, korrigiert: 2026-09-09 (Hold 10→5 Bars, Target +3 %→+5 % PnL, fixer 8-%-Stop statt ATR, RSI-Filter ersatzlos entfernt)

## Kernlogik

### Entry
Long-Entry, wenn die 5-Tages-Rendite stark negativ ist.
Signal: `Return_5D < -5%`.
Kein RSI-Filter (die Quelle kennt keinen RSI).

### Exit
Vollausstieg bei einem von drei Events (alle quellenbasiert):
1. Fixer Stop-Loss: Positions-PnL `<= -8 %` (`stop_loss`).
2. Profit-Target: Positions-PnL `>= +5 %` (`profit_target`).
3. Time-Exit nach 5 Bars (`time_exit`). Unter `signal_exit_fill="next_open"` (Default; Entries UND Signal-Exits fuellen am naechsten Bar-Open) zaehlt der Hold ab der **Signal-Bar** `i`: Entry-Fill am Open von `i+1`, Exit-Signal auf `i+5`, Fill am Open von `i+6` - die Position ist exakt den Bars `i+1..i+5` ausgesetzt (5 Bars, Open-zu-Open) und damit gleich lang wie frueher „Entry-Open bis Close von Bar 5“. Der Zaehler wird bewusst **nicht** um eins verschoben (das waere ein 4-Bar-Hold). Alle drei Exits sind Market-Exits auf Close-Basis ohne expliziten `exit_price` (seit v1.2.0; ein Preis = Signal-Bar-Close widerspraeche dem Next-Open-Timestamp).

### Stop-Loss
Fixer prozentualer Stop ab Entry (exakt wie die Quellen-Logik `pnl_pct < -0.08`).
Formel: `sl_price = entry_close * (1 - 0.08)`.
Kein ATR-Stop (weder Quelle noch Port nutzen ATR).

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert (die Quelle hat keinen).

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Return | Lookback | 5 Bars |
| Entry Threshold | Return_5D | -0.05 |
| Profit Target | Positions-PnL | +0.05 |
| Stop | Positions-PnL | -0.08 |
| Max Hold | Bars | 5 |

## Risk-Management
- Stop-Distanz: fix 8 % ab Entry
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Hinweis zur Vereinfachung (Scope)
Das Original waehlt wochenweise aus den 500 liquidesten US-Aktien die 20 groessten Verlierer (long) und 20 groessten Gewinner (short), filtert per Return-z-Score (|z| > 1.5) und Volumensspikes, gewichtet nach z-Score, kappt Sektoren bei 30 % und halbiert Exposure in unguenstigen SPY-Regimes. Davon ist nichts single-symbol-exprimierbar und nichts davon ist implementiert: gehandelt wird nur die Long-Seite eines einzelnen Instruments (Short-Leg mit −5-%-Target/+8-%-Stop entfaellt), Rebalancing ist taeglich statt woechentlich.

## Framework-Empfehlung
vectorbt — die Strategie besteht aus Return-Serien und fixer Positionsfuehrung ohne komplexe Orderverwaltung.
