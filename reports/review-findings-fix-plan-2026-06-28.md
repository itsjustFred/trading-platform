# Fix-Plan für Review-Findings aus Opus 4.8 und GPT 5.5

**Datum:** 2026-06-28  
**Basis:** Reviews zu `codex_rewrite -> dev_fred` und `kimchi-kimi-continue -> dev_fred`  
**Quellen:**

- `reports/opus-codex_rewrite_review.md`
- `reports/opus-kimchi_kimi_continue_review.md`
- `reports/gpt-code-review-codex_rewrite-to-dev_fred-2026-06-28.md`
- `reports/gpt-code-review-kimchi-kimi-continue-to-dev_fred-2026-06-28.md`

## Ziel

Dieser Plan beschreibt, wie die in den Reviews gefundenen Fehler behoben werden sollen. Er konsolidiert doppelte Findings, ordnet sie nach Risiko und Abhängigkeiten und definiert pro Arbeitspaket:

- betroffene Dateien
- konkrete Änderungsschritte
- Regressionstests
- Akzeptanzkriterien
- empfohlene Verifikationskommandos

Die Branches sind laut beiden Review-Strängen nicht merge-ready. `kimchi-kimi-continue` ist ein Superset von `codex_rewrite`; daher müssen die `codex_rewrite`-Blocker zuerst behoben werden, bevor Kimchi sinnvoll mergefähig werden kann.

## Priorisierung

### P0: Merge-Blocker

Diese Punkte müssen vor jedem Merge in `dev_fred` behoben werden:

1. cTrader Token-Refresh und Account-Authorization robust machen.
2. Live-Preflight gegen falsche Live/Demo-Auswahl und falsche Accounts-Datei absichern.
3. SQLite-Migrationen für Discord-Dedupe und Equity-Curves backfillen.
4. `save_live_signal` gegen `None`-Dedupe-Bug, Unique-Index-Races und verlorene `broker_order_id` härten.
5. Grid-Market-Data-Download für `m30`/`30m` reparieren.
6. Portfolio-Optimizer-Korrelationsziel korrigieren.
7. Kimchi-Readiness-Gate darf bei fehlenden Pflichtdaten nicht `READY` melden.
8. Repo-Hygiene: große Runtime-/Backtest-Artefakte und Whitespace-Fehler entfernen.
9. `.accounts.example.env` Contract und Secret-/Account-Template sauberstellen.

### P1: Hohe Robustheits- und Korrektheitsrisiken

Diese Punkte sollten im selben Fix-Zyklus oder direkt danach erledigt werden:

1. Grid-Backtest Timeouts pro Zelle abfangen.
2. Daily-Equity finite Checks und Frequenz-Metadaten korrigieren.
3. Portfolio-Optimizer Weight-Normalisierung und Equity-Alignment härten.
4. Validation-Risk-Sizing konservativ runden.
5. Validation-Statistikmodule konsolidieren.
6. Validation-JSON strikt JSON-kompatibel machen.
7. MT5-Importer gegen überlappende Trades, Teil-Exits und Zeitzonenannahmen härten.

### P2: Follow-up und Hygiene

Diese Punkte sind niedriger priorisiert, sollten aber vor einer finalen Release-Freigabe bereinigt werden:

1. Viewer `result_id` validieren und DELETE-Risiko dokumentieren oder absichern.
2. Token-Dateirechte beim Runtime-Write härten.
3. Optimizer interne Trial-Parameter namespacen und Universe-Explosion begrenzen.
4. Grid-ID-Kollisionen vermeiden.
5. CLI- und Test-Nits in `validation/` bereinigen.
6. Frontend-Nits und a11y-Inkonsistenzen beheben.
7. Cron-Watchdog `/tmp`-Datei absichern.
8. Testlücken schließen.

---

# Phase 1: Live-Trading- und Credential-Blocker

## 1. cTrader Refresh korrekt reauthentifizieren und serialisieren

**Review-Finding:** Nach erfolgreichem Token-Refresh werden account-scoped cTrader Requests wiederholt, ohne zwingend erneut Account-Authorization mit dem neuen Access Token auszuführen. Zusätzlich werden Access- und Refresh-Token getrennt persistiert; parallele Refreshes können sich gegenseitig invalidieren.

**Datei:** `live_runner/ctrader_adapter.py`

### Änderungsschritte

1. Einen zentralen Refresh-Pfad einführen oder den bestehenden Pfad so umbauen, dass er atomar folgende Schritte ausführt:
   - Refresh-Token verwenden.
   - Neues Access-/Refresh-Token-Paar vollständig validieren.
   - Token-Paar gemeinsam in Memory aktualisieren.
   - Account-Authorization für alle account-scoped Sessions erneut durchführen.
   - Erst danach den ursprünglichen Request wiederholen.
2. Parallele Refreshes serialisieren:
   - Prozessintern mindestens mit `threading.Lock` oder bestehendem Adapter-Lock.
   - Falls mehrere Prozesse dieselbe `.accounts.env` verwenden, zusätzlich File-Lock prüfen oder dokumentieren.
3. Token-Persistenz atomar machen:
   - Zielinhalt vollständig in temporäre Datei schreiben.
   - `fsync` soweit praktikabel.
   - per `os.replace` atomar ersetzen.
   - Dateirechte auf `0o600` setzen.
4. Access- und Refresh-Token niemals in getrennten unabhängigen Writes aktualisieren.
5. Bei Account-Authorization-Fehler nach Refresh:
   - klaren Fehler loggen.
   - Request nicht blind mehrfach wiederholen.
   - Adapter in definierten Fehlerzustand bringen.

### Tests

Neue oder erweiterte Tests in `tests/test_ctrader_live_fix.py`:

- Refresh triggert erneute Account-Authorization vor Retry eines account-scoped Requests.
- Zwei parallele Refresh-Versuche führen zu genau einem persistierten, konsistenten Token-Paar.
- Crash-/Fehler-Simulation zwischen Token-Erhalt und Persistenz hinterlässt keine gemischten Token.
- Persistierte Accounts-Datei hat `0600`-Rechte.

### Akzeptanzkriterien

