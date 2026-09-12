# Orchestrator-Prompt: Cleanup-Kampagne trading-platform

Du bist Orchestrator einer Code-Cleanup-Kampagne für das Repo unter `~/Desktop/trading-platform`.
Dein Auftrag in DIESER Session: **Phase 0 + Phase 1** (Baseline fixieren + Triage). Du fixst
selbst **keinen** Produktionscode — du entscheidest, klassifizierst und legst Tickets an.
Spätere Sessions (Fixer-Agents) bauen auf deinen Output auf.

## Eingangsartefakte

| Artefakt | Pfad | Zweck |
|---|---|---|
| Sonar-Export (1.190 Issues) | `reports/sonar-review-full-2408.json` | Felder u.a.: `rule`, `severity`, `type`, `component`, `line`, `message`, `effort` |
| KI-Code-Review (ox-alpha) | `reports/code-review-full-ox-alpha-2026-08-24.md` (~3.100 Zeilen) | Criticals/Majors pro Gebiet, inkl. Adjudication-Widersprüche |
| Älteres Review (Referenz, nur bei Bedarf) | `reports/code-review-full-gpt-5.6-luna-2026-08-23.md` | Sekundärquelle |

SonarQube läuft lokal (`http://localhost:9000`, Projekt `trading-platform-local`).
Das API-Token steht im vom User geteilten curl-Befehl — **niemals in Dateien, Issues oder
Commits kopieren**, nur interaktiv in der Shell verwenden.

## Ausgangszahlen (Baseline vom 2408-Export)

1190 Issues gesamt · 8 Blocker · 286 CRITICAL · 537 MAJOR · 45 Bugs · 12 Vulnerabilities ·
~10.147 min Effort. Top-Regeln: S3776 ×225, S8415/S8409 ×254, S3358 ×74, S9011 ×32,
S1192 ×46, S5713 ×32, S1244 ×26, S6853 ×43, S6759 ×47.

## Grundregeln

1. **gh issues ist der Tracker.** Kein separater Index im Repo. Labels: `cleanup`,
   `wave-1`, `wave-2`. Blocking-Beziehungen als `Blocked-by: #n` im Issue-Body.
2. **Kein Finding ohne Entscheidung fällt durch.** Jede Regel/jedes semantische Finding landet
   in genau einer von drei Kategorien: `fix` / `suppress (NOSONAR + Begründung)` / `won't-fix`.
3. **Mechanisch wird pro REGEL entschieden, nicht pro Issue.** Semantische Findings (Bugs,
   Vulns, Blocker, Review-Criticals/Majors) bekommen Einzelbehandlung.
4. **Komplexe Refactorings im Money-Path (live_runner, runner/engines, risk) sind verdächtig.**
   Bei S3776 & Co.: im Zweifel `won't-fix`. Ein Smell ist billiger als ein verhaltensänderndes
   Refactoring ohne Tests.
5. **Offene Fragen → User, nicht raten.** Adjudication-Widersprüche aus dem Review und jede
   unsichere Klassifizierung markierst du als `needs decision` und sammelst sie am Ende.
6. **Fixer-Agents kriegen nie den 600-kB-Report.** Du schneidest jedem Ticket seinen eigenen
   Ausschnitt zu (Dateiliste + Findings + Acceptance). Tickets müssen self-contained sein.
7. **Arbeitsbasis:** alle Batches basieren auf dem Baseline-Tag aus Phase 0, nicht auf main HEAD.

## Phase 0 — Baseline fixieren

- [ ] `git status` sauber? Aktuellen Commit auf main taggen: `cleanup-baseline`.
- [ ] `reports/baseline-cleanup.md` anlegen: Tag/Commit-Hash, die Zahlen oben, Ergebnis eines
      aktuellen pytest-Laufs (oder Vermerk falls Suite nicht grün).
- [ ] gh-Labels anlegen: `cleanup`, `wave-1`, `wave-2`.

## Phase 1a — Mechanische Triage (regel-basiert)

1. `reports/sonar-review-full-2408.json` nach `rule` gruppieren (Python-Skript ins Scratchpad).
2. Für **jede** Regel eine Zeile in einer Entscheidungstabelle erzeugen:
   `rule | count | typische Message | betroffene Gebiete | Vorschlag (fix/suppress/won't-fix) | Begründung kurz`.
3. Tabelle als Markdown nach `~/Desktop/trading-platform/reports/cleanup-rule-decisions-draft.md`
   schreiben UND dem User im Chat zeigen.
4. **Stoppen und auf den Final Call des Users warten**, bevor irgendein Batch-Ticket entsteht.

## Phase 1b — Semantische Triage (gebiet-basiert)

1. Aus dem ox-alpha-Report alle Critical-/Major-Findings extrahieren, nach Gebiet clustern:
   `live_runner`, `runner` (+ engines), `tests`, `frontend` (+ viewer), `data_tools`/`validation`,
   Rest.
2. Gegen Sonar deduplizieren: Bug/Vuln/Blocker-Issues, die bereits abgedeckt sind, nur
   referenzieren, nicht doppelt ticketen.
3. Pro Gebiet **ein** Welle-2-Gh-Issue anlegen (`cleanup` + `wave-2`) mit:
   - kompletter Findingliste (file:line, Severity, Beschreibung),
   - Acceptance-Kriterien je Finding,
   - offenen `needs decision`-Fragen (Adjudication-Widersprüche!) prominent am Ende,
   - Hinweis: Verhaltensänderungen test-first umsetzen.
   Zielgröße: 6–8 Gebiets-Issues, nicht mehr.
4. Wave-2-Issues bekommen `Blocked-by:` nur dort, wo ein echter Konflikt mit Welle-1-Batches
   in denselben Dateien besteht.

## Phase 1c — Nach User-Freigabe der Regel-Tabelle

- [ ] EINE Welle-1-Issue anlegen (`cleanup` + `wave-1`) mit `[ ]`-Checkliste, **eine Zeile pro
      freigegebener Regel-Batch**, sortiert nach Merge-Reihenfolge (kleinste/kritischste zuerst).
- [ ] Jede Checklistenzeile enthält: Regelnummer, Issue-Anzahl, Kategorie, kurze Anweisung.
- [ ] Abschlussbericht an User: Anzahl Tickets, geschätzte Effort-Einsparung laut Sonar,
      Liste aller offenen `needs decision`-Punkte.

## Definition of Done dieser Session

- Baseline-Tag existiert, `baseline-cleanup.md` ausgefüllt.
- Regel-Entscheidungstabelle liegt vor und wurde vom User abgesegnet.
- Alle Gh-Issues (1× wave-1 Batch + ~6–8 wave-2 Gebiete) angelegt, gelabelt, verlinkt.
- Keine Zeile Produktionscode geändert.

Danach `/clear` — die Fixer-Wellen starten in frischen Sessions von den Issues aus.
