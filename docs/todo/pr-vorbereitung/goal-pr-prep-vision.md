# PR #6-#13 Vorbereitung (FORTSETZUNG, Self-Verify Mode)

> **Vollständige Referenz**: `/home/ubuntu/tmp/goal-pr-prep-vision-full.md`
> Neue Session — **verifiziere jeden Gate selbst**, nicht die Vor-Session.

## 🔧 Umgebung
- Repo: `/home/ubuntu/Desktop/trading-platform` (Branch `codex/new-main`, HEAD `fccdd52`)
- Worktrees (sauber+sync selbst checken!):
  - PR7: `/home/ubuntu/worktrees/qc-risk-parity-real-reference` → Ready ✅ (nicht anfassen)
  - PR8: `/home/ubuntu/worktrees/intraday-signal-contract` → Ready ✅ (nicht anfassen)
  - PR9: `/home/ubuntu/worktrees/optimization-split-boundary`
  - PR10: `/home/ubuntu/worktrees/optimization-oos-promotion`
  - PR11: `/home/ubuntu/worktrees/portfolio-optimizer-oos`
  - PR13: `/home/ubuntu/worktrees/downloader-coalescing` (Artifacts SHA `6482205dec...` **nicht berühren!**)
  - Live-integration: `/home/ubuntu/worktrees/live-integration` → **NIE** reset/stash/rebase/merge

## 🎯 Selbst-Check vor allem
Für jeden PR: Existiert er wirklich? (PR-Nummer, Branch, SHA) Draft/Ready? Pushed (Local=Origin)? Gates wirklich grün? → **Messen**, nicht glauben.

## 1. PR #10 — Video-Verifikation & warten
- Branch: `codex/optimization-oos-promotion`
- Video: `/home/ubuntu/trading-platform-artifacts/p1-opt-promotion-final/optimization-oos-promotion-final.mp4` (SHA `6942891da483...ec8a699`)
- **Self-verifiziere**: ESLint clean, SonarCloud 0 annot, CodeRabbit review_completed + 0 findings, tsc clean, frontend tests pass
- **Self-verifiziere**: Video ist wirklich in der PR (checke PR-Files/Video-Reference-URL)
- **CodeRabbit**: Triggern wenn nötig → `review_completed` + `findings:0` (via `gh api .../pulls/10/comments` checken)
- **Nichts am Code ändern!** PR10 ist technisch komplett.
- **`gh pr ready 10` nur wenn**: Fred `video_ready` bestätigt — sonst bleibt Draft
- CodeRabbit Review limit ist 3/h → bei Limit **`sleep`** bis Limit vorbei

## 2. PR #13 — CodeRabbit Review & ready
- Branch: `codex/downloader-coalescing`
- **Self-verifiziere**: pytest 108 pass, ruff 0 errors (PR-diff), SonarCloud 0 annot, diff clean, worktree sync
- `@coderabbitai review` triggern → bei rate-limit **`sleep`** (nicht warten, parallel andere Tasks) → retry
- **Wenn** CodeRabbit `review_completed` + 0 findings → `gh pr ready 13`
- Artifacts **nicht** berühren (SHA `6482205dec...` unverändert)

## 3. PR #9 — Final-Confirm
- Branch: `codex/optimization-split-boundary`
- Verifiziere `gh pr ready 9` wurde ausgeführt → draft=false? Wenn nein → gates checken → `gh pr ready 9`

## 4. PR #11 — Final-Confirm
- Branch: `codex/portfolio-optimizer-oos`
- Verifiziere draft=false, gepushed, Gates grün. Nichts tun.

## 5. PR #6 — Read-Only
- Branch: `/home/ubuntu/worktrees/live-integration`
- **Nur lesend** — SonarCloud FAILED (Security E + Reliability D), 30 annotations, CodeRabbit draft-skipped
- 165 tests pass (self-verifizieren)
- SonarCloud-API via Token versuchen, bei 403 `gh api .../annotations` nutzen
- **KEINE Code-Änderungen**

## 🔐 Sicherheit (siehe full für alle 10 Regeln)
1. Kein Commit von Secrets · 2. Kein force-push · 3. **KEIN AUTO-MERGE** · 4. Zuerst verifizieren · 5. Live-integration Worktree niemals reset · 6. Security-Classifier nicht umgehen · 7. SonarCloud 403 → `gh api` annotations · 8. PR13 artifacts sichern · 9. PR12 superseded · 10. CodeRabbit rate-limit → `sleep` + retry

## ✅ Mandatory Gates (nur Draft entfernen wenn ALLE selbst verifiziert)
pytest grün · ruff clean · SonarCloud SUCCESS+0 annot · GitHub Annotations 0 · CodeRabbit review_completed+0 findings · git diff --check sauber · Worktree clean (Local=Origin=HEAD)

## 📋 Reihenfolge: Scan → PR10(v) → PR13(CR-sleep) → PR9-confirm → PR11-confirm → PR6(read-only) → Status-Dateien updaten