- Kein account-scoped Retry ohne erfolgreiche erneute Account-Authorization.
- Access- und Refresh-Token werden nur als konsistentes Paar persistiert.
- Parallele Refreshes können keine stale Credentials zurückschreiben.
- Bestehende cTrader-Tests bleiben grün.

## 2. Live-Preflight Boolean-Parsing strikt machen

**Review-Finding:** `CTRADER_DEMO` defaultet auf `True`, aber ungültige Werte wie `tru` werden als `False` interpretiert und können unbeabsichtigt Live statt Demo verwenden.

**Datei:** `live_runner/preflight.py`

### Änderungsschritte

1. Eine zentrale Boolean-Parsing-Funktion einführen, z.B. `_parse_bool_env(name, default)`.
2. Nur explizite Werte akzeptieren:
   - true: `1`, `true`, `yes`, `y`, `on`
   - false: `0`, `false`, `no`, `n`, `off`
3. Bei unbekannten Werten mit klarer Fehlermeldung abbrechen.
4. Für Trading-/Connectivity-Kontext konservativ bleiben:
   - fehlendes `CTRADER_DEMO` darf weiter `True` sein.
   - fehlerhaftes `CTRADER_DEMO` darf niemals implizit Live auswählen.

### Tests

Neue oder erweiterte Tests in `tests/test_live_preflight.py`:

- fehlender Wert ergibt Demo.
- `true`/`false` und Varianten werden korrekt interpretiert.
- `tru`, `flase`, leere Strings und zufällige Werte lösen einen Fehler aus.
- Fehlermeldung benennt Variable und gültige Werte.

### Akzeptanzkriterien

- Kein ungültiger Boolean-Wert wird stillschweigend als `False` behandelt.
- Preflight kann durch Tippfehler nicht versehentlich Live ansprechen.

## 3. Live-Preflight muss dieselbe Accounts-Datei lesen und refreshen

**Review-Finding:** `run_live_preflight(..., accounts_env_path=...)` liest eine explizite Credentials-Datei, erstellt den `CTraderLiveAdapter` aber ohne diesen Pfad. Token-Refresh kann dadurch eine andere/default `.accounts.env` schreiben.

**Datei:** `live_runner/preflight.py`

### Änderungsschritte

1. Den geprüften `accounts_env_path` bis zur Adapter-Erzeugung durchreichen.
2. `CTraderLiveAdapter` so instanziieren, dass Refresh-Reads und Refresh-Writes exakt dieselbe Datei verwenden.
3. Falls der Adapter aktuell keinen Pfadparameter besitzt, einen expliziten optionalen Parameter hinzufügen.
4. Logging erweitern:
   - Pfad der validierten Accounts-Datei.
   - Pfad der Datei, in die Refresh-Tokens geschrieben werden.
   - keine Secrets loggen.

### Tests

In `tests/test_live_preflight.py`:

- Preflight mit temporärer Accounts-Datei und ablaufendem Token refreshed genau diese Datei.
- Default `.accounts.env` bleibt unverändert.
- Adapter erhält den erwarteten Pfad.

### Akzeptanzkriterien

- Validierung und Refresh beziehen sich immer auf dieselbe Accounts-Datei.
- Kein Preflight-Lauf erzeugt falsche Sicherheit durch Schreiben in eine andere Datei.

---

# Phase 2: SQLite-Migrationen und Live-Signal-Dedupe

## 4. `discord_message_id` darf bei fehlendem Wert echtes SQL `NULL` bleiben

**Review-Finding:** `str(signal.metadata.get("discord_message_id") or None)` erzeugt den Literal-String `"None"`. Der partielle Unique Index behandelt `"None"` als nicht-null, wodurch Live-Signale ohne Discord-ID ab dem zweiten Eintrag still verworfen werden.

**Datei:** `shared/database.py`

### Änderungsschritte

1. In `LiveSignalModel.from_live_signal` die Konvertierung ändern:
   - fehlender Wert bleibt `None`
   - vorhandener Wert wird zu `str(value)`
2. Leere Strings normalisieren:
   - `""` und Whitespace-only sollten ebenfalls `None` werden.
3. Optional kleine Helper-Funktion einführen, z.B. `_normalize_optional_id(value)`.

### Tests

In `tests/test_database_migrations.py` oder neuer Datei:

- zwei Live-Signale ohne `discord_message_id` werden beide gespeichert.
- zwei Live-Signale mit derselben echten Discord-ID werden dedupliziert.
- `discord_message_id=""` wird wie fehlend behandelt.

### Akzeptanzkriterien

- Keine gespeicherte `discord_message_id` hat den Wert `"None"`.
- Signale ohne Discord-ID sind nicht Teil des Discord-Dedupe-Indexes.

## 5. Migration für bestehende Discord-Dedupe-Daten backfillen

**Review-Finding:** Die Migration fügt `live_signals.discord_message_id` und einen Unique Index hinzu, backfillt bestehende Discord IDs aus `metadata` aber nicht. Alte Signale werden nicht erkannt und können erneut verarbeitet werden.

**Datei:** `shared/database.py`

### Änderungsschritte

1. Nach dem Hinzufügen der Spalte bestehende Datensätze scannen.
2. `discord_message_id` aus `metadata` extrahieren.
3. Nur valide, nicht-leere IDs backfillen.
4. Konflikte vor Erstellung des Unique Index behandeln:
   - bei Duplikaten deterministisch einen Datensatz als kanonisch wählen.
   - andere Datensätze entweder ohne Dedupe-ID belassen oder mit Audit-Log markieren.
5. Backfill idempotent machen.

### Tests

- Migration einer alten DB mit `metadata.discord_message_id`.
- Migration einer alten DB ohne `metadata.discord_message_id`.
- Migration mit Duplikaten.
- Zweiter Migrationslauf verändert nichts und schlägt nicht fehl.

### Akzeptanzkriterien

- Alte Discord-Signale werden nach Upgrade zuverlässig dedupliziert.
- Unique Index kann auch bei realistischen Altbeständen erstellt werden.

## 6. `save_live_signal` gegen Unique-Index-Races absichern

**Review-Finding:** Die Dedupe-Prüfung ist ein Pre-Check vor `commit`. Zwei Worker können beide keinen Datensatz sehen; einer gewinnt, der andere erhält `IntegrityError`.

**Datei:** `shared/database.py`

