# Intraday Volatility Breakout

## Quelle
- Original: MQL5 (MetaTrader 5), `strategy-comparisons/intraday_volatility_breakout/IntradayVolatilityBreakout.mq5`
- Version: 1.2.0 (1.2.0: Time-Exit-Signal eine Bar vor dem Exit-Open für `signal_exit_fill="next_open"`; Stale-Exit am ersten Bar des Tages statt Last-Bar-Look-ahead)
- Konvertiert: 2026-06-24

## Ziel
Intraday-Breakout-Strategie (bzw. Fade-Breakout im Reverse Mode). Pro Tag wird einmalig ein Setup berechnet: auf Basis der Tages-Open zum `Entry_Time` und dem Daily-ATR werden obere und untere Breakout-Level gezogen. Im Entry-Fenster wird in Richtung des Breakouts (oder dagegen) gehandelt. Alle Positionen werden spätestens zum `Exit_Time` geschlossen.

## Kernlogik

### Entry
1. **Setup-Berechnung** (einmal pro Tag, sobald `Entry_Time` erreicht ist):
   - Lese `ATR(Period=ATR_Period)` auf `PERIOD_D1` (vollendeter Vortages-Bar, Buffer-Shift=1).
   - Bestimme `Open`-Preis der M1-Bar exakt zum Zeitpunkt `Entry_Time`.
   - `upper_level = entry_open + ATR * ATR_Multiplier`
   - `lower_level = entry_open - ATR * ATR_Multiplier`
   - `stop_distance = ATR * Stop_ATR_Multiplier`

2. **Regime-Filter** (optional, wird vor jedem Trade geprüft):
   - Lese `ATR(Period=ATR_Short_Period)` und `ATR(Period=ATR_Long_Period)` beide auf `PERIOD_D1` (Buffer-Shift=1).
   - `short_atr < long_atr * ATR_Regime_Factor` -> `REGIME_LOW_VOLATILITY`
   - `short_atr > long_atr * ATR_Regime_Factor` -> `REGIME_HIGH_VOLATILITY`
   - Wenn `Regime_Filter_Mode == REGIME_LOW_VOLATILITY`, dann erlaube nur Trades wenn `short_atr < threshold`.
   - Wenn `Regime_Filter_Mode == REGIME_HIGH_VOLATILITY`, dann erlaube nur Trades wenn `short_atr > threshold`.

3. **Breakout Entry** (`Reverse_Mode = false`):
   - **Long:** `signal_price >= upper_level` (und `Trade_Direction` erlaubt Long)
   - **Short:** `signal_price <= lower_level` (und `Trade_Direction` erlaubt Short)

4. **Fade Entry** (`Reverse_Mode = true`):
   - **Short:** `signal_price >= upper_level` (und `Trade_Direction` erlaubt Short)
   - **Long:** `signal_price <= lower_level` (und `Trade_Direction` erlaubt Long)

5. **Signal-Preis (`GetSignalPrice`):**
   - Wenn `Use_Candle_Close = true`: `Close` der letzten vollendeten Bar auf `Confirmation_Timeframe` (default `PERIOD_M15`).
   - Wenn `Use_Candle_Close = false`: `SYMBOL_LAST` (letzter Tick-Preis).
   - In der MQL5-Logik wird bei `Use_Candle_Close` nur bei neuen Bars der Timeframe geprüft (Bar-Time-Change-Filter via `g_last_confirm_bar`).

6. **Entry-Zeitfenster:**
   - `now >= Entry_Time AND now < Max_Entry_Time`
   - Keine Mehrfach-Eintritte pro Tag (`g_trade_taken` Flag).

### Exit
- **Time-Exit:** Alle Positionen werden zu `Exit_Time` geschlossen (`ShouldClosePosition` -> `CloseAllPositions`).
- **Stop-Loss:** `sl = entry_price +/- stop_distance` (kein Take-Profit).
- **Stale-Position-Schutz:** Positionen, die vom Vortag stammen (nicht am selben Tag eröffnet), werden ebenfalls geschlossen.
- Keine Trailing-Stops, keine Partial Exits, keine TP-Logik.

### Stop-Loss
- `stop_distance = ATR(D1, ATR_Period) * Stop_ATR_Multiplier`
- Long SL: `entry_ask - stop_distance`
- Short SL: `entry_bid + stop_distance`

### Trailing Stop
Nicht vorhanden.

### Partial Exits
Nicht vorhanden.

