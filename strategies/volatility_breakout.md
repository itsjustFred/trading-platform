# Volatility Breakout

## Quelle
- Original: Python-Strategie im Repository, inspiriert durch `zenomtrader-discord`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn der Schlusskurs der aktuellen Bar das obere Session-Band von unten nach oben ueberschreitet.
Formel: `upper_band = session_open + ATR(5) * 0.3`.
Signal: `close > upper_band` und `close[-1] <= upper_band[-1]`.

### Exit
Vollausstieg per Stop-Loss, sobald das Tief der Bar den beim Entry fixierten Session-Open beruehrt oder unterschreitet.
Signal: `low <= entry_session_open`, Ausfuehrung zum Stop-Level.

### Stop-Loss
Initialer und fixer Stop-Loss liegt auf dem Session-Open der Entry-Session.
Formel: `sl_price = entry_session_open`.

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| ATR | Period | 5 |
| ATR | Multiplier | 0.3 |
| Session Open | Quelle | Erster Open jeder Session |
| Breakout Band | Formel | `session_open +/- ATR * 0.3` |

## Risk-Management
- Stop-Distanz: session-open-basiert (`entry_close - session_open`)
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — rein vektorisierte Preis-/ATR-/Session-Logik ohne Pine-spezifische Laufzeitfunktionen.
