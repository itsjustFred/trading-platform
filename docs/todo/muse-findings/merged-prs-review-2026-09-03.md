# Gemergte PRs — Review: schlechter/schlimmer Code, der gefixt werden sollte

- **Datum (UTC):** 2026-09-03 (Stand: 2. Runde + Dup-Bereinigung + Neu-Sortierung)
- **Repo:** `/home/ubuntu/Desktop/trading-platform`
- **Branch:** `main` @ `6caf461367e37aa7dbc3abe91fb71405fc152038`
- **Umfang:** alle gemergten PRs mit `main` als Ziel bzw. in `main` enthalten: #5 (dev_Blob, April), #6 (live-integration), #7 (qc-risk-parity), #8 (mt5-atr-seed), #9 (optimization-split-boundary), #10 (optimization-oos-promotion), #11 (portfolio-optimizer-oos), #12 (intraday-signal-contract), #13 (downloader-coalescing).
- **Methode:** je PR `git diff <merge>^1..<merge>^2` (eigener Diff), per `git blame` gegen `main` verifiziert, dass die bemängelte Zeile aus dem jeweiligen PR stammt (kein Pre-existing); schwerste Vorwürfe zusätzlich per `Read` am Source nachgeprüft (daemon.py:566, alpaca_adapter.py:181-183, portfolio_optimizer.py:141-142, viewer/main.py:3020-3021). 2. Runde mit 2 unabhängigen Agents (nur PR-Ranges + Blame); danach Dup-Prüfung gegen die Issue-Texte #14–#46 (2 Voll-Dups entfernt) und Neu-Sortierung nach Schwere.
- **Lesart:** `hoch` = Geldverlust / falsche Orders / Silent-Failure im Live-Pfad / falsches Vertrauen; `mittel` = falsche Darstellung, schwache Gates, Robustheit. Jeder Fund nennt PR-Nummer, einführenden Commit, aktuelle `Datei:Zeile` in `main`.

---

## Zusammenfassung (25 aktive Befunde, nach Schwere sortiert)

| PR | Titel (kurz) | Urteil |
|----|--------------|--------|
| #6 | Discord→Alpaca Live-Integration (+12k/−2,5k) | **schlimmster PR** — 3 hohe + 7 mittlere Funde im Geld-Pfad |
| #13 | Downloader-Request-Coalescing | 1 hoher (Key-Kollision) + 4 mittlere |
| #11 | Portfolio-Optimizer OOS/Covariance | 1 hoch (tote Penalty) + 3 mittel |
| #10 | OOS-Evidenz in Optimization-Pages | 1 mittel-hoch (grüner Verfall) |
| #7 | QuantConnect-Result-Importer | 4 mittel (Fee-Logik, `timeframe="unknown"`, Rundung, Zeit-Parsing) |
| #9 | Split-Boundary-/OOS-Coverage-Validierung | 1 mittel (1-Bar-Coverage) |
| #8 | MT5-ATR-Seed | **sauber**, kein Fund |
| #12 | Intraday-Signal-Contract (+3/−0) | unkritisch (nur Test) |
| #5 | Signal-Schema v2 / IR-Pipe (April) | Merge nicht mehr in `main`-Historie (rewritten); Codegen-Pfad via Issues #38/#16 abgedeckt |

Empfohlene Fix-Reihenfolge: Funde 1–3 → Fund 4 (Dolthub-Key) → Fund 11 (Penalty) → Rest.

---

## Hoch

Eigene PR-#6-Commits u. a. `7984f7d` (P0-B Safety/Routing), `f7ffba3` (Discord REST-Polling), `d986fbf` (e2e Trades), dazu Sonar-/CodeRabbit-Fix-Runden (`e911121`, `212fe25`, `a0e7646`, `561c13d`, `bd75d9a`, `5cdd276`). Trotz der Review-Runden steht Folgendes heute noch in `main`. PR-#13-Fund 4 aus Commit `2ccd1aa` (im Range `8fb2d60e^1..8fb2d60e^2`).

