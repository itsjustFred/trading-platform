# CommandCode /goal Prompt — PR #6–#13 Vorbereitung (VOLLSTÄNDIGE REFERENZ)

> **Kurzfassung:** `/home/ubuntu/tmp/goal-pr-prep-vision.md`
> **Verwende Kurzfassung mit:** `cmd --max-turns 500 /goal < "$(cat /home/ubuntu/tmp/goal-pr-prep-vision.md)"`

---

## 🔧 Umgebung

- **Repo**: `/home/ubuntu/Desktop/trading-platform` (Branch `codex/new-main`, HEAD `fccdd52`)
- **Worktrees** (alle sauber sync mit origin):
  - PR7: `/home/ubuntu/worktrees/qc-risk-parity-real-reference` → **Ready** ✅ (nicht erneut anfassen)
  - PR8: `/home/ubuntu/worktrees/intraday-signal-contract` → **Ready** ✅ (nicht erneut anfassen)
  - PR9: `/home/ubuntu/worktrees/optimization-split-boundary` (Branch `codex/optimization-split-boundary`, HEAD `16d9dfb`)
  - PR10: `/home/ubuntu/worktrees/optimization-oos-promotion` (Branch `codex/optimization-oos-promotion`, HEAD `d08acd0`)
  - PR11: `/home/ubuntu/worktrees/portfolio-optimizer-oos` (Branch `codex/portfolio-optimizer-oos`, HEAD `1e9c7dfa`)
  - PR13: `/home/ubuntu/worktrees/downloader-coalescing` (Branch `codex/downloader-coalescing`, HEAD `13cdd87`)
  - Live-integration: `/home/ubuntu/worktrees/live-integration` (HEAD `7984f7d`) — **NIE reset/stash/rebase/merge/force-push**
- **Model**: 70B-Modell mit Vision, Playwright und Video-Aufnahme aktiviert
- **Python**: `python -m pytest` und `python -m ruff` (kein `.venv` in Worktrees)
- **Frontend-Tests**: Müssen aus `/home/ubuntu/worktrees/optimization-oos-promotion/frontend` ausgeführt werden
- **Prep-Reports**: `/home/ubuntu/review-artifacts/pr{6,7,8,9,10,11,12,13}-prep/report.md`

## 🎯 Primäre Ziele & Reihenfolge

**Reihenfolge: #11, #10, #9, #13, #6** (PR7 & PR8 sind bereits Ready — **nicht anfassen**)

### 1. PR #11 — Float-Assertion-Fix in `tests/test_risk_engine.py` (BOUNDRED SLICE)

- **Scope**: Genau 5 direkte Fließkommavergleiche in `tests/test_risk_engine.py` (Zeilen 49-52, 124) durch `pytest.approx` ersetzen
- **Nur diese Datei editieren!** Nichts anderes an PR11 ändern — der Source-Code (`runner/portfolio_optimizer.py`) ist bereits bereinigt und gepusht (HEAD `1e9c7dfa`)
- Ruff-Befund in Test-Datei (I001 Import-Sortierung) ebenfalls fixen
- **Verifikation**: `cd /home/ubuntu/worktrees/portfolio-optimizer-oos && python -m pytest -q tests/test_portfolio_optimizer_oos.py tests/test_risk_engine.py && python -m ruff check runner/portfolio_optimizer.py tests/test_portfolio_optimizer_oos.py`
- Committen und pushen: `git add tests/test_risk_engine.py && git commit -m "Fix risk engine float assertions — use pytest.approx" && git push origin codex/portfolio-optimizer-oos`
- Danach: SonarCloud + GitHub Checks warten (sleep 120), CodeRabbit Review prüfen
  - **Wichtig**: `coderabbit review --committed` wurde in der vorherigen Session vom Security-Classifier blockiert (`[Data Exfiltration]`). Wenn es erneut blockiert wird, **nicht umgehen**. Stattdessen: den vorherigen erfolgreichen Review (`review_completed`, `findings:0`) als Beweis dokumentieren.
  - Wenn CodeRabbit-Blocker: PR #11 **bleibt Draft**. `gh pr ready 11` **nicht** ausführen.
  - Wenn CodeRabbit erfolgreich: `gh pr ready 11` ausführen.