### Änderungsschritte

1. Den bestehenden Pre-Check behalten, aber nicht als alleinigen Schutz verwenden.
2. `session.commit()` in `try/except IntegrityError` kapseln.
3. Bei `IntegrityError`:
   - rollback ausführen.
   - existierenden Datensatz anhand `discord_message_id` erneut laden.
   - als Duplikat behandeln, nicht als fatalen Fehler.
4. Verhalten an `save_live_execution` angleichen, falls dort bereits ein robustes Muster existiert.

### Tests

- Zwei simulierte Sessions speichern gleichzeitig dieselbe Discord-ID.
- Ergebnis: genau ein Datensatz, kein ungefangener Fehler.
- Logging zeigt Dedupe statt Crash.

### Akzeptanzkriterien

- Reconnect-/Worker-Races führen nicht zu ungefangenen DB-Fehlern.
- Dedupe ist durch DB-Constraint und Anwendungscode abgesichert.

## 7. Dedupe darf fehlende `broker_order_id` nachträglich ergänzen

**Review-Finding:** Bei Duplikat wird das neue Signal komplett verworfen. Wenn der alte Datensatz noch keine `broker_order_id` hat, das neue Signal aber schon, geht die Order-Verknüpfung verloren.

**Datei:** `shared/database.py`

### Änderungsschritte

1. Bei gefundenem existierendem Signal prüfen:
   - `existing.broker_order_id` leer?
   - `record.broker_order_id` gesetzt?
2. Falls ja:
   - `existing.broker_order_id` aktualisieren.
   - optional weitere harmlose Reconciliation-Felder aktualisieren, aber keine Kernsignaldaten überschreiben.
3. Änderungen committen und eindeutig loggen.
4. Falls keine Ergänzung nötig ist, wie bisher deduplizieren.

### Tests

- Erstes Signal ohne `broker_order_id`, zweites Duplikat mit `broker_order_id`.
- Ergebnis: ein Datensatz mit ergänzter `broker_order_id`.
- Zweites Duplikat mit anderer Order-ID überschreibt nicht unkontrolliert.

### Akzeptanzkriterien

- Dedupe verhindert Duplikate, verliert aber keine spätere Broker-Verknüpfung.

## 8. `backtests.equity_curve` für bestehende Daten backfillen

**Review-Finding:** Bestehende Backtests bekommen bei Migration `[]`; `migrate_json_to_sqlite(skip_existing=True)` überspringt existierende IDs. Der Portfolio-Optimizer liest dadurch leere Equity Curves aus SQLite, obwohl JSON oder `equity_points` Daten enthalten.

**Datei:** `shared/database.py`

### Änderungsschritte

1. Migration so erweitern, dass bestehende Backtests mit leerer `equity_curve` nachbefüllt werden.
2. Datenquellen in priorisierter Reihenfolge:
   - vorhandene relationale `equity_points`, falls vorhanden.
   - zugehörige JSON-Backtest-Dateien.
   - andernfalls leer lassen und warnen.
3. `migrate_json_to_sqlite(skip_existing=True)` so anpassen, dass existierende Backtest-Zeilen mit leerer Equity Curve optional ergänzt werden dürfen.
4. Backfill idempotent gestalten.

### Tests

- Alte DB mit `equity_points`.
- Alte DB plus JSON-Datei.
- Existierender Backtest mit leerer Equity Curve wird ergänzt.
- Existierender Backtest mit nicht-leerer Equity Curve wird nicht überschrieben.

### Akzeptanzkriterien

- Nach Migration liefern bestehende Backtests dem Portfolio-Optimizer brauchbare Equity Curves.
- Keine vorhandenen nicht-leeren Kurven werden versehentlich ersetzt.

---

# Phase 3: Backtest-, Download- und Optimizer-Korrektheit

## 9. Grid-Market-Data-Download für `m30`/`30m` reparieren

**Review-Finding:** `TIMEFRAME_ALIASES` mappt `m30` auf `30m`, aber die Sortierliste kennt `30m` nicht. `ValueError` entsteht vor der Fehlerbehandlung und bricht den Batch ab.

**Datei:** `data_tools/download_grid_market_data.py`

### Änderungsschritte

1. Eine zentrale Timeframe-Order definieren, die alle erlaubten normalisierten Timeframes enthält:
   - `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`
2. Sortierung nicht mit rohem `.index(tf)` außerhalb des `try`-Blocks ausführen.
3. Unbekannte Timeframes früh validieren und sauber als Fehler pro Symbol/Timeframe melden.
4. Prüfen, ob weitere Aliase wie `M30`, `30min`, `H1` existieren oder erwartet werden.

### Tests

In `tests/test_grid_market_data_download.py`:

- `m30` und `30m` werden erfolgreich normalisiert und sortiert.
- Ein unbekannter Timeframe markiert nur den betroffenen Download als failed.
- Batch läuft nach einem Timeframe-Fehler weiter.

### Akzeptanzkriterien

- `m30`/`30m` bricht keinen gesamten Batch ab.
- Fehlerbehandlung greift pro Symbol/Timeframe.

## 10. Portfolio-Optimizer Korrelations-Penalty korrigieren

**Review-Finding:** Das Optimizer-Ziel addiert `5.0 * avg_off_diag` und belohnt damit positive Korrelation, obwohl Diversifikation beabsichtigt ist.

**Datei:** `runner/portfolio_optimizer.py`

### Änderungsschritte

1. Zielwert korrigieren:
   - von `score = sharpe + (5.0 * avg_off_diag) - ...`
   - zu `score = sharpe - (5.0 * avg_off_diag) - ...`
2. Kommentar und Testnamen an die tatsächliche Semantik anpassen.
3. Prüfen, ob negative Korrelation unbegrenzt belohnt werden soll oder ob nur positive Korrelation bestraft werden soll:
   - Variante A: `- 5.0 * avg_off_diag`
   - Variante B: `- 5.0 * max(0.0, avg_off_diag)`
4. Entscheidung dokumentieren.

### Tests

- Zwei synthetische Kurvensets mit identischem Sharpe, aber unterschiedlicher Korrelation.
- Optimizer bevorzugt niedriger oder negativ korrelierte Kombination.
- Regressionstest verhindert erneutes Vorzeichen-Kippen.

