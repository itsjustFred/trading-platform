# Review: 26 offene Cleanup-PRs

## Die 9 Minors im Detail (Stand 2026-09-04 abends, per Token-API auf Added-Lines verifiziert)

Keiner blockiert einen Merge. Sortiert nach Relevanz:

| # | PR | Regel | Stelle | Was | Auswirkung |
|---|---|---|---|---|---|
| 1 | #68 | S3776 (CRITICAL-Label, de facto Minor) | `live_runner/ctrader_adapter.py:1302` | Cognitive Complexity 16 statt 15 | Rein Lesbarkeit. Kein Verhalten. Fix = Helper extrahieren (~10 Min) — mitten im Money-Path kurz vor Merge legitimerweise auch „akzeptiert". Einziger der 9 in Produktionscode mit Live-Berührung. |
| 2 | #71 | S1172 | `live_runner/signal_sources/discord_source.py:573` | Ungenutzter Parameter `channel_id` | Toter Parameter, null Verhalten. Ein-Wort-Fix. Klein irreführend für spätere Leser (suggeriert Channel-Filterung, die es nicht gibt). |
| 3–4 | #68 | 2× S5778 | `tests/test_live_execution_safety.py:684,753` | Exception-Tests mit mehreren möglicherweise-werfenden Calls | Nur Testqualität: wirft der erste Call, laufen die restlichen Asserts nie → könnte eine Regression in genau diesen 2 Tests verdecken. Niedrig. |
| 5–6 | #68 | S1172 + S7503 | `scripts/live_smoke.py:867` | Ungenutzter Param `qty` + `async` ohne `await` | Dev-/Smoke-Skript, null Prod-Impact. |
| 7 | #69 | S2933 | `frontend/src/components/ErrorBoundary.tsx:19` | Member sollte `readonly` sein | Stil, null Laufzeit-Impact. |
| 8 | #69 | S6594 | `frontend/src/components/LiveTerminal.tsx:81` | `RegExp.exec()` statt `match()` | Micro-Stil, null Verhalten. |
| 9 | #71 | S9073 | `tests/live_runner/test_discord_ingestion_gates.py:533` | Zusammengesetztes Assert | Test-Stil, null Impact. |

Empfehlung: 2, 5–9 fixen (je ~1 Min) oder akzeptieren; bei 1 bewusst entscheiden (Fix vs. Akzeptanz-Kommentar). Danach stimmt die „0 Issues"-Aussage in Report 3.2c — oder dort auf „keine Rating-relevanten Issues" umformulieren.

## Was Gruppe-C-Sign-offs sind

Die 10 roten Sonar-Gates werden nicht grün (LoC-Limit). Ein Sign-off ersetzt das grüne Gate durch eine **menschliche Freigabe mit Begründung** — pro PR ein Satz nach dieser Vorlage:

> *Gate rot wegen [Fenster-Artefakt: z.B. 216 pre-existing Issues in viewer/main.py / Dup 4,9 % aus umbenannten Duplikaten / Taint-Regeln neu auf altem Code], changed lines verifiziert clean ([Methode: Added-Lines-Abgleich per Sonar-API / PR-Body-Doku]), daher Freigabe trotz rotem Check.*