### 1. Exit-only schließt Short-Positionen nie — hoch (Geld-Risiko, PR #6)
- **Eingeführt:** `bd75d9a` „refactor(live): reduce cognitive complexity below Sonar threshold" (Zeile 565 zusätzlich `a0e7646`).
- **Ort:** `live_runner/daemon.py:566` (`target_qty = max(resolved_qty, 0.0)`), Fehlerpfad `:580-584`.
- **Was:** Negative Bestände (Shorts) werden auf 0 geklemmt, danach wirft `_metadata_for_exit_only_order` stets „no long position is open". Sell/Exit-Signale auf Shorts laufen permanent ins Leere, die Position bleibt offen. Passt nicht zu `_count_open_broker_positions` (`:597`), das per `abs()` Shorts als offen zählt. Der Sonar-Complexity-Refactor hat hier Logik beschädigt.
- **Fix:** Vorzeichen erhalten (`abs` für Close-Menge, Richtung aus Bestand ableiten).
- **Abgrenzung (Dup-Prüfung 2026-09-03):** #24 B-6 behandelt dieselbe Funktion (`:556–568`, First-Match-Break); der Negativ-Clamp (`:566`) ist ein anderer Mechanismus in derselben Funktion — als Teil-Facette werten, Fix gemeinsam mit B-6 verfolgen.

### 2. Discord-Polling: `int()`-Absturz + Duplikat-Orders via 20er-Fenster — hoch (PR #6)
- **Eingeführt:** `f7ffba3` (REST-Polling-Basis), verschärft durch `a0e7646` + `561c13d`.
- **Ort:** `live_runner/signal_sources/discord_source.py:463`, `:486`, `:488`.
- **Was:** `int(message_id) <= int(last_seen)` wirft bei fehlender/nicht-numerischer ID (`"None"`) `ValueError`, der im `_poll_loop` nur als generische Warnung geloggt wird — der Poll-Lauf für alle Channels bricht ab. Zusätzlich setzt `:488` `seen` jedes Mal auf genau die letzten `POLL_LIMIT=20` Nachrichten zurück: bei >20 neuen Nachrichten werden alte IDs erneut emittiert → doppelte Live-Orders.
- **Fix:** IDs als String vergleichen bzw. `try/except`, `seen` als begrenztes LRU (`deque(maxlen)`) statt Fenster-Reset. **Dup-Prüfung 2026-09-03:** `int()`-Crash in keinem Issue; Seen-Reset = gleicher `_seen_ids`-Mechanismus wie #23 C-3 (dort Drop-Seite, hier Re-Emissions-Seite).

### 3. `connect()` meldet RUNNING, obwohl nie eine Nachricht empfangen wurde — hoch (Silent-Failure, PR #6)
- **Eingeführt:** `f7ffba3`.
- **Ort:** `live_runner/signal_sources/discord_source.py:391-403`, `:587-599`.
- **Was:** Jeder `RequestException` wird zu `return []` geschluckt, `_prime_cursors` schluckt ebenfalls alle Nicht-Auth-Fehler, `connect()` fängt danach alles mit „continuing with background loop" ab und setzt trotzdem `_connected=True` + Status RUNNING. Dauerhafter Netz-/Proxy-Ausfall ist vom Daemon ununterscheidbar von „keine Signale".
- **Fix:** Initial-Poll-Fehlschlag als `connected=False`/FAILED werten oder Retry mit Backoff vor RUNNING-Status.

### 4. Alle Dolthub-Requests kollidieren auf einen Key — hoch (PR #13)
- **Eingeführt:** Commit `2ccd1aa` (im Range `8fb2d60e^1..8fb2d60e^2`).
- **Ort:** `viewer/main.py:3020-3021` (`request_identity.update(symbols=[], timeframes=[], start=None, end=None)` für `dolthub`). Verifiziert per Read.
- **Was:** Jeder Dolthub-Download erhält denselben Request-Key und wird auf den ersten aktiven Task koalesziert — falscher Task/Status für ungleiche Datasets (Staleness-Fehlleitung).
- **Fix:** Dolthub-Dimensionen in den Key aufnehmen oder Dolthub vom Coalescing ausnehmen.

---

## Mittel-hoch

### 5. Frontend zeigt starken OOS-Verfall grün an — mittel-hoch (PR #10)
- **Eingeführt:** Commit `d08acd0` (OOS-Evidenz-Klassifikation).
- **Ort:** `frontend/src/lib/optimizationEvidence.ts:63`, `:76-82`.
- **Was:** Nur `IS>0 → OOS<=0` wird `degraded`; jeder andere finite OOS-Wert (z. B. Sharpe 2,0→0,01) bekommt grün „OOS evidence available". Aktives falsches Vertrauen in overfitte Parameter.
- **Fix:** relativen Verfall (z. B. <50 % IS oder Drawdown-Schwelle) als `degraded` werten.

