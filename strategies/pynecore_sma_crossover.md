# SMA Crossover (PyneCore)

## Quelle
- Original: PyneCore/Pine-Style Python-Strategie im Repository (interne Vorlage)
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn die schnelle SMA die langsame SMA von unten nach oben kreuzt.
Signal: `SMA(10) > SMA(30)` und `SMA(10)[-1] <= SMA(30)[-1]`.

### Exit
Vollausstieg bei einem von zwei Events:
1. Stop-Loss-Hit auf dem ATR-basierten Schutzstop.
2. Bearishes Crossunder der schnellen SMA unter die langsame SMA.

### Stop-Loss
Retrofit fuer den Agent-Standard: ATR-basierter Stop-Loss auf Basis des Fill-Preises.
Formel: `sl_price = entry_price - ATR(14) * 2.0`.

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert. Der Adapter speichert fuer Validierungszwecke die am Exit-Bar wirksame Stop-Loss-Ebene.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| SMA Fast | Length | 10 |
| SMA Slow | Length | 30 |
| ATR | Period | 14 |
| Stop Loss | ATR Multiplier | 2.0 |

## Risk-Management
- Stop-Distanz: ATR-basiert
- Position Sizing: PyneCore-Adapter skaliert die finalen Trades auf `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
pynecore — die Strategie verwendet Pine-/PyneCore-typische `strategy.entry()`, `strategy.exit()` und `strategy.close()`-Semantik.
