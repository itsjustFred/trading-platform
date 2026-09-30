# Live Integration Deploy Runbook

Status: credential-gated as of 2026-07-13.

## Preconditions

- `.accounts.env` exists and remains gitignored.
- Alpaca account blocks `ACC1..3` (Nitro, RamJet, RocketFuel) contain paper endpoints only; IDs werden aus `.accounts.env` aufgeloest, nicht hier dokumentiert.
- Discord bot tokens and channel IDs are set for Nitro, RamJet, and RocketFuel.
- cTrader credentials are demo credentials if the cTrader smoke is run.

## Provisioning and Rotation

Provision the deployment file from `live_deployments.example.yaml` into the
ignored `live_deployments.yaml`. Replace every environment placeholder with a
value obtained from the broker or Discord administration console; do not copy
account references, channel IDs, or author IDs into the example or any tracked
file.

Keep secrets and account references in the ignored `.accounts.env` and local
deployment file. When a broker account, API key, OAuth token, Discord channel,
or trusted author changes, provision the replacement through the provider,
update the local files, run the non-ordering preflight, and revoke the old
credential or access grant. Keep deployments paused until paper verification is
complete. Account references are routing metadata rather than secrets, but they
must still be rotated when an account is retired or reassigned.

## Non-Ordering Preflight

```bash
uv sync --all-extras
uv run python -m live_runner.preflight --config live_deployments.yaml --accounts-env .accounts.env
uv run python scripts/live_smoke.py --mode discord-replay --reports-dir docs/reports
```

## Paper Broker Smoke

For Alpaca smoke, first verify every Alpaca account resolves with `paper=True`.
For cTrader smoke, verify the configured cTrader credentials are demo credentials.

```bash
uv run python scripts/live_smoke.py --mode alpaca --accounts-env .accounts.env --reports-dir docs/reports
uv run python scripts/live_smoke.py --mode ctrader --accounts-env .accounts.env --reports-dir docs/reports
```

## Demo Daemon

Only run after the paper smoke succeeds.

```bash
set -a
. ./.accounts.env
set +a
set -o pipefail
uv run python -m live_runner.daemon 2>&1 | tee "docs/reports/live_daemon_$(date -u +%Y%m%dT%H%M%SZ).log"
```

> NOTE (Stand 2026-09): `daemon.main()` nimmt kein `--config` — Config kommt aus der DB via `shared/database.list_live_deployments`, nicht aus `live_deployments.yaml`.

Keep the daemon running long enough to verify Discord connection, paper order submission, fill handling, and `reconcile_executions`.

## Status- und Demo-Gates

- Das Persistieren von `ACTIVE` ist nur erlaubt, wenn das persistierte Konto
  explizit als Demo verifiziert wurde (`live_accounts.is_demo = true`).
  Migrationen setzen bestehende, unbekannte Konten fail-closed auf `false`.
- Eine Demo-Verifizierung ist mehr als das Flag: Sie braucht einen frischen Zeitstempel
  (`demo_verified_at`, TTL `DEMO_VERIFICATION_TTL_SECONDS`) **und** einen
  Identity-Fingerprint, der zur aktuellen Broker-Identität passt. Der Fingerprint wird
  bei der Verifizierung aus (`broker`, `ctid`, `broker_login`) gebildet und bei jedem
  Gate neu berechnet. Fehlt er, ist er nicht lesbar oder passt er nicht, wird verweigert.
- Die TTL gilt auch **während** des Betriebs: der Daemon prüft vor *jeder* Signal-Dispatch
  erneut das vollständige Verdikt (Flag + Alter + Identity). Eine einmal armierte
  Deployment kann ihre Verifizierung also nicht überleben.
- Wer die Broker-Identität eines Kontos ändert (`ensure_live_account` mit neuem
  `broker_login`/`ctid`/`broker`), verwirft die zugehörige Verifizierung. Ein
  umgepointertes Credential erbt damit keine alte Demo-Bestätigung; es muss neu
  verifiziert werden.