### Akzeptanzkriterien

- Positive Korrelation erhöht den Score nicht mehr.
- Test schlägt mit altem Vorzeichen fehl und mit Fix grün.

## 11. Portfolio-Optimizer Weight-Normalisierung und Alignment härten

**Review-Finding:** Optuna kann theoretisch alle Gewichte `0.0` ziehen; Division durch `weights.sum()` erzeugt `nan`/`inf`. `_align_equity_curves` füllt über die Vereinigungsmenge aller Zeitstempel und synthetisiert Pre-Inception-Perioden.

**Datei:** `runner/portfolio_optimizer.py`

### Änderungsschritte

1. Vor Normalisierung prüfen:
   - wenn Summe nicht finite oder `<= 0`, gleichverteilen oder Trial prunen.
2. `nan`/`inf` im Objective hart verhindern.
3. Equity-Alignment auf gemeinsames Overlap-Fenster umstellen oder explizit konfigurierbar machen.
4. Wenn kein ausreichender Overlap existiert:
   - Trial verwerfen.
   - verständliche Warnung erzeugen.
5. Mindestanzahl gemeinsamer Datenpunkte definieren.

### Tests

- Alle vorgeschlagenen Gewichte `0.0`.
- Eine Kurve startet deutlich später als andere.
- Kein Overlap führt zu sauberem Fehler/Skip.
- Overlap-Fall liefert finite Objective-Werte.

### Akzeptanzkriterien

- Objective gibt keine `nan`/`inf` zurück.
- Korrelation und Sharpe werden nicht durch synthetische Pre-Inception-Flats verzerrt.

## 12. Grid-Backtest Timeouts pro Zelle isolieren

**Review-Finding:** `subprocess.run(..., timeout=...)` ohne `TimeoutExpired`-Handling kann einen gesamten Grid-Lauf abbrechen.

**Datei:** `runner/backtest_grid.py`

### Änderungsschritte

1. `subprocess.TimeoutExpired` pro Grid-Zelle abfangen.
2. Zelle als `failed` mit Fehlerklasse `timeout` markieren.
3. Timeout-Information in Ergebnis-JSON und Summary aufnehmen.
4. Sicherstellen, dass serieller und paralleler Pfad dasselbe Verhalten haben.

### Tests

- Simulierter Timeout in einer Zelle.
- Weitere Zellen laufen weiter.
- Summary zählt Timeout als Failure.
- Prozesspool-/Future-Pfad propagiert den Timeout nicht als Gesamtabbruch.

### Akzeptanzkriterien

- Einzelne hängende Backtests reißen nicht den gesamten Grid-Lauf ab.

## 13. Grid-Backtest Daily-Equity mit aufgelösten Datumswerten attachen

**Review-Finding:** `attach_daily_equity` nutzt Original-`start`/`end` statt der durch `_detect_date_range` aufgelösten Werte.

**Datei:** `runner/backtest_grid.py`

### Änderungsschritte

1. Aufgelöste Datumswerte zentral ermitteln.
2. Dieselben Werte an Backtest, Skip-Existing-Pfad und `attach_daily_equity` übergeben.
3. Ergebnis-Metadaten mit tatsächlich verwendetem Zeitraum speichern.

### Tests

- Aufruf ohne explizites `start`/`end`, aber mit erkannten Daten.
- Skip-Existing-Pfad nutzt denselben Zeitraum.
- Daily Equity deckt exakt den tatsächlichen Backtest-Zeitraum ab.

### Akzeptanzkriterien

- Equity-Fenster und Backtest-Fenster stimmen überein.

## 14. Daily-Equity finite Checks und Frequenz-Fallback korrigieren

**Review-Finding:** `float(trade.pnl or 0.0)` lässt `nan`/`inf` durch. Im Fallback wird eine Nicht-Tages-Kurve zurückgegeben, aber als `equity_frequency="daily"` markiert.

**Datei:** `runner/daily_equity.py`

### Änderungsschritte

1. `math.isfinite` auf PnL und Equity-Werte anwenden.
2. Nicht-finite Trades:
   - überspringen und warnen, oder
   - als Fehler markieren, je nach bestehender Semantik.
3. Fallback darf Frequenz-Metadaten nicht fälschlich auf `daily` setzen.
4. Downstream-Erwartungen im Portfolio-Optimizer prüfen.

### Tests

- Trade mit `nan` PnL.
- Trade mit `inf` PnL.
- Fallback-Kurve behält richtige Frequenz oder markiert `unknown`.
- Optimizer akzeptiert nur valide tägliche Kurven.

### Akzeptanzkriterien

- Keine nicht-finiten Werte kontaminieren Daily Equity.
- Frequenz-Metadaten beschreiben die Daten korrekt.

---

# Phase 4: Kimchi-Validation-Framework

## 15. Readiness-Gate: fehlende Pflichtartefakte dürfen nicht `READY` ergeben

**Review-Finding:** Wenn Regression-, Risk- oder Stats-Artefakte fehlen, erzeugen die Checks nur Info-/Skipped-Einträge. Sind Trade-Checks sauber, kann das Gate `READY` melden, obwohl die Validierung faktisch nicht vollständig lief.

**Datei:** `validation/readiness/validator.py`

### Änderungsschritte

1. Pflichtartefakte definieren:
   - Regressionsergebnis
   - Risk-Comparison
   - Trade-Statistiken
   - ggf. Delta-Report
2. Fehlende Pflichtartefakte als `error` oder eigenen Status `UNKNOWN` behandeln.
3. Readiness-Level erweitern oder Semantik schärfen:
   - `NOT_READY` bei fehlenden Pflichtdaten, oder
   - neuer Level `UNKNOWN`, falls das Modell das unterstützt.
4. `passed` darf nur `True` sein, wenn alle Pflichtchecks vorhanden und bestanden sind.
5. Berichtstext so anpassen, dass Skip-Gründe sichtbar sind.

### Tests

- Kein Regression-Artefakt: nicht `READY`.
- Kein Risk-Artefakt: nicht `READY`.
- Nur Trade-Checks vorhanden: nicht `READY`.
- Vollständige, bestandene Artefakte: `READY`.
- Vollständige Artefakte mit Warnung: `READY_WITH_WARNINGS`.

