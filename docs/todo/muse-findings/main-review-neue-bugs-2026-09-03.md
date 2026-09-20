# Main-Branch Review — Wichtige Bugs, die NICHT in den Issues stehen

- **Datum (UTC):** 2026-09-03 (Stand: 2. Runde + Dup-Bereinigung + Neu-Sortierung)
- **Repo:** `/home/ubuntu/Desktop/trading-platform`
- **Branch:** `main`
- **Commit:** `6caf461367e37aa7dbc3abe91fb71405fc152038` (2026-08-26, „Ignore local .commandcode state directory“)
- **Referenz Issues (geprüft, bewusst NICHT erneut gelistet):** #14 (Sonar-Triage-Epic), #15–#20 (Epics: live_runner, runner/engines, tests, frontend+viewer, data_tools/validation, misc-backend), #21–#28 (live_runner PAUSE/SL-TP/Action-Whitelist, Credentials, Discord-Gates, Broker-Adapter, Risk-Sizing, StockKit-Alias, Destruktiv-Scripts, Scheduler-Hygiene), #29–#32 (CORS/Pfad-Containment/Flask-Dead-Code, Live-Controls/Error-Boundary/Stream-Hygiene, Metric-Contracts/Stats/Viewer-Reliability, Sentry-Frontend), #33–#34 (Test-Isolation, Vacuous-Passes), #35 (Strategies-Parity), #36–#41 (Runner-Contract/Engine-Fixes/Trust-Boundary/Money-Path/Rename), #42–#44 (Data-Tools Atomicity/Downloader/Gate-Hardening), #45–#46 (TS-/Python-Mechanics).
- **Methode:** Manuelle Sichtung von `viewer/main.py`, `viewer/core.py`, `frontend/src`, `runner/`, `strategies/`, `risk/`, `validation/`, `live_runner/`, `scheduler/`, `signal_bots/`, `data_tools/`, `shared/`, `scripts/`, Repo-Root/CI/Secrets; 4 parallele Tiefen-Suchen mit Zeilen-Verifikation am Source; Stichproben-Nachprüfung (u. a. `viewer/main.py:10881`, `live_runner/market_data_stream.py:23`, `runner/stats.py:483-511`, `runner/engines/base.py:49`, `git ls-files`, `.github/`-Baum). 2. Runde mit 2 unabhängigen Agents ohne Vorwissen; danach Dup-Prüfung aller Findings gegen die Issue-Texte #14–#46 (5 Voll-Dups entfernt) und Neu-Sortierung nach Schwere.
- **Lesart:** `critical` = Geldverlust / Datenverlust / RCE / Auth-Bypass / Doppel-Fill; `major` = falsche Kennzahlen/Signale, OOM/Crash-Loops, Staleness, nicht reproduzierbare Optimierung. Jeder Fund nennt `Datei:Zeile(n)` auf `main@6caf461`.

---

## Zusammenfassung (29 aktive Befunde, nach Schwere sortiert)

