# lab_generated_strategy

## Quelle
- **Autor**: Unbekannt (Sample Code)
- **Plattform**: TradingView Pine Script v5
- **Originalname**: Sample EMA Cross

## Kernlogik
Trendfolgestrategie basierend auf dem klassischen Moving Average Crossover zweier Exponentieller Gleitender Durchschnitte (EMA) unterschiedlicher Periodenlängen.

### Entry
**Long Entry**: Auslösung bei bullischem EMA-Crossover (Golden Cross)
- **Trigger**: `ta.crossover(fast, slow)`
- **Bedingung**: EMA(9) schneidet EMA(21) von unten nach oben
- **Order-Typ**: Market Order
- **Position**: Long

### Exit
**Long Exit**: Auslösung bei bärischem EMA-Crossover (Death Cross)
- **Trigger**: `ta.crossunder(fast, slow)`
- **Bedingung**: EMA(9) schneidet EMA(21) von oben nach unten
- **Mechanismus**: `strategy.close("Long")` - vollständige Positionsschließung
- **Exit-Typ**: Signal-basierter Close-Only-Exit

### Stop-Loss
Nicht implementiert. Kein fester Stop-Loss, kein ATR-basierter oder technischer Stop definiert.

## Indikator-Einstellungen

| Parameter | Standardwert | Berechnung | Beschreibung |
|-----------|--------------|------------|--------------|
| `fastLength` | 9 | `ta.ema(close, 9)` | Schneller EMA (Kurzfristtrend) |
| `slowLength` | 21 | `ta.ema(close, 21)` | Langsamer EMA (Langfristtrend) |
| `fast` | Variable | EMA auf Close | Fast-Line Datenreihe |
| `slow` | Variable | EMA auf Close | Slow-Line Datenreihe |

**Preisdaten**: Schlusskurse (`close`)

## Risk-Management

**Implementierungsstatus**: Kein explizites Risk-Management im Originalcode.

**Kritische Fehlstellen**:
- Keine Positionsgrößenberechnung ( fixe Kontraktanzahl impliziert)
- Kein Stop-Loss (unbegrenztes Downside-Risiko)
- Keine Take-Profit Ziele
- Keine Hebelbegrenzung
- Keine Korrelationsprüfung bei Multi-Asset-Einsatz

**Empfohlene Erweiterungen**:
- Stop-Loss: Unter dem letzten Swing-Low oder 2x ATR
- Position Sizing: 1-2% Account-Risk pro Trade
- Trailing Stop: Aktivieren nach 1R Profit

## Framework-Empfehlung

**vectorbt**

Die Strategie eignet sich optimal für vectorbt aufgrund der rein vektorisierten Natur der EMA-Crossover-Logik. Das Framework ermöglicht effiziente Backtests mit `vbt.MA.run()` oder `vbt.EMA.run()`, native Crossover-Erkennung via `.crossed_above()`/`.crossed_below()` und integriertes Portfolio-Management für die Umsetzung fehlender Risk-Management-Regeln (Stop-Loss, Position Sizing). Die Python-native Architektur erlaubt zudem einfache Erweiterung um die im Originalcode fehlenden Risk-Management-Komponenten.