- `GET /api/live/accounts` persistiert die zuletzt erkannten Broker-Identitäten
  (`ctid`, `broker_login`, `is_demo`) aus der Discovery-Antwort — aber nur die
  Werte, die die Discovery **positiv** behauptet hat. Eine Antwort ohne `is_demo`
  (oder mit einem Nicht-Boolean) setzt das gespeicherte Flag nicht herunter: ein
  Read demotiert niemals ein verifiziertes Demokonto. Ohne Behauptung wird auch
  keine Verifizierung geschrieben; die bestehende bleibt bis zur nächsten
  Broker-Bestätigung stehen.
- Der Daemon verweigert Adapter-Construction und jede Signal-Dispatch-Ausführung,
  wenn das DB-Konto nicht als Demo verifiziert ist. Alpaca-Adapter müssen
  zusätzlich `paper=True` sein.
- `PAUSED`/`STOPPED` werden zuerst atomar in SQLite gespeichert. Dieser Write ist
  es, der neue Claims stoppt (`claim_live_deployment_submission` fügt nur ein,
  solange die Zeile `ACTIVE` liest) — der Pause ist also **bereits wirksam**,
  bevor der Endpunkt drainiert. Der anschließende Drain ist eine *Beobachtung*
  der Orders, die schon beim Broker waren, und keine Vorbedingung für den
  Statuswechsel.
- Der Status-Endpunkt meldet deshalb **niemals** einen Fehlschlag für einen
  Statuswechsel, der committed wurde. Er antwortet mit HTTP `200`, dem neuen
  Status und einem expliziten Drain-Ergebnis:
  - `submission_drain_outcome: "DRAINED"` — jeder Claim wurde innerhalb seiner
    TTL freigegeben; nichts war mehr im Flug.
  - `submission_drain_outcome: "IN_FLIGHT"` mit `in_flight_submissions` — ein
    Dispatcher hielt beim Statuswechsel noch einen Claim. Die neue Sperre greift,
    die Order kann aber noch beim Broker ankommen. `warning` sagt das explizit.
  - `submission_drain_outcome: "INDETERMINATE"` mit
    `expired_unreleased_submissions` — ein Claim ist abgelaufen, ohne
    freigegeben zu werden. Ob die Order den Broker erreicht hat, ist **unbekannt**
    und wird nie als sauberer Drain gemeldet.
  In beiden Nicht-`DRAINED`-Fällen gilt `submission_drain_complete: false`,
  `status_change_applied: true` plus ein `warning`. Der Operator wird gewarnt,
  nicht aber so getäuscht, der Pause sei fehlgeschlagen: ein Fehler an einem Stop
  verleitet dazu, auf `ACTIVE` zu klicken und den Bot über eine neue
  Broker-Verifizierung wieder scharf zu schalten. Vor dem Fortsetzen ist der
  Broker zu prüfen.
- Drain-Budget und Claim-TTL hängen an einer einzigen Konstanten
  (`LIVE_SUBMISSION_CLAIM_TTL_SECONDS`, daraus abgeleitet
  `LIVE_SUBMISSION_DRAIN_BUDGET_SECONDS = TTL * 2`). Das Budget muss **größer**
  sein als die TTL: gibt der Drain vorher auf, meldet er eine gesunde, langsame
  Submission fälschlich als „im Flug".
- Ein Claim beendet eine bereits laufende Broker-Submission nicht abrupt. Die
  PAUSE-Antwort garantiert deshalb nur bei `DRAINED`, dass keine Submission mehr
  im Flug ist.
- Der Drain-Observer (`list_live_deployment_submission_claims`) **liest nur** und
  löscht nie. Ein abgelaufener, nie freigegebener Claim bleibt als solcher
  sichtbar; dadurch ist er von „nichts war im Flug“ unterscheidbar. Gereapt wird
  ausschließlich im Claim-Pfad.