| # | Titel | Schwere | Ort |
|---|-------|---------|-----|
| 1 | WS-Task-Log ohne Pfadvalidierung + Full-File-Read pro 0,5 s | critical | `viewer/main.py:10881-10904` |
| 2 | Live-Execution Check-Then-Insert-Race → Doppel-Fill | critical | `shared/database.py:1389-1441` (+Constraint `:1286-1291`) |
| 3 | cTrader `submit_order` gibt `""` als Erfolg zurück | critical | `live_runner/ctrader_adapter.py:1357-1382`, Konsument `live_runner/daemon.py:798` |
| 4 | Alpaca-Stream: Handler-Akkumulation + Executor-Blockade + Re-Subscribe-Spin | critical | `live_runner/alpaca_adapter.py:710-729` |
| 5 | Startup-RCE über persistierte Recovery-Commands aus DB | critical | `viewer/main.py:469-488, 510-518`, Resume `lifespan :594-595` |
| 6 | Default All-in: `risk_per_trade_pct=None` → 100 % Equity pro Trade | critical | `runner/engines/base.py:49`, `runner/cli.py:359`, `runner/engine.py:328-349`, `runner/engines/vectorbt_adapter.py:201-206, 260-262` |
| 7 | Echte Alpaca-Account-IDs + Live-Topologie versioniert (`live_deployments.yaml` tracked) | critical (Security/Privacy) | `live_deployments.yaml:2,5,17` (+`:6,:10,:40,:63`), `.gitignore` ohne Regel dafür |
| 8 | Log-API ohne Byte-Cap + Secrets unredigiert in Response | major | `viewer/main.py:10717, 10757-10764`, `viewer/core.py:2459-2464` |
| 9 | Matrix-CSV-Pfad nur String-Check, keine Canonicalisierung | major | `viewer/main.py:6521-6525` (Gegenbild `viewer/core.py:2375-2381`) |
| 10 | Deduplicate/Remove-Empty: nicht-atomar, destruktiv, nicht idempotent | major | `viewer/main.py:6771-6794, 6803-6815`, Delete-Pfad `:2531-2538` |
| 11 | `str(exc)` an Client + falsche 500er statt 4xx/502 | major | `viewer/main.py:6493, 6518, 6936, 6997, 10687, 10762-10764` |
| 12 | Naiv/Aware-Datetime-Mix über DB-/Viewer-Schicht | major | `shared/database.py:324-326, 372-376, 517-518, 587-588, 697-707`, `viewer/core.py:247-257` |
| 13 | OHLCV-Loader: Glob-Injektion + unbegrenzter Load | major | `viewer/core.py:379-385, 405-441` |
| 14 | Fees/Slippage dauerhaft `0.0` → Netto/Sharpe/CAGR überhöht | major | `runner/engine.py:515-516, 532-535`, `runner/engines/vectorbt_adapter.py:305-329` |
| 15 | CAGR aus Trade-Spanne statt Backtest-Fenster | major | `runner/stats.py:320-333, 336-353, 366-367` |
| 16 | Portfolio-Korrelations-Penalty ist pro Trial konstant → wirkungslos | major | `runner/portfolio_optimizer.py:234-249, 259-281` |
| 17 | Optimizer maximiert `p_value` | major | `runner/optimizer.py:51, 298` |
| 18 | Session-TZ via fixe Offset-Stunden + synthetische Tage ohne Zeitstempel | major | `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py:189-205, 217-219` |
| 19 | Readiness-Gate: halbierter Nenner + doppelte Config-Felder + kein Sharpe/DD-Gate | major | `validation/readiness/validator.py:23-27`, `validation/readiness/models.py:65-80` |
| 20 | `_is_crypto_symbol`: Operator-Precedence stuft `BTCUSD` als Stock ein → kein Feed | major | `live_runner/market_data_stream.py:23, 228` |
| 21 | Discord-Cursor `int(message_id)` ohne Guard → Poison-Message-Hot-Loop | major | `live_runner/signal_sources/discord_source.py:24, 463, 486, 540` |
| 22 | Weekly-Candle epoch-aligniert (Do statt Mo) + kein `max_signal_age` → stale Fills | major | `live_runner/signal_sources/execution_timing.py:57-74`, Submit `live_runner/daemon.py:790-794` |
| 23 | Weitere Live-Robustheit (5 Punkte, gemeinsam priorisieren) | major/mittel | `live_runner/daemon.py`, `live_runner/market_data_stream.py`, `scheduler/scheduler.py` |
| 24 | Doppeltes Sizing mit divergenter Config | major (Geld-Risiko) | `live_runner/daemon.py:534-542`, `live_runner/alpaca_adapter.py:480-482`, `live_runner/ctrader_adapter.py:1321-1323` |
| 25 | `signal_import`: TZ-Raten per Spaltenname + unbekannte Action → `buy` | major | `data_tools/signal_import.py:67-77, 91-112` |
| 26 | Remote-Artefakte ohne Integrität; Lücken als Warnings | major | `data_tools/downloaders/histdata_dl.py:200-209`, `data_tools/downloaders/huggingface_dl.py:27-29, 256-261, 277-282` |
| 27 | `validate`: `iloc[Label]`-Bug + Feiertags-False-Positives | major | `data_tools/validate.py:164-178` |
| 28 | Repo-Hygiene: `pyproject` ungepinnt, Lock-Drift, kein CI-Workflow, Katalog-/Migrations-Staleness | major (operativ) | `pyproject.toml:6-29`, `requirements.txt:39,43` vs `uv.lock`, `.github/` ohne `workflows/`, `data_tools/catalog_builder.py:214-232`, `data_tools/cache_manager.py:168-202`, `data_tools/migrate_csv.py:100-131, 195-196` |
| 29 | Sortino manuell: `len(downside)<2` → `0.0`, Target `0` ohne `risk_free` | mittel | `runner/stats.py:497-511` |

---

## Critical