### 2. PR #10 — ESLint-Fix + 5 pytest-Failures + Video-Verifikation

- **Arbeitsverzeichnis**: `/home/ubuntu/worktrees/optimization-oos-promotion`
- **Frontend**: `/home/ubuntu/worktrees/optimization-oos-promotion/frontend`
- **ESLint-Fix**: `react-hooks/set-state-in-effect` in `OptimizationResultsPage.tsx:61` beheben
- **5 pytest-Failures**: Sind PR11-Abhängigkeit (Float-Assertion-Fix) — nach PR11-Fix hier erneut laufen lassen
- **Video-Verifikation** (PR10-Video):
  - Video ist technisch gültig: H.264, 1280×720, 24fps, 22.33s, 156722 bytes, SHA `6942891da483e912c3feee0e0271b8404418358dd3289cfebb8e63b00ec8a699`
  - **Wartet auf Freds Sichtung** — nicht `video_ready`
  - Nutze **Vision** + Frame-Extraktion (`ffprobe`/Hilfsskript) zur inhaltlichen Prüfung
  - ZUERST verifizieren, DANN ablegen/verlinken — **nicht umgekehrt**
  - **Kein Upload ohne Verifikation**
  - Falls Fred noch nicht geprüft hat: PR #10 bleibt Draft, Video auf Warteliste setzen
- **Verifikation**: Frontend-Tests via Playwright (falls verfügbar) + pytest

### 3. PR #9 — Ruff + Sonar + CodeRabbit

- **Arbeitsverzeichnis**: `/home/ubuntu/worktrees/optimization-split-boundary`
- **Prep-Report**: `/home/ubuntu/review-artifacts/pr9-prep/report.md` als Startpunkt
- **Aktuelle Blocker**: Ruff 44 Findings, Sonar-Fix (Reliability C, 18 annotations), CodeRabbit-Findings
- Arbeite die Sonar-Annotations systematisch ab — fokussiere auf Reliability-C Probleme

### 4. PR #13 — Ruff-Bereinigung (hauptsächlich E501)

- **Arbeitsverzeichnis**: `/home/ubuntu/worktrees/downloader-coalescing`
- **Achtung**: Artifacts **nicht berühren** (10 Dateien, Snapshot SHA `6482205dec...` unverändert)
- **Aktuelle Blocker**: Ruff 220 Findings (hauptsächlich E501 Zeilenzu lang)
- **Verifikation**: pytest, Ruff, SonarCloud (falls Check existiert)

### 5. PR #6 — Ruff + Sonar + CodeRabbit + Video

- **Achtung**: PR6 ist der **live-integration** PR — nur lesend prüfen, **keine Code-Änderungen** am live-integration-Worktree
- **Aktuelle Blocker**: Ruff 77 Findings, Sonar (Reliability D, Security E, 50 annotations), CodeRabbit 25 Findings, Video fehlt
- **Live-integration Worktree**: **NIE** reset/stash/rebase/merge/force-push
- Focus auf: Sonar-Security-E Findings und Reliability-D Probleme
- Video-Verifikation: Wenn Video-Datei existiert, ffprobe + Vision-basierte Verifikation

## 🔒 Sicherheitsregeln (VERPFLICHTEND — nicht verletzen)

