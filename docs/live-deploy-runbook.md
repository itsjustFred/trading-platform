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
- `GET /api/live/accounts` persistiert die zuletzt erkannten Broker-Identitäten
  (`ctid`, `broker_login`, `is_demo`) aus der Discovery-Antwort.
- Der Daemon verweigert Adapter-Construction und jede Signal-Dispatch-Ausführung,
  wenn das DB-Konto nicht als Demo verifiziert ist. Alpaca-Adapter müssen
  zusätzlich `paper=True` sein.
- `PAUSED`/`STOPPED` werden zuerst atomar in SQLite gespeichert. Danach wartet
  der Status-Endpunkt auf laufende Submission-Claims. Ist der Drain nach 10
  Sekunden unvollständig, antwortet der Endpunkt mit HTTP `409` und
  `in_flight_submissions`; `PAUSED` bleibt trotzdem erhalten.
- Ein Claim beendet eine bereits laufende Broker-Submission nicht abrupt. Die
  PAUSE-Antwort garantiert deshalb erst nach erfolgreichem Drain, dass keine
  Submission mehr im Flug ist.

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

Beide Broker implementieren die Kontrollen inzwischen:

- **Alpaca:** `cancel_orders()` (Bulk) und `close_all_positions()`.
- **cTrader:** `ProtoOAReconcileReq` für den aktuellen Stand (Orders und
  Positionen) + `ProtoOACancelOrderReq` / `ProtoOAClosePositionReq`. Der
  Reconcile-Aufruf hat bewusst keine Zeitgrenzen: `ProtoOAOrderListReq` ist
  eine historische Abfrage, und `fromTimestamp=0`/`toTimestamp=0` bedeutet
  nicht „jetzt" – sie könnte nichts liefern und damit einen Storno als
  erfolgreich melden, obwohl die Order noch liegt.
  (Market-/Market-Range-Orders sind nicht stornierbar, da sofort ausgeführt) und
  `ProtoOAClosePositionReq` je offener Position.

Ein Adapter, der eine Aktion nicht kann, meldet das über
`supports_cancel_orders` / `supports_flatten`; der Daemon verweigert dann die
Erfolgsmeldung und versucht es im nächsten Sync erneut.
