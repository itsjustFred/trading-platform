# Post VIX Dip Buy

## Quelle
- Original: Pine Script v5 `post vix dip buy pine.txt` (Post-VIX Panic Intraday Long v4 [Dip + ORB Hybrid])
- Discord: `https://discord.com/channels/1230977283888648315/1458625986039119982/1458626059988762696`
- Konvertiert: 2026-03-08, source-faithful Rewrite: 2026-09-09; Version 1.2.0: Clock-Exits eine Bar frueher fuer Next-Open-Fills

## Kernlogik

### Entry
Long-Only, eine Position zur Zeit. Regime-Gate auf Tagesbasis (VIX + SPX aus dem Vortag):
`VIX chg% >= 10` UND `VIX close >= 19` UND `SPX chg% <= -1%` am Panic-Tag, danach 1–5 Tage aktiv (`daysSince 1..5`), plus Bull-Filter `daily close > SMA(200)` (default AN). SPX-Tageswerte stammen aus dem Strategie-Frame selbst (Quelle laeuft auf SPX/SPY-Chart).
- Dip-Leg (9:30–10:59 NY): `close < VWAP` (tagesreset, hlc3, volumengewichtet falls Volume vorhanden), Wilder-`RSI(14) < 40`, `low <= Vortag-Tief * 1.015` (`useNearSupport`, Buffer 1.5 %, default AN; `requireBullCandle` default AUS).
- ORB-Leg (9:30–10:00 Range aus 9:30–9:59-Bars, Breakout 10:00–14:00 NY): Cross `close > OR-High` nach `close[-1] <= OR-High` (default AN).
Die VIX-Reihe wird aus lokalem `VIX/1d.parquet` auf die SPY-Bars gemappt.

> **Cleanup #35 / Fail-Closed:** Fehlende VIX-Daten schliessen das Regime-Gate
> (**fail-closed**): ohne `VIX/1d.parquet` entstehen keine Entries. Die Spalten
> `VIX_Data_Available` / `VIX_Fallback_Used` spiegeln diesen Zustand wider.
> Die erfundene RSI35/SMA20/ATR-Stop/Time-Exit-Logik des alten Ports ist entfernt.

### Exit
1. Stop-Loss `-0.3%` ab Entry (`low <= entry * 0.997`), Take-Profit `+2%` (`high >= entry * 1.02`); gleichbarige Treffer: Stop zuerst (konservativ).
2. Power-Hour-Scale zum 14:00-NY-Open (Signal-Exits fuellen am naechsten Bar-Open, `next_open`; das Signal geht auf der ersten Bar raus, deren Ende 14:00 erreicht, Bar-Abstand je NY-Tag; kein expliziter `exit_price`): als **Voll**-Exit mit Reason `scale_50pct_power_hour` modelliert (vectorbt kennt keine Partial Fills; dokumentierte Approximation der 50 %-Skalierung).
3. EOD-Flatten zum 15:50-NY-Open (`eod_flatten`, gleiche Regel). Eine am naechsten NY-Tag noch offene Position (fehlende Bars) wird auf dessen erster Bar geflattet (`eod_flatten`).

### Stop-Loss
`sl_price = entry * (1 - 0.3 %)`, `tp_price = entry * (1 + 2 %)` stehen als Indikatoren fuer Sizing/intrabar-Aufloesung bereit.

### Trailing Stop (falls vorhanden)
Kein Trailing Stop (Quelle hat keinen).

### Partial Exits (falls vorhanden)
Quelle: 50 % Scale-Out ab 14:00 NY — in vectorbt als Voll-Exit approximiert (s. Exit).

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| VIX Panic | chg% / Level / SPX-Drop | 10.0 / 19.0 / 1.0 |
| Aktiv-Fenster | Tage nach Panic | 1–5 |
| Bull-Filter | SMA | 200 (daily close) |
| RSI | Wilder-Length / Oversold | 14 / 40 |
| VWAP | Reset | taeglich (hlc3) |
| Near-Support | Buffer | 1.5 % |
| ORB | Range / Breakout (NY) | 9:30–10:00 / 10:00–14:00 |
| TP / SL | % ab Entry | +2.0 / -0.3 |
| Power-Hour / EOD (NY) | ab | 14:00 / 15:50 |

## Risk-Management
- Stop-Distanz: 0.3 % fix ab Entry
- Position Sizing: Runner ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — Regime-Gate plus intraday Dip/ORB-Regeln sind ohne event-getriebene Orders in einem vektorisierten Workflow mit Next-Open-Execution abbildbar (serielastig durch Tages-Resampling, aber deterministisch).
