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

## Volle Historie 2018–2026: ATR-Glättung, D1-Bars, Broker-Uhr

Die drei Ursachen unten erklären fast alle Abweichungen über die volle Historie
(492/597/512 MT5-Trades). Evidenzquelle ist jeweils das EA-Log: Pro Handelstag
schreibt der EA um 16:30 Serverzeit `Levels calculated | Open: … | Stop: …`, also
den M1-Open um 16:30 Server und `Stop = 0.25 · iATR(D1, 5)[shift 1]`.

1. **iATR ist SMA(TR, 5), nicht Wilder.** Der EA nutzt den eingebauten
   `iATR(_Symbol, PERIOD_D1, …)` (MetaQuotes-ATR: laufende Summe / Periode). Auf
   Broker-D1-Bars nachgerechnet trifft SMA(TR,5) den geloggten ATR auf 1e-14
   (EURUSD 2024–2026: 568 von 641 Tagen; NAS100 1.334, SP500 1.325 Tage gesamt),
   Wilder an keinem einzigen Tag. Der alte Test `test_mt5_atr_uses_sma_seed_then_wilder_recurrence`
   hat also falsche MT5-Semantik festgeschrieben; Wilder ist jetzt `_wilder_atr`
   (`atr_smoothing="wilder"`), `_mt5_atr` ist die SMA.
2. **D1-Bars = Handelstage auf der Serveruhr.** Die Platform nahm für EURUSD das
   UTC-Tages-Parquet (Sonntagabend-Zeilen, falsche Tagesgrenzen) bzw. resamplte
   M1-Bars, die an Wochenenden/Feiertagen/geschlossenen Stunden mit flachen
   Volumen-0-Bars aufgefüllt sind (`m1_bid.parquet`: ~940k Filler-Bars je Symbol).
   Diese erzeugten Sa/So-Zeilen im 5-Tage-Fenster. MT5 hat nur Bars mit Ticks.
   Jetzt: D1 aus M1 auf der Broker-Uhr, Filler-Bars ignoriert
   (`daily_skip_filler_bars`), Sa/So-Zeilen verworfen, außer die Broker-Historie
   führt sie (`daily_weekend_bars="auto"`). Belegt: 2018–2023 hat MT5 *keine*
   Sonntag-23:00-Bars (US-only-DST-Wochen), 2024-03-10/17/24/31 und 2024-11-09
   *schon* (ATR jeweils nur mit dieser Wahl exakt).
3. **Serveruhr ist keine zivile Zeitzone.** Aus dem geloggten 16:30-Open gegen
   UTC-M1 (nur Tage, an denen genau ein Offset passt: 3.999 Tage) und den
   MT5-M1-CSV-Exports (EURUSD 2026-02-20..04-02 über US- *und* EU-Umstellung):
   - Legacy-Historie bis 2023-12: Forex = `Europe/Athens` (EU-Daten), Indizes =
     **fix UTC+2** ohne DST (0 Widersprüche bei je ~1.100 entschiedenen Tagen);
   - 2023-12 bis 2024-04-05: fix UTC+2 für alle (Woche 2024-04-01 noch +2);
   - ab 2024-04-06: New-York-Close (`America/New_York` + 7 h, UTC+2/+3 an
     **US**-Daten), aber Herbst 2024 eine Woche zu spät (2024-11-04..08 noch +3).
   Kein IANA-Zonenname passt; modelliert als explizite Segmente in
   `shared/broker_clock.py` (`dukascopy_mt5_forex`, `dukascopy_mt5_index`,
   generisch `ny_close`), Tests um jede Umstellung 2018–2026. Modell vs.
   Log: 3.993/3.999 Tage; die 6 Ausreißer sind isolierte EURUSD-Tage mitten in der
   Saison (zufällig gleicher Preis eine Stunde früher).

### Parität vorher/nachher (Paarung: Server-Handelstag + Richtung)

`score_parity.py` (dieses Verzeichnis) gegen `parse_mt5_reference.py`-Output;
PnL = Preis·Lots·Kontrakt (USD, wie die Log-Runs), ohne Währungsumrechnung.

| Symbol | MT5 | vorher matched / mt5_only / pf_only | nachher matched / mt5_only / pf_only | gleiche Entry-Minute nachher | PnL MT5 | PnL vorher | PnL nachher |
|---|---|---|---|---|---|---|---|
| EURUSD H1 | 492 | 265 / 227 / 233 | **484 / 8 / 7** | 479 | 22.080,69 | 29.537,38 | 21.391,36 |
| NAS100 M15 | 597 | 244 / 353 / 345 | **525 / 72 / 47** | 508 | 51.682,60 | 42.599,10 | 43.319,01 |
| SP500 M1 | 512 | 222 / 290 / 355 | **484 / 28 / 20** | 461 | 47.379,01 | 71.758,82 | 32.633,26 |