- Es gibt **genau einen** Submission-Claim pro Deployment: `deployment_id` ist in
  `live_deployment_submission_claims` UNIQUE, durchgesetzt von der Datenbank. Ein
  zweiter Dispatcher (z. B. ein zweiter Daemon-Prozess) bekommt den Claim nicht und
  sendet nichts ab. Abgelaufene Claims (TTL) werden vor jedem Claim-Versuch
  gelöscht und committed, damit ein abgestürzter Claimor eine Deployment nicht
  dauerhaft blockiert. Die Migration auf UNIQUE behält bei mehreren gleichzeitigen
  Claims den neuesten und verwirft die überholten (mit Warn-Log).

## Status-Semantik: PAUSE vs. STOP

`PAUSED` und `STOPPED` sind **nicht** dasselbe. Der Unterschied ist sicherheitsrelevant:

| Status | Neue Positionen | Offene Orders | Offene Positionen | Umkehrbar |
| --- | --- | --- | --- | --- |
| `PAUSED` | keine | bleiben liegen | bleiben offen | ja, sofort |
| `STOPPED` | keine | **werden storniert** | **werden glattgestellt** | ja, via explizites `ACTIVE` |

- `PAUSED` löst **nie** eine Liquidation aus. Ein versehentliches Pause kann keine
  Position auflösen.
- `STOPPED` bedeutet: erst alle offenen Orders stornieren, danach alle Positionen
  schließen. Der Daemon erzwingt das im Sync-Loop, nicht nur im Status-Endpoint —
  es gilt also auch, wenn der Status durch Restart oder einen zweiten Operator
  gesetzt wurde.
- Das Erzwingen passiert **einmal pro Stop-Transition**. Wird eine Deployment
  erneut `ACTIVE` und danach wieder `STOPPED`, wird erneut storniert und
  glattgestellt.
- Kann ein Adapter eine Aktion nicht (beide unterstützten sie inzwischen, aber ein
  künftiger Broker oder ein cTrader-Fehlerfall kann es), meldet der Daemon einen
  Fehler und markiert die Deployment **nicht** als erzwungen. Sie wird im nächsten Sync erneut versucht. Eine
  Deployment wird also nie als "gestoppt/flat" gemeldet, wenn der Broker nicht
  tatsächlich flach gestellt wurde.
- "Kein Adapter" heißt nicht "nichts offen": direkt nach einem Restart ist
  `active_adapters` für jedes Konto leer, deshalb bleibt eine solche Deployment
  bewusst außerhalb des Enforcement-LEDGER und wird erneut versucht. Ausnahme: ein
  Konto, das **nie** einen Adapter bekommen kann (fehlend oder nicht-Demo), ist ein
  terminaler Zustand. Der Daemon loggt das als ERROR — mit dem Hinweis, die Positionen
  manuell beim Broker zu schließen — statt als INFO-Retry, der nie aufhört.

Beide Broker implementieren die Kontrollen inzwischen:

- **Alpaca:** `cancel_orders()` (Bulk) und `close_all_positions()`.
- **cTrader:** `ProtoOAReconcileReq` für den aktuellen Stand (Orders und
  Positionen), `ProtoOACancelOrderReq` je stornierbarer Order und
  `ProtoOAClosePositionReq` je offener Position. Market-/Market-Range-Orders sind
  nicht stornierbar, da sofort ausgeführt. Der Reconcile-Aufruf hat bewusst keine
  Zeitgrenzen: `ProtoOAOrderListReq` ist eine historische Abfrage, und
  `fromTimestamp=0`/`toTimestamp=0` bedeutet nicht „jetzt" – sie könnte nichts
  liefern und damit einen Storno als erfolgreich melden, obwohl die Order noch
  liegt.

Ein Adapter, der eine Aktion nicht kann, meldet das über
`supports_cancel_orders` / `supports_flatten`; der Daemon verweigert dann die
Erfolgsmeldung und versucht es im nächsten Sync erneut.