## Indikator-Einstellungen
| Indikator | Parameter | Wert | Quelle (MQL5) |
|-----------|-----------|------|---------------|
| ATR (Haupt) | Periode / Glättung | 5 / SMA (MT5-iATR) | `iATR(_Symbol, PERIOD_D1, ATR_Period)` |
| ATR (Regime Short) | Periode | 1 | `iATR(_Symbol, PERIOD_D1, ATR_Short_Period)` |
| ATR (Regime Long) | Periode | 5 | `iATR(_Symbol, PERIOD_D1, ATR_Long_Period)` |
| Confirmation Close | Timeframe | M15 | `iClose(_Symbol, Confirmation_Timeframe, 1)` |
| Entry Open | Timeframe | M1 | `iOpen(_Symbol, PERIOD_M1, shift)` zur `Entry_Time` |

## Input-Parameter (MQL5)
| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|--------------|
| `ATR_Period` | int | 5 | ATR-Periode (Daily) für Level-/Stop-Berechnung |
| `ATR_Multiplier` | double | 0.25 | Multiplikator für Breakout-Level-Breite |
| `Stop_ATR_Multiplier` | double | 0.25 | Multiplikator für Stop-Loss-Distanz |
| `Use_Candle_Close` | bool | true | Erfordere Candle-Close-Bestätigung auf Confirmation_TF |
| `Confirmation_Timeframe` | ENUM_TIMEFRAMES | PERIOD_M15 | Timeframe für Close-Bestätigung |
| `ATR_Short_Period` | int | 1 | Kurzfristiger ATR (Daily) für Regime-Filter |
| `ATR_Long_Period` | int | 5 | Langfristiger ATR (Daily) für Regime-Filter |
| `ATR_Regime_Factor` | double | 1.0 | Faktor für Regime-Schwellwert |
| `Regime_Filter_Mode` | ENUM_REGIME_MODE | REGIME_LOW_VOLATILITY | Erlaube Trades nur bei niedriger/hoher Volatilität |
| `Trade_Direction` | ENUM_TRADE_DIRECTION | TRADE_BOTH | Long, Short oder beide Richtungen |
| `Reverse_Mode` | bool | false | Fade statt Follow (spiegelt Signale) |
| `Fixed_Risk_Amount` | double | 500.0 | Festes Risiko in Kontowährung pro Trade |
| `Entry_Time` | string | "16:30" | Start des Entry-Fensters (HH:MM) |
| `Max_Entry_Time` | string | "18:05" | Ende des Entry-Fensters (HH:MM) |
| `Exit_Time` | string | "22:55" | Alle Positionen schließen (HH:MM) |
| `Magic_Number` | int | 123456 | Magic Number für Order-Zuordnung |

## Dateninputs / Backtest-Reports
Die XLSX-Reports des Vergleichs referenzieren folgende Haupt-Timeframes:

| Report/Symbol | Haupt-Timeframe (XLSX) | ATR-Quelle (EA) | Confirmation TF |
|---------------|------------------------|-----------------|-----------------|
| EURUSD | H1 | PERIOD_D1 | M15 (falls `Use_Candle_Close=true`) |
| NAS100 | M15 | PERIOD_D1 | M15 |
| SP500 | M1 | PERIOD_D1 | M15 (falls `Use_Candle_Close=true`) |

Hinweis: Die EA-Logik berechnet ATR und Regime-Filter immer auf `PERIOD_D1`, unabhängig vom Haupt-Timeframe des Backtests. Der `Entry_Open`-Preis wird auf `PERIOD_M1` gesucht. Die Close-Bestätigung (`Use_Candle_Close`) läuft auf `Confirmation_Timeframe` (default M15).

## Risk-Management
- Stop-Distanz: ATR-basiert (`ATR * Stop_ATR_Multiplier`)
- Position Sizing: `lots = (Fixed_Risk_Amount / sl_distance) * (tick_size / tick_val)`, gerundet auf `lot_step`, begrenzt auf `min_lot`/`max_lot`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)
- Kein explizites Leverage-Limit im EA; der Broker-`max_lot` begrenzt.

## Framework-Empfehlung
`vectorbt` — Begründung: Die Strategie nutzt einfache Indikatoren (ATR), hat keine komplexe Order-Management-Logik (kein CTrade-Modify, kein Portfolio), keinen Trailing Stop und keine Partial Exits. Die tickbasierte `OnTick`-Logik lässt sich gut auf barbasierte Entry/Exit-Signale abbilden. Zeitfilter und Signalpreis (Close auf M15) sind als boolean-Masken auf dem DataFrame umsetzbar.

## MQL5 → Python Übersetzungshinweise