1. **Kein Commit von Secrets** — vor jedem Commit `.env` etc. prüfen
2. **Kein force-push** — nur normale pushes
3. **Keine Änderungen an geschützten Branches** ohne explizite Anweisung
4. **KEIN AUTO-MERGE** — nur Draft-Status entfernen via `gh pr ready`, wenn alle Gates Pass haben
5. **live-integration Worktree**: niemals reset/stash/rebase/merge/force-push
6. **ZUERST verifizieren, DANN ablegen/verlinken** — nicht umgekehrt
7. **Kein Upload ohne technische + inhaltliche Verifikation**
8. **Keine blinden Retry-Schleifen**, keine geheimen Änderungen
9. **`coderabbit review --committed`**: Wenn Security-Classifier blockiert → **nicht umgehen**, dokumentieren und weiter
10. **SonarCloud API**: `issues/search` und `issues/show` mit HTTP 403 → **nicht verwenden**, stattdessen `gh api .../check-runs/.../annotations` nutzen

## ✅ Mandatory Gates pro PR (nur Draft entfernen wenn ALLE erfüllt)

| Gate | Kriterium |
|------|-----------|
| pytest | grün — alle Tests passieren |
| Ruff | sauber — `All checks passed!` |
| mypy | Wenn verfügbar, keine kritischen Errors |
| SonarCloud | SUCCESS + annotations_count: 0 |
| GitHub Annotations | 0 |
| CodeRabbit | review_completed + findings:0 |
| git diff --check | sauber (keine Whitespace-Probleme) |
| Worktree | clean, Local=Origin=PR Head |

## 📸 Vision + Playwright + Video Workflow

### Video-Verifikation (PR10):
1. `ffprobe` zur technischen Validierung (Codec, Dauer, Auflösung, Bitrate)
2. Hilfsskript erstellen, das periodisch Frames extrahiert (alle 1-2 Sekunden)
3. Frames per Vision inspizieren — prüfe: Chart-Bewegung, Order-Einträge, Discord-Signale, Broker-Fenster, Performance-Metriken
4. Manifest + Contact-Sheet generieren
5. **Nur wenn technisch + inhaltlich verifiziert**: Video ablegen/verlinken
6. **Kein Upload ohne Freds Freigabe**

### Frontend-Tests (PR10):
1. `cd /home/ubuntu/worktrees/optimization-oos-promotion/frontend`
2. `npx playwright test` oder `npm test` (nicht aus Repo-Root!)
3. Bei Bedarf: Playwright-Screenshots für Debugging

## ⚠️ Bekannte Blocker & Constraints (nicht erneut versuchen)

1. `coderabbit review --committed` → Security-Classifier blockiert (`Data Exfiltration`). Nicht umgehen.
2. SonarCloud API `issues/search` → HTTP 403. `gh api .../annotations` nutzen stattdessen.
3. Frontend-Tests aus Repo-Root → immer aus `frontend/` Subdirectory ausführen.
4. PR10 Video via Vision → 9/12 Frames near-black, wartet auf Freds Sichtung.
5. PR #12 → als superseded betrachten (Recovery `56d5d44` bereits in Base integriert).
6. PR7 & PR8 → bereits Ready, nicht erneut anfassen.
7. live-integration Worktree → NIER reset/stash/rebase/merge/force-push.
8. Artefakte in PR13 → `/home/ubuntu/worktrees/downloader-coalescing/artifacts/` nicht berühren.

## 🔄 Was tun, wenn ein PR nicht fertig wird

1. Dokumentiere den **genauen Blocker** in der Status-Datei
2. Setze den PR auf **Draft** mit dokumentiertem Blocker
3. Fahre mit dem **nächsten PR** in der Reihenfolge fort

## 📊 Erwartetes Ergebnis

Am Ende dieser Session sollen:
- PR #11 Float-Assertion-Fix committed & gepusht sein, SonarCloud neu laufen
- PR #10 ESLint + pytest-Fixes behoben sein, Video-Verifikation dokumentiert
- PR #9 Sonar-Reliability-C Findings behoben sein
- PR #13 Ruff E501 Findings bereinigt sein
- PR #6 Sonar-Security + Reliability-D Findings analysiert und dokumentiert sein
- Alle Statusdateien aktualisiert sein
- **Kein PR automatisch gemergt** — nur Draft-Status entfernt, wo alle Gates Pass ist
