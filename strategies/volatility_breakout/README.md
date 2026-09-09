# Volatility Breakout

## Quelle
- Original: NT8 `optimized_version_vola_breackout nt8.txt` (VolaBreakout: Tages-ATR-Breakout um den RTH-Open)
- Discord: `https://discord.com/channels/1230977283888648315/1342609786633584670/1433816630118449322`
- Konvertiert: 2026-03-08, source-faithful Rewrite: 2026-09-09 (inventierter ATR-Regime-Filter und MT5-Clock entfernt)

## Kernlogik

### Entry
Long- und Short-Breakouts um den Session-Open (beide Seiten, kein Richtungsfilter).
Tages-ATR(5, Wilder) aus Tages-Bars, Wert der **Vortages**-Bar (`[1]`):
- `long_level = rthOpen + 0.3 * ATRprev`, `short_level = rthOpen - 0.3 * ATRprev`.
- Long: `close > long_level` (Market), Short: `close < short_level` (Market), nur wenn flat, nur 08:30–15:00 NY, erst ab 6. Tages-Bar und 20. Intraday-Bar (`CurrentBars[1] >= 6`, `BarsRequiredToTrade = 20`).
- `rthOpen` = Open der ersten In-Session-Bar des Tages (NY-Datum).

### Exit
1. Schutz-Stop AUF DEM SESSION-OPEN (Stop-Order): Long bei `low <= rthOpen`, Short bei `high >= rthOpen`, Fill am `rthOpen` (`stop_loss`).
2. Force-Close 60 Min vor Session-Ende (default): ab 14:00 NY wird jede offene Position per Market geschlossen (`force_close`). Laeuft vor dem Session-Gate, greift also auch ausserhalb des Entry-Fensters.
3. Session-Close (`IsExitOnSessionCloseStrategy`, 30 s): Flatten an der letzten In-Session-Bar (`session_exit`); relevant v. a. bei `ForceCloseInMin = 0`.
4. Wiedereinstieg nach Force-Close/Stop bleibt moeglich (Quelle setzt ein Flag, das nie gelesen wird — effektives Verhalten, dokumentiert).

Jeder Exit setzt `exit_price` + `exit_reason`, jeder Entry `sl_price` (= rthOpen) + `direction`: kein NaN-Sizing/Pricing (Fix der NaN-PnL-Kaskade: frueher erzeugte ein preisloser Time-Exit `pnl = NaN`, danach war das Equity `NaN` und alle Folge-Trades hatten `qty/pnl = NaN` bei 8 gezaehlten Trades).

### Stop-Loss
Initialer und (mangels Trailing im Original) laufender Schutz-Stop auf dem Session-Open der Entry-Session.
Formel: `sl_price = entry_session_open`.

### Trailing Stop (falls vorhanden)
Kein Trailing Stop (Original nutzt fixen Session-Open-Stop).

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Tages-ATR | Periode / Glättung / Versatz | 5 / Wilder / Vortages-Bar |
| Breakout | Offset | 0.3 |
| Session Open | Quelle | Erste In-Session-Open (NY-Tag) |
| Breakout Band | Formel | `session_open +/- 0.3 * ATRprev` |
| Session (NY) | Entry-Fenster | 08:30–15:00 |
| Force-Close | Min vor Session-Ende | 60 (→ 14:00 NY) |

## Risk-Management
- Stop-Distanz: `|entry - session_open|` (≈ 0.3 * ATR zum Entry)
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price` (NT8-Kontrakt-/Risiko-%-Rechnung ist engine-seitig)
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — rein vektorisierte Preis-/Tages-ATR-/Session-Logik ohne NT8-Laufzeit (SessionIterator); Tages-Resampling plus NY-Session-Uhr sind deterministisch abbildbar.
