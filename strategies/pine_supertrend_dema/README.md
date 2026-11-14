# Supertrend + DEMA

## Quelle
- Original: Python-Strategie im Repository, manuelle Pine-Script-Konvertierung aus `indikatoren-repo`
- Konvertiert: 2026-03-08

## Kernlogik

### Entry
Long-Entry, wenn der Supertrend von bearish auf bullish flippt und der Schlusskurs gleichzeitig oberhalb der DEMA liegt.
Signal: `trend == 1`, `trend[-1] == -1` und `close > DEMA(9)`.

### Exit
Vollausstieg bei einem von zwei Events:
1. Trailing-Stop-Hit: Das Tief der Bar erreicht oder unterschreitet das aktuelle untere Supertrend-Band.
2. Supertrend-Flip nach bearish: `trend == -1` und `trend[-1] == 1`.

**Fill-Konvention (Version 1.1.0):** Marktausstiege auf Close-Basis (Supertrend-Flip nach bearish) tragen kein `exit_price` mehr (kein Close-Fill) und fuellen am naechsten Bar-Open (`signal_exit_fill="next_open"`). Der Trailing-Stop bleibt ein explizites Level (`exit_price`).

### Stop-Loss
Initialer Stop-Loss entspricht beim Entry dem unteren Supertrend-Band.
Formel: `sl_price = supertrend_lower_band`.

### Trailing Stop (falls vorhanden)
Ja. Das untere Supertrend-Band fungiert als aktiver Trailing Stop; fuer Long-Trades wird das Stop-Level nur nach oben nachgezogen (`max(previous_stop, lower_band)`).
Das verwendete Exit-Level wird in `trailing_stop_at_exit` festgehalten.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Supertrend | ATR Period | 10 |
| Supertrend | Multiplier | 3.0 |
| DEMA | Length | 9 |

## Risk-Management
- Stop-Distanz: Supertrend-/ATR-basiert ueber das untere Band
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — die Pine-Logik wurde bereits manuell in vektorisierbare Python-Indikatoren uebertragen.