### Akzeptanzkriterien

- Readiness-Gate kann keine Paper-Freigabe auf unvollständiger Datenbasis melden.

## 16. Doppelte Statistikmodule konsolidieren

**Review-Finding:** Es gibt `validation/statistics/calculator.py` und `validation/stats/calculator.py` mit divergenten mathematischen Konventionen. `validation/statistics/` ist derzeit toter Code, aber gefährlich.

**Dateien:**

- `validation/statistics/calculator.py`
- `validation/stats/calculator.py`
- `validation/statistics/__init__.py`
- Importstellen unter `validation/`

### Änderungsschritte

1. Per `rg "validation.statistics|statistics.calculator|TradeStatisticsCalculator"` alle Nutzer bestätigen.
2. Kanonisches Modul festlegen, bevorzugt das produktiv genutzte `validation/stats/`.
3. Totes Modul löschen oder als dünne Kompatibilitätsschicht mit Deprecation-Warnung auf das kanonische Modul mappen.
4. Mathematische Konventionen dokumentieren:
   - Stichproben- vs. Populations-Std.
   - Sortino-Denominator.
   - Drawdown-Einheiten.
   - Annualisierung.
5. Tests auf kanonische Werte ausrichten.

### Tests

- Statistiktests prüfen konkrete erwartete Werte.
- Kein Importpfad verwendet versehentlich das alte Modul.
- Optional: Kompatibilitätsimport liefert identische Werte.

### Akzeptanzkriterien

- Es gibt nur eine fachlich relevante Statistikimplementierung.
- Kennzahlen sind reproduzierbar und dokumentiert.

## 17. Risk-Sizing konservativ runden

**Review-Finding:** `round(position_size / lot_size) * lot_size` kann aufrunden und das Risikobudget überschreiten.

**Dateien:**

- `validation/risk/fixed_risk.py`
- `validation/risk/percent_risk.py`

### Änderungsschritte

1. `round()` durch `math.floor()`-basierte Rundung ersetzen.
2. Floating-Point-Toleranz berücksichtigen, z.B. über integer steps.
3. Sicherstellen, dass Mindestlotgrößen nicht ungewollt Risiko überschreiten:
   - entweder Position `0` erlauben,
   - oder klaren Fehler/Warning erzeugen.
4. Tatsächliches Risiko nach Rundung berechnen und zurückgeben.

### Tests

- Fall, bei dem `round()` bisher aufrundet.
- Tatsächliches Risiko ist `<=` Budget.
- Mindestlot größer als Budget wird sauber behandelt.

### Akzeptanzkriterien

- Risk-Modelle überschreiten das konfigurierte Budget nicht durch Rundung.

## 18. JSON-Reports strikt JSON-kompatibel machen

**Review-Finding:** `nan`/`inf` können in Reports landen; `json.dumps` emittiert dann `NaN`/`Infinity`, was kein valides striktes JSON ist.

**Datei:** `validation/reporting/json_report.py`

### Änderungsschritte

1. Vor Serialisierung alle numerischen Werte normalisieren:
   - finite Zahlen bleiben Zahlen.
   - `nan`/`inf` werden `None` oder strukturierte Strings mit Flag.
2. `json.dumps(..., allow_nan=False)` verwenden.
3. Fehlerfall mit hilfreicher Diagnose versehen.

### Tests

- Report mit `nan`.
- Report mit `inf`.
- Output lässt sich mit strengem JSON-Parser lesen.

### Akzeptanzkriterien

- Reports enthalten kein `NaN`, `Infinity` oder `-Infinity`.

## 19. MT5-Importer gegen Paarungs- und Zeitzonenfehler härten

**Review-Finding:** Entry/Exit-Paarung erfolgt per Zeilen-Adjazenz; überlappende Trades oder Teil-Exits können falsch gepaart werden. Naive MT5-Zeitstempel werden still als UTC interpretiert.

**Dateien:**

- `validation/importers/mt5_xlsx.py`
- `validation/importers/mt5_html.py`

### Änderungsschritte

1. Paarung bevorzugt über Position-/Order-ID implementieren.
2. Teil-Exits explizit modellieren:
   - entweder aggregieren,
   - oder mehrere Exit-Legs pro Entry zulassen.
3. Falls IDs fehlen:
   - Fallback-Strategie dokumentieren.
   - Warnungen für mehrdeutige Paarungen erzeugen.
4. Zeitzone konfigurierbar machen:
   - Importer-Parameter `source_timezone`.
   - Default dokumentieren.
   - keine stille UTC-Annahme ohne Hinweis.
5. HTML- und XLSX-Importer konsistent halten.

### Tests

- Überlappende Long/Short-Trades.
- Teil-Exit mit gleicher Position-ID.
- Naiver Timestamp in Nicht-UTC-Zeitzone wird korrekt nach UTC konvertiert.
- Legacy-Fallback bleibt für einfache Reports funktionsfähig.

### Akzeptanzkriterien

- Importer liefern bei realistischen MT5-Reports stabile, nachvollziehbare Trade-Paare.
- Zeitversatz ist konfigurierbar und getestet.

## 20. Comparison-/Regression-Pass-Logik klären

**Review-Finding:** Teilweise müssen Prozent- und Absoluttoleranz beide erfüllt sein, obwohl sie fachlich oft Alternativen sind. Delta-/Regression-Reports können gematchte Trades als Deviations zählen. `Anomalies.detect()` matcht Trades erneut statt vorhandene Ergebnisse zu nutzen.

**Dateien:**

- `validation/comparison/comparison.py`
- `validation/comparison/delta_report.py`
- `validation/regression/runner.py`
- `validation/comparison/anomalies.py`

### Änderungsschritte

1. Toleranzsemantik festlegen:
   - numerischer Vergleich besteht, wenn Prozent- oder Absoluttoleranz erfüllt ist, sofern beide konfiguriert sind.
2. Deviations nur aus echten Mismatches, fehlenden Trades oder Schwellenüberschreitungen bilden.
3. `Anomalies.detect()` so umbauen, dass ein vorhandenes `MatchResult` übergeben werden kann.
4. Berichtsfelder eindeutig benennen:
   - matched
   - mismatched
   - missing_expected
   - missing_actual
   - warning_deltas

