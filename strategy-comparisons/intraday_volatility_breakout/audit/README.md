# Strategy-Test-Audit: IntradayVolatilityBreakout (MT5 ↔ Platform)

Branch: `strategy-test-audit`. Ziel: MT5-Code (`../IntradayVolatilityBreakout.mq5`,
Referenz-Runs in `/home/ubuntu/trading-platform-references/`) gegen die
Platform-Strategie (`strategies/intraday_volatility_breakout/`, native Engine)
verifizieren, bis Trade-Listen und Stats (nahezu) identisch sind.

## Referenzen (alle drei trianguliert, Preise/Zeiten maxdiff 0.0)

| Quelle | EURUSD H1 | NAS100 M15 | SP500 M1 |
|---|---|---|---|
| MT5-Tester-Logs (USD-Runs, deals) | 492 / +22.080,69 / WR 45,1 % | 597 / +51.682,60 / WR 43,6 % | 512 / +47.379,01 / WR 45,5 % |
| XLSX-Reports (EUR-Runs, inkl. Swaps) | 492 / +21.868,81 / PF 1,197557 / Sharpe 2,71 | 597 / +43.791,14 / PF 1,341993 / Sharpe 4,68 | 512 / +39.255,92 / PF 1,352901 / Sharpe 4,60 |
| M1-CSVs (gleiche Ticks wie Tester) | 20.02.–02.04.26 | 20.03.–01.05.26 | 20.03.–15.05.26 |

Kontrakte (auf den Cent verifiziert): EURUSD 1 Lot = 100.000, Indices 1 Lot = 1.
XLSX = EUR-Account (500 € Risk, Swaps, USD→EUR-Konvertierung), Logs = USD-Runs.

Reproduktion der Referenz-Seite:

```bash
.venv/bin/python strategy-comparisons/intraday_volatility_breakout/audit/parse_mt5_reference.py --out /tmp/audit_ref
```

(Skript failt laut bei jeder Inkonsistenz: Deal-Sequenz, Volumen-Paarung,
Final-Balance-Delta, XLSX↔Log-Keys.)

## Platform-Backtests (exakte MT5-Daily-ATR injiziert, sonst andere ATR-Historie)

Config: native Engine, M1, `fixed_risk_amount=500`, `max_leverage=100`,
`spread_price_col='spread_price'`, `signal_exit_fill='open'`,
MT5-CSVs als `mt5audit`-Symbole (inkl. Spread-Spalte in Preis-Units).

| Fenster | MT5 | Platform | Gemeinsame Trades | PnL-Diff |
|---|---|---|---|---|
| EURUSD | 7 / −1.414,20 | 7 / −1.415,60 | 7/7 | −1,40 (Ticks-Rundung) |
| USATECH | 10 / +2.222,41 | 9 / +1.361,63 | 9/9, dTot **+0,00** | 1 Trade fehlt (s.u.) |
| USA500 | 14 / +1.650,32 | 13 / +788,27 | 13/13, dTot **+0,02** | 1 Trade fehlt (s.u.) |

Entries exakt (SHORT = Bid-Open; LONG = Open + Spread, 16/16 auf 0.000000),
SL-Exits centgenau, Lots exakt, Time-Exits = 22:55-Open (+Spread bei SHORT).

## Gefundene Abweichungen (alle belegt, Fixes im Branch)

1. **ATR-Historie** (kein Codefehler): CSV-Wilder (6 Wochen) vs. MT5-Wilder
   (8 Jahre) → median ~5 % Level-Diff. Neu: `warmup_start` (Adapter/Engine).
2. **`entry_open` bei Lücken**: MT5-`iBarShift` nimmt nächste Bar (28.04. USATECH:
   16:29 statt 16:32) → Fix in Strategie + Tests.
3. **`trade_direction` vor Reverse-Swap** erlaubte falsche Seiten
   (nur direction≠both + Reverse) → Filter nach Mapping + Tests.
4. **Engine hatte kein Spread-Modell**: LONG-Entries um vollen Spread daneben →
   `spread_price_col` (LONG ask/SHORT bid, opt-in) + Tests.
5. **Lots aus |Fill−SL** statt MT5-ATR-Distanz → `stop_distance`-Sizing + Tests.
6. **Time-Exits füllten per Bar-Close** statt First-Tick (Bar-Open) →
   `signal_exit_fill=open` (opt-in, SHORT-Close +Spread) + Tests.
7. **03-20-Ersttag-Trades fehlen** (je ~+$861): Regime braucht Vortag, CSV beginnt
   dort — Datenlimit, kein Fix möglich.
8. Nicht replizierbar: MT5-Tick-Rundung (sub-tick), EUR-Stale-Rates aus
   Dukascopy-Löchern (22–23h UTC), Real-Tick-Spikes vs. M1-Export (3 SL-Timings,
   Preise gleich), MT5-Sharpe (braucht Tick-Equity inkl. Floating), Swaps
   (Engine-Modell fehlt; post-hoc exakt injizierbar).

Framework: **native** (vectorbt ignoriert Stops ohne Intrabar-Resolution und kennt
kein Fixed-Risk — für diesen EA ungeeignet; 7 s vs. ~110 s Laufzeit).
