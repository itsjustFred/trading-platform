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

**Fill-Konvention (Version 1.1.0):** Marktausstiege auf Close-Basis (Supertrend-Flip nach bearish) tragen kein `exit_price` mehr (kein Close-Fill) und fuellen am naechsten Bar-Open (`signal_exit_fill="next_open"`). Nur das **initiale** Supertrend-Band ist auf native protective: `sl_price` wird nur in der Entry-Bar geschrieben, der Stop des Engines zieht also nie nach. Das nachgezogene Level reist in `exit_price`/`trailing_stop_at_exit` der Exit-Bar, das Engines nur mit `exit_order_type="level"` als Level ausfuehren (wird hier nicht gesetzt) — ein Trailing-Stop- oder Flip-Ausstieg fuellt daher als Market-Exit zum naechsten Bar-Open, das nachgezogene Level selbst wird nicht als Fill-Preis erzwungen.

### Stop-Loss
Initialer Stop-Loss entspricht beim Entry dem unteren Supertrend-Band.
Formel: `sl_price = supertrend_lower_band`.

### Trailing Stop (falls vorhanden)
Ja. Das untere Supertrend-Band fungiert als aktiver Trailing Stop; fuer Long-Trades wird das Stop-Level nur nach oben nachgezogen (`max(previous_stop, lower_band)`).
Das verwendete Exit-Level wird in `trailing_stop_at_exit` festgehalten.
`use_trailing_sl` ist kein Parameter dieser Strategie; wird er dennoch gesetzt, lehnt native die Konfiguration ab (Decision 2026-11-23).

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Supertrend | ATR Period | 10 |
| Supertrend | Multiplier | 3.0 |
| DEMA | Length | 9 |

### Indikator-Semantik (P3, #317)
Supertrend nutzt `ta.atr` (Wilder/RMA, SMA-Seed) und die manuelle Pine-Bandrekursion (`nz(band[1], band)`); DEMA ist `2*ta.ema - ta.ema(ta.ema)` mit SMA-Seed. Warm-up-Bars sind `NaN`, nie `0`. Gegenueber der Legacy-Implementierung: kein SMA(TR), kein Null-Warm-up, keine First-Seed-EMAs.

## Risk-Management
- Stop-Distanz: Supertrend-/ATR-basiert ueber das untere Band
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — die Pine-Logik wurde bereits manuell in vektorisierbare Python-Indikatoren uebertragen.
