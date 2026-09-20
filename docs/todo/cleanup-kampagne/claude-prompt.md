Bitte orchestriere die Umsetzung des Cleanup-Backlogs. Deine Aufgabe ist es, die 26 aktiven Sub-Issues inklusive PR-Pipeline zu implementieren, und anschließend einen HTML-Report zur Übersicht zu erstellen. Du läufst auf glm-5.3-flash, einem vision-fähigen Model. Nutze Claude Code Workflows.

# Ausgangslage:
- 7 Issues, mit insgesamt 26 Subissues
- Decision Records in Subissue-Body oder als Kommentar im Parent-Issue
- in Subissues steht teilweise Blocker-Reihenfolge, die beachten

# Vorgehensweise:
- Nutze Claude Code Workflows zur Implementierung, inklusive paralleler Subagents wo sinnvoll
- Erstelle in ```~/tmp/cleanup-run-state.md``` Checkliste aller 26 PRs in folgendem Format:
```markdown
| Sub-Issue | PR | Implementiert | Pipeline | Ready | Zyklen |
|---|---|---|---|---|---|
| #21 15-A | cleanup/15-a | [x] | [x] | [x] | 2 |
```
- Erstelle für jedes Subissue einen eigenen Worktree mit eigener Branch
- Blocked Sub-Issues erst angehen, wenn Blocker das Label ```ready-for-human``` hat
- Jeder Subagent, der implementiert soll dabei nach dem Implement-Skill von matt pocock (in claude skills folder) vorgehen
- Danach soll er per gh cli die PR eröffnen:
        - Titel ```Cleanup <id>: <Kurzbeschreibung>```
        - Body verlinkt Subissue und alles weitere relevante
- Review-Pipeline runnen (siehe unten)
- ```ready-for-human``` setzen wenn komplette Review-Pipeline grün ist, aber nicht selber mergen

# Review-Pipeline:
Alle Schritte nacheinander durchgehen. Wenn ein Schritt failed, nicht weitermachen, sondern Findings beheben und komplette Pipeline von neu starten.
1. Sonar-Findings aus PR nehmen (bei PR läuft automatisch SonarQube, schreibt Findings als Kommentar in PR).
2. mutation-testing (Skill in ~/.claude/skills; nur für #21 (15-A), #22 (15-B), #24 (15-D), #25
  (20-A), #39 (16-D1); alle anderen PRs überspringen die Stufe.) Scope sind in PR geänderte Files, ohne Rückfragen arbeiten. Survived Mutation = fehlender Test: in PR ergänzen, bis Mutations killed. Läuft nur in erstem Review-Zyklus, in darauffolgenden nur Kill der Mutation durch Tests verifizieren.
3. pre-mortem (Skill in ~/.claude/skills), Scope nur über geänderte Files. Hardening-Vorschläge ohne Scope-Erweiterung in PR umsetzen, alles Weitere im PR-Body notieren.
4. code-review (per matt pocock skill). Wenn sinnvoll, Standards- und Spec-Achse über parallele Subagents. fixed Point ist merge-base mit ```main```.
5. CodeRabbit-CLI (Free Tier: max 3 Reviews/h, max 150 Files/Review => CodeRabbit-Runs danach orchestraten). Nur wenn alle Schritte davor grün, um keine unnötigen Uses zu verbrauchen. Info/Trivial Findings sind erlaubt. Versuch, einen Run während Rate-Limit zu starten, zeigt Restzeit bis zum nächsten verfügbaren Run an.

Fix-Schleife endet erst, wenn alle Stufen ohne Findings bleiben. Die auslassen-Regeln aus #14 gelten nur für den Bestands-Backlog — auf neuem Code in PRs werden auch diese Regeln clean fixen. Die Stufen-Reihenfolge bleibt fest. Bei 5 Durchläufen ohne Erfolg => Label ```blocked```, Eintrag in Checkpoint-Datei, andere Subissues weitermachen

# Resource-Guard:
- bei Ausführen von Tests etc. darauf achten, dass global States unabhängig von Worktrees (z.B. DATA_DIR, SQLite) kollidieren können, und sich real schreibende Tests gegenseitig killen.

# Abschluss-Report (wenn alle PRs grün):
- außerhalb vom Repo (am besten unter ~/tmp/cleanup-report-<YYYY-MM-DD>.html) eine Übersichtsdatei erstellen:
        - Übersichtstabelle über alle 26 PRs (ID, Link, Status, eventuelle Blocker, usw.)
        - Merge-Order als Liste mit Begründung (Blocker-Chain + Risk)
        - offene Punkte (blocked PRs mit Grund, Diskrepanzen zwischen Decision-Record und Umsetzung mit Grund, usw.)
        - übersichtliches Layout inklusive Farbstatus usw.
- danach self-check der HTML-Datei mit Screenshot und Vision (bzw. wenn Vision nicht verfügbar dann Text-Inspection der HTML)