### Tests

- Kleine absolute Abweichung bei großer Prozentabweichung besteht.
- Große absolute Abweichung bei kleiner Prozentabweichung besteht, falls Prozenttoleranz erfüllt ist.
- Gematchte Trades werden nicht als Fehler gezählt.
- Anomalie-Erkennung nutzt übergebenes MatchResult.

### Akzeptanzkriterien

- Regressionsergebnis spiegelt echte Abweichungen wider.
- Keine doppelte oder widersprüchliche Match-Berechnung.

## 21. Golden-Re-Import pfadunabhängig machen

**Review-Finding:** `regression/runner.py` nutzt `golden_ref.source_path` direkt; relative Pfade hängen vom aktuellen Arbeitsverzeichnis ab.

**Datei:** `validation/regression/runner.py`

### Änderungsschritte

1. Relative Pfade relativ zur Golden-Datei oder zum Projektroot auflösen.
2. Gewählte Semantik dokumentieren.
3. CLI-Aufrufe aus beliebigem CWD unterstützen.

### Tests

- Regression aus Projektroot.
- Regression aus Unterverzeichnis.
- Golden-Datei mit relativem `source_path`.

### Akzeptanzkriterien

- Regression-Läufe sind unabhängig vom aktuellen Arbeitsverzeichnis reproduzierbar.

---

# Phase 5: Repo-, Security- und Merge-Hygiene

## 22. `.accounts.example.env` Contract stabilisieren

**Review-Finding:** Kimchi benennt `.accounts.example.env` in `.accounts.env.example` um. Das bricht bestehende Setup-/Onboarding-Pfade, sofern nicht alle Referenzen migriert werden.

**Dateien:**

- `.accounts.example.env`
- `.accounts.env.example`
- Dokumentation und Setup-Skripte

### Änderungsschritte

1. Entscheidung treffen:
   - bevorzugt: bestehenden Namen `.accounts.example.env` beibehalten.
   - alternativ: Rename vollständig und dokumentiert migrieren.
2. Per `rg "accounts.example.env|accounts.env.example"` alle Referenzen prüfen.
3. Nur einen kanonischen Namen verwenden.
4. Falls Kompatibilität nötig ist, zweiten Namen als Kopie oder Hinweisdatei verwenden, aber nicht widersprüchlich.

### Tests/Checks

- Setup-Dokumentation verweist auf existierende Datei.
- Preflight-/Credential-Code erwartet denselben Namen wie die Docs.

### Akzeptanzkriterien

- Neue Entwickler finden eine funktionierende Example-Datei unter dem dokumentierten Namen.

## 23. Echte Konto-IDs aus Example-Env entfernen

**Review-Finding:** `.accounts.example.env` enthält reale Alpaca-Paper-Kontonummern statt Platzhalter.

**Datei:** `.accounts.example.env`

### Änderungsschritte

1. Alle echten Konto-IDs durch Platzhalter ersetzen.
2. Beispielwerte erkennbar fiktiv machen:
   - `your_alpaca_account_number_here`
   - `your_ctrader_account_id_here`
3. Prüfen, ob Reports oder Docs echte Konto-IDs enthalten.
4. Falls echte Secrets jemals committed wurden:
   - Team über History-Cleanup/Secret-Rotation entscheiden lassen.

### Tests/Checks

- `rg` nach bekannten Konto-ID-Mustern und alten Werten.
- `git diff` prüfen, dass keine echten IDs neu auftauchen.

### Akzeptanzkriterien

- Getrackte Template-Dateien enthalten keine echten Account-Identifier.

## 24. Große generierte Artefakte aus dem Merge-Diff entfernen

**Review-Finding:** Beide Review-Stränge nennen sehr große generierte Artefakte, darunter `.kimchi/ferments/...`, `*.trades.json`, PDFs, XLSX, Matrix-/Graphify-Outputs. Diese blähen Diff, History und Reviews auf.

**Betroffene Pfade laut Reviews:**

- `.kimchi/ferments/**`
- `reports/*.trades.json`
- `frontend/public/matrix-grid-overview.json`
- `graphify-out/graph.json`
- CSV-/XLSX-/PDF-Reportdumps

### Änderungsschritte

1. Artefakte kategorisieren:
   - notwendige kleine Fixtures
   - generierte Runtime-Daten
   - große Backtest-Ergebnisse
   - externe Review-/Report-Ausgaben
2. Runtime- und Ergebnisartefakte aus Git entfernen.
3. `.gitignore` erweitern:
   - `.kimchi/`
   - `graphify-out/`
   - große `reports/*.trades.json`, sofern keine kuratierten Fixtures
   - generierte Matrix-/Grid-Outputs
4. Kleine Fixtures nur unter eindeutigem Fixture-Pfad behalten, z.B. `tests/fixtures/`.
5. Falls große Referenzdaten fachlich nötig sind:
   - Git LFS oder externen Artefaktspeicher verwenden.
   - README mit Download-/Regenerationsanweisung ergänzen.

### Checks

- `git diff --stat dev_fred...branch`
- `find . -path ./.git -prune -o -size +5M -print`
- CodeRabbit oder andere Reviewtools können den bereinigten Diff verarbeiten.

### Akzeptanzkriterien

- Merge-Diff enthält nur kuratierte Quell-, Test- und kleine Fixture-Dateien.
- Keine Tool-Runtime-Verzeichnisse werden versioniert.

## 25. Whitespace-Fehler beseitigen

**Review-Finding:** `git diff --check` schlägt in beiden Branches fehl.

**Betroffene Beispiele:**

- `runner/grid_tester.py`
- `runner/portfolio_optimizer.py`
- `scripts/overnight_strategy_discovery.py`
- `signal_bots/health_check.py`
- `strategy-comparisons/risk_parity/risk_parity_strategy.py`
- `.agents/strategy-conversion.md`
- `docs/validation_*.md`
- `todo/kimchi-todo.md`

### Änderungsschritte

1. Trailing whitespace entfernen.
2. Keine inhaltlichen Doc-Änderungen mit Whitespace-Fix vermischen, falls möglich.
3. Editorconfig oder Formatter prüfen.

### Checks

- `git diff --check dev_fred...codex_rewrite`
- `git diff --check codex_rewrite...kimchi-kimi-continue`
- vor finalem Merge: `git diff --check dev_fred...target_branch`

