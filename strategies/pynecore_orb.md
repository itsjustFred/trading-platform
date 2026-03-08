# Opening Range Breakout

## Quelle
- Original: PyneCore/Pine-Style Python-Strategie im Repository, manuelle Konvertierung aus NinjaTrader 8 (C#)
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn die schnelle SMA der Opening-Range die langsame SMA von unten nach oben kreuzt und der Preis oberhalb der EMA liegt.
Short-Entry, wenn die schnelle SMA die langsame SMA von oben nach unten kreuzt und der Preis unterhalb der EMA liegt.
Standardparameter: `or_length=5`, `slow_sma = sma(or_length * 4)`, `EMA(200)` als Trendfilter.

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
| Opening Range SMA Fast | Length | 5 |
| Opening Range SMA Slow | Length | 20 |
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