Praktisch: als Kommentar auf den jeweiligen PR posten (damit es am PR hängt, nicht nur im Report) und beim Mergen formal approven — aktuell hat **keiner** der 26 PRs ein Human-Approval (`reviewDecision` leer). Mindestens bei den Money-Path-PRs (#48/#70/#68/#72) bitte echten Review-Approve klicken, bei den mechanischen reicht der Kommentar.

## Re-Check 2026-09-04 abends (HTML + alle 26 PRs frisch gelesen)

- **HTML (36k, 20:33):** #56 jetzt in der Merge-Order (nach #62, vor #65) ✓, 3.2b/3.2c vorhanden ✓, #66-„landed on main" korrigiert (explizite Correction-Note im Body, verifiziert) ✓. Doc-Punkte erledigt.
- **PR #62 Two-Axis-Re-Run (18:34, 5 neue Commits bis `f1c5ba8`):** Die 10 Deep-Review-Findings wurden **im PR gefixt statt als Tickets** — inkl. MAJOR date-only-`--from`-TZ-Bug (empirisch: TZ=UTC 2 Trades vs. NY 0) und Sonar-Dup 3,1 %→0,0 %. Sonar-Check PASS verifiziert, 1288 passed, ready-for-human wieder gesetzt. Besser als meine Ticket-Empfehlung; #62-Todo entfällt. (CodeRabbit-Spalte zeigt weiter „rate limited" — die 0-CRITICAL/MAJOR-Aussage beruht auf CLI-Runs.)
- **PR #68 neuer Commit `21a0496` (15:42):** echten Rot-Test gefixt (notional-cap `realized_risk_amount`, aus Money-Path-Rehearsal, 74/74 Tests grün). Changed-Lines-Re-Check: **keine neuen Sonar-Issues** durch den Commit — es bleibt bei den bekannten 5 Minors. CodeRabbit-Spalte jetzt „completed".
- **PR #57 HEAD `0d9d12a`:** Commit-Message sagt „(PR #68)", Inhalt (`pynecore_adapter.py` + Containment-Tests) gehört aber zu #57 — nur falsch gelabelt, kein falscher Branch. Gleicher Zahlendreher in `cr-findings-todo.md` („PR #68/16-C", 16-C ist #57). Kosmetik, beim Sign-off ignorieren.
- **Gesamtstand:** alle 26 MERGEABLE, Labels intakt, UNSTABLE-Set unverändert (die bekannten 10, #67=CANCELLED). Sonar-PASS-Set stabil (16), keine neuen Commits außer #62 (5) und #68 (1).

## Zu tun bis Merge-Ready (Stand 2026-09-04 nachmittags)

- [ ] 9 triviale Sonar-Minors fixen oder akzeptieren (#68: 5, #69: 2, #71: 2 — Details im Sonar-Befund unten). Sonst stimmt die „0 Issues"-Aussage in 3.2c nicht. (Re-Check: #68-Neucommit bringt keine neuen — es bleibt bei 9.)
- [x] #61 Golden-Master-Diff — Human-Ok (Strategien werden ohnehin alle neu gecheckt).
- [x] #62-Follow-up-Tickets — entfällt, im PR gefixt (Re-Check abends verifiziert).
- [x] 2 Doc-Fixes — erledigt (HTML-Order + #66-Body, Re-Check abends verifiziert).
- [x] Money-Path-Tests lokal gelaufen (2026-09-04 abends, in den Kampagnen-Worktrees, je mit DATA_DIR+flock): main-Baseline 131 passed; 15a (wt-21) 131; 15b (wt-22) 131; 20a (wt-25) 131; 15d (wt-24) 131; 15c (wt-23) 165 passed (inkl. Discord-Gate-Tests). Alles grün, 0 Failures.
- [x] Gruppe-C-Sign-offs gepostet (10/10 PR-Kommentare, 2026-09-04) + 9 Minors in SonarCloud als Accepted markiert (mit Kommentar, Fenster 36→31 / 82→80 / 13→11).
- [ ] Branch Protection: **main ist nicht protected** — Approve ist technisch nicht nötig, Merge-Klick reicht. Trotzdem Empfehlung: bei #48/#70/#68/#72 Approve klicken (Audit-Trail für Live-Trading-Änderungen; aktuell hat kein PR ein Human-Approval).
- [ ] Danach sequenziell mergen: #53 → #55 → #47 → #49 → #51 → #52 → #62 → #56 → #65 → #66 → #61 → #50 → #54 → #69 → #58 → #60 → #59 → #48 → #70 → #68 → #72 → #71 → #57 → #64 → #63 → #67. Nach jedem Merge Downstream rebasen + Checks abwarten.
- [ ] Folgetickets (nach den Merges): Sonar-Zukunft (bezahlen/public/manuelles Gating), lokaler Post-Merge-Rescan, Backlogs (Security-Taint, S3776-Rest).

## Nachprüfung 2026-09-04 (nach Nacharbeit)

Die Nacharbeit adressiert meinen Hauptkritikpunkt (CodeRabbit-HEAD-Lücke) substanziell — verifiziert:

- **Alle 6 Batch-Reviews sind durch** (B1–B6, 48 Findings: 4 CRITICAL + ~11 MAJOR). Methodik (Greedy-Batches ohne File-Overlap, PR-Zuordnung via `git log -- file`) ist solide dokumentiert (Report-Sektion 3.2b, `cr-findings-todo.md`).
- **Alle 12 Fix-Agents haben gepusht** — Stichprobe bestätigt Fix-Commits auf den Branches: #54 (`e6f46ba`), #67 (`b2ba9ab` + Folgefix `ac148d2`), #69 (`0868f0b`), #71 (`af8ec17`), #59 (`3a1a62a`), #49 (`6e90b92`). Keine False-Positive-Schlamperei (1 echter FP + 1 Out-of-Diff korrekt delegiert). Alle 26 PRs weiterhin MERGEABLE.
- **Gruppe B ist damit faktisch aufgewertet:** Die CodeRabbit-Abdeckung auf HEAD ist über den Batch-Umweg hergestellt. Verbleibende methodische Restlücke (fairerweise notiert, kein Blocker): Die Batch-Reviews liefen auf separaten Batch-Branches, und die Fix-Commits selbst haben kein CodeRabbit-Re-Review — sie sind klein, mit Regressionstests und verifiziert, aber die PR-Check-Spalte zeigt weiterhin „Review rate limited", was beim Mergen leicht missverstanden werden kann.

## Sonar-Befund (2026-09-04 nachmittags) — Rot ist OK, Begründung:

**Warum manche Gates grün, manche rot:** Das Gate bewertet nicht den PR-Diff, sondern das gesamte „New Code"-Fenster (`previous_version`, Main-Analyse stale seit Mai 2026). Jeder PR bekommt ein anderes Fenster → Lotterie, kein Qualitätsurteil:

- #50/#54/#57: `viewer/main.py`-Fenster mit 216 pre-existing Issues; #57 zusätzlich S8414-BLOCKER, der inhaltlich zu #46 gehört.
- #64: Duplikation 4,9 % > 3 % durch umbenannte (nicht neu erzeugte) Duplikate.
- #67: Taint-Regeln (S5131/S5145/S6549), die im Baseline-Scan noch gar nicht aktiv waren.
- #59: 16 pre-existing Issues (S1244/S8707) auf unveränderten Zeilen; #61: nur Baseline-S3776.
- **Lücke:** #68/#69/#71 dokumentieren im Body gar keine Gate-Analyse — dort ist nicht mal „0 Issues auf changed lines" belegt (#68 als Hochrisiko nachholen!).
- Gate-Flakiness empirisch belegt: #68 grün→rot, #62 rot→grün, ohne Code-Änderung. SonarCloud-Issues-API meldet für alle 10 roten PRs `total: 0` offene Issues.
- **Docs-Verifikation (ctx7, SonarQube-Cloud-Docs):** „Nur Diff prüfen" ist bereits das definierte Verhalten (*„for pull requests, only conditions on new code are applied; new code = changes relative to the target branch; new issues on old code are not reported"*). Es gibt **keinen Schalter** dafür — wenn Altlasten im PR-Fenster landen, ist das ein Erkennungsproblem (stale Target-Analyse, hier Main seit Mai 2026), kein Konfigurationsproblem. `sonar.pullrequest.base`-Settings greifen nur bei CI-Scans, nicht bei Automatic Analysis.
- **Korrektur zu meiner eigenen Einordnung:** #67s Security-Rating ist teilweise **legitim per Design** („Issue backdating": neue Regeln wie S5131/S5145/S6549 erzeugen neue Issues auf alten Zeilen, die im PR auftauchen dürfen). Nicht alles Rot ist Artefakt.

**LoC-Limit:** Repo ist private mit ~128k LoC (100k Python + 27k TS; größte Brocken: `tests/` 32k, `frontend/` 27k, `viewer/` 16k). Nachgerechnet: selbst mit Exclusions für Tests + Scripts + Strategies (~85k Rest) kommt man nicht unter die 50k-Free-Cap, ohne Produktionscode aus der Analyse zu werfen. Free Tier reicht strukturell nicht. **Entscheidung: kein Grün-Erzwingen** (Tage Aufwand für ein bereits manuell verifiziertes Signal). Stattdessen: Gruppe C per Hand freigeben (ein Satz pro PR: „Gate rot wegen X, changed lines clean"), Sonar-Zukunft (bezahlen / public / manuelles Gating) als separates Folgeticket nach den Merges.

**Lokaler SonarQube-Scan:** läuft (v26.8, Docker, `localhost:9000` UP). Daten in dessen DB + Snapshots (`reports/sonar-review-full-2408.json`, `~/tmp/claude-cleanup/logs/sonar-*.json`). Kein LoC-Limit.

**Nachtrag — Token-Verifikation (sonar-CLI, 2026-09-04):** Der Reviewer hatte den Token-Zugang zunächst nicht genutzt; mit Token direkt gegen die API verifiziert:
- #67-Check steht auf **CANCELLED** („last analysis has failed") — bestätigt, beim Sign-off nicht als Rot werten.
- #67-Fenster (428 Issues): **kein S2083** enthalten; BLOCKER sind S5131 (viewer/core.py) + S8414 (viewer/main.py), alle pre-existing (März 2026). Fazit „kein PR-Finding" bestätigt.
- **Korrektur zur „0 Issues"-Tabelle:** Eigener Changed-Lines-Abgleich (Diff-Added-Lines vs. Issues-API) findet sehr wohl Treffer — ausschließlich Minor: #68: 5 (S3776 Complexity 16/15 in ctrader_adapter.py:1302, S1172+S7503 in live_smoke.py:867, 2× S5778 in Tests); #69: 2 (S2933 readonly ErrorBoundary.tsx:19, S6594 RegExp.exec LiveTerminal.tsx:81 — **kein S6959** im Fenster, die Tabellenzeile ist so nicht reproduzierbar); #71: 2 (S1172 unused channel_id discord_source.py:573, S9073 in Tests). Keine davon Rating-relevant → Sign-off-Begründung sollte „keine Rating-relevanten Issues auf changed lines" lauten statt „0 Issues"; die 9 Trivial-Findings idealerweise fixen (Minutenaufwand) oder explizit akzeptieren.

**Alle ~1190 Findings fixen? Nein:** (1) Großteil Duplikate (225× S3776, 254× S8415/S8409) — Wochen Arbeit für Style-Hygiene mit Regressionsrisiko. (2) Die Triage pro Regel (fix/skip) existiert bereits (`reports/cleanup-rule-decisions-draft.md`). (3) Der Rest ist als Backlog erfasst (Security-Taint, S3776-Rest, #62-Findings). Nach den Merges: lokaler Post-Merge-Rescan von Main als Hygieneschritt, kein Blocker.

## Was NICHT merge-ready ist (konkret):

- [ ] **Gruppe C (50, 54, 57, 59, 61, 64, 67, 68, 69, 71):** Hand-Sign-off pro PR fehlt (Sonar-Rot per obiger Begründung akzeptieren, nicht wegfixen).
- [ ] **#68/#69/#71:** Changed-Lines-Abgleich fehlt im Body — lokal nachholen (#68 zuerst).
- [ ] **#61:** Golden-Master-Diff (+9408 → −3679) human-reviewen.
- [ ] **#62:** Follow-up-Tickets für die 10 Deep-Review-Findings.
- [ ] **Docs (je 5 Min):** #56 fehlt weiter in der HTML-Merge-Order (einordnen: nach #62, vor #65); #66-Body („landed on main") immer noch irreführend — vor #66-Merge fixen + Rebase nach echtem #65-Merge re-verifizieren.
- [ ] **Merge-Prozedur:** strikt in Sequenz (#48→#70→#68→#72→#71; #62→#56→#65→#66; #63→#67 zuletzt), nach jedem Merge Downstream rebasen.

Offen / nicht umgesetzt (bitte noch nachholen):

1. **Gruppe C per Hand freigeben** (s. Sonar-Befund oben) — statt Main-Reanalyse, die durch das LoC-Limit blockiert ist. Das UNSTABLE-Set ist unverändert dieselben 10 PRs.
2. **#56 fehlt weiterhin in der HTML-Merge-Order** — die Order listet 25 PRs (55, 53, 47, 49, 51, 52, 54, 50, 69, 58, 60, 59, 48, 70, 68, 72, 71, 62, 65, 66, 61, 57, 64, 63, 67). Im Run-State als „akzeptiert" vermerkt, aber im HTML nicht eingepflegt. Einordnung: nach #62, vor #65.
3. **#66-Body immer noch irreführend** („16-D1 is landed on main", „since #65 was merged on top") — ebenfalls nur als „ausstehend" gelistet, nicht korrigiert. Vor dem Merge von #66 Formulierung fixen + Rebase nach echtem #65-Merge re-verifizieren.
4. Kosmetik: `cr-findings-todo.md`-Checkboxen sind alle noch offen, obwohl die Fixes gepusht sind.

**Aktualisierte Kurzfassung:** Gruppe A (5 PRs) ready. Gruppe B (11 PRs) faktisch ready (CodeRabbit via Batches + Fixes). Gruppe C (10 PRs) per Hand-Sign-off freigeben — Sonar-Rot ist begründet OK, kein Blocker. Rest: #68-Abgleich, #61-Review, #62-Tickets, 2 Doc-Fixes (5 Min), dann sequenziell mergen.

**Vorgehen:** Prompt (`claude-prompt.md`), Run-State (195 Zeilen, 26/26 als `ready-for-human` deklariert), HTML-Abschlussreport gelesen; dazu unabhängig verifiziert: `gh pr checks` für alle 26 PRs, SonarCloud-Issue-API, CodeRabbit-Kommentare, File-Overlaps, Stichproben-Diffs (#72 voll, #48/#61/#66/#68 Bodies + #72-Diff-Hunks).

## Vorab: 3 übergreifende Befunde, die alle Verdicts relativieren

1. **CodeRabbit hat bei 20 von 26 PRs den HEAD nie reviewed.** `gh pr checks` meldet nur bei **51, 53, 55, 56, 64, 72** `Review completed`, bei allen anderen `Review rate limited` (Free-Tier-Limit). Der Status-Check steht trotzdem auf SUCCESS — das ist irreführend. Die im Run-State behaupteten „CodeRabbit 0 final" beziehen sich auf Zwischen-Commits, nicht auf den HEAD. **Vor jedem Merge dieser 20 PRs braucht es einen CodeRabbit-Run auf dem HEAD** (~7h bei 3/h, ggf. gestaffelt nach Risiko).
2. **Sonar-FAIL ist plausibel ein Artefakt, aber nicht von mir verifizierbar.** 10 PRs sind UNSTABLE (50, 54, 57, 59, 61, 64, 67, 68, 69, 71). Die SonarCloud-Issue-API liefert für diese PRs `total: 0` offene Issues → der Fail kommt aus Quality-Gate-Metriken (Coverage/Duplikation/New-Code-Fenster), was zur dokumentierten Stale-Main-Analyse (seit 2026-05-04) passt. Beleg für die Instabilität: #68 war laut Report grün, ist jetzt rot; #62 war rot, ist jetzt grün — ohne Code-Änderung. Trotzdem: ohne Token konnte ich die QG-Conditions nicht einsehen, „0 Issues auf changed lines" ist nur Agent-behauptet.
3. **Es gibt keine pytest-CI** (kein `.github/workflows/`). Alle „x Tests passed"-Angaben sind Agent-Self-Reports. Und: **massive File-Overlaps** (`daemon.py` ×6, `engine.py` ×6, `pynecore_adapter.py` ×7, `vectorbt_adapter.py` ×6, `viewer/main.py` ×4) — alle PRs sind gegen dieselbe Baseline `6caf461` geschnitten. Sobald der erste PR einer Kette mergt, sind die nachfolgenden konfliktbehaftet und brauchen Rebase + Re-Review. „Ready for merge" gilt daher immer nur **in Sequenz**, nie parallel.

## Verdicts

**Gruppe A — Ready (Sonar PASS + CodeRabbit completed auf HEAD):** nur 4 PRs ohne Einschränkung: **#51, #53, #55, #72**. (#56 und #64 haben `completed`, aber #64 Sonar-FAIL → Gruppe C, #56 → Gruppe A mit Stern, s.u.)

| PR | Titel | Checks | Empfehlung |
|---|---|---|---|
| #53 17-A | Test-Isolation | CLEAN, CR completed | ✅ **Ready.** Klein, test-only, gutes Fundament — als erstes mergen. |
| #55 17-B | Vacuous passes / Parity-Fixture | CLEAN, CR completed | ✅ **Ready.** CRITICAL-Fix (exec im Validator) gehört früh auf main. |
| #51 20-D | Scheduler truthfulness | CLEAN, CR completed | ✅ **Ready.** Mittleres Risiko (DATA_DIR-Resolver), aber vollständig reviewed. |
| #72 15-D | Broker-Adapter (Volumen/Persistenz) | CLEAN, CR completed | ✅ **Ready mit Sequenz-Vorbehalt.** Diff-Stichprobe ist qualitativ hochwertig (Unit-Contract, fail-closed). Nur nach #48 → #70 → #68 mergen (Body flaggt SL/TP-Persistenz-Abhängigkeit zu #48 selbst). |
| #56 16-B | Engine/Grid-Fixes | CLEAN, CR completed | ✅ **Ready, aber:** fehlt in der Merge-Order des Reports (Lücke, s.u.). Inhaltlich groß (40 Commits), vollständig reviewed. |

**Gruppe B — Bedingt ready (Sonar PASS, aber HEAD ohne CodeRabbit-Review):** #47, #48, #49, #52, #58, #60, #62, #63, #65, #66, #70.

| PR | Titel | Empfehlung |
|---|---|---|
| #47 20-B | Per-bot processors | ✅ Bedingt ready. Klein, isoliert. CodeRabbit auf HEAD nachholen (niedrig prior). |
| #52 18-D | Sentry mini-PR | ✅ Bedingt ready. Isoliert, 10 Files. |
| #58 19-A | Atomic writes | ✅ Bedingt ready. Basis für #60 — davor mergen. |
| #60 19-B | Downloader correctness | ✅ Bedingt ready. Saubere Spec-Story (HTTPS-only, Digest fail-closed). |
| #70 15-B | Credential mapping | ✅ Bedingt ready, **Sequenz #48 → #70 beachten** (live money path, Refusals statt Fallback = Verhaltensänderung im Deployment). |
| #48 15-A | PAUSE/SL-TP fail-closed | ⚠️ **Nur nach CodeRabbit-HEAD-Run mergen.** Hochrisiko (Live-Order-Semantik: relative SL/TP, Alpaca-Bracket). Body-Doku exzellent, 18 Commits/5 Zyklen — aber genau deshalb muss der HEAD reviewed sein. Erster der 15er-Kette. |
| #62 16-A | Cross-engine contract | ⚠️ **Nur mit Follow-up-Tickets mergen.** Die 10 post-ready Deep-Review-Findings (F8-Parity-Widerspruch `modeling_resolution="1-lower"`, pynecore date-only `--to`, Conformance-Test-Blind-Spot) sind substanziell und nicht im PR. Als Test-Backbone für #65/#66 trotzdem früh mergen, Findings als Tickets. |
| #65 16-D1 | Money-path fixes | ⚠️ **Nur nach #62 + CodeRabbit-HEAD-Run.** Hochrisiko (Backtest-Ergebnisse ändern sich), aber Mutation-getestet (31/13→19 Tests). |
| #66 16-D2 | Structural consolidation | ⚠️ **Nur direkt nach #65.** Body behauptet fälschlich, #65 sei „landed on main" — Rebase-Lage nach #65-Merge unbedingt re-verifizieren (engine_pkg-Split). Ansonsten sauberer Refactor mit Merge-Rehearsal. |
| #63 14-A | TS-Batch (47 Files) | ✅ Bedingt ready, mechanisch, spät mergen (nach Frontend-PRs). |
| #49 20-C | Destructive scripts | ✅ Bedingt ready. Scripts-only, kein Cross-Dependency. |

**Gruppe C — Sonar FAIL, per Hand-Sign-off freigeben (s. Sonar-Befund oben, kein Grün-Erzwingen):** #50, #54, #57, #59, #61, #64, #67, #68, #69, #71. CodeRabbit-Abdeckung ist via Batches hergestellt; nach dem Sign-off → Merge in Sequenz. PR-spezifisch:

| PR | Zusatz zum Standard-Vorgehen |
|---|---|
| #68 20-A | Hochrisiko (trusted qty-channel ändert Adapter-Interface). War laut Report grün, jetzt rot → Paradebeispiel für Gate-Flakiness. Inhaltlich stark (23 Mutationen killed, Bypass entfernt). Nach Klärung vor #72 mergen. |
| #69 18-B | Frontend, baut auf #50/#54 — erst nach denen. |
| #71 15-C | Letzte der 15er-Kette; B-4-Teil bewusst Follow-up (Schema-Change) — ok, aber dokumentiert lassen. |
| #61 strategies | 🔴 **Zwingend Human-Review des Golden-Master-Diffs vor Merge:** QG-Strategie 62 Trades/+9408 → 42 Trades/−3679. Richtung plausibel (Fade→Breakout), aber Handelsergebnisse ändern sich real. |
| #50 18-A | Großlöschung (−1883 Zeilen Flask-Layer). Früh mergen (nach Klärung), damit Folge-PRs saubere Basis haben. |
| #54, #57, #59 | Mittleres Risiko, keine Zusatzflags über Gate + Rate-Limit hinaus. |
| #64 16-E | Einziger Sonar-FAIL mit `Review completed`. Rename + Alias, nach #65 mergen (F7-Divergenz). |
| #67 14-B | 57 Files (+4771/−3213), größter Diff, touched fast alles → **als allerletzter mergen.** |

## Merge-Order

Die Report-Reihenfolge (Tests → isoliert → Ketten in Blocker-Reihenfolge → Batches zuletzt) ist richtig und wird bestätigt — **mit zwei Korrekturen**: (1) **#56 fehlt im Report** — einordnen nach #62, vor #65 (16-Kette); (2) bedingte PRs erst nach ihren Freigaben (Sonar-Reanalyse, CodeRabbit-HEAD). Praktisch also: #53 → #55 → #47 → #49 → #51 → #52 → #62 → #56 → #65 → #66 → … (Rest per Report), #48 → #70 → #68 → #72 → #71 strikt in dieser Folge, #63 → #67 ganz zuletzt. Nach jedem Merge: Downstream-PRs rebasen, Checks neu abwarten.

## Pre-Merge-Checkliste (zwingend)

1. (Ersetzt durch Sonar-Befund: kein Grün-Erzwingen — Gruppe C per Hand-Sign-off, Sonar-Zukunft als Folgeticket.)
2. (Erledigt via Batch-Reviews + Fix-Agents — verifiziert.)
3. Human-Sign-off: #61 (Golden-Master), #62-Follow-up-Tickets anlegen, #68-Changed-Lines-Abgleich (lokal), Money-Path (#48/#68/#70/#72) + betroffene Tests lokal laufen lassen (keine pytest-CI vorhanden).
4. Sequenziell mergen + rebasen, nicht parallel.

**Kurzfassung:** 4 PRs sofort ready (#53, #55, #51, #72 in Sequenz), 11 bedingt ready nach je einem CodeRabbit-HEAD-Run, 10 erst nach Sonar-Reanalyse von main. Nichts davon ist ohne die drei Vorbehalte (Rate-Limit-Lücke, Gate-Artefakt, Rebase-Ketten) mergbar — die `ready-for-human`-Labels bedeuten „Pipeline durchlaufen", nicht „unabhängig verifiziert".