### 1. WS-Task-Log ohne Pfadvalidierung + Full-File-Read-Loop — critical
**`viewer/main.py:10881-10904`**
`log_path = TASK_LOGS_DIR / f"task_{job_id}.log"` ohne Regex-Whitelist, ohne `resolve()`/`is_relative_to`-Check (im Gegensatz zu `GET /api/logs/{log_name}` in `:10725-10727`). Danach `handle.read()` des gesamten Rests alle 0,5 s (`:10911`) und `send_text` pro Zeile. Crafted `job_id` (`../..`, URL-encodete Varianten) kann fremde Dateien streamen; große Logs → OOM/WS-Flood.
**Nicht in Issues:** #29 behandelt String-Prefix-Containment bei Lösch-Endpoints und Bulk-Delete-Pfadverkettung, nicht diesen WS-Pfad. **Fix:** `job_id` auf `^[A-Za-z0-9-]{8,64}$`, `resolve().is_relative_to(TASK_LOGS_DIR)`, Tail mit Byte-Cap/Backpressure wie `core.py:2915`.

### 2. Live-Execution Check-Then-Insert-Race → Doppel-Fill / Double-Spend — critical
**`shared/database.py:1389-1400` vs `:1413-1441`, Constraint `:1286-1291`**
`save_live_execution` prüft `select(dedupe_key).first()` und macht dann `add+commit`; bei `dedupe_key=None` greift der partielle Unique-Schutz (`uq_live_executions_dedupe_key WHERE NOT NULL`) nicht, parallele Fills gleicher `signal_id` erzeugen Duplikate. `save_live_execution_up_to_cumulative (:1425)` nutzt korrekt `BEGIN IMMEDIATE`, hier fehlt es.
**Nicht in Issues:** #16/#31 behandeln `_running_jobs`-In-Memory-Races, nicht diesen DB-Race mit Finanzwirkung. **Fix:** immer `dedupe_key` setzen (UUID-Fallback), Unique-Constraint als einzige Wahrheit + `IntegrityError`-Pfad wie `save_live_signal (:1367-1380)`.

### 3. cTrader `submit_order` meldet `""` als Erfolg — critical
**`live_runner/ctrader_adapter.py:1357-1382`, Konsument `live_runner/daemon.py:798`**
Wenn `_extract_order_id_from_payload` `None` liefert, bleibt `broker_order_id=""`, es wird nur geloggt und `""` zurückgegeben. Der Daemon wertet das als Erfolg: kein Retry, keine Persistenz, Signal still verloren (oder schlimmer: Folgelogik glaubt, eine Position zu haben).
**Nicht in Issues:** #24 behandelt Persist-before-Validate und Volumen-Einheiten, nicht diese Rückgabe-Validierung. **Fix:** `raise RuntimeError`, wenn `order_id is None`, statt Leerstring.

### 4. Alpaca-Stream: Handler-Akkumulation + Executor-Blockade + Hot-Spin — critical
**`live_runner/alpaca_adapter.py:710-729`**
Jede Loop-Iteration ruft `subscribe_trade_updates(_on_trade_update)` ohne Unsubscribe auf — bei Reconnect doppelte Callbacks → Doppel-Persist von Fills. `await to_thread(stream.run)` blockiert für immer einen Executor-Thread (Leak). Der `else`-Zweig setzt nur Backoff zurück ohne Sleep — sauberes Return führt zu engem Re-Subscribe-Spin (Reconnect-Storm).
**Nicht in Issues:** #23 behandelt Discord-Burst-Drops, nicht diese Stream-Idempotenz-Lücke. **Fix:** einmal subscriben, `run` in dediziertem Task/Thread, `else`-Pfad mit Sleep + Duplikat-Guard.