| MQL5-Konstrukt | Python-Äquivalent | Hinweis |
|----------------|-------------------|---------|
| `iATR(_Symbol, PERIOD_D1, period)` | `_mt5_atr`: **SMA(TR, period)** über die D1-Bars der Broker-Serverzeit, Shift 1, dann auf den Haupt-Timeframe forward-fillen. | MT5s eingebauter ATR ist ein einfacher gleitender Mittelwert der True Range (laufende Summe/period), **nicht** Wilder (MT4/pandas_ta/TradingView). Belegt am geloggten EA-ATR (`Stop`/0.25): EURUSD 2024–2026 auf 1e-14 identisch, Wilder trifft keinen Tag. `atr_smoothing="wilder"` bleibt wählbar. |
| `PERIOD_D1`-Bars | Resampling der M1-Bars auf Kalendertage der **Broker-Serveruhr** (`broker_clock`); Filler-Bars (flach, Volumen 0) zählen nicht; Sa/So-Zeilen nur, wenn die Broker-Historie sie führt (`daily_weekend_bars="auto"`). | MT5 hat nur Bars mit Ticks. Native UTC-Tages-Parquets haben andere Tagesgrenzen (Sonntagabend-Zeilen) und werden mit `broker_clock` abgelehnt. |
| `TimeCurrent()` / `"16:30"` | `broker_clock` (z. B. `dukascopy_mt5_forex`, `dukascopy_mt5_index`, `ny_close`), siehe `shared/broker_clock.py`. | Serverzeit ist keine zivile Zeitzone: Dukascopy-demo-Historie = Athen (Forex) bzw. fix UTC+2 (Indizes) bis 2023-12, fix UTC+2 bis 2024-04-05, danach New-York-Close (UTC+2/+3 an **US**-Umstelltagen), Herbst 2024 eine Woche verspätet. `session_timezone` bleibt für zivile Zonen. |
| `iOpen(_Symbol, PERIOD_M1, shift)` | Nächste M1-Bar zur `Entry_Time` pro Tag (Ties → frühere Bar) | MT5-`iBarShift(..., exact=false)`-Parität: fehlt die Entry-Bar (Datenlücke), wird die zeitlich nächste Bar genommen — verifiziert am 2026-04-28 USATECH (16:30/16:31 fehlten, MT5 nutzte 16:29). |
| `iClose(_Symbol, Confirmation_Timeframe, 1)` | `df["close"].shift(1)` auf dem Resampled Confirmation-DataFrame; oder direkt `df["close"]` wenn Haupt-TF = Confirmation-TF. | In vektorbasiertem Backtest: Signal triggert am Close der vorherigen Bar. |
| `Use_Candle_Close` | Bestätigungs-Logik als separate boolesche Serie: `signal_price = confirmation_df["close"].shift(1)` | Nur ein Signal pro neuer Confirmation-Bar erlauben. |
| `IsInEntryWindow()` | `(df.index.hour * 60 + df.index.minute >= entry_mins) & (df.index.hour * 60 + df.index.minute < max_entry_mins)` | Tägliche Maske. |
| `ShouldClosePosition()` / `Exit_Time` | Exit-Signal auf der **ersten Bar, deren Ende `exit_time` erreicht** (Bar-Start + Bar-Länge ≥ `exit_time`), danach auf jeder weiteren Bar. | MT5 schließt am ersten Tick ≥ `Exit_Time` = Open der ersten Bar ab `Exit_Time`. Die Engines füllen Signal-Exits am **nächsten** Bar-Open (`signal_exit_fill="next_open"`, Default) — das Signal muss also eine Bar früher kommen: M1 22:54 → Fill 22:55-Open; M15 22:45 / H1 22:00 → Fill 23:00-Open (erster Open ≥ 22:55; die EA-Exit-Minute liegt dort innerhalb der Bar). Die Bar-Länge ist der häufigste Bar-Abstand des Frames; sonst nur Uhr-Zeitplan, kein Blick auf spätere Bars. |
| `PositionSelect(_Symbol)` | In vectorbt: Engine verwaltet Position; in `generate_signals` nur Entry/Exit-Booleans liefern. Maximal ein Trade pro Tag via `~entries.cumsum().groupby(df.index.date).diff().gt(0)` oder ähnlich sicherstellen. | `g_trade_taken`-Flag mappen auf „nur erster Entry-Signal pro Tag". |
| `Reverse_Mode` | Vertausche Long/Short-Bedingungen: `entries_long = lower_hit, entries_short = upper_hit` statt umgekehrt. | Einfache boolean-Flip. |
| `Trade_Direction` + `Reverse_Mode` | Richtungsfilter wird auf die finale Seite nach dem Reverse-Mapping angewandt (MT5 prüft `Trade_Direction` gegen die tatsächlich gehandelte Seite). | Vor dem Fix wurde vor dem Swap gefiltert — bei `long_only`+Reverse entstanden fälschlich SHORTs statt Fade-LONGs. |
| `Fixed_Risk_Amount` / `CalculateLotSize` | Im Adapter via `sl_stop` / `size`-Parameter oder manuell: `size = Fixed_Risk_Amount / (sl_distance * (tick_value / tick_size))` | In `vectorbt` wird Sizing oft über `size` und `sl_stop` gesteuert. |
| `Magic_Number` | Strategie-Name in `STRATEGY_META["name"]"` | Dient nur zur Zuordnung/Filterung. |
| `ParseTime("HH:MM")` | Python `datetime.strptime` oder direkt als `params["entry_hour"], params["entry_min"]` | Parameter als ints übergeben. |
| `IsNewDay()` / `ResetDailyState()` | In vektorbasiertem Backtest: Gruppierung nach `df.index.date`; nur erstes Signal pro Tag erlauben; Levels pro Tag einmalig berechnen. | `groupby(date)`-Pattern. |