### Akzeptanzkriterien

- `git diff --check` ist sauber.

## 26. Viewer Grid-Result-Pfade validieren

**Review-Finding:** `result_id` wird in `viewer/core.py` direkt in einen Pfad eingesetzt. DELETE ist destruktiv und die Viewer-Oberfläche hat keine Auth.

**Datei:** `viewer/core.py`

### Änderungsschritte

1. `result_id` gegen Whitelist validieren:
   - `^[A-Za-z0-9_-]+$`
2. Pfad mit `resolve()` normalisieren.
3. Containment prüfen:
   - `resolved.parent == GRID_RESULTS_DIR.resolve()`
4. Für ungültige IDs `400` zurückgeben.
5. Für DELETE prüfen, ob zusätzliche lokale Schutzmechanismen nötig sind:
   - nur localhost dokumentieren.
   - optional Feature-Flag für destructive endpoints.

### Tests

- valide ID.
- ID mit Slash, `..`, URL-encoded Traversal, Sonderzeichen.
- DELETE löscht nur Datei im Grid-Results-Verzeichnis.

### Akzeptanzkriterien

- Kein Request kann außerhalb des Grid-Result-Verzeichnisses lesen oder löschen.

---

# Phase 6: Weitere Korrekturen und Tests

## 27. Optimizer interne Parameter namespacen und Universe-Explosion begrenzen

**Datei:** `runner/optimizer.py`

### Änderungsschritte

1. Internen Optuna-Parameter `symbol_universe` umbenennen, z.B. `__internal_symbol_universe`.
2. Sicherstellen, dass Strategieparameter gleichen Namens nicht herausgefiltert werden.
3. Symbol-Subsets nicht unbegrenzt eagerly materialisieren.
4. Cap oder Lazy-Generator einführen.

### Tests

- Strategieparameter `symbol_universe` bleibt in `best_params` erhalten.
- Große Symbolmenge überschreitet definiertes Cap nicht.

## 28. `grid_id` kollisionssicher machen

**Datei:** `runner/grid_tester.py`

### Änderungsschritte

1. Timestamp um Mikrosekunden erweitern.
2. Zusätzlich kurzen UUID-Suffix anhängen.
3. ID-Format in Tests und Docs aktualisieren.

### Tests

- Zwei Grid-Runs innerhalb derselben Sekunde erzeugen verschiedene IDs.

## 29. Ungenutztes `--trials` in `grid_tester.py` entfernen oder verdrahten

**Datei:** `runner/grid_tester.py`

### Änderungsschritte

1. Entscheiden, ob das Flag fachlich gebraucht wird.
2. Wenn ja: an Optimizer/Grid-Logik durchreichen.
3. Wenn nein: CLI-Flag entfernen und Docs anpassen.

### Tests

- CLI-Test für erwartetes Verhalten.

## 30. Cron-Watchdog temporäre Datei absichern

**Datei:** `scripts/cron_wakeup_watchdog.sh`

### Änderungsschritte

1. Festen Pfad `/tmp/cron_failed_ids.txt` durch `mktemp` ersetzen.
2. Cleanup per `trap` sicherstellen.
3. `job_id` optional gegen `^[A-Za-z0-9_-]+$` validieren.

### Tests/Checks

- Shellcheck, falls verfügbar.
- Manuelle Dry-Run-Ausführung mit Beispielinput.

## 31. Frontend-Nits bereinigen

**Dateien laut Review:**

- `frontend/src/pages/BacktestsPage.tsx`
- `frontend/src/pages/MatrixGridPage.tsx`
- `frontend/src/components/OptimizeModal.tsx`
- `frontend/src/pages/LiveDetail.tsx`

### Änderungsschritte

1. `useEffect`-Fetches mit Cancellation-Flag oder AbortController absichern.
2. `OptimizeModal` Button-Disabled-Logik an tatsächliche Validierung angleichen.
3. Als Button gestylte Links mit passender Semantik versehen oder echte Buttons verwenden.
4. Frontend-Build-Abhängigkeiten installieren bzw. Lockfile prüfen.

### Tests/Checks

- `npm run build`
- vorhandene Frontend-Tests, falls vorhanden.
- Manuelle Smoke-Prüfung der betroffenen Seiten.

## 32. Validation CLI- und Test-Nits beheben

**Datei:** `validation/cli.py` plus `tests/validation/**`

### Änderungsschritte

1. `--time-tolerance` konsistent typisieren.
2. Negative Toleranzen verbieten.
3. `--reference` Hilfetext und Verhalten angleichen.
4. `MT5HTMLImporter` aus `validation/importers/__init__.py` exportieren.
5. Fixture-Pfade über `Path(__file__)` statt CWD bauen.
6. Tautologische Assertions durch konkrete Erwartungen ersetzen.

### Tests

- CLI-Parsing negativer und fractional Toleranzen.
- Import aus `validation.importers`.
- Tests laufen aus anderem CWD.

## 33. Testabdeckung gezielt schließen

**Review-Finding:** Mehrere neue kritische Pfade haben keine oder schwache Tests.

### Priorisierte neue Tests

1. `live_runner/preflight.py`
   - Boolean parsing.
   - Accounts-Datei-Pfad.
   - Refresh-Pfad.
2. `data_tools/download_grid_market_data.py`
   - Timeframe-Aliasing.
   - Symbol-Aliasing.
   - Batch-Fehlerisolierung.
3. `runner/daily_equity.py`
   - nicht-finite Werte.
   - Datums-/Frequenz-Fallback.
4. `runner/portfolio_optimizer.py`
   - Korrelation.
   - Zero weights.
   - Overlap-Alignment.
5. `tests/test_walkforward.py`
   - echte Verhaltenstests statt nur Signaturtests.
6. cTrader-Protobuf-Mocks
   - näher an echter API modellieren.

### Akzeptanzkriterien

- Jeder P0-Fix hat mindestens einen Regressionstest, der vor dem Fix fehlschlägt.
- Kritische Live- und DB-Pfade sind ohne echtes Netzwerk testbar.

---

# Empfohlene Umsetzungsreihenfolge

## Sprint 1: Harte Merge-Blocker