### 5. Startup-RCE über persistierte Recovery-Commands aus DB — critical
**`viewer/main.py:469-488, 510-518`, Resume in `lifespan :594-595`**
`_resume_interrupted_tasks` liest `raw_config["command"]/["cmd"]` aus `background_tasks`-DB und übergibt die Liste direkt an `_start_jforex_task` / `_start_data_task` (`subprocess.Popen`). Keine Re-Validierung gegen die typisierten Builder. Wer die SQLite-DB beschreiben kann (Backup-Restore, Teil-Fix aus #29 bleibt Pfad-Thema), bekommt Befehlsausführung beim Neustart.
**Nicht in Issues:** #29 nennt Resample-Subprocess mit unvalidiertem Client-Pfad und Strategie-Pfad-ACE; #31 nennt Watchdog/`asyncio.run`-Kontext — keiner nennt Vertrauen in persistierte Commands beim Boot. **Fix:** Raw-Commands verwerfen, aus typisierten Feldern (`symbols,timeframes,source`) neu bauen, Allowlist + Signatur. **Dup-Prüfung 2026-09-03:** #18 nennt dieselbe Funktion (`_resume_interrupted_tasks`) nur für die `asyncio.run`-Facette — das Vertrauen in persistierte Commands bleibt neu.

### 6. Default All-in: `risk_per_trade_pct=None` → 100 % Equity pro Trade — critical
**`runner/engines/base.py:49`, `runner/cli.py:359`, `runner/engine.py:328-349`, `runner/engines/vectorbt_adapter.py:201-206, 260-262`**
`risk_per_trade_pct` defaultet `None`; dann `shares/qty = equity/entry_price`. `max_position_pct`/`max_leverage`-Caps greifen in VectorBT nur im `risk_pct`-Zweig. Ein CLI-`run` ohne `--risk-pct` riskiert Totalverlust pro Signal.
**Nicht in Issues:** #25/#41 behandeln Unit-Mix, Cap-Höhe und Rename-Semantik, nicht diesen teuren Default-Pfad. **Fix:** konservativer Default (z. B. `0.01`/`0.20`) oder harter Fehler ohne explizites Sizing.

### 7. Echte Alpaca-Account-IDs + Live-Topologie im Git-Index — critical (Security/Privacy)
**`live_deployments.yaml:2,5,17` (analog `:6,:10,:40,:63`); verifiziert via `git ls-files`**
Paper-Account-IDs (`PA3E767TJ91D` …), `broker_login`/`account_id` und Discord-`channel_id`s sowie Deployment-Topologie (Strategie→Account, Symbole, Risk-Settings) sind versioniert. `.env` (`*.env` in `.gitignore:27-30`) und `.accounts.env` (`.gitignore:60`) sind ignoriert, aber für `live_deployments.yaml` fehlt jede `.gitignore`-Regel; `live_deployments.example.yaml` liegt daneben — Verwechslungsgefahr. IDs sind keine Keys, aber enumerierbare Kontoreferenz.
**Nicht in Issues:** #22 behandelt Credential-Mapping/Precedence, nicht versionierte Live-Topologie. **Fix:** `git rm --cached live_deployments.yaml`, nur Example mit Dummies behalten, `.gitignore`-Regel ergänzen, IDs rotieren.

---

## Major — Viewer/Frontend/DB

### 8. Log-API ohne Byte-Cap + Secrets unredigiert — major
**`viewer/main.py:10717` (`lines: int = Query(le=100000)`), `viewer/core.py:2459-2464`, Model `:10757-10760`**
`_collect_log_entries` macht `read_text().splitlines()` über alle `*.log` (bei `ALL_LOGS`), baut `list[dict]`, sortiert, filtert erst danach. 100 k Lines × mehrere GB-Logs = OOM. Zeilen (inkl. Alpaca-Keys/Tokens) gehen 1:1 an den Client.
**Nicht in Issues:** #30 nennt Poll-Frequenz von `LoggingPage`, nicht Speicher-Cap/Redaction. **Fix:** Byte-Cap + `seek`-Tail, Streaming, serverseitiges Redact (`api_key|secret|token|broker_login`).

### 9. Matrix-CSV-Pfad nur String-Check — major
**`viewer/main.py:6521-6525`** (`if not normalized or ".." in normalized or startswith("/")`), Gegenbild `_resolve_grid_result_path` (`viewer/core.py:2375-2381` mit Regex + `parent != base`).
Kein `resolve()`-Check, kein Regex, `/`-Subdirs erlaubt, `%2e%2e`/`..\\`/absolute Windows-Pfade passieren.
**Nicht in Issues:** #29 nennt `startswith`-Containment bei Deletes und Bulk-Delete-Verkettung, nicht diese Matrix-Stelle. **Fix:** `^[\w-]+\.csv$` + `resolve().is_relative_to(MATRIX_RESULTS_DIR)`.

### 10. Deduplicate/Remove-Empty: nicht-atomar, destruktiv — major
**`viewer/main.py:6771-6794, 6803-6815`**, Delete-Pfad `:2531-2538`
Loop über Gruppen mit einzelnem DB-Delete + `unlink`, ohne Transaktion, ohne Dry-Run/Confirm-Token. Teilfehler → halbe Gruppe weg, `kept_ids/deleted_ids` inkonsistent, Retry löscht weiter.
**Nicht in Issues:** #29 nennt Pfad-Bau bei Bulk-Delete, nicht Atomicity. **Fix:** eine DB-Transaktion, Two-Phase (sammeln→bestätigen→löschen), Idempotency-Key. **Dup-Prüfung 2026-09-03:** #18 listet Confirm-Lücken bei *anderen* Destruktiv-Aktionen (Services/Portfolios/Modals) — Atomicity/Idempotenz dieses Endpoints bleibt neu.

### 11. `str(exc)` an Client + falsche 500er — major
**`viewer/main.py:6493, 6518, 6936, 6997, 10687, 10762-10764`**
`HTTPException(500, detail=str(exc))` für Strategie-Payload, Rename, Backtest-Load, Log-Read, Unlink leakt OS-Pfade/SQL-Fehler; Client-Fehler (fehlende Datei) als 500 statt 404/400, Downstream-Fehler als 500 statt 502.
**Nicht in Issues:** keine Issue nennt Statuscode-Treue/Leakage an diesen Stellen. **Fix:** generische `detail` + `error_id`, korrekte Codes, `logger.exception` serverseitig behalten.

### 12. Naiv/Aware-Datetime-Mix — major
**`shared/database.py:324-326` (`_utcnow_naive`), `:517-518, 587-588` (naiv) vs `:372-376` (aware UTC) und `:697-707` (aware parse); `viewer/core.py:247-257`**
Naive Service-/Deployment-Zeiten vs. aware Signal-/Evidence-Zeiten → Vergleichs-/Sortier-Fehler (je nach Treiber `TypeError` oder stille Verschiebung), Replay/Stats-Sortierung falsch.
**Nicht in Issues:** #18/#31 nennen Coverage-Fenster, nicht TZ-Typen. **Fix:** einheitlich aware-ISO (`+00:00`), Migration für Bestand, Typ-Check in `to_service`/`to_live_deployment`.

### 13. OHLCV-Loader: Glob-Injektion + unbegrenzter Load — major
**`viewer/core.py:379-385`** (`rglob(f"**/{normalized_symbol}/{normalized_timeframe}.parquet")` ohne Allowlist; Muster per Read bestätigt), `:405-441` (alle Treffer `read_parquet` + concat/Dedup). `*`/`..`/breite Symbole → Vollscan + OOM/CPU-DoS, unauthentifiziert auslösbar (kein Auth laut #18/#29). In keiner Issue genannt. **Fix:** Symbol/Timeframe-Allowlist, Treffer-Limit, Größen-Cap.

---

## Major — Runner/Strategien/Risk/Validation

### 14. Fees/Slippage dauerhaft `0.0` — major
**`runner/engine.py:515-516, 532-535`, `runner/engines/vectorbt_adapter.py:305-329`**
Nativ: `fees=0.0; net_pnl=pnl-fees`; VectorBT: `pnl=(exit-entry)*qty` ohne Kosten. `stats.py` rechnet Sharpe/CAGR auf Brutto.
**Nicht in Issues:** #16 nennt Converter-Fee-Mapping (LEAN/Stockkit), nicht fehlende Kosten in beiden Execution-Engines. **Fix:** Fee/Slippage-Config (bps/lot) durch `_close_partial`/`_finalize_position` und `risk_math` fädeln.

### 15. CAGR aus Trade-Spanne — major
**`runner/stats.py:320-333, 336-353, 366-367`**
`_resolve_backtest_period_bounds` nutzt `min(entry)/max(exit)`. Geclusterte Trades → Tages-Nenner → CAGR/Calmar explodiert; Fallback `now/now` verschleiert.
**Nicht in Issues:** #31 nennt `stats keys`, nicht Periodenwahl. **Fix:** immer Equity-Curve-Ränder bzw. `config.start/end`.

### 16. Portfolio-Penalty konstant → Diversifikation wirkungslos — major
**`runner/portfolio_optimizer.py:259-281` (insb. `:265, 274`), Sharpe-Rohform `:234-249`**
`correlation_penalty` einmalig vor Trials berechnet, dann `score=sharpe-5*penalty` — für alle Gewichte identisch. `_portfolio_sharpe_from_weights` roh (nicht annualisiert, ohne `rf`, `+1e-10`-Fudge).
**Nicht in Issues:** #16 nennt No-Trade-Auswahl/Stale-Params, nicht konstante Strafe. **Fix:** Penalty pro Trial aus gewichtetem Portfolio-Return, Sharpe annualisieren.

### 17. Optimizer maximiert `p_value` — major
**`runner/optimizer.py:51`** (`p_value` als Target registriert) + **`:298`** (`create_study(direction="maximize")`, hardcoded für alle Metriken; per Read bestätigt). Kleinerer p-Wert = besser — Maximierung selektiert insignifikante Trials. (Der Optuna-Seed-Teil desselben Befunds wurde schon in Runde 1 berichtet.) In keiner Issue genannt. **Fix:** `p_value` minimieren/invertieren.

### 18. Session-TZ via fixe Offsets + synthetische Tage — major
**`strategies/intraday_volatility_breakout/intraday_volatility_breakout.py:189-205, 217-219`**
Ohne `session_timezone` nur `+offset_hours` — DST-Wechsel (US/EU) verschiebt Entry/Exit-Window um 1 h; Fallback `day=len//100` erfindet Sessions ohne Zeitstempel.
**Nicht in Issues:** Timezone-/Session-Kategorie ist in keiner Issue genannt. **Fix:** IANA-TZ erzwingen, DST-Test, ohne Zeit kein Backtest.

### 19. Readiness-Gate zu lax — major
**`validation/readiness/validator.py:23-27`, `validation/readiness/models.py:65-80`**
`_abs_pct_diff=max(|exp|,|act|)` statt `|exp|` halbiert Fehler (100→200 = 50 % statt 100 %) → `READY` trotz Drift. `require_*`-Felder doppelt definiert (zweite gewinnt still). `_check_stats` prüft nur Win-Rate/Net-Profit, kein Sharpe/DD.
**Nicht in Issues:** #16/#38 nennen Trust-Boundary/Codegen-Gate, nicht Toleranz-Math. **Fix:** Nenner `|expected|` + Null-Guard, Duplikate entfernen, Sharpe/DD-Toleranzen.

---

## Major — Live/Scheduler/Signal

### 20. `_is_crypto_symbol` stuft `BTCUSD` als Stock ein — major
**`live_runner/market_data_stream.py:23`** (`"/" in s or s.endswith("USD") and "-" in s`; `and` bindet stärker), Routing `:228`
`BTCUSD/ETHUSD/SOLUSD` ohne `/`/`-` abonnieren `StockDataStream` statt `CryptoDataStream`, bekommen nie Bars. Verifiziert am Source (Lesung 2026-09-03).
**Nicht in Issues:** #24 nennt Volumen-Chaos, nicht Routing. **Fix:** Klammerung + Suffix-Liste + Unit-Test.

### 21. Discord-Cursor-Crash → Hot-Loop — major
**`live_runner/signal_sources/discord_source.py:463, 486`** (`int(message_id)` ohne `try`), Loop `:540`, `POLL_INTERVAL :24`
`None`/nicht-numerische ID wirft `ValueError`, wird nur geloggt und alle 8 s endlos wiederholt — Poison-Message ohne Backoff.
**Nicht in Issues:** #23 nennt Ticker-Minting/Preis-Parsing/Burst-Drops, nicht Cursor-Robustheit. **Fix:** `try/except`, fehlerhafte ID überspringen, exponentieller Backoff.

### 22. Weekly epoch-aligniert + kein `max_signal_age` — major
**`live_runner/signal_sources/execution_timing.py:57-74`** (`((epoch//tf)+1)*tf`; Weekly `604800 s` → Epoch-Donnerstag statt Montag; Tages-Kerzen ohne Börsenkalender), Submit `live_runner/daemon.py:790-794`
`received_at` alt + `delay<=0 → return` führt sofort ohne Altersprüfung aus — Stunden alte Discord-Signale werden zum stale Preis submitted.
**Nicht in Issues:** #23 nennt Parsing, nicht Clock-Skew/Candle-Race. **Fix:** kalender-alignierte Closes, `max_signal_age` + Preis-Revalidierung nach Wait.

### 23. Weitere Live-Robustheit (je major/mittel, gemeinsam priorisieren)
- **Sequenzielles Balance-Polling blockiert alle Accounts** — `live_runner/daemon.py:1550-1563` (`await _poll_adapter_balance` in Schleife, ein `snapshot_time`; kein `gather`/Timeout). Nicht #21 (PAUSE-Snapshot). Fix: `asyncio.gather` + Timeout + eigener Timestamp.
- **Shutdown-Leak: Listener-/Sync-Tasks nie gecancelt** — `live_runner/daemon.py:2068, 2078, 2096, 2111-2136` (nur `service_sync/active_discord*` gecancelt). Nicht Matrix-`pgrep`-Thema (#28). Fix: alle canceln + `gather(..., return_exceptions=True)`.
- **Scheduler `task_id` Sekunden-Kollision + 5-min-Misfire schluckt Ausfälle** — `scheduler/scheduler.py:155, 121, 127` (`%Y%m%d%H%M%S` + upsert; `coalesce + grace 300`). Nicht #28 (Health-Check-nicht-registriert). Fix: Mikro-/UUID-Suffix, Grace kleiner, Misfire alarmieren.
- **Global-Lock während Netzwerk-I/O** — `live_runner/market_data_stream.py:231-253` (`with self._lock` um `worker.stop/start`, `set_symbols`). Nicht Matrix-TOCTOU. Fix: unter Lock nur Keys kopieren, I/O außerhalb.
- **`FAILED→RUNNING`-Zwangsreanimation ohne Credential-Wechsel** — `live_runner/daemon.py:217-228` (jedes `FAILED` → `RUNNING`; permanentes Auth-Fail wird gestürmt). Invers zu #23 (`transient→permanent`), daher neu. Fix: nur nach Config-Trigger / mit begrenztem Backoff. **Dup-Prüfung 2026-09-03:** #15 B-5 behandelt dieselbe Funktion (`_recover_failed_services`), aber die Gegenrichtung (bleibt FAILED); die Zwangsreanimation bleibt neu.

### 24. Doppeltes Sizing mit divergenter Config — major (Geld-Risiko)
**`live_runner/daemon.py:534-542`** (Daemon sizt mit Deployment-`RiskConfig`, schreibt `explicit_qty`; per Read nachverifiziert) + Agenten-Befund: `live_runner/alpaca_adapter.py:480-482`, `live_runner/ctrader_adapter.py:1321-1323` rufen erneut `size_live_signal(signal, balance)` mit Defaults (`0.01/0.05/5`, `risk/live.py:87-95`) — Adapter-Seite agenten-verifiziert, von mir nicht nachgelesen. Welche Config gewinnt, entscheidet das letzte Sizing; Deployment-Limits können überschrieben werden.
**Abgrenzung:** #25 nennt Metadata-Bypass und Unit-Mix, nicht dieses Doppel-Sizing. **Fix:** genau einmal sizen; Adapter behandeln eingehendes `qty` als verbindlich statt neu abzuleiten. **Dup-Prüfung 2026-09-03:** gleiche Familie wie #20/#25 (Bypass/Units/Metadata); Doppel-Sizing als solches steht in keinem Issue.

---

## Major — Daten/Shared/Scripts/Root

### 25. `signal_import`: TZ-Raten + `buy`-Default — major
**`data_tools/signal_import.py:91-112`** (`"America/New_York" if "ny" in header else "UTC"`, `to_datetime(errors="coerce")` ohne `utc=True`), `:67-77` (`return "buy"`).
Naive CSV-Zeiten je nach Spaltenname unterschiedlich lokalisiert; Unbekanntes wird Long. Falsche Tageszuordnung + falsche Seite im Replay.
**Nicht in Issues:** #19/#44 nennen Fee/Direction/Pairing/PnL und Gate-Hardening, nicht TZ-Heuristik/Action-Default. **Fix:** TZ explizit fordern (sonst Abbruch), Unbekanntes droppen/loggen, `utc=True`.

### 26. Remote-Artefakte ohne Integrität; Lücken als Warnings — major
- **HistData:** `data_tools/downloaders/histdata_dl.py:200-209` (`session.post`, nur `<100`-Untergrenze, `ZipFile(BytesIO(...))`, `namelist()[0]` blind als CSV). Kein Limit/Decompression-Cap, kein Hash. Nicht JVM-Integrity-Thema (#19). Fix: `Content-Length`-Limit, Decompressed-Cap, Eintrags-Allowlist, SHA256 in `source_meta`.
- **HuggingFace:** `data_tools/downloaders/huggingface_dl.py:27-29, 256-261, 277-282` (`pd.read_parquet(url)` im Loop ohne Pinning; `except → warnings.append(Skipping …)`; danach trotzdem Merge). Nicht Dukascopy-Hole-Thema. Fix: Manifest mit SHA256, fail-closed ab N Lücken, Warnings in `DownloadResult`/Katalog hochstufen. **Dup-Prüfung 2026-09-03:** der HF-Silent-Skip steht in #42 DV-B16 (`:257-261` wörtlich); unbegrenzte ZIP-Reads und fehlende Checksummen/Pinning bleiben neu.

### 27. `validate`: Index-Bug + Feiertags-False-Positives — major
**`data_tools/validate.py:164-178`** (`for idx in large_gaps.index[:20]: ts.iloc[idx-1]` — Labels nach `dropna` ≠ Positionen → falsche Gap-Grenzen; nur Fr→Mo/So-Skips, Feiertage/DST/Forex-Sonntag als `gaps`).
**Nicht in Issues:** #43 nennt Tick-Bid/Ask/JForex/4h-Buckets, nicht diesen Index-Bug. **Fix:** `reset_index(drop=True)` vor Diff, Exchange-/Forex-Kalender. **Dup-Prüfung 2026-09-03:** #19 nennt Weekend/Holiday-Skips (`validate.py:173-178`); der `iloc`-Label-Bug (`:167-170`) bleibt neu.

### 28. Repo-Hygiene (operativ, gemeinsam fixen) — major
- **Deps ungepinnt + Lock-Drift + kein CI:** `pyproject.toml:6-29` (alles `>=` ohne Cap) vs `requirements.txt:39,43` (`pandas==3.0.1`, `pyarrow==23.0.1`) vs `uv.lock` (z. B. `aiohttp 3.14.3` vs `3.13.3`); `.github/` enthält **kein** `workflows/` (nur `agents/`, `skills`), kein `Dockerfile` (verifiziert 2026-09-03). Frischer Install kann Majors ziehen; kein Job erzwingt Tests/Lint. Nicht #33/#34 (Isolation/Strict-Mode). Fix: `>=,<n+1`-Caps oder `uv sync`, `requirements.txt` aus Lock generieren, Minimal-Actions (`pytest -q`, `ruff`).
- **Katalog-Staleness:** `data_tools/catalog_builder.py:214-232` (`mtime <= last_updated + 1e-6`, ISO ohne ns) + `data_tools/cache_manager.py:168-202` (blindes `catalog.json`). Schnelle Rewrites wirken unverändert. Nicht Atomic-Write-Thema (#42). Fix: `mtime_ns` + `size_bytes` Pflicht-Vergleich, Fingerprint einbetten.
- **Migration Staleness ohne Backup:** `data_tools/migrate_csv.py:100-131, 195-196` (`dest.exists() → SKIP`, direkt `to_parquet`, `copy2` ohne Backup). Re-Run nach CSV-Fix aktualisiert nie; Erst-Run ohne Provenienz. Nicht Destruktiv-Confirm-Thema (#27). Fix: Quell-Hash + Zeilenzahl, Backup `*.bak.<ts>` + `--force`.

---

## Mittel

### 29. Sortino manuell: `len(downside)<2` → `0.0`, Target `0` ohne `risk_free` — mittel
**`runner/stats.py:497-511`**
(Hinweis: Der Sharpe-Teil dieses Befunds — Populationsvarianz `:483-494` — steht wörtlich in #16 F17 und wurde entfernt.) Übrig: `_sortino_manual` verlangt `len(downside)>=2` sonst `0.0`, Downside als `sum(r²)/N` mit Target `0` ohne `risk_free`; diese Sortino-Details nennt F17 nicht. **Fix:** Sortino-Target dokumentieren, `periods_per_year` durchreichen.

---

## Geprüft und ausgeschlossen (bereits in Issues — NICHT oben enthalten)

- **Resample-`source_path` ohne Containment via Worker** (`viewer/main.py:10382-10395`): bereits Issue #29 („resample spawns a subprocess on an unvalidated client path :10381-10409“) — daher kein „neuer“ Fund.
- **Matrix-Queue-Lock-TOCTOU + `pgrep`-Heuristik + nicht-atomarer State** (`scripts/matrix_queue_runner.py:60-100`): bereits Issue #28 („lock acquisition is TOCTOU (no O_EXCL/flock) and the pgrep liveness probe can delete a live lock“) — daher kein „neuer“ Fund.
- **Zusätzlich in 2. Runde als Duplikate bestätigt (jeweils erneut gefunden, bereits abgedeckt):** CORS-Wildcard, fehlende Auth (#29/#18); Strategie-Pfad-ACE, Resample-Subprocess, CSV-Signal-Import-Pfad, Delete-Prefix-Containment (#29); SL/TP nie an Broker (#21 C-2); `explicit_qty`-Bypass + fehlender Notional-Cap (#25); Log-OOM/Secrets, `str(exc)`-Leak, `live_deployments.yaml` im Git, fehlende CI (Funde 8/11/7/28 hier).
- Alle in der Kopf-Tabelle unter „Referenz Issues“ gelisteten Scopes wurden je Fund auf Überlappung geprüft; wo Item-Namen ähnlich klangen (z. B. Bulk-Delete-Pfad vs. Deduplicate-Atomicity; Discord-Parsing vs. Discord-Cursor; Volumen-Units vs. Symbol-Routing), ist die Abgrenzung beim Fund vermerkt.

## Grenzen dieser Review

- Statisch + Stichproben-verifiziert, kein Full-Test-Run (der wäre nächster Schritt: `pytest -q`, `ruff`, gezielte Repro-Skripte für Nr. 20 (Crypto-Routing) und Nr. 22 (Weekly/Stale-Signale)).
- Startup-RCE (Nr. 5) braucht DB-Schreibzugriff als Vorbedingung; Schwere gilt unter der Bedrohungslage „Backup/DB manipulierbar oder Insider“.
