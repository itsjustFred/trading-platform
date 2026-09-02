# SMA Crossover ATR Bracket — slug `pynecore_orb`

> **Naming-Integrity (Cleanup #35 / Settlement S15):** Diese Strategie
> berechnet KEINE Opening Range. Implementiert ist ein SMA-Crossover
> (schnelle SMA gegen 4x langsame SMA) mit EMA(200)-Trendfilter und
> ATR-TP/SL-Bracket. Der Verzeichnis-/Slug-Name `pynecore_orb` bleibt aus
> Kompatibilitaetsgruenden mit bestehenden Backtest-Artefakten erhalten.

## Quelle
- Original: PyneCore/Pine-Style Python-Strategie im Repository, manuelle Konvertierung aus NinjaTrader 8 (C#)
- Konvertiert: 2026-03-08
- Umbenannt/umetikettiert: 2026-09-02 (Cleanup #35)

## Kernlogik

### Entry
Long-Entry, wenn die schnelle SMA die langsame SMA von unten nach oben kreuzt und der Preis oberhalb der EMA liegt.
Short-Entry, wenn die schnelle SMA die langsame SMA von oben nach unten kreuzt und der Preis unterhalb der EMA liegt.
Standardparameter: `fast_length=5`, `slow_sma = sma(fast_length * 4)`, `EMA(200)` als Trendfilter.

### Exit
Vollausstieg ausschliesslich ueber ATR-basiertes Take-Profit oder Stop-Loss.
- Long: `limit = entry_price + ATR(14) * 2.0`, `stop = entry_price - ATR(14) * 1.0`
- Short: `limit = entry_price - ATR(14) * 2.0`, `stop = entry_price + ATR(14) * 1.0`

### Stop-Loss
ATR-basierter Initial-Stop auf Basis des Fill-Preises.
Long: `sl_price = entry_price - ATR(14) * 1.0`
Short: `sl_price = entry_price + ATR(14) * 1.0`

### Trailing Stop (falls vorhanden)
Kein klassischer Trailing Stop. Der Adapter speichert fuer die Validierung die am Exit-Bar materialisierte Stop-Loss-Ebene.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| SMA Fast | Length | 5 |
| SMA Slow | Length | 20 |
| EMA Trend Filter | Length | 200 |
| ATR | Period | 14 |
| Take Profit | ATR Multiplier | 2.0 |
| Stop Loss | ATR Multiplier | 1.0 |
| EMA Filter | Enabled | true |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: PyneCore-Adapter skaliert die finalen Trades auf `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
pynecore — die Strategie nutzt Pine-/PyneCore-typische `strategy.entry()` / `strategy.exit()`-Orderlogik und ist bereits als PyneCore-Skript umgesetzt.