### 6. Fee-Reconciliation: unverifiziert-ok + spröde Exaktgleichheit — mittel-hoch (PR #7)
- **Eingeführt:** `aada3ea` (Modell-Pfad) auf `87e16b9` (Derivation `:793`, Delta-Check `:803`).
- **Ort:** `validation/importers/quantconnect_result.py:765-786`, `:793`, `:803`.
- **Was:** Ohne `fee_model` kehrt `load()` mit `fee_model_not_supplied`/`unresolved` erfolgreich zurück statt zu failen; mit Modell gilt `max(minimum,|qty|*rate)` mit `delta != 0 → raise` — reale IB-Fees (Caps, Staffeln, Stornos, FX) erfüllen Exaktgleichheit oft nicht. Also je nach Lage falscher Success oder falscher Fail, beides mit PnL-Wirkung.
- **Fix:** ohne Modell fail-closed; mit Modell Toleranzband statt `!=0`.
- **Korrekturhinweis (2. Runde, Zweit-Agent, blame-geprüft):** Der `fee_model_not_supplied`-Pfad (`:765-786`) deklariert ehrlich `evidence_classification: "unresolved"` / `resolution_status: "fee_model_not_supplied"` (`:774-776`, per Read bestätigt) statt still zu importieren. Fund bleibt als „ohne Modell kein harter Gate" bestehen, ist aber ggf. auf mittel herabzustufen, sobald verifiziert ist, dass Caller auf `resolution_status` gaten — offene Nachprüfung.

---

## Mittel

### 7. `verify_ctrader_order.py`: hartcodierte IDs + Import-Seiteneffekte — mittel (PR #6)
- **Eingeführt:** `8da55a3`.
- **Ort:** `scripts/verify_ctrader_order.py:26`, `:28`, `:35-36`, `:72-76`, `:113`.
- **Was:** Defaults `27678592`/`47644617` lassen das Skript ohne Env gegen eine fremde historische Order „verifizieren" (falsches Vertrauen); `asyncioreactor.install()` + `sys.path.insert` auf Modulebene; `env["CTRADER_APP_ID"]` (`:73`) wirft `KeyError` und läuft dann 4×30 s Retry; Report via `open(..., "w")` mit Default-Perms.
- **Fix:** keine Defaults (Pflicht-Env/Argumente), Reactor-Install + Pfad-Hack entfernen, `env.get` mit klarer Fehlermeldung, `0o600` für Reports.

### 8. `validate_live_account_routes` erkennt Fehlroutung nicht — mittel (PR #6)
- **Eingeführt:** `7984f7d` (alle Zeilen per blame dort).
- **Ort:** `live_runner/account_routing.py:10`, `:30`.
- **Was:** `ctrader_account_identifier` fällt auf lokale `account.id` zurück (nie eine Broker-ID) — passiert die Validierung, scheitert erst zur Laufzeit im Adapter-Auth. `routing_key`-Sonderfall `<= 0` umgeht die numerische Normalisierung.
- **Fix:** nicht-numerische/fehlende cTID bereits in der Validierung ablehnen statt erst im Preflight.

### 9. `test_signal_trigger.py`: echte Orders ohne DB-Spur — mittel (PR #6)
- **Eingeführt:** `7984f7d` (`git log -S save_live_signal` im PR-Range zeigt nur diesen Commit; Diff löscht Import/Aufruf).
- **Ort:** `live_runner/test_signal_trigger.py:50` (`submit_order` ohne vorheriges `save_live_signal`).
- **Was:** echte Alpaca-Orders ohne `LiveSignal`-Datensatz — Fills/Reconciliation und Slippage-Auswertung laufen ins Leere.
- **Fix:** Persistierung wieder einführen oder Skript als dry-run kennzeichnen.

### 10. Singleflight ohne TTL, unbegrenzte Caches — mittel (PR #13)
- **Eingeführt:** `9e6482c` (Key/Registry) + `2ccd1aa`/`ac3b973` (Cleanup-Pfad).
- **Ort:** `viewer/core.py:2848-2900` (`_find_active/_register/_start_singleflight`), `viewer/main.py:2823-2829` (`_COALESCED_CLEANUP_PENDING`).
- **Was:** Nur `FINAL_TASK_STATUSES` geben den Key frei; eine hängende `starting`-Reservierung blockiert denselben Key ewig (kein TTL/Heartbeat). `DATA_TASKS` und `_COALESCED_CLEANUP_PENDING` wachsen bei dauerhaft fehlendem `delete_background_task_record` unbegrenzt.
- **Fix:** TTL + Heartbeat/Eviction, Bounded-Set mit Drop-Policy.