## Parameter für MT5-Parität (Platform-seitig)
| Parameter | Default | Bedeutung |
|-----------|---------|-----------|
| `atr_smoothing` | `"sma"` | `"sma"` = MT5-iATR (SMA der TR); `"wilder"` = Wilder-Glättung (nicht MT5). |
| `broker_clock` | `""` | Benannte Broker-Serveruhr aus `shared/broker_clock.py`; schließt `session_timezone`/`session_timezone_offset_hours` und `daily_ohlcv` aus. |
| `daily_weekend_bars` | `"auto"` | `"auto"`: Sa/So-D1-Zeilen verwerfen, außer die Broker-Uhr meldet sie für den Zeitraum; `"drop"`/`"keep"` erzwingen. |
| `daily_skip_filler_bars` | `true` | Flache Bars mit Volumen ≤ 0 (Feed-Padding an Wochenenden/Feiertagen/geschlossenen Stunden) bilden keine D1-Bars. |

Evidenz und Parität pro Symbol: `strategy-comparisons/intraday_volatility_breakout/audit/README.md`.

## Bekannte Abweichungen
- `ORDER_FILLING_FOK` wird nicht explizit simuliert; vectorbt nutzt Market-Order am Open der nächsten Bar.
- `SYMBOL_LAST` (Tick-Preis) wird nicht verfügbar sein; bei `Use_Candle_Close=false` wird `df["close"]` als Proxy verwendet.
- Der EA checkt in `OnTick` Positionen auf Vortags-Staleness und schließt diese am ersten Tick des neuen Tages. Die Strategie meldet dafür ein Exit-Signal auf der **ersten Bar eines neuen Server-Tages** (nur vor dem Entry-Fenster, damit es nie mit einem Entry-Signal zusammenfällt; ohne offene Position wirkungslos). Seit 1.2.0 gibt es kein „letzte Bar des Tages"-Signal mehr (das war Look-ahead): bei vorzeitigem Sitzungsende (Datenlücke über `exit_time`) liegt der Exit deshalb eine Bar nach dem Tageswechsel statt am Tageswechsel selbst.
- Fehlt die Bar vor `exit_time` (Datenlücke), kann das nicht vorausgesehen werden: das Signal kommt auf der ersten vorhandenen Bar ab dort, der Fill eine Bar nach MT5s erstem Tick.
- Time-Exit auf M15/H1-Frames füllt am ersten Open ≥ `exit_time` (23:00), MT5 schließt bereits 22:55; für exakte MT5-Parität die Strategie auf M1-Bars ausführen (die Parity-Läufe tun das).
- Seit 1.2.0 setzt der Time-Exit das Signal **eine Bar vor** den Exit-Open und ist für den Default `signal_exit_fill="next_open"` gebaut. Der frühere Opt-in `signal_exit_fill="open"` ist seit Issue #153 entfernt (Look-ahead); die Parity-Läufe nutzen den Default.
- Der EA verwendet `SYMBOL_ASK` für Long-Entries und `SYMBOL_BID` für Short-Entries. In einer Bar-Engine wird typischerweise `close` als Einheits-Entry-Preis verwendet; eine Ask/Bid-Spread-Simulation ist optional.

## Backtest-Ergebnis (Referenz)
*Noch nicht erstellt — wird in einem späteren Schritt des Konvertierungs-Protokolls ergänzt.*
