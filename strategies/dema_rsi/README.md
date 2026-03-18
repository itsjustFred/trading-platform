# DEMA + RSI

## Quelle
Pine Script v5 (TradingView), originärer Strategiename "Supertrend + DEMA + RSI Strategy"

## Kernlogik

### Entry
**Primärer Trigger:** Trendwechsel des Supertrend-Indikators  
- **Long:** Supertrend wechselt von Abwärtstrend zu Aufwärtstrend (`trend == 1 and trend[1] == -1`)  
- **Short:** Supertrend wechselt von Aufwärtstrend zu Abwärtstrend (`trend == -1 and trend[1] == 1`)

**Filterbedingungen (Strong Confluence):**
1. **DEMA Filter:** Preis muss oberhalb (Long) bzw. unterhalb (Short) des DEMA liegen
   - Long: `close > dema`
   - Short: `close < dema`

2. **RSI Filter (optional):** Prüfung, ob RSI innerhalb eines Lookback-Fensters überkauft/überverkauft war
   - **Current RSI:** Lookback von 12 Bars, Long wenn RSI ≤ 35, Short wenn RSI ≥ 65
   - **HTF-RSI:** Zeitraum 240min (4h), Lookback 5 HTF-Bars, Long wenn RSI ≤ 30, Short wenn RSI ≥ 70
   - **Modus:** HTF-RSI kann Current RSI ersetzen (`useHTFRsiBias`)

### Exit
**Drei Ausstiegsmechanismen:**

1. **Stop-Loss Hit:** Preis erreicht oder durchbricht dynamischen Supertrend-SL
   - Closing der Position mit Kommentar zum PnL in Prozent

2. **Take Profit Hit (nur bei Fixed RRR):** Preis erreicht statisches Take-Profit-Level basierend auf Risk/Reward Ratio
   - Berechnung: `Entry + (Risk * RRR)` für Long, `Entry - (Risk * RRR)` für Short

3. **Trendwechsel:** Impliziter Exit durch gegensätzlichen Entry-Signal (Positionsumkehr bei neuem Supertrend-Wechsel)

### Stop-Loss
- **Long:** Supertrend Untergrenze (`up`)
- **Short:** Supertrend Obergrenze (`dn`)
- **Initialisierung:** Bei Entry wird SL auf aktuellen Supertrend-Wert gesetzt
- **Reset:** Nach Exit (SL oder TP) werden `entryPrice`, `stopLoss` und `takeProfit` auf `na` zurückgesetzt

### Trailing Stop
**Dynamische Anpassung:** Bei laufender Position wird der SL verschoben, wenn der Supertrend-Preis profitabler wird
- **Long:** Wenn `up > stopLoss` (Supertrend-Band steigt), wird `stopLoss := up`
- **Short:** Wenn `dn < stopLoss` (Supertrend-Band fällt), wird `stopLoss := dn`

## Indikator-Einstellungen

### Supertrend
| Parameter | Standardwert | Beschreibung |
|-----------|---------------|--------------|
| ATR Period | 10 | Lookback für ATR-Berechnung |
| ATR Multiplier | 3.0 | Multiplikator für ATR-Abstand |
| Source | hl2 | Berechnungsgrundlage (High+Low)/2 |
| Change ATR Method | true | `ta.atr()` statt `ta.sma(ta.tr)` |

### DEMA (Double Exponential Moving Average)
| Parameter | Standardwert | Beschreibung |
|-----------|---------------|--------------|
| Length | 9 | Glättungsperiode |
| Source | close | Schlusskurs |

### RSI Settings
| Parameter | Standardwert | Beschreibung |
|-----------|---------------|--------------|
| Use RSI Filter | false | Aktivierung des Filters |
| RSI Length | 14 | Berechnungsperiode |
| Long Threshold | 35 | Überverkauft-Schwelle |
| Short Threshold | 65 | Überkauft-Schwelle |
| Lookback Bars | 12 | Bars in der Vergangenheit zur Prüfung |

### HTF-RSI Settings
| Parameter | Standardwert | Beschreibung |
|-----------|---------------|--------------|
| Timeframe | 240 | 4-Stunden Chart |
| RSI Length | 14 | Berechnungsperiode auf HTF |
| Oversold | 30 | Untere Schranke |
| Overbought | 70 | Obere Schranze |
| Lookback Bars | 5 | HTF-Bars zur Prüfung |
| Replace Current RSI | true | HTF-RSI ersetzt Standard-RSI |

## Risk-Management

### Fixed Risk/Reward Ratio (optional)
- **Aktivierung:** `useFixedRRR = false` (default)
- **Ratio:** 2.0 (1:2 Risk/Reward)
- **Berechnung:**
  - Risk = |Entry - SL|
  - TP = Entry ± (Risk * Ratio)

### Positionsmanagement
**Keine Partialexits** definiert; Position wird vollständig bei SL oder TP geschlossen

**PnL-Tracking:** Bei jedem Exit wird Prozentgewinn berechnet und als Kommentar protokolliert:
- Formel Long: `((close - entryPrice) / entryPrice) * 100`
- Formel Short: `((entryPrice - close) / entryPrice) * 100`

### Visuelles Risk-Management
- HTF-RSI Zustände visualisiert durch Background-Coloring (grün für Oversold, rot für Overbought)
- Transparenz einstellbar (Standard: 85%)

## Framework-Empfehlung

**vectorbt**

Begründung: Die Strategie basiert auf einer Kombination von vektorisierten Indikatoren (Supertrend, DEMA, RSI) mit komplexen Lookback-Bedingungen und Multi-Timeframe-Logik. vectorbt bietet native Unterstützung für:
- Effiziente Berechnung von DEMA und Supertrend auf großen Datensätzen
- Flexible Timeframe-Handling für HTF-RSI-Filter über `resample`
- Einfache Implementierung von Lookback-Fenstern über `rolling` und `shift`
- Dynamische Stop-Loss- und Take-Profit-Logik mit `from_signals` oder `from_orders`
- Trailing-Stop-Mechanismen über iterative oder vektorisierte Anpassungen

Alternative: Für Produktiv-Trading mit automatisiertem Order-Routing wäre **lean** (QuantConnect Engine) aufgrund der robusten HTF-Daten-Handling und Live-Trading-Infrastruktur präferierbar, erfordert jedoch C# oder Python-OOP-Wrapper.
