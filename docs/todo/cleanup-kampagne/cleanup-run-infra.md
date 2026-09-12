# Cleanup-Run Infrastruktur (verifiziert 2026-08-31)

## Sonar
- **SonarCloud (autoritativ für PRs, per User-Entscheidung 2026-08-31):** Projekt `itsjustFred_trading-platform`, Org `itsjustfred`. Analyse läuft automatisch auf der PR. Issues-Abfrage via authentifizierte `sonar` CLI: `sonar api get "/api/issues/search?componentKeys=itsjustFred_trading-platform&pullRequest=<PR>&resolved=false&ps=500"`. KEINE lokalen Scanner-Token nutzen.
- Lokale SonarQube (localhost:9000, `trading-platform-local`) existiert, wird für diese Kampagne NICHT verwendet (Quelle des Baseline-Snapshots `reports/sonar-review-full-2408.json`).
- Batch-Gate #45/#46: alle Scope-Regeln müssen im SonarCloud-Scan auf 0 fallen. File-Limit 150/Review.

## Reviews
- CodeRabbit CLI authentifiziert (itsjustFred). Free Tier: 3 Reviews/h, 150 Files. `coderabbit review --agent --base main` im Worktree.
- Rate-Limit-Fehler zeigt Restzeit bis zum nächsten Run an — dann orchestrieren (warten, andere PRs weitermachen).

## Skills (nicht interaktiv nutzbar → als Prompt-Anweisung mit vorgegebenen Pfaden)
- Implement: `~/.claude/plugins/cache/claude-plugins-official/mattpocock-skills/1.2.3/skills/engineering/implement/SKILL.md` (TDD an Seams, typecheck, Full-Suite einmal, /code-review, commit)
- TDD: `.../tdd/SKILL.md` | Code-Review: `.../code-review/SKILL.md` (Standards+Spec, Fixed Point = merge-base mit main)
- Mutation: `~/.claude/skills/honnibal-mutation-testing/SKILL.md` (nur #21,#22,#24,#25,#39)
- Pre-Mortem: `~/.claude/skills/honnibal-pre-mortem/SKILL.md` (3–5 Post-Mortems, Output ~/tmp/pre-mortem-<id>.md)

## Tests / Resource-Guard
- pytest: 1306 Tests, pytest.ini (testpaths=tests, timeout 180). Max 1–2 Agents gleichzeitig mit Test-Runs.
- Jeder Test-Running Agent: `DATA_DIR=<worktree>/.test-data` setzen. Frontend: kein jest; `tsc -b` als Gate (npm install in Worktree nötig — node_modules per Symlink oder reinstall).
- 4 CPUs → Workflow-Cap ~10 Agents, aber Test-Last manuell drosseln.

## GitHub
- Repo `itsjustFred/trading-platform`, gh auth ok. Labels vorhanden: cleanup, sub-issue, blocked, ready-for-human, needs-decision.
- PR-Label bei grüner Pipeline: `ready-for-human` (nicht mergen!). Bei 5 Zyklen ohne Erfolg: `blocked` + Checkpoint.

## Wellen
- W0: #21 #26 #27 #28 #29 #31 #32 #33 #34 #37 #38 #42 #43 #44 #45 #46
- W1: #22 #25 #30 (←#21) | #35 #36 (←#34)
- W2: #23 #24 (←#22) | #39 #41 (←#36)
- W3: #40 (←#39)
- "Blocked by: X" heißt: erst wenn X PR das Label `ready-for-human` hat.
