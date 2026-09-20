# Reste der Cleanup-Kampagne (gemergte Cleanup-PRs)

- **Datum (UTC):** 2026-09-06 (notiert 2026-09-08)
- **Quelle:** `~/tmp/issue-verification-weg1.md` (Weg-1-Verifikation: 20 offene Per-PR-Issues vs. Main @ `5184b46`, 4 Subagents, read-only, ohne Testläufe)
- **Stand Main:** `5184b46` — alle Cleanup-PRs (#47–#72) gemergt; Spot-Checks unten am HEAD nachverifiziert
- **Zweck:** Sammelstelle für alles, was nach der Kampagne noch offen ist — Follow-up-Tickets, Doku-Hinweise, offene Prozesspunkte

## Verdict-Übersicht (aus der Verifikation)

18 von 20 Per-PR-Issues: ERLEDIGT. 2× TEILWEISE: #41 (16-E, PR #64) und #46 (14-B, PR #67). Korrektur: #36 (16-A) braucht entgegen einer Annahme KEIN Follow-up-Ticket — alle 10 vermuteten Follow-ups sind per `git log -S` im #62-Squash selbst gelandet (`runner/engines/contract.py`, `native_adapter.py`, `pynecore_adapter.py`, `tests/test_engine_contract.py`).

## Echte Reste (Follow-up-Tickets anlegen)

### 1. #41-Rest (16-E): deprecated Alias mit stillem Fallback — am HEAD bestätigt
- **Ort:** `runner/engine_pkg/trading_engine.py:70` (`float(self.config.max_position_pct or 0.05)`), stale F22-Kommentar `:72-75`
- **Was:** Native Engine liest weiter das deprecated Alias mit stillem 0.05-Fallback
- **Ticket-Vorschlag:** „16-E remainder: native engine still reads deprecated max_position_pct with silent 0.05 fallback"

### 2. #46-Reste (14-B), drei Stück
- **2a. Redundante `response_model=dict[str,Any]`** — am HEAD bestätigt: `viewer/main.py:7225` (`get_strategy`), `viewer/main.py:11247` (`invalidate_data_cache`). (`:12010` mit `SymbolsResponse` ist intentional.)
- **2b. 14 S3776-Score-≥40-Findings ohne dokumentierte Disposition** (strategy-excluded vs. Backlog) — PR-Stage-1-Report vs. Issue-Wording „all non-strategy ≥40". Gebündelt oder einzeln ticketen.
- **2c. Sonar-Gate auf gemergtem HEAD nie grün (`cancelled`)** — security-nahe Regeln (S5131/S5145/S6549/S2583) als Backlog im PR-Body; brauchen eigenen Slice + Re-Run auf Main-HEAD.

### 3. #68 Metadata-Bypass — Code gelandet, Tracking-Ticket fehlt
- **Hintergrund:** 15er-Kette (#48, #70, #68, #72, #71) hatte nie eigene Issues; Abnahme nur via Review-Datei + Sign-offs.
- **Code-Stand am HEAD verifiziert:** `risk/live.py:95-96` und `:151-152` strippen `explicit_qty`/`risk_sizing_mode` als untrusted Keys; Sizing läuft immer über die RiskEngine (`:141-146`). Der Bypass aus dem Rebase-Kompromiss ist damit raus.
- **Ticket-Vorschlag:** reines Tracking-Ticket („#68 metadata-bypass removal — verify + close"), kein Code mehr nötig.

## Hinweise ohne Ticket-Bedarf (bewusst out-of-scope, dokumentiert)

- #29: Auth/AuthZ + Loopback-Bind-Policy weiter offen (Decision Record, kein AC).
- #38: `POST /api/backtests/run` bypassed das Approval-Gate (im PR-Body als pre-existing/out-of-scope).
- #40: `docs/grid_tester.md` gelöscht, in `docs/backtest_grid.md` umgeschrieben, Code blieb — Doku beschreibt Zwei-Lagen-Layout ohne `grid_tester`-Seite (Kompromiss-Nebenwirkung).
- #42: per-source Gap-Override aus Issue-„Open (proposed)" bewusst nicht gebaut (keine Spec).
- #45: lange Page-Bodies als out-of-mechanical-scope im PR vermerkt.

## Offene Prozesspunkte (noch nicht ausgeführt)

1. Follow-up-Tickets anlegen (#41-Rest, #46-Reste gebündelt/einzeln, #68-Tracking).
2. Danach 20 Issues + bei Erledigung Epics #15–#20 schließen (einzig bereits geschlossen: #30). Issues wurden in der Verifikation bewusst NICHT geschlossen.
3. 5 ungeklärte Post-Merge-Test-Fails (Baseline-Vergleich auf `main-copy-pre-cleanup-merges` noch ausstehend).

### Nachtrag 2026-09-08 (Merge-Lauf): Die 5 Fails sind identifiziert

Die 5 ungeklärten Post-Merge-Test-Fails sind die gesamte Datei `tests/test_auto_agent_backtest_timeout.py`:

- `test_run_backtest_kills_hung_child_after_timeout`
- `test_run_backtest_returns_output_on_success`
- `test_run_backtest_raises_after_deadline_for_real_silent_subprocess`
- `test_run_backtest_raises_after_deadline_for_partial_line_child`
- `test_run_backtest_returns_completed_output_for_fast_real_subprocess`

**Ursache:** Namespace-Drift aus PR #66 (16-D2, engine/agent/grid consolidation) — die Tests mocken `runner.auto_agent._backtest_timeout` und `runner.auto_agent.subprocess`, aber die implementierende Streaming-Schleife lebt seit dem Split in `runner/agent_process.py` (`run_backtest(..., timeout_seconds=...)`); das Modul-Attribut `auto_agent._backtest_timeout` existiert nicht mehr, und `auto_agent.subprocess` ist verschwunden.

**Baseline-Vergleich:** verifiziert am Merge-Endstand Main `5184b46` — dieselben 5 Fails (`5 failed, 6 passed`); nicht durch einen der Merge-Läufe neu eingeführt, sondern seit #66 auf Main rot.

**Fix-Richtung (fürs Ticket):** Entweder die Tests auf den echten Seam (`agent_process.run_backtest` + `timeout_seconds`-Param) umstellen, oder einen `_backtest_timeout()`-Shim in `auto_agent` wieder anbieten. Ersteres ist der saubere Weg — die select-basierten Tests pinnen Verhalten, das der PR-`communicate()`-Ansatz (#57) ersetzt hat.