1. `shared/database.py`
   - `discord_message_id` Normalisierung.
   - `save_live_signal` Race-Handling.
   - `broker_order_id` Reconciliation.
   - Discord- und Equity-Backfills.
2. `live_runner/ctrader_adapter.py`
   - Refresh-Serialisierung.
   - erneute Account-Authorization.
   - atomare Token-Persistenz.
3. `live_runner/preflight.py`
   - striktes Boolean-Parsing.
   - Accounts-Datei-Pfad durchreichen.
4. `data_tools/download_grid_market_data.py`
   - `30m` Fix.
5. `runner/portfolio_optimizer.py`
   - Korrelations-Penalty.
6. `validation/readiness/validator.py`
   - fehlende Pflichtartefakte nicht `READY`.

**Exit-Kriterien Sprint 1:**

- Alle neuen P0-Regressionstests grün.
- Keine ungefangenen Live-/DB-Blocker aus den Reviews offen.

## Sprint 2: Korrektheit und Robustheit

1. `runner/backtest_grid.py`
   - Timeout-Isolation.
   - aufgelöste Datumswerte für Daily Equity.
2. `runner/daily_equity.py`
   - finite Checks.
   - Frequenz-Fallback.
3. `runner/portfolio_optimizer.py`
   - Zero weights.
   - Overlap-Alignment.
4. `validation/risk/*`
   - konservative Rundung.
5. `validation/stats`/`validation/statistics`
   - Konsolidierung.
6. `validation/reporting/json_report.py`
   - strict JSON.

**Exit-Kriterien Sprint 2:**

- Portfolio-/Backtest-/Validation-Korrektheitsregressionen grün.
- Keine bekannten `nan`/`inf`-Pfade in Optimizer und Reports.

## Sprint 3: Importer, CLI und Hygiene

1. MT5-Importer Paarung und Zeitzonen.
2. Comparison-/Regression-Pass-Logik.
3. Golden-Pfade CWD-unabhängig.
4. `.accounts.example.env` Contract.
5. echte Konto-IDs entfernen.
6. große Artefakte aus Git entfernen und `.gitignore` erweitern.
7. Whitespace-Fixes.
8. Viewer- und Frontend-Nits.

**Exit-Kriterien Sprint 3:**

- `git diff --check` sauber.
- Diff enthält keine unkuratierten Runtime-Artefakte.
- Setup-Dokumentation und Example-Env stimmen überein.

---

# Verifikationsplan

## Lokale Checks nach jedem relevanten Paket

```bash
python -m compileall data_tools live_runner runner scheduler scripts shared signal_bots strategies viewer validation
```

```bash
python -m pytest \
  tests/test_database_migrations.py \
  tests/test_ctrader_live_fix.py \
  tests/test_live_preflight.py \
  tests/test_grid_market_data_download.py
```

```bash
python -m pytest \
  tests/test_runner_cli.py \
  tests/test_backtest_grid.py \
  tests/test_backtest_matrix.py \
  tests/test_auto_download.py
```

```bash
python -m pytest \
  tests/validation \
  tests/strategies/test_intraday_volatility_breakout.py \
  tests/strategies/test_risk_parity.py
```

## Hygiene-Checks

```bash
git diff --check dev_fred...codex_rewrite
```

```bash
git diff --check codex_rewrite...kimchi-kimi-continue
```

```bash
git diff --stat dev_fred...HEAD
```

```bash
find . -path ./.git -prune -o -size +5M -print
```

## Tooling-Checks

Falls Abhängigkeiten installiert sind:

```bash
python -m ruff check .
```

```bash
npm run build
```

Falls CodeRabbit erneut verwendet wird, sollte der Diff vorher von generierten Artefakten bereinigt werden, damit keine Payload-Limits greifen.

---

# Merge-Gates

Eine Branch darf erst erneut als merge-ready bewertet werden, wenn alle folgenden Bedingungen erfüllt sind:

1. Alle P0-Findings sind behoben und durch Regressionstests abgedeckt.
2. `git diff --check` ist sauber.
3. Keine echten Secrets, Tokens oder Account-Identifier sind in getrackten Template-/Report-Dateien.
4. Große generierte Artefakte sind aus dem Merge-Diff entfernt oder bewusst als kuratierte Fixtures dokumentiert.
5. Live-Preflight kann nicht durch ungültige Env-Werte in Live statt Demo kippen.
6. cTrader-Refresh ist atomar, serialisiert und reauthorisiert account-scoped Requests.
7. SQLite-Migrationen verschlechtern bestehende Daten nicht.
8. Portfolio-Optimizer belohnt Diversifikation statt positive Korrelation.
9. Kimchi-Readiness meldet bei fehlenden Pflichtdaten nicht `READY`.
10. Die relevanten Python- und Frontend-Checks wurden erfolgreich ausgeführt oder ihre Nicht-Ausführbarkeit ist dokumentiert.

---

# Offene Entscheidungen

Diese Punkte sollten vor oder während der Umsetzung explizit entschieden werden:

1. Soll das Readiness-Gate bei fehlenden Pflichtdaten `NOT_READY` oder einen neuen Status `UNKNOWN` liefern?
2. Welche Statistik-Konvention ist kanonisch: Stichproben- oder Populationsstandardabweichung, welche Sortino- und Drawdown-Einheiten?
3. Welche MT5-Server-Zeitzone ist in der Zielumgebung maßgeblich?
4. Soll `.accounts.example.env` der kanonische Dateiname bleiben?
5. Sollen große Backtest-/Validation-Referenzdaten über Git LFS, externen Storage oder Regenerationsskripte verwaltet werden?
6. Ist der Viewer garantiert nur lokal erreichbar, oder braucht der DELETE-Endpunkt eine stärkere Schutzschicht?

---

# Definition of Done

Der Fix-Zyklus gilt als abgeschlossen, wenn:

- alle P0- und vereinbarten P1-Fixes implementiert sind,
- alle zugehörigen Regressionstests grün sind,
- Repo-Hygiene-Checks grün sind,
- der bereinigte Diff erneut reviewbar ist,
- offene Entscheidungen dokumentiert sind,
- eine neue Merge-Bewertung für `codex_rewrite` und danach für `kimchi-kimi-continue` erstellt wurde.