Ablation (EURUSD/NAS100/SP500 matched): nur SMA 220/204/197, nur Wochenend-Drop
420/408/344, nur Broker-Uhr 253/291/280, Wochenend-Drop+SMA 448/431/381,
Uhr+Drop+SMA 482/515/474, + Filler-Skip + Auto-Wochenende (final) 484/525/484.
Die Einzelfaktoren helfen allein kaum (SMA allein verschlechtert sogar, weil das
Fenster mit Wochenend-Zeilen verseucht ist) – erst zusammen wird der ATR exakt.

### Verbleibende Abweichungen

- **Datenquelle vor 2024 / vor 2020 (Indizes):** Platform-M1 (Dukascopy) weicht
  von der Broker-Historie ab, ATR median 0,1–0,2 % (EURUSD 2018–2023), NAS100
  2018–2019 median 2,5–9,5 %, SP500 2020–2023 1–4 %. Knappe Level-Kreuzungen
  kippen (z. B. EURUSD 2019-03-11 SHORT nur MT5, 2018-04-19 SHORT nur Platform).
  2018–2019 stellen 90 der 119 NAS100- und 34 der 48 SP500-Waisen.
- **Broker-Historienlücke:** USATECH hat 2023-08-28..2023-11-09 keine EA-Aktivität
  im Log (keine `Levels calculated`) → 11 Platform-only-Trades.
- **Platform-Datenlücke:** USTEC M1 fehlt 2026-04-06..08 → mt5_only.
- **Warm-up:** Platform-Daten beginnen 2018-01-01, MT5 hat D1 ab 2017
  (EURUSD 2018-01-04 LONG fehlt).
- **Jahreswechsel-D1:** 2019-01-02, 2020-12-30/31 (EURUSD) ATR 14–24 % daneben –
  Feiertags-D1-Bars der Broker-Historie anders.
- Einzelne Tage mit exaktem ATR (EURUSD 2026-02-17/05-27, SP500 2024-05-30,
  2026-03-05/06-02): M15-Close-/Level-Gleichstand bzw. Tick-vs-M1-Unterschiede.
- **PnL-Delta** trotz hoher Match-Quote kommt aus den Waisen und dem Exit-Pfad:
  SP500 −14,7k = Waisen −11,2k (davon 2018–2019 −9,6k) + gematchte −3,5k (davon
  −3,2k aus 10 Trades mit anderem Exit-Typ SL↔Time, Real-Ticks vs. M1);
  NAS100 −8,4k = Waisen −5,6k + gematchte −2,8k; EURUSD −0,7k.

### Gleicher Bug außerhalb dieser Strategie

`validation/statistics/cross_platform.py` las die MT5-XLSX-Zeiten als
`Europe/Athens`; für diese Broker-Historie ist das 2018–2023 für Indizes im
Sommer und ab 2024-04 in den US-only-DST-Wochen falsch (1 h, betroffen sind
389/1024 SP500-, 392/1194 NAS100- und 18/984 EURUSD-Zeitstempel der Reports).
Behoben: die Vergleichsmatrix importiert die Reports jetzt über denselben
`broker_clock` (`dukascopy_mt5_forex` / `dukascopy_mt5_index`), den auch
`scripts/regenerate_mt5_parity_artifacts.py` für die Platform-Runs nutzt.
`scripts/analyze_mt5_parity.py` nutzt denselben Weg: Report-Import und
Session-/Jahres-Bucketing laufen auf der Broker-Serverzeit des jeweiligen
Symbols statt auf `Europe/Athens`.
## Hinweis 2026-11-13: neuer Default `signal_exit_fill="next_open"`

Der Owner-Entscheid vom 2026-11-13 lässt Signal-Exits standardmäßig zum **nächsten**
Bar-Open füllen (wie Entries). Der MT5-Time-Exit füllt aber am ersten Tick der
Exit-Minute = Open der Signal-Bar selbst (22:55). Die MT5-Parity-Läufe müssen deshalb
weiterhin `signal_exit_fill='open'` setzen (Config oben, unverändert, ergibt identische
Ergebnisse); mit dem neuen Default liegen die Time-Exits genau eine Bar (M1: eine Minute)
zu spät (Exit-Preis-Treffer auf den Audit-Fenstern 6→0 bzw. 3→0).
