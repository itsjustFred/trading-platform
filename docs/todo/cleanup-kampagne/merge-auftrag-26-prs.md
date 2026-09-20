# Auftrag: 26 Cleanup-PRs sequenziell mergen

Repo: `itsjustFred/trading-platform`, Checkout: `/home/ubuntu/Desktop/trading-platform` (Branch `main` @ `6caf461`, sauber halten).
Auth: `gh` + `sonar`-CLI sind authentifiziert. Main ist **nicht** branch-protected — umso sorgfältiger arbeiten.

## 1. Sequenz (strikt einhalten, strikt nacheinander, NIEMALS parallel mergen)

```
#53 → #55 → #47 → #49 → #51 → #52 → #62 → #56 → #65 → #66 → #61 → #50 → #54 → #69 →
#58 → #60 → #59 → #48 → #70 → #68 → #72 → #71 → #57 → #64 → #63 → #67
```

Ketten mit echten Interface-Abhängigkeiten (Reihenfolge ist Pflicht, nicht Empfehlung):
15er: #48 → #70 → #68 → #72 → #71 · 16er: #62 → #56 → #65 → #66 · 19er: #58 → #60 → #59 · Batches #63 → #67 zuletzt.

## 2. Prozedur pro PR (jeden PR exakt so)

1. **Rebase:** PR-Branch auf aktuellen `origin/main` rebasen (`git fetch origin`, Worktree anlegen falls nötig, `git rebase origin/main`), danach force-push auf den PR-Branch (`git push --force-with-lease` — nur auf Cleanup-Branches, NIE auf main).
2. **Checks abwarten:** `gh pr checks <N>` bis SonarCloud + CodeRabbit-App-Check final sind (Poll alle ~60s, max. 15 Min). Hinweis: Die CodeRabbit-*Spalte* ist der GitHub-App-Check (läuft bei jedem Push automatisch bzw. zeigt „rate limited") — die *inhaltlichen* Reviews liefen per CLI in Batches (dokumentiert, Report 3.2b). Bei Bedarf (`gh pr checks` zeigt echte neue Findings statt Rate-Limit) zusätzlich `coderabbit review --agent --base main` im PR-Worktree laufen lassen.
3. **Gate-Interpretation (Regel, keine Ausnahme):**
   - Sonar-Rot ist erwartet (stale-Main-Fenster). Per `sonar`-CLI Changed-Lines-Abgleich: PR-Diff (`gh pr diff <N>`, Added-Lines) vs. `api/issues/search?...&pullRequest=<N>&resolved=false`. **Weiter nur wenn:** keine NEUEN Rating-relevanten Issues (Reliability/Security) auf geänderten Zeilen. Reine Pre-existing-Funde + akzeptierte Minors = OK (Kontext: Review-Datei `~/tmp/muse-review-claude-cleanup.md`, Abschnitt Sonar-Befund).
   - **STOPP** bei: neuen BLOCKER/CRITICAL auf geänderten Zeilen, neuen Security-Findings, Duplikations-Sprung durch den Rebase selbst.
   - `#67`: Sonar-Status CANCELLED ist bekannt und OK (nicht als Rot werten).
   - CodeRabbit „rate limited" ist OK (Batch-Reviews + Fixes sind dokumentiert); echte neue Findings → fixen oder STOPP.
4. **Tests:** Money-Path-Auswahl MUSS grün sein (in der PR-Worktree, mit `DATA_DIR=<worktree>/.test-data`, unter `flock /tmp/cleanup-pytest.lock`):
   `tests/test_live_execution_safety.py tests/test_live_daemon.py tests/test_risk_engine.py tests/test_live_risk_sizing.py tests/validation/test_risk_sizing.py tests/test_risk_consistency.py tests/test_signal_bots_risk.py` (+ `tests/live_runner` falls im Branch vorhanden).
   Bei Unsicherheit: volle Suite ohne e2e/integration (`--ignore=tests/e2e --ignore=tests/integration`); Ergebnis mit Baseline `6caf461` vergleichen — nur identische Pre-existing-Fails sind OK.
5. **Merge:** `gh pr merge <N> --squash` (Squash! Main-Log lesbar halten; Default-Message mit PR-Nummer behalten). PR-Branch danach NICHT löschen (Cleanup am Ende gesammelt).
6. **Danach:** `git checkout main && git pull`, nächsten PR der Sequenz mit Schritt 1 beginnen. Nach jeder Kette (Tests/Frontend/19er/15er/16er/Batches) Zwischenbericht an den Human (siehe 4).

## 3. STOPP-Regeln (sofort anhalten + Human holen, kein eigenmächtiges Weiter)

- Rebase-Konflikt in `live_runner/`, `risk/`, `runner/engines/`, `runner/engine_pkg/` → Diff zeigen, Human entscheidet. Konflikte anderswo (Tests, Docs, Frontend) selbst auflösen, im Bericht vermerken.
- Test-Failures, die NICHT Byte-identisch zur Baseline sind.
- Neue Rating-relevante Sonar-Issues (s. Gate-Regel oben).
- `gh pr merge` meldet Blockaden, Checks laufen > 15 Min nicht durch, oder irgendetwas weicht von dieser Prozedur ab.

## 4. Berichte

- Pro Merge: eine Zeile (PR, Merge-Commit, Checks, Tests, Konflikte ja/nein).
- Pro Kette: Zwischenbericht + explizites Go des Humans für die nächste Kette abwarten (gilt ab der 15er-Kette; die ersten beiden Ketten dürfen ohne Go durchlaufen, sofern keine STOPP-Regel griff).
- Am Ende: `git worktree list` + offene Branches melden (Cleanup comes later, nichts selbst löschen).

## 5. VERBOTEN

- `gh pr merge --auto`, parallele Merges, `--force` auf main, `git push --force` irgendwo außer Cleanup-Branches.
- Sonar-Issues umstellen/resolven (die 9 Accepts sind gesetzt; neue Findings NIEMALS selbst accepten).
- Worktrees/ Branches löschen, Main-Reset/Rebase auf main, zusätzliche Code-Änderungen über Konflikt-Resolution hinaus.
- Abkürzungen bei Schritt 3+4 („wird schon grün sein") — jeder PR bekommt den vollen Durchlauf.