### 11. Tote Diversifikations-Strafe — mittel-hoch (falsche Finanz-Math, PR #11)
Eigene PR-#11-Commits: `0283cb3` (Refactor), `fdb6fc0` (quality gates), `b6c56a9` (Sonar), `1e9c7df` (persistence hardening). Hinweis: `risk/engine.py`, `runner/engine.py`, `scripts/run_risk_parity_backtest.py` haben in diesem Range **kein Diff** (verifiziert) — dafür kein Fund.
- **Eingeführt:** `fdb6fc0`.
- **Ort:** `runner/portfolio_optimizer.py:265` + `274`.
- **Was:** `correlation_penalty` wird einmal aus ungewichteten IS-Returns berechnet und als Konstante von jedem Trial-Score abgezogen — differenziert nie zwischen Gewichten, die Optimierung „sieht" sie nicht. Irreführender Objective-Term.
- **Fix:** pro Trial gewichtete Portfolio-Returns-Korrelation berechnen oder Penalty streichen.

### 12. Null-Gewichte werden still zu Equal-Weight — mittel (PR #11)
- **Eingeführt:** `0283cb3`.
- **Ort:** `runner/portfolio_optimizer.py:141-142` (verifiziert per Read; Docstring „finite equal-weight fallback").
- **Was:** `_normalize_weights` fällt bei `sum<=0`/non-finite auf `ones(n)/n` zurück statt zu prunen (früher `TrialPruned`). Degenerierter All-Zero-Trial kann Best-Trial werden.
- **Fix:** `TrialPruned`/Fail statt Fallback, Fallback nur außerhalb der Objective.

### 13. OOS-Metriken verschwinden still — mittel (PR #11)
- **Eingeführt:** `fdb6fc0`.
- **Ort:** `runner/portfolio_optimizer.py:302-317` (`_compute_oos_metrics`), `358-365` (`_build_optimization_result`).
- **Was:** Bei <2 Zeilen/Spalten nach `require_overlap=True`-Intersection kommt `None,None`, `oos_sharpe/oos_split` werden ersatzlos weggelassen — ununterscheidbar von „kein OOS gewünscht".
- **Fix:** `oos_status`/`oos_error` (z. B. `insufficient_overlap`) immer persistieren.

### 14. Importer hardcodiert `timeframe="unknown"` — mittel (PR #7)
- **Eingeführt:** Commit `87e16b9` (QC-Importer).
- **Ort:** `validation/importers/quantconnect_result.py:443`.
- **Was:** Jede timeframe-abhängige Downstream-Math (Annualisierung, Per-Bar-Normierung) rechnet mit unbekannter Frequenz weiter, ohne zu failen.
- **Fix:** Timeframe aus Export/CSV inferieren oder fail-closed fordern.

### 15. OOS-Coverage = 1 Bar genügt — mittel (PR #9)
- **Eingeführt:** Commit `16d9dfb` (Split-Boundary/OOS-Coverage).
- **Ort:** `data_tools/auto_download.py:147` (`.any()`), `:175`; `runner/optimizer.py:231-243`.
- **Was:** `has_ohlcv_bars_in_range` + Loop fordern nur ≥1 Bar je Train/Test-Segment und Symbol — ein monatelanges Segment mit einer einzigen Lücken-Bar passiert die „Coverage"-Prüfung (OOS-Leakage durch Schein-Coverage).
- **Fix:** Mindest-Barzahl/Dichte proportional zu Segmentlänge und Timeframe fordern.

### 16. 403 eines Channels tötet die ganze Discord-Source — mittel (PR #6)
- **Eingeführt:** `f7ffba3` (agenten-verifiziert per blame).
- **Ort:** `live_runner/signal_sources/discord_source.py:405`.
- **Was:** 401 UND 403 werden identisch als terminaler `DiscordAuthenticationError` für die ganze Source gewertet — ein Channel ohne Zugriff stoppt alle Channels.
- **Abgrenzung (Dup-Prüfung 2026-09-03):** #15 B-4 beschreibt dieselbe Defektkette inkl. `403 → DiscordAuthenticationError → terminal` (Daemon-Schicht); dies ist die Source-Schicht derselben Kette — Fix gemeinsam mit B-4 verfolgen (`auth-blocks per channel`). **Fix:** nur 401 terminal, 403 pro Channel skippen.

### 17. Initial-Poll prüft nur einen Channel — mittel (PR #6)
- **Eingeführt:** `f7ffba3` (agenten-verifiziert per blame).
- **Ort:** `live_runner/signal_sources/discord_source.py:572`.
- **Was:** `connect()` primt alle Channels, pollt danach aber nur `next(iter(channel_ids))` (mengenordnungs-nichtdeterministisch). Fehler weiterer Channels erst verspätet im Hintergrund-Task sichtbar. Ergänzt Fund 3 (RUNNING ohne Daten). **Fix:** Initial-Poll über `sorted(channel_ids)`.

### 18. `gather` ohne `return_exceptions` — eine Account-Exception killt den Tick — mittel (PR #6)
- **Eingeführt:** `7984f7d` (agenten-verifiziert per blame; per Read nachverifiziert).
- **Ort:** `live_runner/daemon.py:1873-1884`.
- **Was:** `_ensure_adapter...` fängt nur `connect()`-Fehler; Unerwartetes aus Create/Lookup bricht die ganze Sync-Iteration ab, restliche Accounts werden diesen Tick nie versucht. **Fix:** `return_exceptions=True` + pro Account loggen.

### 19. Disconnect mit `active_` statt `runnable_` IDs — terminal-geblockte Adapter überleben — mittel (PR #6)
- **Eingeführt:** `7984f7d` (agenten-verifiziert per blame; per Read nachverifiziert).
- **Ort:** `live_runner/daemon.py:2025-2027` (`runnable_account_ids = active - BLOCKED` berechnet, aber `_disconnect_inactive_adapters(active_adapters, active_account_ids)` aufgerufen).
- **Was:** Schlägt `_disconnect_blocked_adapters` vorher fehl (Exception bricht Schleife ab), überlebt ein terminal-geblockter Adapter. **Fix:** `runnable_account_ids` übergeben (Defense-in-Depth, fail-closed).

### 20. Dukascopy/JForex-Key ohne `allow_native_fallback` — falsches Coalescing — mittel (PR #13)
- **Eingeführt:** `2ccd1aa` (agenten-verifiziert per blame).
- **Ort:** `viewer/main.py:3022` (Key-Bau; Flag-Nutzung `:10161`).
- **Was:** Key unterscheidet Dukascopy/JForex nicht nach `allow_native_fallback`; identische Anfragen mit/ohne Fallback koaleszieren fälschlich (falsche Daten als „coalesced"). Ergänzt Fund 4 (Dolthub-Kollision). **Fix:** Flag + `source_label` in den Key.

### 21. `cleanup_pending`-Marker + Sync-DB-Delete auf Hot-Path — mittel (PR #13)
- **Eingeführt:** `9e6482c`/`c438525`/`ac3b973` (agenten-verifiziert per blame).
- **Ort:** `viewer/main.py:2972` + `:2822` (`_get_task`, `_hydrate_task_cache_once`).
- **Was:** Löschfehler → Marker `status="cleanup_pending"` (nicht final); Hot-Path ruft synchron `delete_background_task_record` auf, Fehlschläge sammeln sich in unbegrenztem `_COALESCED_CLEANUP_PENDING`. Dauer-DB-Fehler = blockierte API-Calls; nach Restart wird die Zeile als Ghost-Task hydriert. Ergänzt Fund 10. **Fix:** Marker final/separat filtern, Cleanup asynchron + bounded mit Retry-Limit.

### 22. Singleflight-Starter-Fehler erzeugt `failed`-Stub — mittel (PR #13)
- **Eingeführt:** `2ccd1aa` (agenten-verifiziert per blame).
- **Ort:** `viewer/core.py:2893`.
- **Was:** Exception-Pfad ruft `_mark_task_finished(...failed...)` auf der Minimal-Reservation auf → nutzersichtbarer `failed`-Task ohne Name/Command/Total statt stiller Verwerfung. **Fix:** Reservation aus `DATA_TASKS` entfernen, nur echte Tasks als `failed`.

### 23. QC-Importer: `ROUND_DOWN`-Toleranz — mittel (PR #7)
- **Eingeführt:** `87e16b9` (blame `87e16b9f`; per Read nachverifiziert).
- **Ort:** `validation/importers/quantconnect_result.py:250-255`.
- **Was:** `_decimal_export_matches` akzeptiert neben `ROUND_HALF_UP` auch `ROUND_DOWN` — trunkierte CSVs (`1.999` vs `"1.99"`) gelten als „matched", Fehler bis ~1 Cent/Feld werden abgehakt. **Fix:** nur `ROUND_HALF_UP` + dokumentierte Cent-Toleranz.

### 24. QC-Importer: unparsbare Zeiten als Rohstring — mittel (PR #7)
- **Eingeführt:** `87e16b9` (blame `87e16b9f`; per Read nachverifiziert).
- **Ort:** `validation/importers/quantconnect_result.py:210-211` (`if parsed is None: return text`).
- **Was:** Unparsbare Trade-/Order-Zeiten landen als Strings in Trade-Objekten/Sortierschlüsseln (lexikografische Fehlordnung möglich), während Preise/IDs strikt fail-closed geprüft werden. **Fix:** `ValueError` für Trade-/Order-Zeiten.

### 25. `ValueError` pro Trial kommentarlos geschluckt — mittel-niedrig (PR #11)
- **Eingeführt:** `fdb6fc0`.
- **Ort:** `runner/portfolio_optimizer.py:292` (`catch=(ValueError,)` in `_run_optimization`).
- **Was:** Objective-Fehler verschwinden ohne Log/Attribution; sichtbar nur der spätere „No trial completed"-Fallback.
- **Fix:** nicht fangen oder als failed Trial mit `trial.set_user_attr("error",…)` + Logger werten.

---

## Ohne Befund / ausgenommen

- **PR #8** (MT5 ATR seeding + Wilder recurrence, `7206c9d2`): Kurzcheck an `strategies/intraday_volatility_breakout/…:105-118` (Blame `cb80367`) — SMA-Seed der ersten `period` validen TRs, danach `(prev*(n-1)+TR)/n`, NaN-Gaps bleiben NaN. Zweit-Agent hat per Ausführung verifiziert. **Kein Fund.**
- **PR #12** (intraday signal metadata + daily ATR override): +3/−0 in einer Testdatei. **Unkritisch.**
- **PR #5** (signal schema v2, partial exits, IR-Pipeline, `dev_Blob` → `main`, April): Merge-Commit `9da7c30` ist kein Vorfahre von `main` mehr (Historie rewritten); Inhalte leben teils via `codex/new-main`-Linie weiter. Tiefen-Review hier ausgespart; der Codegen-/Trust-Pfad (`strategy_ir/`, `shared/strategy_registry.py`) ist durch Issues #38/#16 abgedeckt.
- **Geprüft ohne Befund (PR #6):** `config_parser.py:14-18` (nur `is_file`-Check, schwach aber harmlos), `preflight.py:97-124` (Refactor äquivalent), `ctrader_adapter.py:138-171` (Locking via `fcntl.flock` vorhanden), `scripts/live_smoke.py:747` (nur flaky Demo-Smoke). `_parse_bool_env` und `live_smoke`-Subprocess-Worker sind per blame pre-PR#6 und fallen raus.
- **In 2. Runde als Duplikate bestätigt (erneut gefunden, bereits in den Erstrunden-Funden / Issues):** Penalty-Konstante, Weights-Fallback, OOS-still, 1-Bar-Coverage (dort von Zweit-Agent als hoch eingestuft — unterstreicht Fund 15), OOS-grün, Discord-`int()`-Crash, Dolthub-Key-Kollision, Singleflight-ohne-TTL. PR #8 erneut ohne Befund (dort per Ausführung verifiziert).

---

## Grenzen

- Nur PR-eigene Zeilen gewertet (Blame-geprüft).
- `shift(-1)`-Parity-Hack und Sharpe/CAGR/Fees-Themen aus dem Main-Review sind überwiegend **nicht** diesen PRs zuzuordnen (Initial-Commit bzw. andere Branches: `09afb15`, `56d5d44`, `8d0df89`) und stehen daher im Schwester-Dokument `main-review-neue-bugs-2026-09-03.md`, nicht hier.
- Kein Full-Test-Run; nächster Schritt wären Repro-Tests für Funde 1–3 (Exit-Only-Short, Discord-Dedup, RUNNING-ohne-Daten) und 4/11/13 (Dolthub-Key, Penalty, OOS-still).
- **Dup-Prüfung 2026-09-03** (alle Findings gegen Issue-Texte #14–#46): Token-Fallback und Alpaca-Block-0 als Voll-Dups entfernt (#15 B-4/B-1, dort wörtlich mit denselben Zeilen); Teil-Überlappung bei Fund 2 (C-3-Mechanismus), Fund 1 (B-6-Funktion), Fund 16 (B-4-Kette) — dort vermerkt. Rest ohne Treffer, u.a. Gather/Disconnect-IDs, Coalescing-Keys, QC-`ROUND_DOWN`/Rohzeit, `failed`-Stub, Verify-Skript, Routing.
