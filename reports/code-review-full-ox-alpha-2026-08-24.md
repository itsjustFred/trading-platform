# Full-Repo Code Review — trading-platform

- **Model:** ox-alpha (max effort)
- **Date:** 2026-08-24
- **Scope:** every tracked code file in the repo (`.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.css`) — not a diff
- **Method:** expert panel — 3 independent blind reviewers per area + synthesizer merge; reference report (`code-review-full-gpt-5.6-luna-2026-08-23.md`, weaker model) used only as hints-to-verify at synthesis, never shown to reviewers

## Checklist

- [x] runner
- [x] live_runner
- [x] frontend
- [x] viewer
- [x] data-validation
- [x] misc-backend
- [x] tests-a
- [x] tests-b
- [x] tests-c

## Summary

### Findings by severity × area

| Area | Critical | Major | Minor | Total |
|---|---|---|---|---|
| runner | 4 | 41 | 105 | 150 |
| live_runner | 3 | 16 | 45 | 64 |
| frontend | 1 | 20 | 63 | 84 |
| viewer | 4 | 21 | 31 * | 57 * |
| data-validation | 2 | 51 | 118 | 171 |
| misc-backend | 1 | 38 | 176 | 215 |
| tests-a | 4 | 19 | 71 | 94 |
| tests-b | 2 | 7 | 79 | 88 |
| tests-c | 1 | 15 | 126 | 142 |
| **Total** | **22** | **228** | **814** | **1064** |

\* viewer's counts cover bugs + security only; its Fowler-smell inventory is enumerated separately inside its section (not double-counted), plus 1 trivial finding. Each area section carries a full settlement log for reviewer-vs-reviewer and reviewer-vs-reference disputes.

### Top 10 most important findings (severity × blast radius)

1. **Naked live positions: SL/TP computed but never sent to either broker** — `live_runner` §C-2 (`daemon.py:664–667`, `ctrader_adapter.py:1437–1448`, `alpaca_adapter.py:421–456`). Every live trade opens without protective orders while position sizing assumes a bounded stop; an FX gap is unbounded loss.
2. **Live trades parsed from free-form Discord chat** — `live_runner` §C-3 (`discord_source.py`). Any uppercase word can mint a ticker (whitelist not enforced at dispatch), `$1,234` parses as price 1.234 (1000× off), no author allowlist, >20-message bursts silently dropped.
3. **Unauthenticated API with reflected-origin CORS reaches arbitrary code execution** — `viewer` §4.1 (`core.py:202-207` → `main.py:7863-7880`; CORS `main.py:632-638`; prefix-bypass deletion `main.py:10666-10671`). Starlette reflects any Origin when `allow_credentials=True`; `POST /api/backtests/run {"strategy": "/tmp/evil.py"}` executes attacker Python; drive-by CSRF against deletes, downloads, and live-deployment creation.
4. **Operator PAUSE is not honored on live deployments** — `live_runner` §C-1 (`daemon.py:1069/1616-1630/2006`). Listener tasks hold a stale deployment snapshot; pausing one deployment does not stop its trading while siblings stay active.
5. **`regenerate_backtests.py` purges all backtest tables and every `*.trades.json` before regenerating on guessed mappings** — `misc-backend` Group scripts (`scripts/regenerate_backtests.py:201-218`). No confirmation, dry-run, or backup; one accidental run is irreversible corpus loss.
6. **Risk-engine sizing bypasses and unit conflation** — `misc-backend` risk group (`risk/live.py:45-95`, `risk/engine.py:127,151-152,216-218`). Metadata `qty` skips max_positions/exposure/per-trade checks entirely; fraction-vs-currency conflation permits up to ~5× configured per-trade risk; RiskConfig rebuilt from untrusted signal metadata can crash live sizing.
7. **Backtest engine defects that corrupt research conclusions** — `runner` section: `lean_adapter.py:29` NameError kills the LEAN engine; `grid_tester.py:74` omits required `strategy_path` (every grid cell fails); `backtest_grid.py:184` passes an unknown CLI flag (subprocess-mode cells all fail argparse); `strategy_ir/codegen_vectorbt.py:81-105` keyword-sniffs prose like "stop" into `(rsi < 30)` entry rules while labeled deterministic; native engine merges signals vertically instead of horizontally (`engine.py:874`).
8. **Merged Dukascopy tick download is doubly broken** — `data-validation` §A DV-C1/DV-C2 (`dukascopy_native.py:1016-1028`, `:790-791`). Guaranteed TypeError on the whole `--merge-tick`/bid+ask path, and once fixed, the "ask" leg re-downloads bid URLs so ask columns equal bid — silent corruption behind a crash.
9. **Remote JARs loaded into the JVM with zero integrity checking** — `data-validation` DV-B6/B7 (`jforex_dl.py:280-311, 344-356, 549`). Env-overridable JNLP URL → downloaded jars straight onto the classpath; bootstrap failures swallowed into a generic warning. Supply-chain RCE surface adjacent to the trading process.
10. **The test suite cannot fail exactly where it claims to protect** — `tests-a/b/c` sections: e2e lab conversion validates a checked-in fixture when extraction fails (`e2e/lab/test_conversion.py:74-82`); API/e2e_frontend tests pass vacuously on empty stores or assert nothing (`test_api.py:38-135`, `e2e_frontend.js:148-154`); three independent silent-skip mechanisms zero whole layers while CI stays green (`strategies/conftest.py`, `conftest.py`, `integration/test_strategy_pipeline.py`); the "intentionally broken" negative-control fixture actually passes its validator (`fixtures/ai_generated_strategies/intentionally_broken_strategy.py`).

### Coverage

Every tracked code file is accounted for: **371 files** across the nine areas — runner 36, live_runner 18, frontend 59, viewer 4, data_tools/validation 50, misc-backend 76 (73 package `.py` + `pyproject.toml`, `pytest.ini`, `live_deployments.yaml`), tests 128 in three alphabetical chunks. Totals: **22 critical / 228 major / 814 minor (+1 trivial)**, each with `file:line` citations, per-area panel provenance, dispute-settlement logs, and scanned-clean records for the remainder.

---

## live_runner

# Live Trading Runner (`live_runner`) — Merged Panel Review

**Panel:** model **ox-alpha**, 3 independent blind reviewers (r1, r2, r3), merged and re-verified line-by-line against `live_runner/**` source. Where reviewers disagreed on facts or scale, the dispute was settled by reading the code, not by vote — every such ruling is recorded in the *Discrepancy log* at the end. Findings from the reference report (`code-review-full-gpt-5.6-luna-2026-08-23.md`, "Luna") were consulted as hints only; verified ones are merged (marked *(ref)*), unverified ones are flagged or excluded with reasons.

**Merged totals: 🔴 Critical 3 · 🟠 Major 16 · 🟡 Minor 45**

---

## A. Critical — live-money defects

### C-1 · PAUSE is not honored: Discord listeners trade from a stale deployment snapshot *(r1 only; verified critical)*
The deployment-sync loop rebuilds its grouping dict every iteration (`deployments_by_account = _group_deployments_by_account(deployments)` — `daemon.py:2006`), but running listener tasks captured the **old** dict object at creation (`_start_missing_discord_tasks` → `_run_discord_listener_with_retries(account_id, adapter, deployments_by_account, …)`, `daemon.py:1655–1663`) and re-read only that same object on retry (`deployments = deployments_by_account.get(account_id, [])`, `daemon.py:1069`). Inside the listener, `_active_discord_deployments_for_account` filters `status == "ACTIVE"` against that stale list (`daemon.py:684–694`). Because `_cancel_inactive_discord_tasks` cancels only when the whole **account** leaves the Discord-active set (`daemon.py:1616–1630`), flipping one deployment to `PAUSED`/`STOPPED` does **not** stop its live trading while any other Discord deployment on the account stays active. An operator control over live funds silently fails. Fix: pass a fresh lookup (or re-query deployments) per dispatch, or key tasks per-deployment.

### C-2 · Protective stop-loss / take-profit are never sent to either broker *(r1: major, r2: critical, r3: silent — resolved as critical)*
Verified end-to-end: the daemon computes, validates, and persists SL/TP into signal metadata (`daemon.py:664–667`, risk sizing explicitly assumes a bounded stop via `enforce_stop_direction`, `daemon.py:532`, sizing = risk_amount / stop_distance). Neither adapter transmits them:
- cTrader `_build_new_order_request` sets only account/symbol/MARKET/IOC/volume/clientOrderId — no `stopLoss`/`takeProfit`, no follow-up protective order (`ctrader_adapter.py:1437–1448`);
- Alpaca `_build_order_request` builds a plain market or single-leg limit — no bracket/OTO (`alpaca_adapter.py:421–456`);
- repo-wide search finds no protective-order fields anywhere downstream of `daemon.py`.
Every live position opens **naked** while sized as if bounded; on leveraged FX an adverse gap is unbounded. Fix: attach native protective orders (cTrader absolute-price `stopLoss`/`takeProfit`; Alpaca brackets) and refuse submission when a signal carries SL metadata the adapter cannot honor. *(r3 recorded no finding here; treated as an omission, noted in the discrepancy log.)*

### C-3 · Live trades parsed from free-form Discord chat with garbage-tolerant fallbacks, and >20-message bursts are dropped *(r1: major, r2: critical, r3: major×2 — resolved as critical composite)*
Three compounding, independently verified defects in `signal_sources/discord_source.py`:
1. **Fallback ticker = any uppercase word.** `_extract_symbol` descends six stages to `_fallback_uppercase_symbol` — first non-stopword ALL-CAPS word of the first non-empty line (`discord_source.py:174–182`); `_SKIP_WORDS` (`:55–97`) omits ordinary words like ON/IS/AT/TO, so prose mints tickers. The daemon-side symbol whitelist does not close the hole: `_deployment_covers_discord_symbol` treats an empty `symbols` set as "cover everything" (`daemon.py:749–754`) — preflight requires symbols (`preflight.py:324–325`) but the daemon never enforces them for DB-created deployments.
2. **Price corruption:** `\$\s*(\d+(?:[.,]\d+)?)` captures `"1,234"` and `float("1,234".replace(",", "."))` yields **1.234** — a 1000× wrong entry price into sizing/slippage math (`discord_source.py:45–47, 200–209`). `_extract_price` also takes the first `$` number anywhere in the message.
3. **No origin authentication + page-drop:** author ID is recorded but never checked against any allowlist (`discord_source.py:286`, `_poll_once :459–484`), so any channel member's message becomes an order; and because `_seen_ids[channel_id]` is replaced with only the fetched page (`:488`) under `POLL_LIMIT = 20`, a burst of >20 posts silently orphans everything older than the newest 20 (`:451–488`) — a missed-trade bug with no log line. Broad sell classification ("momentum reversal" / bare sell|short → action `sell`, `:49, 217`) completes the chain into `submit_order_with_risk`. Minimum hardening: allowlist authors/bot IDs, require marker phrases, enforce non-empty `symbols` at dispatch, parse thousands separators properly.

---

## B. Major — wrong-account / missed-trade / data-integrity risks

### B-1 · Hint-less multi-account configs route every account through ACC1's keys *(r1: major, r2: critical, r3: major — resolved as major)*
In `resolve_alpaca_credentials`, the ambiguity refusal fires only when blocks carry hints (`alpaca_adapter.py:157–173`); with ≥2 `ALPACA_*_ACC{N}` blocks and **no** `*_ACCOUNT_ID_ACC{N}`/`*_BROKER_LOGIN_ACC{N}` anywhere, an unmatched DB account falls through to `return _alpaca_block_credentials(blocks[0])` (`:183`) — both accounts trade the same broker account while the DB believes otherwise. Requires an ops omission (hints missing), hence major rather than critical — but the failure mode is silent wrong-account live trading. Return `None` instead of `blocks[0]`.

### B-2 · Any unmapped signal action becomes a live SELL *(r1+r2 agree)*
`action="buy" if signal.action in {"buy","long"} else "sell"` (`daemon.py:677`); same defect at the final gate in both adapters (`alpaca_adapter.py:443, 452`; cTrader side `ctrader_adapter.py:1442–1444`). `"close"`, `"flat"`, a processor bug, or a typo becomes a market SELL — an outright short wherever shorts are allowed. Whitelist `{buy,long,sell,short}` and raise otherwise.

### B-3 · Typo'd boolean config silently disables candle-close waiting *(r1: major, r2/r3: minor — resolved as major)*
`_deployment_wait_for_candle_close` parses configured values and `DISCORD_WAIT_FOR_CANDLE_CLOSE` with the lenient parser (`daemon.py:380–388` → `daemon.py:137–140`): any unrecognized token returns `False` → immediate submission instead of waiting for the candle close, changing execution timing/price for live orders. The strict parser exists in the same file for exactly this hazard class (`daemon.py:147–158`). Same footgun in `_deployment_allows_short` (`daemon.py:373–377`), where silent-False is at least conservative. Rated major because it changes live execution behavior on a config typo.

### B-4 · One bot token/timeframe per account, chosen arbitrarily; one inaccessible channel auth-blocks the whole account *(r1: major, r2: major+minor split, r3: minor — resolved as major)*
`primary_deployment = next(iter(active_discord_deployments))` decides the token **and** listener timeframe for all channels of an account (`daemon.py:892–900`), with a silent generic-token fallback (`DISCORD_BOT_TOKEN`, `daemon.py:728–746`) — two deployments meant for different bots poll both channels with whichever sorts first; the second bot gets 403 → `DiscordAuthenticationError` → classified terminal → the entire account's Discord trading is blocked including channels that worked (`daemon.py:1080–1093`). Route tokens/channels per deployment-group; scope auth-blocks per channel.

### B-5 · A Discord bot service FAILs permanently after one transient reconnect failure *(r2 only; verified)*
`_reconnect_discord_bot_source` writes `ServiceStatus.FAILED` on **any** non-terminal exception (`daemon.py:1265–1290`); `_sync_discord_bot_services` processes only `RUNNING` services (`daemon.py:1398–1400`); `_recover_failed_services` runs once at startup (`daemon.py:2075`). One network blip ⇒ the bot receives no signals until daemon restart, silently. Retry transient reconnects or re-arm recovery.

### B-6 · Exit-only closes only the first matching position *(r1: minor, r2: major — resolved as major)*
`_resolve_exit_only_target_qty` takes the first position whose symbol matches and breaks (`daemon.py:556–568`). cTrader hedging accounts return one entry per open position, so an exit signal flattens one lot and leaves residual exposure the operator believes is closed. Sum matched quantities.

### B-7 · cTrader volume-unit chaos: hardcoded ÷1000, raw-volume fills, metadata bypass *(r1: major (scale factor), r2: major (fill units + bypass), r3: major (fill units) but disputed the bypass — bypass verified)*
Three distinct defects around one missing abstraction ("broker volume vs lots"):
- **Metadata bypass of risk sizing:** `_resolve_order_volume` short-circuits on `ctrader_volume`/`volume` metadata ahead of the risk-sized `qty` (`ctrader_adapter.py:757–761` via `_explicit_metadata_volume :206–217`) — any upstream path (or crafted signal) setting `volume` trades an arbitrary size with zero risk checks. Clamp to the risk engine's maximum. (Confirms reference **L1**, which r3 had contradicted.)
- **Hardcoded ×1000 factor, never validated per-symbol:** `volume = int(qty*1000)` (`:766`), `qty = volume/1000` (`:1531–1532`); the adapter fetches only `symbolId`, never min/max/step — instruments where a lot ≠ 1000 units mis-size by orders of magnitude.
- **Fill quantities persisted in raw broker units:** streaming fills persist `filledVolume` verbatim (`ctrader_adapter.py:1575–1588`, `_resolve_filled_volume :1660–1669`; reconciliation `:1221–1240`) while positions divide by 1000 — `LiveExecution.filled_qty` for cTrader is ~1000× every other record of the same trade concept, and anything summing fills inherits the distortion. Normalize at the boundary; introduce a `BrokerVolume` type.

### B-8 · Persist-before-validate permanently wedges signals on both brokers *(all three reviewers; confirms reference L2)*
cTrader: `save_live_signal` runs before symbol resolution and action validation (`ctrader_adapter.py:1346` vs `:1350–1352`, `_resolve_symbol_id :1430–1435`); an unknown symbol raises after persist, so every retry hits the duplicate guard (`:1333–1344`) and ends in `RuntimeError("Persisted cTrader signal has no unique recoverable broker order…")` forever — unrecoverable without DB surgery. Alpaca: same shape (`save_live_signal` `alpaca_adapter.py:496` before request build; recovery `:377–397`). Validate symbol/action before first persistence.

### B-9 · Environment-precedence inversion: services registered with one credential set, executed with another *(r2: major, r3: called correct-by-design — resolved as major per code)*
`service_discovery.discover_discord_bots_from_env` lets `os.environ` **overwrite** `.accounts.env` values (`service_discovery.py:22–24`), while the daemon resolves runtime credentials file-first (`accounts_env_values.get(key) or os.environ.get(key)`, `daemon.py:1118`). A bot can be registered with the env's token/channel and subsequently run with the file's — operating on an unintended channel under an unintended identity. Related family members (minors below): `load_dotenv(accounts_env)` mutating process env (`account_discovery.py:766`), market-data streams resolving from `os.environ` only, adapter refresh write-back to `os.environ` (`ctrader_adapter.py:486–487`). Define precedence once in a shared resolver.

### B-10 · Exposure estimate fed to the risk engine mixes incompatible units *(r3 only; verified)*
`_broker_exposure_estimate` computes `open_positions × max_risk_per_trade` (risk-at-stake) and `submit_order_with_risk` passes `current_exposure=max(current_exposure, broker_exposure)` (`daemon.py:602–615, 642–652`), but `RiskEngine` treats `current_exposure` as budget already consumed against `max_total_exposure`. The exposure cap effectively binds at `max_positions` and almost never at `max_total_exposure`; `max()` compares dimensionally different quantities. Conservative direction (undersizes), so major-not-critical — but the comment's promise ("max_positions / max_total_exposure actually bind", `daemon.py:642–643`) is half true.

### B-11 · One Discord signal executes on at most one deployment *(r3 only; verified)*
`_dispatch_discord_trade_signal` breaks after the first deployment passing route/symbol filters (`daemon.py:836–850`); a second ACTIVE deployment on the same channel/symbol silently never trades, selected by DB row order.

### B-12 · Discovered `is_demo` can contradict the adapter's actual paper/live mode *(r1: major; panel rules minor after verification)*
Discovery derives Alpaca `paper` solely from `ALPACA_ENDPOINT_ACC{idx}` containing "paper", defaulting True (`account_discovery.py:127–135`, endpoint check at `:135`), while the adapter honors `ALPACA_PAPER_ACC{idx}` (`alpaca_adapter.py:91–95`). `ALPACA_PAPER_ACC2=false` with no endpoint ⇒ safety metadata in the DB says demo while the trading adapter routes live. Kept at **minor**: discovery populates dashboards, not order routing — but fix by sharing one credential-block parser (the duplication has already drifted).

---

## C. Minor findings (robustness, hygiene, hardening)

**`daemon.py`**
- Submission failures swallowed with warning only — no retry/dead-letter/metric for a failed live order; `capture_exception` used only for daemon crash (`daemon.py:797–811`).
- Busy-spin when an account's listener returns immediately with no routable channels — retry loop sleeps only in the exception branch (`daemon.py:1059–1105`).
- Blanket unblock of Discord services on any `.accounts.env` mtime change, even unrelated keys (`daemon.py:1424–1438`); contrast the correct per-signature cTrader gating (`daemon.py:264–286`).
- Shutdown gaps: cancelled tasks never awaited; `active_listener_tasks` neither cancelled nor awaited; adapters never `disconnect()`ed (`daemon.py:2111–2136`).
- Module-global mutable block registries mutated from concurrent loops — safe only under a single undocumented event-loop invariant (`daemon.py:64–70`).
- Misleading skip diagnostics: logs "symbol not covered" when the real cause was a StockKit route mismatch (`daemon.py:836–861`); silent `except ValueError: continue` registering discovered services (`:198–213`); typo'd deployment status collapses to UNKNOWN with one warning (`:1716–1720`); `_coerce_live_int` truncates `2.9→2` for risk knobs (`:317–329`); `pip_value` defaults to 1.0 for equities (`:522–523`); `_build_processor_market_data` ignores its `timeframe` parameter (`:442–461`); serial balance polling with no timeout budget stalls snapshots behind one slow broker (`:1559–1562`); two independent `.accounts.env` caches between the two loops (`:1419–1426, 1983–1991`); terminal-error classification by substring incl. bare `"unauthorized"` (`daemon.py:93–109`).

**`ctrader_adapter.py`**
- Drained execution events lost if their DB persist fails — reconcile flag set only on queue overflow, not persist failure (`ctrader_adapter.py:1703–1704, 555–562`); `disconnect()` swaps the queue wholesale, discarding buffered fills (`:1284`) and wipes state before transport stop with a 0.1 s stop timeout (`:1273–1303`).
- Refreshed OAuth tokens written to the single shared global `CTRADER_*` keys — last writer wins across accounts, and the signature change tears down/reconnects all cTrader adapters; plus export to `os.environ` (`ctrader_adapter.py:463–487`). Confirms reference **L5** (rated major there; kept minor-to-major boundary here since single-token deployments work and the write itself is excellent: atomic temp-file + `os.replace` + chmod 0600 + fcntl lock, `:138–171` — flagged positive).
- Fabricated `datetime.now()` on bad/absent broker timestamps — audit-trail corruption (`:67–76`; same in `alpaca_adapter.py:50–58` and `discord_source.py:244–251`).
- Redundant second sizing call per submission (result used only for an error message; extra round trip) (`:1321–1328`); process-global Twisted reactor flag breaks multi-loop use (`:30, 179–203`); silent `except Exception: pass` teardown (`:998–1007`); equity falls back to balance ignoring floating PnL (`:1483–1488`); fractional metadata volumes truncated (`:210–213`).

**`alpaca_adapter.py`**
- Sync REST `get_account()` inside `async def connect()` — brief loop stall during startup gathers (`alpaca_adapter.py:225–229` vs wrapped calls at `:511, 531, 540`).
- Trade-update handler re-subscribed on clean `run()` return — duplicate handler chains accumulate (dedupe prevents double-persist) (`:697–711`); `connect()` unlocked — concurrent callers can leak websocket streams (`:471–473, 697–709`).
- Reconciliation merges "order unknown" with auth/network faults, zero logging (`:262–268`); blanket handler swallow (`:691–695`); float-native fractional qty without Decimal rounding (`:441–455, 482`); reconcile dedupe-key theoretical collision across cancel/refill edges (`:286–291`).

**`account_discovery.py`**
- Blocking TLS/REST conversation inside `async def`s with 12–20 s timeouts (`:691–715, 156–167`) — **recalibrated from r1's major**: current callers are CLI/viewer/scripts, not the daemon loop; latent-freeze hazard if ever embedded.
- Import-time `from ctrader_open_api import Protobuf` installs the default Twisted reactor, contradicting the file's own comment (`:15–31`) and poisoning later adapter use in the same process (r3; latent today).
- Unbounded length-prefixed frame read — hostile peer can force a huge allocation (`:365–368`); balance defaults to 0.0 on partial discovery (`:197–200, 588, 684`); error classification by `repr()` substrings (`:676–678, 100–111`); demo/live dedupe assumes globally unique ids (`:727–758`); bare `TimeoutError()` (`:363`); debug dumps of account payload fields (ref l5, verified acceptable-but-noisy — `MessageToDict` account metadata, not secrets).

**`signal_sources/discord_source.py`** — non-numeric message id raises mid-poll and retries forever (`:463, 486`); unbounded signal queue (`:338`); full message text persisted into trade-store metadata (`:287`); `"401"` substring classified as terminal auth (`:107–117`); channel id interpolated into URL unvalidated (`:384`); DB status write per parsed signal (`:480–483`); double-fetch on connect (`:571–572`).

**`market_data_stream.py`** — worker thread races: `self.stream` swapped on the worker thread during reconnect while `set_symbols` subscribes from the caller thread (`:146, 161–173`); dead workers stay registered so bars freeze silently after connection-limit exit (`:122–134, 240–253`); speculative arg-less `unsubscribe_bars()` swallowed then symbols re-added on top (`:165–173`); malformed bar timestamps crash the ws loop into churn (`:41–59`); `BTCUSD`-style crypto routed to the stock stream via fragile `a or b and c` precedence (`:19–23`); threads never joined (`:175–182`); lock held across blocking stop (`:235–238`); unbounded symbol-set growth; credentials resolved from env-only (see B-9).

**`preflight.py`** — per-account `asyncio.run()` collides with the global reactor: multi-account cTrader connectivity checks get spurious failures (fail-closed, but invites operators to skip preflight) (`preflight.py:97–124` + `ctrader_adapter.py:179–203`); adapter construction outside error capture crashes the CLI (`:232–241`); unbounded wall-clock per account; `errors.append` gating suppresses the more actionable credentials message (`:207–212`); results print balances/positions to stdout.

**Other files**
- `execution_timing.py` — UTC-epoch candle-close math ignores sessions: `1d` equities wait to midnight UTC, queue as DAY, fill at next open with overnight gap risk (`execution_timing.py:57–60`); unknown/typo'd timeframe silently disables the wait (`:65–67`).
- `config_parser.py` — unknown account refs warn here but error in preflight; fail fast at load (`config_parser.py:42–53` vs `preflight.py:320–323`).
- `stockkit_routing.py` — production account IDs hardcoded (`:8–12`); bare `KeyError` on unmapped routes, latent (`:59–60`).
- `ctrader_deployment.py` — plan concentrates everything on `ctrader_accounts[0]` (`:66–69`, mitigated by PAUSED-by-default `:64, 80`); score mixes unscaled ratio/percentage/probability terms (`:17–27`); NaN guarded but ±inf flows into scores and poisons ranking (`:9–14`); `limit<=0` still returns one entry via `max(1, int(limit))` (`:56`, ref l9 verified).
- `test_signal_trigger.py` — real-order tool with no `--confirm-live` gate (`:30–52`); bypasses `resolve_alpaca_credentials` so multi-block envs trade default keys (`:31`); `assert` stripped under `-O` (`:33`); blocking price fetch inside `_run` (`:34`); `load_dotenv` lets pre-existing env vars override the chosen file (ref l8 verified, `:65`); no adapter disconnect on exit (ref l7 verified, `:50` — trivial).
- `base_adapter.py` / `base_source.py` — interface drift: `get_positions`/`disconnect` (and source `pause()/reconnect()/is_paused/is_connected`) relied upon duck-typed by the daemon but absent from the ABCs, forcing `getattr` probes (`daemon.py:576–578, 608–609, 1012–1014`; `base_adapter.py:8–25`; `base_source.py:9–20`); ABC `listen_signals` signature doesn't match the async-generator implementations.
- `service_discovery.py` — `DISCORD_BOT_{NAME}_TOKEN` naming convention re-derived in three modules (`:12, 32–33`; `daemon.py:735–736, 1116–1117`) — Shotgun Surgery; bots missing channel keys skipped silently (`:34–36`).

**Verified positives (do not regress):** fail-closed duplicate-submit recovery in both adapters; cumulative-fill clamping and delta handling; no-default-size cTrader volumes and sub-minimum rejection; numeric routing-key normalization and fail-closed stockkit validation; TLS 1.2+ with hostname verification; strict bool parsing guarding live-mode selection in preflight; secrets never logged anywhere; tz-aware datetimes throughout.

---

## D. Cross-cutting maintainability themes

1. **Duplication with divergence (top systemic issue):** bool-env parsers ×5 files in 2 semantics; `PROJECT_ROOT/.accounts.env` constants ×5; Alpaca ACC-block parsers ×2 already drifted on paper-mode; cTrader credential resolvers ×3; retry/backoff scaffolds ×4; error-classification marker vocabularies ×4 (same broker error terminal in one layer, retriable in another). Extract: one strict bool parser, one credential module owning file+env merge and paper/demo inference, one `classify_broker_error()`.
2. **God modules:** `daemon.py` (2,144 lines: deployment lifecycle + Discord plumbing + credential rotation + risk orchestration + balances) and `ctrader_adapter.py` (1,767 lines: transport + OAuth + order building + streaming + reconciliation + persistence). Split along those seams; extract the task-bookkeeping helper families (~8 near-identical functions) into a `TaskRegistry`.
3. **Primitive obsession at money boundaries:** broker volume vs lots (B-7 is precisely this missing type), credential tuples, string statuses beside existing enums.
4. **Repeated Switches:** broker dispatch ×4 sites (daemon, preflight, discovery, market-data) — introduce a factory keyed by broker.
5. **Highest-risk hot spot:** the Discord regex constellation + `_SKIP_WORDS` decides *what to buy* from free text with no golden-test corpus; cheapest available insurance is characterization tests before touching it.

## E. Fixed-checklist verdict (Python)

Mutable default args clean; no bare `except:` (silent `except Exception: pass` flagged where money-relevant); races flagged on module-global registries, stream-worker swap, alpaca connect; no SQL string building (DB via `shared.database`); no subprocess use; tz handling consistently aware — residual issues are fabricated `now()` on parse failure and UTC-session alignment; resource leaks flagged (unawaited shutdown tasks, unjoined threads, queue swap, ws replace-without-stop).

---

## F. Discrepancy log (settled by reading code, not by vote)

1. **Naked SL/TP — r1 major / r2 critical / r3 absent:** ruled **critical** (C-2). Verified no protective orders reach either broker; sizing math assumes bounded loss; r3's silence treated as omission.
2. **Alpaca `blocks[0]` fallback — r2 critical / r1&r3 major:** ruled **major** (B-1): hole confirmed exactly as described, but requires an ops omission (hints absent) rather than failing in normal operation.
3. **Discord parser chain — r2 critical / r1&r3 major:** ruled **critical** composite (C-3): verified the daemon leaves the symbol whitelist unenforced (`daemon.py:749–754`), the `$1,234→1.234` corruption, the >20-message drop, and the absence of author authentication.
4. **Stale deployment snapshot — r1 only:** verified as filed and ruled **critical** (C-1); r2/r3 missed it.
5. **Blocking discovery I/O — r1&r2 major / r3 minor:** recalibrated **minor** (latent): verified via call-site search that only CLI/viewer/scripts invoke it, never the daemon loop.
6. **Metadata volume bypass (ref L1) — r2 yes / r3 no ("no-default-size policy is excellent"):** both partly right: the fail-closed *no-default* path is real, but the `ctrader_volume`/`volume` short-circuit ahead of risk-sized qty is also real (`ctrader_adapter.py:757–761`) → bypass stands (B-7).
7. **Adapter re-sizing overrides deployment risk config — r2 major / r3 cosmetic:** r3 is right on mechanism: `size_live_signal` echoes existing `qty`/`explicit_qty` (`risk/live.py:45–85`) rather than re-deriving from adapter defaults → downgraded to redundancy/minor round-trip cost; the genuine override path is the metadata `volume` bypass (B-7).
8. **Service-discovery precedence — r2 major / r3 "env-overrides-file is correct":** ruled in favor of **r2** (B-9): the inconsistency with the daemon's file-first resolution (`daemon.py:1118`) is objectively present; whichever order is intended, having two is the defect.
9. **Reference L3 (generic-token fallback):** verified and merged into B-4 (`daemon.py:745`); severity aligned with r1/r2's analysis of the blast radius.
10. **Reference L4 (preflight accepts unsupported brokers):** **excluded as not reachable** — `broker` is `Literal["alpaca","ctrader"]` in the config models (`shared/models.py:160,171`), so an unknown broker cannot reach `_preflight_account`; retained only as a hardening nit (an explicit `else` would future-proof).
11. **Reference L5 (token refresh global mutation):** verified (`ctrader_adapter.py:463–487`); merged as minor-to-major boundary within B-9/C-minors — Luna's "one account's refresh replaces another's credentials" is accurate for multi-account setups.
12. **Reference L6 (tz-naive datetimes preserved):** **not reproduced** — `_to_datetime` returns whatever datetime the SDK supplied, and all *created* timestamps are aware; panel found the real timestamp defect to be fabricated `now()` on garbage input (minor, listed above) rather than naive passthrough. Flagged as disagreement, not smoothed over.
13. **Reference l1/l2 (worker join/races):** verified and merged into the market-data minors; l3–l9 verified as filed and merged (numeric-id guard, parser weakness folded into C-3, debug dumps, temp-file residue, no-disconnect, env override, `limit<=0`).
14. **Severity-scale note:** r1/r2 counted the naked-orders finding once across two files; this merge follows the same root-cause convention. Per-reviewer baselines were: r1 = 1C/13maj/44min, r2 = 3C/14maj/48min, r3 = 0C/8maj/28min.

## G. Coverage

All 18 tracked files accounted for: findings above cover `daemon.py`, `ctrader_adapter.py`, `alpaca_adapter.py`, `account_discovery.py`, `discord_source.py`, `market_data_stream.py`, `preflight.py`, `execution_timing.py`, `config_parser.py`, `stockkit_routing.py`, `ctrader_deployment.py`, `test_signal_trigger.py`, `base_adapter.py`, `base_source.py`, `service_discovery.py`; scanned-clean: `live_runner/__init__.py`, `signal_sources/__init__.py`, `account_routing.py` (fail-closed validator singled out by all three reviewers as the model to imitate).

---

## viewer

## 4. viewer

**Panel:** model ox-alpha — 3 blind, independent reviewers (r1, r2, r3), synthesized by ox-alpha.
**Scope:** all four tracked files — `viewer/__init__.py` (0 ln), `viewer/comparison.py` (197 ln), `viewer/core.py` (4,888 ln), `viewer/main.py` (10,913 ln). Every reviewer read all files end-to-end; the synthesizer independently verified contested citations in source before merging.
**Consolidated total (bugs + security): 57 findings — 4 critical · 21 major · 31 minor · 1 trivial.** Baseline (Fowler) smells are enumerated separately in §4.7 and are judgement calls, not counted in the totals.

| File | Critical | Major | Minor | Trivial |
|---|---|---|---|---|
| `viewer/__init__.py` | 0 | 0 | 0 | 0 |
| `viewer/comparison.py` | 0 | 2 | 5 | 0 |
| `viewer/core.py` | 2 | 6 | 9 | 0 |
| `viewer/main.py` | 2 | 12 | 17 | 1 |

### 4.0 The area's central defect (panel consensus)

The unresolved `viewer/core.py` ↔ `viewer/main.py` split drives most findings below: a dead Flask-era route layer duplicating live FastAPI endpoints path-for-path, verbatim-duplicated helpers, duplicates that have *behaviorally diverged*, an import-order-dependent monkey-patch bridge, and inconsistent security controls (three correct containment checks coexisting with two vulnerable patterns). One deliberate migration — helpers into a service module, delete the route layer, kill the alias wall — would eliminate the majority of this section.

---

### 4.1 Security

- **[critical] Strategy resolution accepts arbitrary existing filesystem paths and feeds them to a subprocess (arbitrary code execution).** `viewer/core.py:202-207` — `(STRATEGIES_DIR / identifier).resolve()` returns *any* existing path; the live `POST /api/backtests/run` resolves the client-supplied `strategy` field through it (`viewer/main.py:7752-7754`) and the result becomes `--strategy <path>` of `python -m runner.cli run` (`viewer/main.py:7863-7880`). With no authentication anywhere and wildcard CORS (below), posting `{"strategy": "/tmp/evil.py"}` executes attacker-chosen Python with the server's privileges. Fix: resolve identifiers through the strategy registry only, or enforce `candidate.is_relative_to(STRATEGIES_DIR.resolve())`. *(Reference report V3/V12 rated this major; raised to critical — see §4.10.)*
- **[critical] Wildcard CORS combined with `allow_credentials=True`.** `viewer/main.py:632-638`. Verified against the installed Starlette 1.3.1 source: with `allow_all_origins and allow_credentials`, the middleware **reflects the request's Origin** instead of sending literal `*`, so browsers accept credentialed cross-origin requests from *any* site. Every mutating endpoint — deletes, bulk deletes, subprocess spawning, live-trading deployment creation — is CSRF/drive-by reachable from any web page the operator visits. *(r2/r3 assumed browsers reject `*`+credentials and rated major; r1's "reflects any origin" reading is correct — see §4.10.)*
- **[critical] No authentication or authorization on any endpoint, including live-money operations.** `POST /api/live/deployments` (`viewer/main.py:9693`) and `POST /api/live/deployments/ctrader/top5` with `activate=True` (`viewer/main.py:9646-9690`) create active live-trading deployments; deletes/bulk deletes (`6818`, `9082`), dedupe-destructive endpoints (`6770`), downloads (`10253`) and the LLM agent pipeline (`10288`) are equally open. Nothing enforces even a LAN boundary (historical posture: `--host 0.0.0.0` default, `viewer/core.py:4874`). Minimum fix set: origin allowlist, drop `allow_credentials`, token auth for mutating routes, refuse non-loopback bind unless configured.
- **[critical] File-deletion containment uses string prefix matching (sibling-prefix bypass), live and legacy.** `viewer/main.py:10666-10671`: `str(file_path).startswith(str(data_root))` accepts `/home/ubuntu/data_backup/x` for `DATA_DIR=/home/ubuntu/data`, then `unlink()`s it (`10681`) and rebuilds the catalog. Identical flaw in the legacy twin `viewer/core.py:4829-4836` (which additionally deletes *any* file under DATA_DIR — backtests, portfolios, parquet, DB artifacts — with no scoping, `4820-4847`). Combined with zero auth this is remote arbitrary file deletion within and adjacent to the data root. Fix: `is_relative_to` (+ extension whitelist + confirm token). *(Corrects r2's citation of `core.py:2827` — that region is a download-identity dict.)*
- **[major] Bulk-delete endpoints concatenate raw IDs into unlink paths (path traversal).** `_delete_backtest_with_artifacts` builds `BACKTESTS_DIR / f"{normalized_id}.trades.json"` (`viewer/main.py:2531`) and `_delete_portfolio_with_artifacts` likewise (`2030`), fed by `POST /api/backtests/delete-bulk` (`6818-6829`), `POST /api/portfolios/delete-bulk` (`9082-9093`) and `DELETE //api/portfolios/{id}` (`9159-9163`). Body-supplied ids are not URL-constrained, so `{"ids": ["../../…"]}` deletes matching files anywhere the process can write. Same class in the dead-but-exported core helpers (`_load_result` `core.py:119-123`, `_optimization_path` `126-130`, portfolio read/delete/compare `2304-2340`; create-side name→filename `2108-2109, 2295`). The correct pattern exists in-repo — `_resolve_grid_result_path` (`core.py:2365-2381`, regex whitelist + resolved-parent equality) — and should be applied everywhere. *(Reference V2 verified and merged.)*
- **[major] ID-keyed rename/read endpoints lack whitelists; whole-file non-atomic rewrite.** `rename_backtest` writes `BACKTESTS_DIR / f"{backtest_id}.trades.json"` from a URL id with no `[A-Za-z0-9_-]+` check and round-trips the entire result through `result.save()` to change one notes field (`viewer/main.py:6921-6936`) — a mid-write failure destroys the artifact (no temp-file+rename; same pattern in the legacy `core.py:1074-1092`). `get_portfolio`/`rename_portfolio` (`9096-9131`) and `compare_portfolios` (`8741-8744`) build paths from raw ids. `create_portfolio` itself sanitizes correctly via `_portfolio_identifier` (`5089-5092`, verified slug+timestamp).
- **[major] Signal-replay reads an arbitrary client-supplied CSV path.** `run_backtest_from_signals` calls `load_signals_from_csv(file_path)` (`viewer/main.py:8485`), echoes parse errors (`8487-8489`) making it a found/not-found oracle, and persists the expanded path into metadata (`8519`). Restrict to an uploads/import directory. *(Reference V4 verified.)*
- **[major] Resample spawns a subprocess on an unvalidated client path.** Live `POST /api/data/resample` passes `payload.source_path` straight to `data_tools.resample --source` (`viewer/main.py:10381-10409`); legacy twin `core.py:4673-4701`. Validate under `DATA_DIR` and require a supported regular file. *(Reference V5 verified.)*
- **[major] Unauthenticated LLM code-execution pipeline with no input bounds.** `agent_convert` forwards arbitrary user `code` to `run_auto_agent` via `_launch_agent_task` (`viewer/main.py:10288-10333`), which generates/executes-writes strategy files; no size cap (memory + persisted into task config/SQLite). Provider validation uses local `AGENT_CONVERT_PROVIDER_NAMES = ("nim", "copilot_sdk")` (`main.py:164, 10295`) while the dead core twin validates against `SUPPORTED_LLM_PROVIDER_NAMES` (`core.py:4611-4626`) — same route, two contracts.
- **[minor] Subprocess argument-injection surface.** Client strings flow into `runner.cli` argv (`viewer/main.py:7860-7894`; legacy shape `core.py:1801-1866`) — dash-prefixed `symbols` entries could parse as flags. Partially mitigated: `runner/cli.py` resolves `engine` via `get_engine(args.engine)` lookup rather than import. Low likelihood; cheap per-field validators close it. *(All `subprocess` calls use list argv — no shell injection found; panel-confirmed.)*
- **[minor] Error handlers echo internal exception text.** `detail=str(exc)` in many handlers (`viewer/main.py:6493, 6518, 6901, 6936, 6997, 8123, 9123, 9501, 10764`) leaks filesystem paths/library internals; the global handler (`1056-1069`) sanitizes correctly — standardize on it.
- **Positive controls to preserve (not defects):** log reader containment `resolve()+is_relative_to` (`viewer/main.py:10721-10730`); matrix filename guard (`6521-6525`); SPA static middleware containment (`1004-1005`); grid-results whitelist (`core.py:2365-2381`).

### 4.2 `viewer/comparison.py`

- **[major] `compare_backtests` silently ignores everything past the first two strategies.** Docstring promises "2+ backtests" (`comparison.py:139-160`), the live endpoint forwards N ids (`viewer/main.py:7638-7678`), but the combined view uses only `strategies[0]`/`strategies[1]` (`comparison.py:162-164`): a 3-way compare renders a plausible "combined portfolio" of A+B only, no marker. Missing ids are also skipped silently (`main.py:7648-7649`). Implement true N-way blending or reject ≠2. *(Reference v14/v3 merged; r2/r3 agree — major.)*
- **[major] `pearson_correlation` has no length-equality/finiteness guard.** `comparison.py:38-50` — `n = len(x)` drives means/covariance while std terms sum their own lists; mismatched lengths yield silently wrong coefficients, and empty `y` raises `ZeroDivisionError`. Latent today (callers pass aligned series) but it is public module API. *(Merges r2 F-c2 and reference v12; rated major as latent-public-API, above reference's minor.)*
- **[minor] Alignment is exact-string timestamp intersection.** `comparison.py:53-60` — `Z` vs `+00:00` vs epoch formats never intersect ("No overlapping dates" for perfectly overlapping data); duplicate timestamps collapse last-wins. *(r2 F-c3, reference v15.)*
- [minor] Degenerate normalization produces a silently flat combined curve. `comparison.py:72-73` — first value ≤ 0 ⇒ every point of that leg becomes `1.0`; combined stats (Sharpe 0, DD 0) look legitimate. NaN/error marker would be honest; also undocumented that normalization is a synthetic 50/50 rebalance-at-first-overlap. *(r2 F-c4, r3 #2, reference v11/v16.)*
- [minor] Near-zero previous values produce astronomically large returns. `comparison.py:20` guards only exact-zero denominators; a near-zero prior equity dominates Sharpe/Sortino/correlation downstream. No winsorization/floor. *(r3 only.)*
- [minor] Sharpe/Sortino conventions diverge from canonical runner stats. Population ÷n std annualized √252 assuming daily observations (`comparison.py:93-97`) on what are usually intraday per-bar returns (inflated ≈√24); Sortino downside-dev divides by count of negative returns, not all periods (`121`); `runner/stats.py` deliberately differs (its `236`, `494`, `511`). Also: net-return ternary at `116` is dead (`88` returned early); `overlapping_days`/`start_date`/`end_date` (`133-135`) come from raw date strings; max-drawdown here is negative-signed while `/api/backtests` exposes positive `max_drawdown_pct` (`main.py:2263`) — cross-endpoint unit inconsistency. *(Merges r1 #4/#6/#7, r2 F-c5/F-c6, r3 #4/#5/#6, reference v13.)*
- Smells (uncounted): Duplicated Code — drawdown math re-implemented here (`25-35`), in `core.py:930-938` and `main.py:1745-1753` (three hand-rolled variants, subtly different sign/zero-guard semantics); Primitive Obsession/Data Clumps — `[time_str, value]` pairs and parallel `(dates, vals_a, vals_b)` clumps with no length validation before `zip` truncates silently (`63-77`); an `EquitySeries`/`AlignedSeries` type would kill the class.

### 4.3 `viewer/core.py` — correctness

- **[major] Orphaned statements prove mid-surgery breakage.** `core.py:1505-1506` — `comparison = compare_backtests(strategies)` / `return jsonify(comparison)`, unreachable after `get_backtest_ohlcv`'s return at `1502`, referencing undefined `strategies`. Remains of a deleted `/api/compare` handler; delete. *(r3 claimed this breaks import with NameError — refuted by direct read: the lines are indented inside the function. See §4.10.)*
- **[major] The entire Flask-compat route layer is dead code.** `_NoopRouteApp.route` is an identity decorator (`97-109`), `request` a raising placeholder (`81-86`), `send_from_directory` raises by contract (`93-94`), and nothing imports `viewer.core.app`. **Verified: 45 `@app.route` registrations spanning `~1003-4858` plus `main()` calling the always-raising `app.run()` (`4871-4888`) ≈ 2,300 dead lines**, duplicating live FastAPI routes path-for-path. Delete the layer, keep the imported helpers. *(Settles r1 "~700 lines" vs r2 "~2,300": r2 is right.)*
- **[major] Stats payload mislabels averages as extremes.** `"best_trade": raw.get("avg_winner", 0)` / `"worst_trade": raw.get("avg_loser", 0)` (`core.py:1132-1133`), duplicated verbatim in the live endpoint (`viewer/main.py:6982-6983`) — fixing one fixes nothing.
- **[major] Race conditions and unbounded growth on `_running_jobs`.** Plain module dict written from request and worker threads without a lock (`core.py:69, 1835, 1880-1887`; continued `main.py:7790, 7935, 8059-8096`; aliased into main at `main.py:209`); multi-key status/result/error updates interleave so readers see torn transitions; entries never evicted (leak); 8-char ids can hijack slots. The newer `DATA_TASKS` registry uses a lock (`2811-2812`) — the old dict is the leftover.
- **[major] Backtest watchdog cannot catch a hung child.** The Popen pump drains stdout until EOF (`viewer/main.py:7978-7999`) and only then reaches `proc.wait(timeout=300)` (`8019`) — the 300 s bound covers post-EOF teardown, not the run; a chatty-or-silent hung child runs forever. Watchdog must wrap the whole read loop. (Legacy `subprocess.run` paths do have working timeouts: `core.py:1792, 1874`.)
- **[major] Process-global stdout/stderr redirect inside a thread.** `redirect_stdout(handle), redirect_stderr(handle)` in `_launch_agent_task` (`core.py:4083-4096`) swaps `sys.stdout/sys.stderr` for the whole process while the agent runs — concurrent request logging lands in this task's file. Reachable only via the (currently dead) core route, but the thread-bound handler at `2935-2959` exists precisely to avoid this; use it.
- **[minor] Task-runner threads lack bottom-level `finally` for unexpected exceptions.** `_start_data_task` catches only `(subprocess.SubprocessError, OSError)` (`3401`); dukascopy/jforex inner loops catch narrow tuples (`3568`, `3793-3802`) — an unexpected exception type aborts remaining combinations or strands the task at "running". Daemon threads also orphan in-flight subprocesses at shutdown (`3413`, `3646-3648`, `4016-4018`); a shutdown during parquet write could leave truncated files passing the mtime+size cache signature (`390-397`). *(Merges r3 #7 and r1 #5/#6; SIGSTOP/process-group concern retained as hardening note.)*
- **[minor] Unbounded in-memory caches/registries.** `_OHLCV_DF_CACHE` grows without eviction for process lifetime, pinning whole DataFrames (`74-78`; eviction only when files vanish, `386-389`); `_running_jobs` never pruned; `DATA_TASKS` grows until manual DELETE.
- **[minor] User-controlled symbol interpolated into a glob pattern.** `core.py:385` — `rglob(f"**/{symbol}/{timeframe}.parquet")`; `*?[` symbols alter match sets. Validate `^[A-Z0-9.\-]+$`.
- **[minor] Inconsistent entry/exit time key ordering for the equity seed point.** `core.py:296` prefers `entry_time` for the initial-capital anchor while ordering uses `last_exit_time or entry_time` (`290,303`) — the capital point can land after/out-of-order vs the first trade and be collapsed by the last-wins dedupe (`273-278`). Also `_normalize_equity_points` silently drops `unix_time <= 0` points (`265`).
- **[minor] Regex-based indicator detection can double-count.** Presence fallbacks (`1225-1234`) add SMA(20)/EMA(12)/RSI(14) even when explicit matches extracted real periods (dedup keys include period) — cosmetic chart pollution; heuristics also break on nested parens/multi-line calls (`1176-1184`).
- **[minor] Replay/trade-context indicator computation is O(bars × indicators) per request.** Warm-up slice recomputed per request (`1441-1450`) plus Python-level row scan in `_indicator_to_points` (`1360-1366`).
- **[minor] Portfolio IDs collide within the same second.** `name-slug-%Y%m%d%H%M%S` (`2264`) + truncating `write_text` (`2295`) — second creation silently overwrites the first. Add uuid suffix (live `_portfolio_identifier` shares the property).
- **[minor] Dolthub pull and Dolthub status scan different roots.** `_DOLTHUB_REPOS = Path.home()/"data"/"dolthub"` (`1772`) vs `_scan_dolthub_repos` on `DATA_DIR/"dolthub"` (`4170`); `DATA_DIR` defaults to `<project>/data` (`shared/constants.py:39,107`), so the roots differ in the default deployment. Recalibrated to minor: both current call sites sit in the dead route layer (`1806`, `4233/4265`), so impact is latent until reused.
- **[minor] Legacy `get_log` builds `LOGS_DIR / log_name` with no containment** (`core.py:2558-2565`) — the live twin added `is_relative_to` (`main.py:10721-10730`), proof of a patched vulnerability never propagated back; latent in shared helper code.

### 4.4 `viewer/main.py` — correctness

- **[major] Unexpected exceptions strand tasks forever and leak job records.** The `_run()` closures catch only narrow tuples — `(subprocess.SubprocessError, OSError, RuntimeError, ValueError)` at `viewer/main.py:8082` and `(RuntimeError, ValueError, OSError)` at `8436`. A `KeyError`, `AttributeError`, or pandas internal escapes the thread: `_mark_task_finished` never runs, the task shows "running" indefinitely, and the job record leaks. Broaden to `except Exception` around lifecycle finalization (or `finally`).
- **[major] Win rate counts raw execution rows, not closed trades.** `_calculate_realized_live_pnl` counts wins/losses per round-trip close (`3703-3735`) but the denominator is `len(execution_rows)` (`3741-3743`) — every partial fill deflates win_rate; displayed on the live dashboard (`9595-9604`). Denominator needs closed-trade count. *(Single-reviewer (r1) but arithmetic verified in source.)*
- **[major] `asyncio.run()` in helpers one refactor away from the event loop.** `viewer/main.py:410` (`_resume_interrupted_tasks`, lifespan startup), `1148` (`_restart_interrupted_task`), `3381` (`_discover_live_accounts_payload`). All current chains run in worker threads where a fresh loop is legal; any future caller in async context raises `RuntimeError`, and each restart spins a throwaway loop. Replace with explicit thread-based invocation or native-async endpoints.
- **[major] Import-time monkey-patching of the legacy module.** `viewer/main.py:3194-3202` rebinds eight core privates (`legacy_viewer._create_task_record = …` etc.) so core's task engine gains persistence. Behavior depends on whether `viewer.main` was imported first; standalone `viewer.core` users silently skip persistence; the wrapper chain is invisible to static analysis. Inject the hook explicitly.
- **[major] Sync `async def` routes doing heavy blocking I/O on the event loop.** `data_overview` (`9953-9973`), `data_category_details` (`9976-10017`), `list_symbols` (`10767-10773`) run parquet-metadata scans synchronously; `get_benchmark` (`10776-10834`) loads/resamples DataFrames inline; trade-context/replay/compare (`7463-7678`) share the pattern. During any of these the whole server — WebSockets included — stalls. Sibling routes correctly use `asyncio.to_thread`; these were missed.
- **[major] 1 Hz live polling: N+1 DB access and per-second broker round-trips.** `_live_accounts_stream_payload` runs `get_latest_live_balance` per account (`3449-3469`) refetched every second per open WebSocket (`1255`, broker loop `1356-1390`); positions construct a fresh adapter per poll (`9333`); `GET /api/live/dashboard` serially awaits full stats+trades scans per account (`9594-9608`). Batch queries/gather concurrently; add jitter/cache/event-driven refresh — broker rate-limits are the failure mode.
- **[major] Hardcoded vendor coverage windows already stale.** `HF_INTRADAY_COVERAGE_END = "2025-07-31"`, `ALPACA_INTRADAY_START = "2025-07-01"` (`5931-5933`) drive `_auto_download_source_plan` (`5992-6054`): every range extending past the constant silently drops the HuggingFace leg to Alpaca — or to yfinance daily-grade data without creds, with only a log line. Move to config with explicit "coverage unknown" behavior.
- **[minor] 8-character task IDs.** `str(uuid.uuid4())[:8]` at `7768, 8238, 8602` (32-bit) — collisions cross-wire status polling; data-task endpoints already use full UUIDs (`10259`).
- **[minor] Shared task-dict mutation during serialization.** `_snapshot_task` returns a shallow copy (`2858-2860`); nested `progress_detail`/`progress_history` mutated by `_ensure_task_runtime_metadata` (`1522-1534`) while workers append — occasional doubled/lost history samples. Deep-copy or lock appends.
- **[minor] Filter helpers can leak non-numbers into comparisons.** `_safe_float(value, value)` default trick returns the original object on unparsable input (`2408-2422`), flowing into `<` in `in_range` (`2401-2406`) — currently shielded by pydantic-typed params; fragile. Relatedly, `_normalize_win_rate` divides anything in `(1.0, 100]` by 100 (`2233-2243`), so a stored 1.5 % silently becomes 0.15 % — a guess, not a spec; document.
- **[minor] Blocking file I/O in the WebSocket log tailer.** `task_log_socket` opens/reads/seeks synchronously every 0.5 s inside the async handler (`10877-10913`) — serialize risk under many viewers; move to `asyncio.to_thread`/watcher thread.
- **[minor] Live-stats drawdown/ordering robustness.** Peak/drawdown tracking assumes ascending balances (`3750-3759`); loaders do `ORDER BY timestamp ASC` today (`shared/database.py:2009, 2288`) — verified safe, add a defensive sort. Balance-series-only drawdown ignores equity spikes. Component win counting `> 0` weighted pnl (`8913-8916`) disagrees with realized-PnL fill handling (`3720-3723`); `recent_fills` sorted by ISO string (`9632`) — mixed offsets/precision would misorder; sort by the unix field.
- **[minor] Resource leaks / unbounded reads.** `pq.ParquetFile(parquet_path)` at `5458` never closed (fd pressure on catalog scans; use context manager); `get_log`/service-log search read entire logs then slice (`10714-10764`, `9419-9465`) — stream tails instead (reuse `_read_task_log`'s `max_bytes`). `_parse_iso_datetime_cached` LRU caches user-controlled strings incl. sticky `None`s (`1455-1471`, bounded 8192 — acceptable).
- **[minor] Silent fallbacks and magic caps.** Missing trade symbol falls back to `"SPY"` (`7478`); OHLCV payload caps `symbols[:10]` silently (`7631`); `_backtest_summary_from_meta` hardcodes `"net_profit": 0.0` (`2286-2294`) — 0 is a lie that survives arithmetic; duplicate filter params `drawdown_min/max` vs `max_drawdown_min/max` with silent precedence (`6703-6704`).
- **[minor] Portfolio dual-storage reconciliation is implicit.** Save writes SQLite record AND JSON non-atomically; snapshot prefers FILE with DB gap-fill (`1888-1920`) so a stale file shadows a newer DB row; rename writes both; documented nowhere.
- **[minor] Recovery path skips re-validation.** Persisted `query_timeout_seconds` re-read with only `_safe_int` default (`484`) — poisoned record injects out-of-range values post-restart (argparse downstream likely clamps).
- **[trivial] `_seed_default_live_accounts` is an empty stub** (`3519-3520`) — delete or implement.

### 4.5 Cross-file correctness

- **[major] Indicator points map to wrong bars on replay slices (label/positional index confusion).** `_slice_replay_ohlcv` returns `df.iloc[np.flatnonzero(keep_mask)]` — positional rows **preserving original labels** (`viewer/main.py:7419-7425`), e.g. labels `0, 37, 120…`. `_compute_indicators` (`core.py:1253-1297`) never resets the index, and `_indicator_to_points` iterates `series.items()` (labels) but indexes `df.iloc[i]` (positions) (`1360-1366`) — on non-contiguous slices, indicator points attach to wrong timestamps. The trade-context call site resets (`core.py:1449`), masking the bug there; the live replay chain (`main.py:7587-7589`) does not. Fix: `reset_index(drop=True)` before computing, or `.loc`. *(Reference v6 rated minor — raised to major: verified live data-corruption path in charts traders act on. See §4.10.)*

### 4.6 Operational/robustness notes (panel-verified, lower confidence)

- Progress persistence writes SQLite on every tick — progress callbacks fire per symbol and per intra-download percent (`core.py:3524-3531`), each tick taking `DATA_TASKS_LOCK` then acquiring another lock inside `_persist_task_snapshot` (`viewer/main.py:2908-2969`) — WAL churn and contention with 1 s pollers. Throttle (≥1 % delta or ≥500 ms).
- Cache constellation: seven module-global caches with paired locks and hand-rolled invalidate (`1239-1277`) plus core's DF cache; `_ensure_portfolio_meta_cache` releases before refresh (stampede window). One TTL-cache utility would remove ~300 lines.
- Startup blocks on `_resume_interrupted_tasks` (`594-595`) — bounded today (DB reads + thread starts) but startup latency scales with interrupted-task count; consider backgrounding.

### 4.7 Baseline smells (judgement calls; repo-standard overrides applied)

- **God modules / Divergent Change (critical-as-smell).** `main.py`: ≥10 unrelated reasons to change (backtests CRUD, matrix/grid, signal replay, CAPM, portfolios, live trading + deployments + WebSockets, services, verification, data management/downloads, logs, SPA middleware) in 10,913 lines. `core.py`: ≥7 (serialization, OHLCV caching, indicators, strategy registry, portfolios, task engine, data catalog, logs, frontend shim) in 4,888. Split routers/services (FastAPI `APIRouter`; task engine → own module).
- **Shotgun Surgery (major).** API-field changes require synchronized edits across `main.py`, `core.py`, `frontend/src/api/client.ts`, `frontend/src/api/types.ts` (`docs/ARCHITECTURE.md` documents the coupling — mitigates, doesn't excuse), plus the dead twins. The `best_trade` bug surviving in two files is the smell made flesh. Task-lifecycle changes touch four parallel key lists (`_task_config_payload` 2542-2585, `_build_task_from_record` 2671-2735, `_apply_recovery_data_metadata` 2738-2785, `_decorate_task_from_record` 2611-2668).
- **Duplicated Code (major).** Verbatim: `_safe_float` (`core.py:822-829` ≡ `main.py:1445-1452`), `_downsample_numeric_minmax` (`core.py:961-1000` ≡ `main.py:1670-1718`), plus a third min-max downsampler `_downsample_equity_curve_minmax` (`main.py:7017-7111`). Near-verbatim: optimization job body (`core.py:1904-2012` ≈ `main.py:8592-8695`), stats mapping (`core.py:1110-1145` ≈ `main.py:6955-6997`), benchmark buy&hold (`core.py:2609-2680` ≈ `main.py:10776-10834`), indicator normalization (`core.py:1413-1431` ≈ `main.py:7236-7288`), dukascopy genesis (`core.py:4654-4670` ≡ `main.py:10336-10352`), portfolio engines (`core.py:2108-2298` ≈ `main.py:8833-9068`). Internal: `_resolve_component_backtest_id` vs `_resolve_strategy_backtest_id` (`3996-4099` vs `4102-4169`, ~80 % shared), pause/resume/cancel triples (`10572-10630`), stage-reporting closures (`7902-7930` vs `8286-8311`), restart-config reconstruction (`387-409` vs `1125-1147`), 11-key recovery loops (`2570-2585` vs `2719-2733`), `maybe_fraction`≡`maybe_win_rate` (`2408-2422`), and the ~200-line dukascopy/jforex scaffolding twins (`core.py:3421-3649` vs `3652-4019` — parameterize the downloader callable). Drawdown: four implementations area-wide (§4.2).
- **Middle Man (major).** The alias wall: **96 verified** `… = legacy_viewer.*` assignments (`main.py:154-264`) plus forwarding wrappers (`rename_backtest_name` 6939-6941, `rename_portfolio_name` 9129-9131, `list_tasks` 10554-10556) and the redundant `_LEGACY_*` second block (`257-264`) — two names for every concept, updated on every core change.
- **Primitive Obsession (major).** Tasks are `dict[str, Any]` with stringly statuses compared at ≥12 sites, `threading.Event`/Popen/thread handles stored inside JSON-bound dicts, hand-rolled fingerprints (`2341-2350`, `1948-1964`), magic weight sentinel `0.000001` (`4679`); ~120 defensive `.get()` coercions. A `TaskRecord` dataclass separating runtime handles from payload kills the bug class (partial credit: `_PreparedDownloadRequest` `10031-10045` — extend it to the task side and the download kwargs clumps).
- **Long Function (major).** `run_backtest` `7736-8103` (~370), `run_backtest_signals` `8106-8455` (~350), `create_portfolio` `8833-9068` (~235), `_normalize_portfolio_payload` `4851-5086` (~235), `_compute_portfolio_contributions` `4538-4776` (~240), `_extract_ast_parameters` (`core.py:1578-1686`), `_start_jforex_task.run_download` (`3691-4014`).
- **Repeated Switches (minor).** Source dispatch recurs in `_missing_ohlcv_details` (`core.py:480-492`), `_download_task_labels` (`4350-4378`), `_task_cache_invalidation_reason` (`4388-4403`), `data_download` (`4505-4596`), `_auto_download_source_plan`/`_download_missing_ohlcv_data` (`main.py:5992-6161`), `_generic_download_command` (`10196-10221`) — lists already drift (`harvest` appears only in the invalidation map). One SOURCE_REGISTRY collapses them.
- **Message Chains / Feature Envy (minor).** `meta.get("data", {}).get(...)` ladders ~15× in each file; `_matrix_grid_row_with_trades`/`_backtest_compare_payload` reach deep into `result.meta.data[…][…]`; task-lifecycle functions exist to poke `DATA_TASKS` entries (`core.py:3080-3152`). Hide behind accessors/model methods.
- **Speculative Generality (minor).** Dead Flask layer (§4.3, counted there); `_seed_default_live_accounts` stub; `include_validation` pass-through (`10020-10028`); `_UVICORN_ACCESS_PATH_RE` parses uvicorn access-log format although the access logger is disabled five lines later (`924` vs `950-956`) — one mechanism is dead; eager `_strategy_payload` scans popped immediately by callers (`core.py:1689-1731`).
- **Mysterious Name (minor).** `legacy_viewer` (hosts the live task engine, not merely legacy), identity `jsonify` (`core.py:89-90`), `AGENT_CONVERT_PROVIDER_NAMES` gating the conversion endpoint, `legacy_risk_per_trade_pct` fields that are weights (`4486`), preview-vs-preview_only fallback pair, terse `dd`/`cov` in comparison.py.
- **Maintainability hot spots (ordered).** 1) The core↔main split itself (alias wall + monkey-patch = likeliest "works in dev, not prod" incident). 2) Task lifecycle spread over three layers (dict state in core, persistence wrappers in main, SQLite rows) — every stuck-task bug report traverses four hops. 3) Dual storage (JSON↔SQLite) with per-entity precedence differences. 4) Progress write amplification (§4.6). 5) Testing: pure functions well isolated (`_check_entry_range_coverage`, `_auto_download_source_plan`, `_calculate_realized_live_pnl`); endpoint handlers embed business logic inline until routers split. Add `python -c "import viewer.main"` to CI (the orphan fragment shows there is no import smoke test).

### 4.8 Fixed-checklist sweep (Python)

| Item | Result |
|---|---|
| Mutable default args | **None harmful** — `= {}`/`= []` on SQLModel request models (`main.py:698, 743, 759, 808-809`) are deep-copied per instance by pydantic; no plain-function mutable defaults found. *(Reference report's v1 flagged these as minor defects — contradicted by all three reviewers and by framework semantics; excluded.)* |
| Bare `except:` | None (all typed; broad `except Exception` flagged individually where it swallows task lifecycle). |
| Race conditions | Found: `_running_jobs` unlocked (above); `DATA_TASKS` locked except nested `pause_event` read-modify-write outside the lock (`core.py:4742-4745`, `main.py:10582-10586`); decoration-window tears (minor). |
| SQL string building | None — parameterized ORM/`shared.database` throughout. Balance/stats queries explicitly `ORDER BY timestamp ASC`. |
| Subprocess timeouts | Present on batch `run` calls (`core.py:1787, 1874`) but **ineffective on the live backtest pump** (post-EOF-only, §4.4) and absent on streamed task Popens by design (cancel-event governed). |
| tz handling | Consistently UTC-aware (`datetime.now(timezone.utc)`, `pd.to_datetime(..., utc=True)`; naive-input assumption documented at `core.py:252-255`). No tz-naive datetimes found. |
| Resource leaks | `_running_jobs` growth; `_OHLCV_DF_CACHE` unbounded; `pq.ParquetFile` handle (`main.py:5458`); full-log reads; daemon-thread orphans at shutdown. Log/task-file handles otherwise context-managed. |

### 4.9 Coverage

All four tracked files accounted for by all three reviewers (each read `comparison.py`, `core.py`, `main.py` in full; `__init__.py` scanned, empty — correct as-is). No findings dropped except strict duplicates (merged with all original citations preserved) and the claims refuted in §4.10.

### 4.10 Reviewer/reference disagreements resolved by reading the code

1. **Orphan fragment `core.py:1505-1506`** — r3 claimed top-level placement causing import-time `NameError` (backend fails to boot). **Refuted**: lines are indented inside `get_backtest_ohlcv`, unreachable but harmless at import. Kept at major (unfinished-refactor evidence), not critical.
2. **Dead Flask layer scale** — r1 "~700 lines" vs r2 "~2,300". Grep verified **45 `@app.route` registrations** spanning `~1003-4858` plus `main()`/`app.run()`; r2's scale adopted.
3. **CORS `*` + credentials behavior** — r2/r3 assumed browsers reject the combination (major); r1 claimed Starlette reflects any origin (critical). Installed Starlette 1.3.1 source confirms reflection when `allow_all_origins and allow_credentials`. **Critical adopted.**
4. **Strategy-path execution severity** — reference V3/V12 rated major; raised to **critical** given the verified live chain (`main.py:7752` → `core.py:206-207` → `runner.cli` argv) combined with zero auth and reflected-origin CORS.
5. **Indicator misalignment on replay slices** — reference v6 rated minor ("can misalign"); r1 rated major. Source walk confirmed the live chain (`main.py:7425` label-preserving slice → `core.py:1253` no reset → `core.py:1360-1362` label/positional mix). **Major adopted.**
6. **Shallow-copy cache poisoning (`main.py:8338-8359`)** — r1 flagged major-if-pandas<2. **Refuted/excluded**: `requirements.txt:39` pins `pandas==3.0.1` (Copy-on-Write default).
7. **Live drawdown ordering** — r1 hypothesized newest→oldest balance ordering producing garbage drawdown. **Partially refuted**: loaders order ascending (`shared/database.py:2009, 2288`); retained only as a defensive-sort minor.
8. **Dolthub root split severity** — r2 rated major; both call sites verified to sit in the dead route layer (`core.py:1806`, `4233/4265`), so recalibrated to **minor/latent** (divergence itself confirmed: `Path.home()/"data"` vs `DATA_DIR/"dolthub"`, and `DATA_DIR` defaults to `<project>/data`).
9. **Citation correction** — r2 cited the prefix-containment flaw at `core.py:2827-2836`; actual location is `core.py:4829-4834` (`2827` is a download-identity dict). Finding unchanged.
10. **Reference v3 (missing backtests silently omitted in `/api/compare`)** — verified (`main.py:7648-7649`) and folded into §4.2's pairwise finding. Reference v16 (undocumented normalization) folded into §4.2 normalization item. r1's self-withdrawn items (closure binding at `7929`, `asi8`, `"+N-1"` label, parse-cache "real issue") were verified withdrawn and excluded.

---

## tests-a

**Panel:** model ox-alpha; three blind, independent reviewers (r1, r2, r3), synthesized by ox-alpha.
Contradictions between reviewers (and against the gpt-5.6-luna reference report) were settled by reading the cited source under `tests/`, not by vote; every settlement is annotated inline. Findings marked *(verified)* were confirmed directly against the code during synthesis.

**Finding totals (this merged section): critical 4 · major 19 · minor 71 — 94 findings across 39 tracked files.**
Reviewer baselines for calibration: r1 = 0C/22Maj/47min, r2 = 0C/~9Maj/~40min (no explicit tally), r3 = 5Crit/24Maj/60min. Divergent severities are recalibrated inline with reasons.

**Coverage:** all 39 tracked files in this chunk were scanned by all three reviewers and are accounted for in the per-file table at the end. Files with no findings are listed as clean rather than dropped.

---

## Theme 1 — Silent-skip architecture: suite infrastructure (`tests/conftest.py`, `tests/orchestrator.py`, `tests/strategies/conftest.py`)

Three independent mechanisms can zero out large parts of the suite while runs stay green. All three reviewers independently identified this as the highest-impact structural problem in the area.

### tests/strategies/conftest.py

- **M1 — Major — autouse fixture silently skips entire strategy modules** *(verified)* — `tests/strategies/conftest.py:117-138`: `skip_when_required_data_or_dependency_missing` (`autouse=True`, module scope) skips every module whose parquet dataset under `DATA_DIR/ohlcv` is missing or lacks requested coverage. There is no marker, no aggregated warning, no strict-mode opt-out, and no surfaced skip count; on any machine without the local datasets the whole ~16-file regression block degrades to skips behind a green exit code, and `tests/orchestrator.py` reports the skip column without failing. Textbook "environment-dependent disabled tests" pattern. Fix direction (r1/r2 agreed): emit a session-end summary of data-skips or support a fail-on-missing-data env flag. (r1+r2+r3 concur; r1 cites `:100-138`, r2 `:79`.)
- **M2 — Major — coverage check unions extents, hiding gaps between shards** *(verified)* — `tests/strategies/conftest.py:87-114`: `_covers_requested_period` takes min(start)/max(end) across *all* candidate files, so shard A covering Jan–Mar plus shard B covering Dec satisfies an Apr–Nov request. Gated tests then fail deep inside engine code with confusing errors — or silently backtest clipped windows. Also asserted against the union, not the file actually used downstream. (r3 found it; corroborated by reference-report t5; consistent with r1's `:76-96` variant of the same observation.)
- **M3 — Major — swallowed exceptions convert data corruption into "skip"** *(verified)* — `tests/strategies/conftest.py:68-84` (`_path_time_coverage`, two `except Exception: return None, None` blocks): a corrupt/unreadable parquet (permission error, schema change like a renamed `timestamp` column) becomes "no coverage" → module skip, hiding data-corruption regressions as environmental unavailability. At minimum log the exception. (r1+r2+r3 concur.)
- **m28 — Minor — `1d` fallback counts any symbol-shaped file as coverage** *(verified)* — `tests/strategies/conftest.py:27-29,34-35`: for `timeframe == "1d"` any `<SYMBOL>.parquet` matches, and `_has_ohlcv_data` checks existence only (no readability/schema), so the skip gate can wrongly *not* skip on a junk file and defer failure into the test body.
- **m26 — Minor — redundant glob pattern** — `tests/strategies/conftest.py:24`: `root.rglob(f"**/{symbol}/{timeframe}.parquet")` — `rglob` already recurses; the `**/` prefix is a no-op that confuses readers. (r2 only; harmless.)
- **m27 — Minor — end-bound expansion heuristic is undocumented and partial-session-blind** — `tests/strategies/conftest.py:58-65`: date-only `end` gains one day minus 1µs; correct for UTC-daily bars, but for intraday timeframes a request `end=2025-06-30` passes although the last session may be partial; an end given *with* a time component excludes the rest of that day, surprising for inclusive-calendar reasoning. (r1 `:53-59` + r3 `:58-65`; same mechanism, merged.)
- Smell *(verified)*: the coverage/normalization logic re-implements domain logic that also exists in `data_tools/auto_download.py` (`_normalize_coverage_date`/bars-in-range) with subtly different semantics (union-of-extents vs per-file bar check) — already diverged; one shared helper would prevent drift (r3).

### tests/orchestrator.py

- **M6 — Major — layer names misrepresent what executes** *(verified)* — `tests/orchestrator.py:30-38`: `"validation"` → `tests/integration/test_ai_code_quality.py`; `"strategies"` → `tests/integration/test_strategy_pipeline.py`. Neither layer ever runs `tests/validation/**` (353+ lines) or the actual `tests/strategies/**` regression block. The real strategy suites land in the `"unit"` selection (`tests -m "not integration and not e2e and not ai_code"`) purely because they carry **no markers** — r3 verified via grep that no `tests/strategies/*` file sets `pytestmark` and `pytest.ini` registers only e2e/integration/unit/slow/ai_code. An operator reading `_print_summary` concludes the strategy and validation suites ran when they did not. Rename layers to match content or fix the mappings. (r1+r2+r3 concur — strongest consensus finding in the area alongside M1.)
- **M7 — Major — failed/broken layers render as zero-count rows** *(verified)* — `tests/orchestrator.py:63-76`: if pytest dies before writing the JSON report (plugin missing → usage error; unwritable report dir crashing the reporter after tests passed), `summary={}` and the layer reports `FAIL/PASS 0/0/0/0` derived solely from exit code, with no cross-check of `summary["total"]` against actual collection. Triage-active misinformation. (r1 `:63-66` + r2 `:47-53` + r3 `:63-66`; r3 additionally notes a green-looking zero-count row can hide a collection crash.)
- **m5 — Minor — `subprocess.run` without timeout** *(verified)* — `tests/orchestrator.py:55-60`: a hung layer (integration suite's 120 s-per-case polling × N cases, stuck Playwright launch) blocks the orchestrator forever. **Severity disagreement flagged:** the reference report rates the identical issue **major** (T3); the panel unanimously rates it minor because this is a manually invoked entry-point script, not a wired CI stage. The defect itself is real and all three reviewers + reference agree on the mechanism.
- **m6 — Minor — unguarded `json.loads` on the report** — `tests/orchestrator.py:65`: a truncated/corrupt report crashes the orchestrator mid-summary instead of being reported as a layer failure. (r1.)
- **m7 — Minor — `"error"` vs `"errors"` JSON key** — `tests/orchestrator.py:75` reads `summary.get("error", 0)`; depending on the pinned pytest-json-report version setup-error counts may be emitted as `"errors"` — worth verifying, else the Errors column is always 0. (r3; r2 notes the same mapping is at least undocumented.)
- **m8 — Minor — layer identity duplicated across the file; e2e browser pinned twice** *(verified)* — the layer→args mapping at `:30-39` must be kept in sync with `choices=` at `:97` and the order list at `:104` (Shotgun Surgery for adding a layer); separately `:36` hardcodes `--browser firefox` while `tests/e2e_frontend.js` picks its own browser — two sources of truth. Cosmetic layer-order note (r3 `:104`) folded here.

### tests/conftest.py

- **m2 — Minor — Playwright probe: cached transient failures + prose-based skip detection** *(verified; settles an r1/r2/r3 disagreement)* — `tests/conftest.py:34-55`: `_playwright_browser_launchable` is `lru_cache`d and launches a real browser once per name; a one-off zombie-lock/display hiccup is cached for the whole session (r3, r1). Skip gating keys off substrings `"executable doesn't exist"` / `browser`+`not found` (`:34-39`). **Recalibration note:** r2 rated the overall skip policy **major** ("a machine with broken Playwright shows a fully green e2e layer"), r1/r3 rated it minor. Reading the code settles it toward minor-with-teeth: when the probe returns False and the message does *not* match, `pytest_runtest_setup` (`:58-69`) does **not** skip — the test proceeds and fails loudly at browser launch, so unknown failure modes surface as errors, not silent skips. Only message-matching failures skip, which is the intended purpose; the residual risks are (a) transient failures cached permanently and (b) Playwright wording changes flipping intended skips into hard errors across versions. Hence minor, not major.
- **m1 — Minor — env pinning is order-sensitive and aims at the real data dir** *(verified)* — `tests/conftest.py:13-14`: `os.environ.setdefault("DATA_DIR", ...)` applies only when absent; if the machine pre-exports `DATA_DIR` (or `shared.constants` was imported earlier), data-dependent tests silently target another tree, while by default they target the repo's real `data/` — combined with the DB-writing migration tests (M8) test runs touch live workspace data. (r3 + r2, merged — same two lines.)
- **m3 — Minor — skip hint hardcodes firefox** — `tests/conftest.py:66-69` always suggests `python -m playwright install firefox` even when the resolved browser (`:17-31`) is chromium/webkit. (r3.)
- **m4 — Minor — speculative option-shape handling** — `tests/conftest.py:24-30` defensively handles list/tuple `browser` options the plugin never produces. (r2 `:22-26` + r3; unexercised branches.)

---

## Theme 2 — Tests that cannot fail: vacuous and conditional passing

### tests/test_api.py

- **C1 — Critical — six endpoint tests execute zero assertions when the store is empty** *(verified)* — `tests/test_api.py:38-48, 82-115, 129-136`: `test_get_strategy_detail`, `test_get_backtest_info`, `test_get_backtest_stats`, `test_get_backtest_trades`, `test_get_backtest_equity`, `test_get_portfolio_detail` all wrap their bodies in `if <list>:` with **no else** — on a fresh checkout, cleaned data dir, or broken DB init the core read API is never exercised yet reported green, leaving no trace in the skip count (unlike `pytest.skip`). `test_list_strategies` (`:27-36`) correctly requires `len(data) > 0`, proving the authors knew the pattern. (r3 critical, r2 major, r1 major; reference t6 agrees on mechanism at minor — panel keeps **critical**: this is the definition of "passes while the claimed behavior is unverified", and it spans the entire detail-API surface.)
- **m42 — Minor — benchmark tests codify ambiguity** *(verified)* — `tests/test_api.py:147-156,158-160`: `test_benchmark_valid_symbol` accepts 200-with-body *or* bare 404; `test_benchmark_with_dates` accepts `(200, 404)`. The endpoint can regress to always-404 and every benchmark test stays green. (All three reviewers; reference t7 concurs. Rated minor rather than major by panel consensus since a dedicated `test_benchmark_nonexistent` pins true 404 behavior at `:162-164`.)
- **m43 — Minor — double assertion guarantees churn** *(verified)* — `tests/test_api.py:58-65`: `test_get_strategy_detail_includes_parameters` asserts parameters equal `load_strategy_meta(...)` output *and* the literal `{"fast_length": 9, "slow_length": 21}` — the literal repins the test to one strategy's defaults on every `example_sma` edit. (r2.)
- **m44 — Minor — ambient environment oracle, no seeding/isolation** *(verified)* — `tests/test_api.py:18-23`: the fixture builds `TestClient` on the real `viewer.main` app against whatever the developer machine has in `data/`; no tmp `DATA_DIR`, no seeded records, so the suite is meaningful on one workstation and vacuous on another, and results shift as other tests write backtests (ties directly into C1's trigger condition). (r1 `:30-33` + r2 + r3.)
- **m45 — Minor — dead imports** *(verified)* — `tests/test_api.py:3-9`: `json`, `os`, `tempfile`, `patch`, `MagicMock` imported, never used — remnants of removed mocking. (r2+r3.)
- **m46 — Minor — opaque pin on `example_sma`** — `tests/test_api.py:52-53`: `next((item for item in strategies if item["slug"] == ...), None)` + `assert target is not None` gives no hint of available slugs when the strategy is renamed. (r3.)
- **m47 — Minor — `test_symbols_has_content` has no skip-guard rationale** — `tests/test_api.py:177-182` requires downloaded market data present and fails confusingly on fresh environments, inconsistently with the data-gated skips used elsewhere. (r3.)
- Smell: the `resp = client.get("/api/backtests"); backtests = resp.json().get("items", [])` preamble repeats five times (`:82-115`) — extract a fixture that creates a known-good backtest rather than discovering one from ambient state (r1+r2).

### tests/e2e_frontend.js

- **C2 — Critical — "No critical console errors" structurally cannot fail** *(verified)* — `tests/e2e_frontend.js:148-154`: collects `pageerror`s all run, filters favicons, prints up to three warnings, then asserts nothing ("We don't fail on console errors — just report"). A guaranteed-green row inflating the pass count; the name promises a check the body cannot perform. Either `assert(critical.length === 0)` or remove it from the tally. (r3 critical, r2 major, r1 major; reference t1 agrees on mechanism at minor. Panel escalates to critical per the "asserts nothing" criterion.)
- **C3 — Critical — conditional-pass tests: autocomplete and detail page** *(verified)* — `tests/e2e_frontend.js:92-102`: `'Symbol autocomplete works'` wraps its whole body in `if (await input.count() > 0)` with no else — if the modal from the previous test closed or the placeholder changed, the test silently passes having done nothing. Same pattern at `:107-116`: `'Shows stats and equity chart'` passes without navigating when zero backtests exist. Knock-on: `'Benchmark dropdown exists'` (`:118-123`) has **no `goto` of its own** and asserts selects/Benchmark text on whatever page the previous (possibly skipped) test left open — arbitrary pass/fail depending on data. (r3 critical covering `:92-116`, r1/r2 minor on the same sites; panel adopts critical — identical anti-pattern family as C1.)
- **M15 — Major — hidden cross-test state chains three tests** *(verified)* — `'Create Backtest modal opens'` (`:80-90`) opens the modal that `'Symbol autocomplete works'` depends on (comment at `:93`), and the detail-page pair (`:107-123`) shares navigation state; any earlier failure cascades misleadingly and violates test independence. (r3 major; r2 minor on the same mechanism — escalated because the chain crosses the C3 conditional boundary, making failures actively misleading.)
- **M14 — Major — blanket fixed sleeps throughout** *(verified)* — `waitForTimeout(2000)` ×9 (`:51,:65,:75,:82,:98,:112,:130,:140`…), `1000` (`:86`), `800` (`:98`), `3000` (`:112`): classic flaky timing — slow machines intermittently fail layout/text assertions measured after arbitrary waits, fast machines waste ~20 s/run; Playwright auto-waiting/`expect.poll` exists for this. (r3 major, r2 minor — kept major: nine sites across every test in the file makes the whole harness flaky-by-design.)
- **m71 — Minor — near-tautological text assertions** — `text.includes('Log') || text.includes('log')` (`:138-142`) matches "login", "catalog", "technology"; likewise the Portfolio check (`:128-133`). Pass on virtually any prose-bearing page. (r2.)
- **m72 — Minor — viewport-coupled layout threshold** — `width > 1400` (`:56`) hardcoded against the 1920 viewport (`:30`); smaller CI viewports fail for layout reasons unrelated to regressions. (r3.)
- **m73 — Minor — collects only `pageerror`, not console `error` events** — `:34` — despite the flagship test being named "console errors", further limiting the already toothless C2. (r2+r3.)
- **m74 — Minor — hygiene: cleanup and config** — `browser.close()` sits outside try/finally (`:157`; an IIFE throw relies on process exit to reap the browser), and `BASE = 'http://localhost:5556'` (`:9`) ignores env override. Homegrown runner also duplicates what the pytest-playwright layer provides and is invisible to `tests/orchestrator.py` summaries (framework-level duplication smell, r1+r2).

### tests/integration/test_strategy_pipeline.py

- **M11 — Major — import-time network discovery + module-level self-skip** *(verified)* — `tests/integration/test_strategy_pipeline.py:104-110`: `_DISCOVERY = _discover_strategy_matrix()` performs HTTP at **collection time** (even `pytest --collect-only` needs a live server), and any failure triggers `pytest.skip(..., allow_module_level=True)`. Combined with the orchestrator's skip-tolerant exit codes, the entire integration layer disappears on any machine without the viewer daemon; plain `pytest tests/` runs hit live localhost HTTP whenever a server happens to be up. Lazy session-scoped-fixture discovery preserves behavior hermetically. (r1 minor / r2 major / r3 major — kept major; the collection-time side effect is the structural hotspot all three identify.)
- **M12 — Major — `min_trades=0` reduces the pipeline test to schema checking** *(verified)* — `tests/integration/test_strategy_pipeline.py:155-159`: the sole assertion battery is `BacktestResultValidator().validate(result, BacktestExpectation(min_trades=0))` — stats-keys presence, finiteness, `sharpe ∈ [-2,10]` (>10 only a warning per `tests/validators/backtest_validator.py`), `exit_time >= entry_time`. A strategy producing **zero trades and flat equity** — the classic silent break — passes, while the test reads as functional coverage. **Severity disagreement flagged and settled:** r3 rates this **critical**, r1/r2 minor/major. Panel sets **major**: the critique is fully verified, but the test *is* honest about being a smoke layer (marked integration+slow, validator warnings visible), unlike C1/C3 where the test names promise verification it cannot perform. Recommended fix (all reviewers): require `min_trades >= 1` for a known-good subset of strategies in the fixed 2024-H1 AAPL window.
- **m59 — Minor — registry regression becomes a skip** — `:46-53,16`: `MIN_STRATEGIES = 5`; dropping to 4 discoverable strategies skips the whole module instead of failing — environment drift hides as "skipped". (r2+r3.)
- **m60 — Minor — evicted tasks burn the full timeout with a misleading error; poll loop not blip-resistant** — `:71-101`: a task absent from `/api/tasks` is treated as "not yet present" until the 120 s deadline, then reported "did not finish (last=unknown)" even though the real cause was eviction; `raise_for_status()` inside the loop (`:77`) turns a transient 502 into immediate abort instead of retry. (r2+r3; reference t3's "fixed sleep polling" note is superseded by this more precise version — the loop does use a `time.monotonic()` deadline, which r1 confirms.)
- **m61 — Minor — arbitrary alphabetical cap** — `:45,66`: `unique_names[:MIN_STRATEGIES]` restricts the matrix to the first five sorted slugs; adding strategies adds no coverage and which five run shifts with naming. (r2.)
- **m62 — Minor — fixed 2024-H1 window** — `:61-62`: data-dependent results drift as local datasets change (currently moot given M12's vacuity). (r3.)
- **m63 — Minor — discovery folds all errors into skip** — `:25-31`: `except Exception` around discovery converts auth/config errors into the same module-level skip (same silent-skip family as M1/M3; reference t2 concurs). (r1.)
- Smell: the `case` dict's eight keys repeated at `:113-127` want a small dataclass to prevent typos (r3).

### tests/e2e/lab/test_conversion.py

- **C4 — Critical — fallback validates a checked-in fixture instead of the system under test** *(verified)* — `tests/e2e/lab/test_conversion.py:74-88`: when `_extract_generated_code` yields nothing, the test substitutes `valid_trend_strategy.py` and validates *that*; the subsequent `assert generated_code.strip()` (`:80-82`) and `AICodeValidator().validate(...)` therefore pass even when the Auto-Convert pipeline produced **nothing** — precisely the scenario this flagship e2e test exists to catch. Extraction races, agent crashes, or regex misses all convert to green-by-fixture. Delete the fallback or assert extraction succeeded before validating. (r3 critical, r1 major, r2 major; reference T1 agrees at major — panel keeps critical: the test's sole purpose defeated by construction. Verified the fallback branch and the fixture path resolve to a real, valid file.)
- **m65 — Major→Minor — fragile output selector** *(verified)* — `:67-72`: `page.locator("div.font-mono").first` grabs the first monospace div on `/lab`; any other mono block above the agent output breaks extraction — which then routes into C4's fallback and masks the breakage as a pass. r3 rated major, r2 minor; panel sets **minor** only because its harm is entirely mediated by C4 — fixing C4 removes the masking, leaving ordinary selector brittleness. The two combine badly today, which is why both are kept.
- **m66 — Minor — task id scraped by regex from rendered body text** — `:61-65`: brittle to copy changes; `.first` on a body-wide match means a second matching hint flips which id wins. (r2+r3.)
- **m67 — Minor — extraction race** — `:74`: `output_text` captured immediately after "Starting strategy conversion agent" becomes visible while LLM generation may still be streaming into `output_area` — intermittent empty extractions push runs into the C4 fallback. (r1.)
- **m68 — Minor — hardcoded base URL, inconsistent convention** *(verified)* — `:12` ignores `os.getenv("BASE_URL")` honored by `tests/integration/test_strategy_pipeline.py:13`; running the layers against a non-default host silently breaks only one suite. (r1+r2+r3.)
- **m69 — Minor — screenshot naming collides and accumulates** — `:86,90`: names keyed on `int(time.time())` collide on retries within the same second; screenshots accumulate unbounded in `<repo>/artifacts/screenshots` with no cleanup. (r1+r2+r3.)
- **m70 — Minor — extractor reads disk paths found in UI text** — `:30-34`: `path_match` reads any `*.py` path echoed by agent output — surprising side effect in an "extractor" and a low-impact path-injection vector (the test would validate arbitrary readable repo files). (r3.)

## Theme 3 — Strategy regression template: 16 copy-pasted files under `tests/strategies/`

The family: `test_channel_breakout.py`, `test_gold_momentum.py`, `test_mean_reversion_rsi_bb.py`, `test_post_vix_dip_buy.py`, `test_short_term_reversal.py`, `test_trend_following.py`, `test_volatility_breakout.py`, `test_stat_arb_pairs.py`, `test_sector_rotation.py`, `test_cross_asset_momentum.py`, `test_pine_superosc_vumanchu.py`, `test_pine_supertrend_dema.py`, `test_pynecore_orb.py`, `test_pynecore_sma_crossover.py` (14 pure-template files), plus the divergent variants `test_qg_30min_es_breakout_strategy.py` and `test_custom_swings_supertrend.py`. Shared findings cited once at the template anchor (`tests/strategies/test_channel_breakout.py`) unless file-specific.

- **M4 — Major — golden-master snapshots are unfalsifiable and environment-coupled** *(verified)* — every file pins exact trade count and net profit with `math.isclose(..., abs_tol=0.50)` (anchor: `tests/strategies/test_channel_breakout.py:28-29,53-57`), captured from one run against a specific local parquet snapshot ("Validiert am: …"). Any data refresh, fee/fill change, library bump, or float summation-order drift flips these red with no signal whether strategy or data changed; conversely, because of M1 the tests simply don't run in most environments. Net effect: brittle-false-red or silently-absent. The `$0.50` tolerance on sums of dozens of float PnLs is tighter than float associativity across platforms; docstrings forbid changing the period ("NICHT verändern") without stating why, training maintainers to regenerate constants blindly. Per-file pins recorded by r1 for churn ranking (all major as instances of M4): gold_momentum `:31-32` ($15,439.32 / 29 trades), cross_asset_momentum `:30-32` (2-year window, 21 trades), mean_reversion_rsi_bb `:30-31` (**2 trades**, −$324.18), short_term_reversal `:30-31` (**1 single trade**, $2,441.70 — weakest possible regression signal), pine_superosc_vumanchu `:30-31`, pine_supertrend_dema `:30-31` (3 trades), post_vix_dip_buy `:30-31` (3 trades, losing), trend_following `:30-31` (2 trades), volatility_breakout `:30-31` (9 trades, losing), stat_arb_pairs `:30-31` (4-year window), sector_rotation `:30-31` (2023–2024, losing). The pynecore pair is rated **minor** by r1 because those modules are dependency-gated to skips without pynecore installed anyway (`test_pynecore_orb.py:29-30`, `test_pyneecore_sma_crossover.py:29-30`) — adopted.
- **M5 — Major — risk-limit gate is 1.5× looser than configured and divides by a hardcoded baseline** *(verified)* — anchor `tests/strategies/test_channel_breakout.py:65-76`: `equity = 100_000.0` hardcoded while `BacktestConfig(risk_per_trade_pct=0.01)`, assertion `<= 0.015` — a sizing bug inflating risk up to 50% passes, and the slack constant is unexplained. The check validates arithmetic against a locally simulated equity path, not that the engine actually sized positions off `sl_price`. Reference t16's note that the assertions omit point value/multiplier/currency folds in here as an additional reason the check may not exercise canonical sizing. (r1+r2 agree on mechanism; r2 adds the stale-equity ordering observation.)
- **M13 — Major — 16× duplicated template with proven divergent fixes (Shotgun Surgery)** *(verified)* — ~70 lines × 16 files duplicating fixture + four-test class; a fix must be hand-applied 16 times, and history proves the cost: `test_qg_30min_es_breakout_strategy.py:80` already switched to `backtest_result.meta.initial_capital` with a division guard while the other copies still hardcode `100_000.0` — concrete evidence of lagging duplicates. Extract one parametrized harness keyed on a `(strategy, symbol, timeframe, start, end, expected_trades, expected_profit)` table. Smell add-ons (r2): Primitive Obsession/Data Clump — bare module-level floats instead of an `ExpectedResult` record; `sys.path.insert(0, project_root)` boilerplate repeated in all files plus `tests/test_api.py:15` and others (20+ sites suite-wide; pytest rootdir should own it); `test_all_trades_have_sl_in_meta` duplicates the `sl is not None` check `test_no_trade_exceeds_risk_limit` makes anyway (r1).

File-specific findings:

- **m29 — Minor — wrong-symbol header suspicion on the QG variant** *(verified)* — `tests/strategies/test_qg_30min_es_breakout_strategy.py:8`: header claims `Symbol/Timeframe: XAUUSD / 1h` for an **ES (index futures)** breakout strategy, with `TEST_START = "2006-01-02"` (`:30`) predating plausible local dataset depth — constants look inherited from the gold_momentum template. If the data gate then skips (M1), the pins decay unobserved while the file appears maintained. Also the only member passing explicit `data_dir` into `BacktestConfig` (`:50`) while all others rely on ambient `DATA_DIR` (inconsistent isolation).
- **m30 — Minor — single-symbol validation defeats the strategy concept** — "Cross-Asset Momentum" (`test_cross_asset_momentum.py:1,28`), "Sector Rotation" (`test_sector_rotation.py:1,28`), and stat-arb **pairs** (`test_stat_arb_pairs.py:1,28`) all backtest a single AAPL series — no pairing/rotation/multi-asset logic is exercised; golden values encode degenerate fallback behavior, giving false confidence the core logic is regression-covered. (r1 per-file majors folded here as instances; r2 found the same independently.) Counter-note (r3): `test_stat_arb_pairs.py:59-62` uniquely checks `ZScore` in meta — good.
- **m31 — Minor — engine-contract divergence between pynecore and vectorbt siblings** — `test_pynecore_orb.py:52,59,71` and `test_pynecore_sma_crossover.py:71` guard `trade.meta or {}` while all vectorbt siblings dereference `trade.meta` directly (e.g. `test_channel_breakout.py:66`); if meta can be None in one engine it presumably can in the other — one of the two contracts is misunderstood. (r1+r2 concur.)
- **m48 — Minor — weaker risk-check variant silently bypasses NaN stops** *(verified)* — `tests/strategies/test_custom_swings_supertrend.py:130-144`: this variant `continue`s when `sl_price` is None *or NaN* (also re-importing `math as _math` inside the loop, `:136`), so trades without stops contribute zero risk assertions where siblings hard-assert — two different guarantees under the same test name across the family; NaN sl (a real bug symptom) escapes the gate. Same root pattern noted by reference t14/t16 territory but distinct site.
- **m49 — Minor — unseeded RNG fixtures that violate bar invariants** *(verified)* — `tests/strategies/test_custom_swings_supertrend.py:81-87,156-163`: `np.random.uniform` without seeding, sampled independently so `open < low` / `close > high` occur constantly (open∈[1800,2000] vs low∈[1700,1800]). Currently harmless (shape-only assertions), but any value-sensitive future assertion flakes, and ATR/stops paths may hit NaN nondeterministically across numpy versions. Seed like `_make_df` does in the intraday test and build self-consistent bars. (r2+r3 concur; reference t14 agrees.)
- **m50 — Minor — AST default-presence check is weak and call convention unrepresentative** *(verified)* — `tests/strategies/test_custom_swings_supertrend.py:101-121`: counts `defaults + kw_defaults > 0` — a single positional default passes while the message claims it protects the optimization UI; misses `ast.AsyncFunctionDef`; and `mod.generate_signals(df)` (`:88,164`) is invoked without the params dict production passes, exercising a call convention the engine may never use. (r2+r3 concur; reference t15 agrees.)
- **m51 — Minor — triple-pasted importlib loader inside the custom-swings file** — `:72-79,148-155,173-175` repeat the module-load block verbatim inside individual tests instead of a module-level helper/fixture (within-file duplication atop M13). (r2.)
- **m52 — Minor — helper contract doesn't match its use** — `test_intraday_volatility_breakout.py`: `_make_df(n_bars, start=…)`'s "one trading day" docstring vs callers passing 200–300 bars spanning multiple days (`:185,:429`). (r2.)

### tests/strategies/test_intraday_volatility_breakout.py

- **M9 — Major→Minor recalibration — "one trade per day" test passes vacuously when entries vanish** *(verified)* — `tests/strategies/test_intraday_volatility_breakout.py:233-236`: `days.nunique() == len(entry_indices)` holds at 0 == 0; there is no `assert entries.any()` guard, so a refactor killing all entries keeps the test green. r3 rates major, r1/r2 minor. Panel sets **minor**: agreed mechanism, but the sibling tests in this file do assert `entries.any()` and the file has strong adjacent coverage (real ATR-recurrence expectations, DST-aware session test, NaN-gap handling — all three reviewers rank it among the strongest files); the missing-guard fix is one line. Related documentation-level minors merged in: the comment claims a "2-day dataset" though 300 minutes from 16:00 spans one day (r3 `:205-212`), and the `entry_reference_price ≈ signal-bar high` pin at `:538-540` sits under `order_timing="next_bar_open"` (`:536`) — r1 finds the fill-reference semantics suspicious enough to warrant deliberate confirmation against the signal contract, while r2 notes both reference-price conventions can be legitimate under different modes; panel keeps it minor (confirm semantics, add mode-distinguishing messages).
- **m53 — Minor — duplicated assertion** *(verified)* — `tests/strategies/test_intraday_volatility_breakout.py:501-508`: identical `entry_reference_price == approx(df["close"].iloc[entry_idx + 1])` twice back-to-back; second occurrence dead weight (copy-paste remnant). (r1+r2+r3 concur.)
- **m54 — Minor — degenerate warmup in regime-filter test** — `:416`: `atr_regime_factor = 1e9` with only 60 bars of warmup exercises the degenerate path rather than the regime logic itself. (r2.)
- **m55 — Minor — repeated param-block duplication** — every signal test repeats the 5–7-line override block (`use_candle_close=False`, `entry_time="16:30"`, …) with values already diverging (`"18:00"` vs `"18:05"`) unstated; also `np.random.seed(42)` global seeding in helpers (r3 verified the OHLC invariant `low <= open/close <= high` holds — OK). (r1+r2.)

### tests/strategies/test_risk_parity.py

Panel consensus: best-written file in the group (seeded RNGs, PSD/symmetry checks, warmup-error path, threshold-boundary coverage). Findings are all minor:

- **m32 — Minor — crisis-regime assertion accepts either label** — `tests/strategies/test_risk_parity.py:87-98`: `assert regime in ("elevated", "crisis")` lets a regression collapsing non-normal regimes pass, while the very next test asserts de-risking only for "crisis" — the distinction is behaviorally relevant. (r1+r2+r3 concur.)
- **m33 — Minor — unbounded de-risking** — `:93-98`: the crisis adjustment asserts equity total strictly decreases with no lower bound — a bug zeroing all weights passes. (r3.)
- **m34 — Minor — heavy monkeypatch couples to implementation names** — `:169-178`: five internals of `run_backtest` mocked out; renames break with AttributeError rather than meaningful failures; the weight-sequence iterator yields only 3 dicts, so added intermediate rebalances raise StopIteration (loud, acceptable). No lighter-mocked complement exists in the module. (r1+r2 concur.)
- **m35 — Minor — string-prefix datetime filtering is brittle** — `:190-192`: `exit_time.startswith("2024-01-02")` breaks silently if timezone representation changes (`+00:00` suffix formats), surfacing as an opaque count mismatch later. (r3.)
- **m36 — Minor — classification margin near boundary** — fixed seeds make it deterministic today, but the normal/elevated margin at n=30 recent days is unverified. (r2.)

### tests/strategies/test_volatility_breakout_reference_logic.py

Right *style* of test per all reviewers (synthetic bars, exact assertions); two precision gaps:

- **m37 — Minor — asymmetric indicator keys suggest a typo'd key that happens to exist** *(verified)* — `tests/strategies/test_volatility_breakout_reference_logic.py:28-29`: LONG entry checks `sl_price`, SHORT entry checks `stop_loss`. Verified in source: both lines exist exactly as cited. If the strategy emits both keys, the assertions pass while pinning nothing about their consistency; one is likely a typo'd key that should fail. (r3; unique finding.)
- **m38 — Minor — exit timing unpinned** — `:25-30`: entry indices `[5, 8]` and directions exact (good), but exits only via `exits.any()` — a one-bar-late exit regression goes undetected. (r1+r3 concur.)
- **m39 — Minor — direct package import couples run to layout** — `:3`: `from strategies.volatility_breakout…` assumes repo-root `sys.path` insertion from elsewhere; standalone invocation from another cwd ImportErrors. Portability inconsistency with siblings that self-insert. (r2.)

---

## Theme 4 — Root-level account/API/agent/parity tests

### tests/test_account_discovery.py

- **M10 — Major→Minor recalibration — sys.modules surgery for the Alpaca SDK** *(verified)* — `tests/test_account_discovery.py:74-77`: three `monkeypatch.setitem(sys.modules, …)` stand-ins fake `alpaca`, `alpaca.trading`, `alpaca.trading.client`; works only because production resolves imports through exactly those names inside function bodies. r3 rates **major** (risk that a real SDK import earlier in the session leaks through and attempts live network auth), r2 minor (sturdy seam argument). Panel sets **minor**: verified the stub installs before `discover_alpaca_accounts("k","s",True)` runs in the same test, credentials are dummies, and the failure modes are ordering fragility/error — not silent false confidence; the test fails loudly if the fake misses. Residual findings kept: incomplete fake graph vs typical `alpaca.common` chains (r3 `:75-77`) and the obscuring `__import__("sys")` round-trip instead of plain `import sys` (r2).
- **m56 — Minor — happy path only** — discovery tested solely for success shape (`:59-82`); no API-failure, partial-accounts, or `is_live` coverage. (r2.)

### tests/test_account_routing_safety.py

Positive consensus: some of the best safety coverage in the area — duplicate-ID rejection, numerically equivalent ctids `"00123"` vs `"123"`, adapter-never-created invariant.

- **m57 — Minor — loop termination via patched global `asyncio.sleep`** *(verified)* — `tests/test_account_routing_safety.py:105-107` (patch), `:99-101,:139-148` (CancelledError propagation): patches `daemon.asyncio.sleep` — shared stdlib attribute reached through the daemon's import — to raise CancelledError on first call; the duplicate-detection assertion rides entirely on `adapter_creations == []`. Safe under pytest's single-threaded model, but mutates shared state mid-session and breaks confusingly if daemon pacing changes or validation ever moves after a startup sleep; internal cancellation handling would also spook `pytest.raises`. (r1+r2+r3 concur at minor.)

### tests/test_alpaca_account_routing.py

Scanned clean by all three reviewers — model file (mutant-killing rationale in docstrings, hint-block precedence, ambiguity refusal instead of fallback). Two diagnostic notes kept as minor: bare boolean tuple elements in assertions give poor swap diagnostics (`:23,:63,:82`, r3), and near-identical ACC{1..3} env dicts across five tests want a builder (r1 smell).

### tests/test_analyze_mt5_parity.py

Positive consensus: high-value parity checks (UTC-spelling normalization `:59-71`, Athens-local bucketing, exit-reason classification) asserted concretely. Minors:

- **m58 — Minor cluster** — `_trade()` helper hardcodes `id=1` (`:16-44`): harmless today since matching keys on (entry_time, direction), but a landmine if parity ever keys on trade id (r2+r3); `test_series_stats_handles_empty_and_quartiles` name overstates coverage — asserts n/mean/median, never touches quartiles (`:135-141`, r2); sharpe-candidates test checks existence + one ordering relation but no numeric ranges, so a systematic scaling bug (×√252 dropped everywhere) passes (`:143-171`, r3); Athens DST pinned only for winter dates — July would need +3 (`:115-122`, r3); post-construction mutation of `trades[i].pnl` stylistic (`:118-125`, r1).

### tests/test_auto_agent.py

Positive consensus: mocks at the right seams (LLM provider queue with loud exhaustion, subprocess), precise attempt/prompt assertions.

- **m63b — Minor — validation leg mocked away** — `fake_run_backtest` unconditionally writes `'{"status":"ok"}'` and returns 0 (`:137-142,:176-181,:210-220`): retry-on-nonzero-exit is covered but parsing/validation of real backtest output is not; a misparsed-output regression passes this suite. Accepted unit-scope trade-off wanting one integration counterpart. (r2; r1/r3 concur on fidelity limits.)
- **m64 — Minor — fixture encodes a divergent generate_signals contract** *(verified)* — `STEP_THREE_CODE`/`FIXED_STEP_THREE_CODE` return a **3-tuple** (`:51-65`,`:67-75`) while the platform contract enforced elsewhere in this area is 4 elements `(entries, exits, indicators, drawings)` (`test_custom_swings_supertrend.py:71,89`; `test_intraday_volatility_breakout.py`). Two fixtures teach two signatures; whichever is wrong is enshrined as valid by green tests. (r2; unique.)
- **m64b — Minor — brittle couplings** — persona-name (`:230-235`) and emoji-decorated log-line (`:195-200`) assertions break on copy edits; `fake_run_backtest` pasted identically in three tests wants a factory helper (`:137,:176,:210`); `cmd.index("--output")` raising ValueError inside the fake is a cosmetic crash mode (`:131-135`). (r2+r3+r1 merged.)

Reference-report t11 ("stale failed-attempt artifacts not asserted absent") was checked during synthesis: grep of `tests/test_auto_agent.py` shows only `result["attempts"]` assertions (`:149,:232`) and no artifact/cleanup handling — the hint is consistent with the file but no panelist flagged it and it describes absent negative-coverage rather than a defect in cited code; recorded here for completeness, not counted.

### tests/test_auto_download.py

- **M16 — Major — test title promises what the assertion cannot check** *(verified; reference-hint adjudication)* — `tests/test_auto_download.py:10-11`: `test_us_stock_intraday_still_uses_intraday_stock_plan` asserts `_auto_download_source("AAPL","1h") == "huggingface"` — the single-source label — while the title describes plan-splitting behavior implemented in `_auto_download_source_plan` (`data_tools/auto_download.py:353`, whose HF≤2025-07-31 / Alpaca≥2025-07-01 overlap window has **zero test coverage**). Verified both functions exist at the cited locations. **Adjudication:** none of the three panelists raised this; it comes solely from the reference report's chunk-2 list. It is verified true against the code (title/assert mismatch confirmed verbatim) and material (date-boundary logic untested), so it enters as major despite being a sole-source finding — the brief permits including verified reference entries. Companion minor: only index-CFD aliases tested (`:4-7`); the `_is_us_intraday_stock` guards and the six-letter-uppercase `_is_forex_symbol` classification edge (production risk flagged by r3 outside the area) are unpinned. Fix: rename/refocus the test on `_auto_download_source_plan`.

---

## Theme 5 — Shared-state, migration, and AI-quality gating

### tests/test_backtest_equity_curve_migration.py

- **M8 — Major — tests write to the developer's real application database with no isolation or teardown** *(verified)* — `tests/test_backtest_equity_curve_migration.py:23,:36,:86,:117,:162` call `init_db()` against the ambient `shared.database.ENGINE` (repo-default SQLite path); records inserted at `:62-64,:104-106,:149` persist forever (timestamped ids `:45,:88,:125` avoid collisions, ensuring silent accumulation), polluting `/api/backtests` listings consumed by the viewer and by `tests/test_api.py`, making suites order/history-dependent, and racing parallel CI on shared SQLite state. Standard fix: tmp_path-scoped engine (top-level conftest already pins DATA_DIR, so one monkeypatch away). (r1+r2+r3 concur — unanimous major.)
- **M17 — Major — the actual migration path is never exercised** *(verified)* — `:160-176` ("migration on existing table") never constructs a pre-migration schema lacking `equity_curve`; after any successful prior run created the column, the test re-verifies current schema state forever — a broken migration passes permanently once the column exists. Test names promise more than they deliver (misleading-green on the risky upgrade path). r1 rated the same mechanism minor, r2/r3 major; panel sets **major**: unlike a merely weak smoke test, the file's entire stated purpose is the migration and the setup guarantees it is untested after first success.
- **m75 — Minor — conditional tail assertion** *(verified)* — `:172-177`: `SELECT … LIMIT 1` guarded by `if result and result[0]:` — on an empty table the read-back claim is unchecked (same anti-pattern family as C1); also asserts a storage-format detail (JSON string) rather than behavior.
- **m76 — Minor — tz-naive equity timestamps** — `"2024-01-01T00:00:00"` naive strings (`:39-43,:120-123`) perpetuate the naive-time convention the team's own checklist flags.
- **m77 — Minor — microsecond-id collisions theoretically possible** — `%f` strftime ids (`:45,:88,:125`) collide within one microsecond; `uuid4()` idiomatic. (r3.)
- Smell: record-construction boilerplate repeated four times (`:34-60,:84-112,:115-157`) — a `make_record()` helper shrinks the file by a third (r2).

### tests/fixtures/* and the AI-quality chain

- **C5/M-misleading-fixture — intentionally_broken_strategy is fully valid → no negative control anywhere in the AI-quality toolchain** *(verified empirically by r2, corroborated by r1/r3 static analysis)* — `tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py:1-19`: verified the file defines all required methods with correct signatures, imports cleanly, uses no forbidden calls — its only "brokenness" is semantic (threshold 0.0 momentum). Consumed by the glob-driven positive gate at `tests/integration/test_ai_code_quality.py:16-20` which asserts `result.passed` for **every** fixture — so the suite demonstrates no rejection path whatsoever; `AICodeValidator` could regress to accepting garbage unnoticed. **Severity settlement:** r2 critical-by-implication (empirical PASS 1.0, "headline"), r1/r3 major, reference T2 major. Panel records **major**: the validator-gate blindness is severe, but it is a missing-negative-control defect (coverage gap) rather than a live test asserting false behavior — distinct from C1–C4 where named tests actively pass while broken. Kept prominent as the theme's headline. Fixes (all reviewers converge): add genuinely-invalid fixtures (missing `generate_signals`, syntax error, `eval` usage) asserted to FAIL, or move them outside the pass-gated glob and rename this one. Smell: Mysterious Name in the adverse direction.
- **M18 — Major — glob-driven parametrization + import-time mkdir can silently empty the matrix** *(verified)* — `tests/integration/test_ai_code_quality.py:9-11`: `FIXTURES_DIR.mkdir(parents=True, exist_ok=True)` at collection recreates a lost directory (side effect on the repo tree), after which `sorted(FIXTURES_DIR.glob("*.py"))` parametrizes **zero** cases and the headline test trivially passes; only `assert STRATEGY_FILES` in the average-score test (`:24`) catches emptiness, with a confusing message. Deleting fixture files leaves the AI-code gate green. (r1 major + r3 minor + r2 minor — kept major: the gate's subject matter makes vacuity costly; the mkdir side effect folds in here rather than standing alone.)
- **m78 — Minor — average-score test re-asserts validity, diluting its distinctive assertion** — `:23-34`: `assert all(result.passed …)` (`:33`) duplicates the parametrized test before computing the score; with all fixtures scoring 1.0 the `>= 0.7` gate (`:34-35`) currently cannot bind, and each fixture is validated twice. Failure messages report validity errors when the subject is the score. (r2+r3+r1 concur.)
- **m79 — Minor — valid_trend_strategy models state-based entries, not crossover events** *(verified)* — `valid_trend_strategy.py:17-18`: `entries = (fast_ma > slow_ma)` are boolean regions whereas the Pine source they mirror uses crossover events (`sample_strategy.py:10-11`); as the canonical blessed exemplar (and C4's fallback payload) it teaches generators that region-style entries are the house contract; combined with min_trades=0 (M12) nothing tests its trading semantics. r2 minor, r3 minor-with-notes, r1 clean — kept minor (acceptable as fixture; documented hazard).
- **m80 — Minor — sample_strategy.py is Pine Script under a `.py` name** — picked up by any `glob("*.py")` over `tests/fixtures/`; currently harmless (ai_generated_strategies is a sibling dir) but a `.pine` extension plus explicit read path would be safer. (r3; r1/r2 clean.)

---

## Theme 6 — Manual diagnostic script: tests/compare_metrics_quantstats.py

All three reviewers converge: this is a script, not a test, and it carries a stale production-bug narrative.

- **M19 — Major — manual harness that can never fail** — no `test_*` functions and all execution under `if __name__ == "__main__"` (`:203+` per r3's read; print-only ✓/✗ results at `:156-218`), so pytest never collects it and it **always exits 0** — even wired into CI it could never fail on metric drift between `runner.stats` and QuantStats. Inside `tests/` it risks being mistaken for coverage. (r1/r2 minor, r3 major; panel sets major — the entire file's protective purpose is illusory by construction.)
- **M20 — Major — stale "BUG CANDIDATE" Calmar narrative contradicts verified production behavior** *(verified against runner/stats.py by r3)* — `tests/compare_metrics_quantstats.py:105-112` computes `our_calmar_bugged = annualized_return / abs(mdd_fraction / 100)` labeled as the suspect, while r3 cross-checked production: `_max_drawdown_pct_manual` returns a fraction (`runner/stats.py:412-424`) fed directly to `_safe_calmar_ratio` (`runner/stats.py:143-144,:152,:161,:370-375`) which divides by the fraction as-is — i.e. production matches this script's **fixed** branch today. The kept-alive "bugged" narrative invites someone to "fix" stats.py into an actual bug. Delete the dead pipeline or convert the file into a real parametrized test with tolerances. (r1/r2 rated minor; panel sets major because acting on the stale comment would introduce a production regression — a hazard, not just rot.)
- **m81 — Minor — inconsistent rf conventions across sections under one column label** *(verified)* — main comparison passes annual rf with `annualize=True` (`:117-121`, documented at `:115-116`) while deep-dives pass `rf=daily_rf` (`:237,:270-271`); the printed quantities differ under identical labels and nothing asserts consistency between them (production convention matches line 117 per r3). (r2+r3 merged.)
- **m82 — Minor — dead code** *(verified)* — `daily_rf` computed and unused in `compare_scenario` (`:92`, triple-repeated at `:221,:249`); `make_equity_curve_objs`/`make_dummy_trade` never called (`:41-52`). (r1+r2+r3 concur.)
- **m83 — Minor — module-level global seeding churns all scenarios on reorder** — repeated `np.random.seed(...)` at module level (`:58,:71,:79`); prefer local `default_rng(seed)`. (r1+r3.)

---

## Adjacent-context observations (files outside this chunk's list, flagged for their owners)

- `tests/validators/code_validator.py:97-133` executes submitted/AI code via `subprocess.run([sys.executable, "-I", "-c", …], timeout=8)` — isolated-mode + timeout are good hygiene, but `-I` is not a sandbox (no network/socket or filesystem restriction); consumers treating this as safe execution of untrusted LLM output should revisit that assumption. (r2.)
- `tests/validators/backtest_validator.py:22-23,:55-61` treats implausibly high Sharpe (>10, "possible lookahead bias") as warning-only, and with `min_trades=0` defaults makes the validator nearly unfalsifiable as used by M12. (r2+r3.)
- `data_tools/auto_download.py:305` `_is_forex_symbol` classifies any six-letter uppercase symbol as forex (six-letter NYSE tickers historically exist) → wrong downloader; unpinned by tests. Production-side, outside this chunk. (r3.)

## Cross-cutting summary judgment

Strongest material in the area: `test_alpaca_account_routing.py` (model file), `test_account_routing_safety.py`, `test_risk_parity.py`, `test_auto_agent.py` (well-seamed mocking), `test_analyze_mt5_parity.py`, and the synthetic-bars style of `test_volatility_breakout_reference_logic.py` / `test_intraday_volatility_breakout.py`.
Weakest: the three-mechanism silent-skip architecture (parquet-coverage skips M1–M3, browser-probe skips m2, API-discovery module skip M11) that can zero coverage behind green CI; the conditional/vacuous test families (C1, C2, C3, m42, m75); the e2e fallback validating a fixture instead of the product (C4); the migration tests that never migrate and pollute the real DB (M8, M17); and the negative control that isn't one (C5-entry).
Dominant maintenance cost: the 16× copy-pasted regression template with divergent fixes (M13, M4, M5).

---

## Coverage statement — 39/39 tracked files accounted for

| # | File | Outcome |
|---|---|---|
| 1 | `tests/__init__.py` | Clean — empty package marker (required for `from tests.validators…` imports). All 3 reviewers. |
| 2 | `tests/compare_metrics_quantstats.py` | Theme 6: M19, M20, m81–m83 |
| 3 | `tests/conftest.py` | m1–m4 |
| 4 | `tests/e2e/lab/test_conversion.py` | C4, m65–m70 |
| 5 | `tests/e2e_frontend.js` | C2, C3, M14, M15, m71–m74 |
| 6 | `tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py` | C5-entry (Theme 5) |
| 7 | `tests/fixtures/ai_generated_strategies/valid_trend_strategy.py` | m79 |
| 8 | `tests/fixtures/sample_strategy.py` | m80 (+clean content check) |
| 9 | `tests/integration/test_ai_code_quality.py` | C5-entry, M18, m78 |
| 10 | `tests/integration/test_strategy_pipeline.py` | M11, M12, m59–m63 |
| 11 | `tests/orchestrator.py` | M6, M7, m5–m8 |
| 12 | `tests/strategies/conftest.py` | M1, M2, M3, m26–m28 |
| 13 | `tests/strategies/test_channel_breakout.py` | Template anchor: M4, M5, M13 |
| 14 | `tests/strategies/test_cross_asset_momentum.py` | Family M4/M5/M13 + m30 |
| 15 | `tests/strategies/test_custom_swings_supertrend.py` | Family + m48–m51 |
| 16 | `tests/strategies/test_gold_momentum.py` | Family (pin recorded in M4) |
| 17 | `tests/strategies/test_intraday_volatility_breakout.py` | M9 (minor), m52–m55 |
| 18 | `tests/strategies/test_mean_reversion_rsi_bb.py` | Family (pin in M4) |
| 19 | `tests/strategies/test_pine_superosc_vumanchu.py` | Family (pin in M4) |
| 20 | `tests/strategies/test_pine_supertrend_dema.py` | Family (headers verified consistent, r3) |
| 21 | `tests/strategies/test_post_vix_dip_buy.py` | Family (pin in M4) |
| 22 | `tests/strategies/test_pynecore_orb.py` | Family at minor + m31 |
| 23 | `tests/strategies/test_pynecore_sma_crossover.py` | Family at minor + m31 |
| 24 | `tests/strategies/test_qg_30min_es_breakout_strategy.py` | Family + m29 |
| 25 | `tests/strategies/test_risk_parity.py` | m32–m36 |
| 26 | `tests/strategies/test_sector_rotation.py` | Family + m30 |
| 27 | `tests/strategies/test_short_term_reversal.py` | Family (pin in M4) |
| 28 | `tests/strategies/test_stat_arb_pairs.py` | Family + m30 (+ZScore positive note) |
| 29 | `tests/strategies/test_trend_following.py` | Family (headers verified consistent, r3) |
| 30 | `tests/strategies/test_volatility_breakout.py` | Family (pin in M4) |
| 31 | `tests/strategies/test_volatility_breakout_reference_logic.py` | m37–m39 |
| 32 | `tests/test_account_discovery.py` | M10 (minor), m56 |
| 33 | `tests/test_account_routing_safety.py` | m57 |
| 34 | `tests/test_alpaca_account_routing.py` | Clean + 2 diagnostic minors (tuple asserts, env-dict builder) |
| 35 | `tests/test_analyze_mt5_parity.py` | m58 cluster |
| 36 | `tests/test_api.py` | C1, m42–m47 |
| 37 | `tests/test_auto_agent.py` | m63b, m64, m64b |
| 38 | `tests/test_auto_download.py` | M16 + companion minor |
| 39 | `tests/test_backtest_equity_curve_migration.py` | M8, M17, m75–m77 |

---

## Appendix A — Adjudicated disagreements (reviewer vs reviewer vs reference report)

Every contradiction encountered during synthesis, and how it was settled:

1. **test_api.py conditional skips** — C1 severity: r3 critical / r2 major / r1 major / reference t6 minor → **critical** kept (verified in source; spans six endpoint tests with zero else-branches).
2. **e2e_frontend.js assert-nothing console test** — r3 critical / r2 major / r1 major / reference t1 minor → **critical** kept (verified: no assert statement exists in the test body).
3. **e2e_frontend.js conditional-pass autocomplete/detail tests** — r3 critical / r1-r2 minor → **critical** kept (same anti-pattern family as #1; benchmark-dropdown knock-on verified at `:118-123`).
4. **test_conversion.py fixture fallback** — r3 critical / r1-r2 major / reference T1 major → **critical** kept (fallback branch and valid fallback file verified in source).
5. **intentionally_broken_strategy / AI-quality negative control** — r2 empirical-critical / r1-r3 major / reference T2 major → **major** recorded as coverage-gap defect, headline of its theme (rationale in finding text).
6. **strategy_pipeline min_trades=0** — r3 critical / r1 minor / r2 loose-major → **major** set: verified critique, but honest smoke semantics unlike C1–C4.
7. **strategies/conftest silent skip** — unanimous major → kept major (all three independently flagged; reference t5 corroborates the union-extents sub-mechanism at minor).
8. **conftest.py Playwright skip policy** — r2 major / r1-r3 minor / reference t4 minor → **minor**: code shows non-matching failures fail loudly rather than skip (settlement reasoning in m2). The reference's stale-cache concern (t4) is real and folded into m2.
9. **orchestrator subprocess timeout** — panel-unanimous minor / reference T3 major → **minor** kept with reason (manual entry-point, not a wired CI stage).
10. **orchestrator layer mislabeling** — r1-r3 major → **major** kept (reference has no equivalent entry; strongest consensus in area alongside #7).
11. **intraday one-trade-per-day tautology** — r3 major / r1-r2 minor → **minor** (recalibration rationale in M9; mechanism undisputed).
12. **account_discovery sys.modules stubbing** — r3 major / r2 minor / r1 clean → **minor** (fails loudly if the fake misses; not silent false confidence — settlement in M10).
13. **compare_metrics_quantstats "cannot fail"** — r3 major / r1-r2 minor → **major** raised (whole purpose illusory; M19). Calmar narrative: r1-r2 minor / r3 major-with-production-verification → **major** kept (M20).
14. **migration shared-DB writes** — unanimous major → **major** kept. Migration-path-never-exercised: r1 minor / r2-r3 major → **major** set (M17).
15. **golden masters & template duplication** — r1 files-per-file majors, r2 single group-level majors, r3 majors → consolidated M4/M13 majors; pynecore pair demoted to minor per r1's dependency-gating argument (adopted).
16. **Reference-only entries**: T2/t6/t7/t14/t15/t16/t5/T1/t1–t4 corroborated and absorbed above. **M16 (test_auto_download title/assertion mismatch)** entered from the reference list alone after verification against `data_tools/auto_download.py:343,353` — sole-source but confirmed. Reference t11 (auto_agent artifacts) inspected, consistent but absent from all three panelist reports; recorded uncounted in Theme 4.
17. **No reference claim was found false**, but several were materially mis-scaled relative to panel consensus; scale corrections above follow the code, not any majority.

## Appendix B — Method note

Synthesis rules applied: strict duplicates merged with all original citations preserved; mechanisms agreed by ≥2 reviewers merged into one finding with attribution; severities recalibrated only where reviewers disagreed or where reading the code showed a clear error of scale (each such case listed in Appendix A); nothing dropped except exact duplicates. Findings marked *(verified)* were checked directly against source during synthesis (files read in full or at cited offsets: `tests/conftest.py`, `tests/strategies/conftest.py`, `tests/orchestrator.py`, `tests/test_api.py`, `tests/e2e_frontend.js`, `tests/e2e/lab/test_conversion.py`, `tests/integration/test_ai_code_quality.py`, `tests/integration/test_strategy_pipeline.py`, `tests/fixtures/ai_generated_strategies/intentionally_broken_strategy.py`, `tests/strategies/test_channel_breakout.py`, `tests/strategies/test_custom_swings_supertrend.py`, `tests/strategies/test_intraday_volatility_breakout.py:495-544`, `tests/strategies/test_volatility_breakout_reference_logic.py`, `tests/strategies/test_qg_30min_es_breakout_strategy.py:1-60`, `tests/test_account_discovery.py:1-85`, `tests/test_auto_agent.py:40-74`, `tests/test_auto_download.py`, `tests/test_backtest_equity_curve_migration.py`, `tests/compare_metrics_quantstats.py:85-144`).

---

## Finding counts (merged section)

| Severity | Count | IDs |
|---|---|---|
| Critical | 4 | C1 (test_api conditional skips), C2/C3 (e2e_frontend.js assert-nothing & conditional-pass), C4 (test_conversion fixture fallback) |
| Major | 19 | M1, M2, M3, M4, M5, M6, M7, M8, M11, M12, M13, M14, M15, M16, M17, M18, M19, M20, plus the intentionally-broken-fixture negative-control entry (C5-entry) |
| Minor | 71 | m1–m8, m26–m39, m42–m47, m48–m55, m56–m58, m59–m70, m63b/m64/m64b, m71–m83, plus M9 and M10 (both recalibrated major→minor) and the two unnumbered test_alpaca_account_routing diagnostics and the test_auto_download companion note |
| **Total** | **94** | |

(M9 and M10 carry M-labels only because reviewer disagreement adjudications reference them; they are counted as minors per the recalibrations recorded in Appendix A. Per-file golden-master pins inside M4 count once under M4; the e2e_frontend.js conditional-pass bullet covers three test sites; the test_api condition-skip bullet covers six test methods; each bullet counted once.)



---

## runner

**Panel:** model `ox-alpha` · 3 blind, independent reviewers (r1, r2, r3) · synthesized 2026-08-24.
Merged from: `runner-r1.md`, `runner-r2.md`, `runner-r3.md` (same directory).
**Method:** the three reviews were merged; findings that agree were deduplicated. Where reviewers contradicted each other (or the weaker reference report `code-review-full-gpt-5.6-luna-2026-08-23.md`), the synthesis settled the dispute by reading the cited code under `runner/`, not by vote. Spot-checked during synthesis: lean_adapter import crash, grid_tester call signature, backtest_grid `--keep-indicators`, walkforward `attach_daily_equity` kwargs, engine.py concat/sizing/exposure lines, pynecore swallow paths, codegen rule sniffing, optimizer objective/study wiring, stats dual paths, matrix `time`/`date` key mismatch, cli date-range logic, auto_agent `Popen`, lean_converter pairing/PnL, portfolio_optimizer objective.
**Severity policy:** critical = broken outright or silently wrong financial results at scale; major = wrong results under realistic conditions, security exposure, or expensive structural traps; minor = limited-blast-radius defects and smells.

**Totals: 4 critical · 41 major · 105 minor (150 findings).** Of the 41 majors, 36 are bugs/correctness/security (F5–F40) and 5 are structural smells (S1–S5); criticals are F1–F4. Findings are numbered for auditability.

---

## Critical

- **F1 [critical]** `runner/engines/lean_adapter.py:28-33` — `LEAN_PROJECT_DIR = Path(os.environ.get(...))` at module level but `os` is never imported (imports at 15-24 are json/logging/subprocess/tempfile/datetime/pathlib). Importing the module raises `NameError`; `cli.get_engine("lean")` and `backtest_grid.get_engine("lean")` crash lazily, so smoke tests never catch it. LEAN engine is entirely unusable. Unanimous critical among reviewers; the reference report rates its duplicate (M1) major — panel keeps critical (module cannot be imported at all). *Verified.*
- **F2 [critical]** `runner/grid_tester.py:74` — `result = engine.run(config)` omits the required positional `strategy_path` (`engines/base.py` ABC signature). Every cell raises `TypeError`, caught by the broad per-cell handler (:87, :101) and recorded as `status:"error"` — the tool always produces an all-error matrix that is still persisted to JSON and SQLite as a completed run. Facets: `:77-85` reads `stats.get("sharpe", 0)` etc., but stats keys are `sharpe_ratio`/`net_profit_pct`-style — even with a correct call, fresh adapter results carry empty stats and the grid would advertise `success` with all-zero metrics (ref m11; r1 major, r2 minor); `:60` the resolved strategy path is never passed to the engine. Unanimous critical; reference M2 says major — panel keeps critical.
- **F3 [critical]** `runner/backtest_grid.py:183-184` — subprocess mode appends `--keep-indicators` to a `runner.cli run` command, but that flag is defined only on the `grid`/`matrix` subparsers (`runner/cli.py:437`, `:462`). Every `--subprocess`-mode cell dies with argparse error 2 ("unrecognized arguments"); the in-process path handles the flag correctly, so only the documented subprocess mode is broken. Found by r3 (sole reporter); *verified* against cli.py.
- **F4 [critical]** `runner/strategy_ir/codegen_vectorbt.py:64-95` — `_rule_from_text` maps prose to trading logic by keyword sniffing: "overbought"/"take profit"/"tp" → `(primary > 70)`, "oversold"/"stop"/"risk-off" → `(primary < 30)` (RSI-threshold guesses!), otherwise `primary > secondary` where primary/secondary are essentially the first two indicators. A stop-loss sentence in an *entry* condition generates RSI-oversold entries; generated logic routinely has no relation to the documented strategy, yet the generator is presented as deterministic. On an automated LLM→IR→code→execute money-adjacent pipeline this silently fabricates strategy logic. r2 critical, r1 major, r3 minor ("heuristic acknowledged in docstring") — panel keeps critical after verifying the mechanism; unmatched conditions fall through to `default_rule` instead of failing loudly.

---

## Majors — Theme A: cross-engine consistency (results are not comparable across engines)

- **F5 [major]** `runner/engines/vectorbt_adapter.py:407-417` (+ `:155-334`, `:332`) — multi-symbol portfolio accounting is fictitious: each symbol compounds sizing from its own per-symbol `equity` accumulator, but the persisted curve restarts at `initial_capital` and replays *globally sorted* trades, crediting PnL at `trade.entry_time`. Reported drawdown/Sharpe describe an equity path the simulator never traded; N symbols each compounding from full capital are summed far above any real portfolio (ref M5 corroborates the entry-time stamping). Same defect: `runner/engines/native_adapter.py:~148-160` (per-symbol TradeEngines, then `running += trade.pnl` across symbols) and `runner/engines/pynecore_adapter.py:162-165`.
- **F6 [major]** `runner/engines/vectorbt_adapter.py:444` — `total_bars = sum(1 for _ in all_trades)` persists the **trade count** as `BacktestMeta.total_bars`; native (`native_adapter.py:109,178`) and pynecore (`pynecore_adapter.py:78,184`) persist actual bar counts. Bar-coverage/turnover consumers get nonsense for vectorbt. (r2 rated minor; r1/r3 major — kept major: field semantics corrupted in persisted artifacts.)
- **F7 [major]** `runner/engine.py:158` vs `runner/engines/vectorbt_adapter.py:1285-1290` vs `runner/cli.py:363-365` — one flag, three meanings: the native engine feeds `--max-position-pct` to `RiskConfig.max_total_exposure` (defaulting to 5% when unset — a silent 20× sizing divergence), vectorbt treats it as a per-position notional cap with asset-class defaults (100%/20%), and the CLI help documents per-position notional. r2 adds (`engine.py:156-167`) that a hidden 1% risk config is constructed even when risk sizing is disabled. Related: `runner/engines/vectorbt_adapter.py:201-261` — the leverage branch logs "leverage used" but still sets `final_notional = required_notional` even when the position cap is exceeded but leverage allows it; the `if`/`elif` arms are behaviorally identical (:236-238), and a conservative `max_leverage=1` is silently overridden toward position-cap semantics (:207-208). r1 read the same block as dead conditional complexity (minor) — panel sides with r2's money-path reading.
- **F8 [major]** Same-bar stop/TP priority diverges: `runner/engine.py:613-618` honors configurable `same_bar_order` (default stop-first), while `runner/engines/vectorbt_adapter.py:1189-1199` and `runner/engines/signal_replay_adapter.py:346-352` hardcode stop-wins with no knob or intrabar refinement. Identical setups produce systematically different (more pessimistic) results outside the native engine — cross-engine comparisons, the platform's selling point, are not apples-to-apples.
- **F9 [major]** End-date boundary semantics diverge: `runner/engines/native_adapter.py:44-47` filters `timestamp <= Timestamp(end)` — a bare `YYYY-MM-DD` end drops the entire final day (midnight-exclusive); `runner/engines/pynecore_adapter.py:530-533` compares tz-aware datetimes against raw date strings with the same exclusive-end effect (plus reliance on pandas string coercion); `runner/engines/vectorbt_adapter.py:1060-1062` deliberately extends date-only ends to `23:59:59.999999` (but misses space-separated `YYYY-MM-DD HH:MM`). Identical invocations yield different datasets per engine, shifting walk-forward fold boundaries and grid cells. Sub-facets: `signal_replay_adapter.py:703-710` applies inclusive raw-compare end filtering; `engine.py:60-62` lets a resolution *coarser* than the timeframe pass through and later silently finds no intrabar data.
- **F10 [major]** Three execution-timing models coexist: next-bar-open (vectorbt `:549-556`, native), same-bar-close fills (`signal_replay_adapter.py:470-475` — mild look-ahead for intra-bar-stamped signals), and post-hoc restatement in pynecore: `_apply_risk_sizing` (`pynecore_adapter.py:604-648`) rewrites historical quantities/PnL after the run using a recomputed equity path, mixing accounting regimes (trades without `sl_price` keep original PnL but still advance equity, :615-617), and `_normalize_trade_exits` (`:550-573`) rewrites exit prices toward theoretical SL/TP levels whenever the CSV price crossed them — fabricated fills assumed tradable-through. PyneCore-reported results are a hybrid of engine output and homegrown simulation.

## Majors — Theme B: silent failure / misleading success

- **F11 [major]** `runner/engines/pynecore_adapter.py:99-106` and `:137-139` — if the `pyne` binary is missing or a symbol's run fails, the symbol is skipped with `logger.error(...); continue`; the adapter still returns a successful `BacktestResult` with zero trades. A broken install yields silently-empty results persisted to SQLite and ranked by optimizers. r2 rated this critical, r1/r3 major — panel settles at major (per-symbol degradation, not total corruption) and flags the disagreement. Related facets: `subprocess.TimeoutExpired` is not caught (`:129-135`), so one slow symbol aborts the whole multi-symbol run while every other failure path merely logs-and-continues; `all_stats = _parse_stats_csv(...)` is overwritten per symbol (`:150-151`) keeping only the last symbol's stats; a strategy whose sibling `_context.py` helpers aren't copied to tmpdir fails inside pyne and is swallowed by the same continue.
- **F12 [major]** `runner/auto_agent.py:798-822` — `_run_backtest` uses raw `subprocess.Popen` + `proc.wait()` with **no timeout**; a hung generated strategy blocks the conversion worker forever. Every other spawn site sets one (`pynecore_adapter.py:134`, `lean_adapter.py:112`, `backtest_grid.py:476`). Unanimous; also ref M7.
- **F13 [major]** `runner/optimizer.py:274-278` — no-trade trials return penalty `0.0` under `direction="maximize"`, which **beats** every honest trial with negative Sharpe; Optuna steers toward parameter regions that avoid trading rather than regions with small losses. Unanimous among reviewers; *verified* in the objective body. Related: `study.optimize(objective, ...)` has no `catch=` (`:299`, r2/r3) so any exception in a trial fails the whole study after N trials of compute; and `study.best_trial` (`:301`) raises a raw Optuna ValueError when all trials failed, unguarded here unlike `portfolio_optimizer`.
- **F14 [major]** `runner/walkforward.py:130-132` — calls `attach_daily_equity(result, start_date=start, end_date=end)` but the signature is `(result, start=None, end=None)` (`daily_equity.py:67-71`) → `TypeError` on the no-equity-curve path. Verified live crash. Compounded by F16 below (the parameters would be ignored anyway).
- **F15 [major]** `runner/walkforward.py:133` — merge order `{**compute_stats_for_result(result), **result.stats}` lets stale engine-provided/persisted stats override freshly computed canonical ones; `cli.py:216` and `backtest_grid` merge the opposite way, so fold IS/OOS sharpes come from heterogeneous sources depending on engine/entry point.
- **F16 [major]** `runner/daily_equity.py:67-76` — `attach_daily_equity`'s `start`/`end` parameters are never used in the function body; five call sites pass date filters expecting them to matter. Dead parameters that mislead callers and caused F14. (ref m1 corroborates.)
- **F17 [major]** `runner/stats.py:120-166` — metric definitions depend on environment and data shape: QuantStats path computes `total_return = (1+r).prod()-1` on resampled daily returns (:145) while the fallback uses equity endpoints (:153); annualization differs (`periods=252` vs inferred frequency); the manual Sharpe divides variance by n (population) vs pandas' sample std (:483-494); and when QS is installed but the daily series can't be built, metrics silently stay 0.0 because the fallback lives in the `else` branch only (r3). The same backtest JSON publishes different Sharpe/Calmar/total_return on different machines.
- **F18 [major]** `runner/backtest_matrix.py:90` — `_load_daily_rows` reads `row.get("time")`, but `daily_equity.build_daily_equity_curve` emits rows keyed `"date"` (`daily_equity.py:62`). The `time` column of `matrix_daily_equity.csv` is always empty; downstream allocation consumers keyed on it get blank timestamps. Found by r3 alone; *verified* both sides. Facet: `:72-95` join keys are rebuilt via `build_run_id` — undocumented coupling to grid naming.
- **F19 [major]** Fabricated/fallback data ranges: `runner/cli.py:113-116` returns `max(starts), min(ends)` without checking `max(starts) <= min(ends)` — disjoint ranges yield start>end flowing into configs/downloads instead of an error, and failed parquet reads silently shrink the range; when nothing is found, hardcoded `("2023-01-01", "2025-12-31")` masks missing data (same constants duplicated at `backtest_grid.py:217,221`). `runner/backtest_grid.py:211-222` additionally reads only `matches[0]` with no intersection logic — drifted duplicate of the CLI version (see Theme E). r1 rated the grid variant minor, r2/r3 major — panel keeps major for the fabricated-range + non-intersection combo.

## Majors — Theme C: engine.py (native TradeEngine) internals

- **F20 [major]** `runner/engine.py:874` — `merged_row = pd.concat([row, signal_row])` runs for **every bar** and concatenates two Series *vertically*, not element-wise: the result has length `len(row)+len(signal_row)` with duplicated labels. When the signals frame carries columns named like OHLCV columns ("high", "low", "open", "close" — common indicator names), `row.get(key)`/level lookups read from the stacked Series ambiguously, and mismatched dtypes produce an object-dtype Series that makes numeric comparisons fragile. r2 rates this critical; r3 major (correctness + per-bar performance); r1 flags only the performance aspect as minor. Panel keeps **major**: label collisions corrupt lookups only when names collide and `.get()` still returns *some* value — but the corruption is silent when it happens, so this is at the top of the major band.
- **F21 [major]** `runner/engine.py:743` + `:302-353` — sizing compounds mark-to-market noise: `self.equity` includes unrealized PnL of open positions and `_calculate_position_size` treats it as spendable cash; `max_value = equity * max_leverage` overstates buying power while positions are open. Also a hidden parameter protocol (`fixed_risk_amount`, `tick_value`, `lot_step`, …) read straight from `parameters` with per-call defaults.
- **F22 [major]** `runner/engine.py:159` — `max_positions` is read from strategy `parameters`: user strategy params silently leak into engine-level risk policy (`int(float(self.config.parameters.get("max_positions", 5) or 5))`). Undocumented cross-talk.
- **F23 [major]** `runner/engine.py:362-389` + `:460-473` — MARKET orders are position-sized twice (optimistically at creation on signal-bar close, then re-sized at fill on next-bar open), while LIMIT/STOP/STOP_LIMIT orders keep creation-price sizing — inconsistent sizing semantics across order types (ref M4 corroborates the market-order half). Also O(n) intrabar replay: `for _, row in window.iterrows()` re-filters the whole pre-sorted intrabar frame per bar (`engine.py:576-600`) instead of searchsorted — quadratic in practice.
- **F24 [major]** `runner/engines/vectorbt_adapter.py:118-123` — when a symbol has no data, `_process_symbol` returns empty trades indistinguishable from "engine produced nothing": the run succeeds with zero trades and no error while other symbols produce results (silent partial backtest). Related: `native_adapter` silently skips no-data symbols into an empty-but-successful result.

## Majors — Theme D: security / trust boundary

- **F25 [major]** `runner/auto_agent.py:215-273, 225-255` — LLM-generated Python is written to `strategies/<slug>/<slug>.py` and immediately executed via `python -m runner.cli run`; there is no static screening (no AST allow-list, no import restrictions) before execution with repo privileges (including `.env` credentials). Combined with viewer-triggered invocations this is an unauthenticated RCE surface if the lab endpoint is reachable; it's the feature's documented purpose, but it deserves an explicit security decision. Facets: retry loop leaves the last broken generated file on disk inside `strategies/` where the registry picks it up as runnable (r2); slug collision silently overwrites another strategy's files (`:282-292`, r1 minor/r2 major — merged here); child env forwards full parent environment including API keys (`:799`).
- **F26 [major]** `runner/engines/pynecore_adapter.py:251-273` — `_compile_helper_namespace` `exec()`s code extracted from strategy helper files **in the runner process** (not even behind the pyne subprocess); AST filtering permits arbitrary `Import` nodes wholesale, so imported-module side effects execute with full privileges. Second unguarded exec surface alongside F25. (r1/r3 minor, r2/ref major — panel keeps major given the auto-agent pipeline feeds this chain; flagged.)
- **F27 [major]** `runner/llm_providers/copilot_sdk_provider.py:301-312` — `PermissionHandler.approve_all` grants every tool permission (file edits, shell) requested by the SDK session for an LLM call that needs only text generation; combined with generated-code execution this widens prompt-injection blast radius. Deliberate but undocumented posture. (r3 minor, r2/ref major.)

## Majors — Theme E: per-module correctness (money-path and data-integrity)

- **F28 [major]** `runner/engines/lean_adapter.py:96-104` — invokes `python -m lean backtest ...`; the pip QuantConnect CLI installs a `lean` console script, not an importable `lean` module. Even with F1 fixed, the subprocess fails with "No module named lean" on standard installs. Combined with F1 the LEAN execution path has plausibly never run.
- **F29 [major]** `runner/llm_providers/copilot_cli_provider.py:65` — the entire composed prompt (full strategy source + system prompt) is passed as a single **argv element** (`-p <prompt>`): Windows ~32k-char limit fails large conversions outright; on Unix the prompt (source code, credentials context) is world-readable via `ps` while the child runs.
- **F30 [major]** `runner/optimizer.py:544-552` + `:634-654` — risk-parity covariance mode prefers returns derived from **per-symbol trade-PnL pseudo-equity** (`_asset_returns_frame_from_base_result`) over actual asset prices; correlations measure strategy-trade overlap, not asset co-movement, yet drive covariance weights and universe selection — and having ≥2 such columns *prevents* the market-data path from running. Methodologically weaker input is the primary one. Unanimous among reviewers; *verified* the preference order at `:544-552`.
- **F31 [major]** `runner/walkforward.py:267-310` — "training" folds reuse one fixed parameter set; no selection happens on IS data, so `is_sharpe` vs `oos_sharpe` is two slices of one backtest. Validates *stability*, not anti-overfitting robustness the README implies — an expectation gap on a headline feature (r1 calls it minor/not-a-defect, r3 major — panel keeps major because the summary invites the wrong trading conclusion; flagged).
- **F32 [major]** `runner/converters/stockkit_converter.py:118-135` — TP1 partial-exit split invents `tp1_pnl` from prices with a residual `runner_pnl`, while `trade.pnl` keeps the CSV's authoritative total; parts won't sum to the total whenever slippage/fees differ per leg, corrupting per-exit analytics while totals look right. The invented TP1 leg also carries `time=""`, violating the ISO timestamp contract and dropping TP1 PnL from daily aggregation (`daily_equity.py` skips empty times).
- **F33 [major]** `runner/converters/lean_converter.py:81-97` — trade pairing assumes strict alternating Entry→Exit per ticker in time order; partial fills, scale-ins, and unrecognized tags produce skipped exits or pair an **entry as the previous trade's exit**, fabricating trades/PnL. No quantity-match or time-order validation (`qty = abs(entry["quantity"])`, :102). Unanimous; *verified* the pairwise consume at `:87-97`.
- **F34 [major]** `runner/converters/lean_converter.py:73-77, 104-111` — PnL looked up from LEAN's `profitLoss` map keyed by exact exit-timestamp string; r3 raises that LEAN's profitLoss may be portfolio-cumulative (making each "trade pnl" a portfolio snapshot) — unconfirmed but flagged; on lookup miss the price-delta fallback ignores exit-leg quantity and fees. Either way converted per-trade PnL is unreliable.
- **F35 [major]** `runner/engines/vectorbt_adapter.py:201-261` (consolidated sizing semantics; see also F7) — sizing updates equity only on trade close (`:332`) while the curve replays interleaved trades; the leverage branch's identical `if`/`elif` outcomes hide a cap that isn't applied; conservative `max_leverage` is silently overridden toward position-cap semantics. Money-path logic whose branches don't mean what they read as.
- **F36 [major]** `runner/portfolio_optimizer.py:244-249` — portfolio "sharpe" is a raw mean/std of per-point returns: not annualized, no risk-free treatment, epsilon denominator distorts returns when equity near/below zero (sign flips) — incomparable to `stats.py`'s Sharpe though both surface as "sharpe" to users (ref m6 corroborates).
- **F37 [major]** `runner/backtest_grid.py:~600-613` — with `max_workers>1` the default in-process path runs cells in a ThreadPoolExecutor and each cell calls `ensure_backtest_data` concurrently: races on downloads/parquet writes to shared cache directories (checklist: race conditions on shared state). Found by r2 alone; mechanism concrete, kept as reported.
- **F38 [major]** `runner/backtest_grid.py:277-292, 425-440` — skip-existing "rehydrate" runs before validating that the existing cell matches current parameters: resuming a grid after changing `--params` silently serves stale results as fresh. (The block is also copy-pasted across both run paths — see Theme E minors.)
- **F39 [major]** `runner/engines/signal_replay_adapter.py:501, 573-585` — replay sizing uses `initial_capital + realized_pnl` (floor $1), ignoring unrealized drawdown of open positions → optimistic sizing during drawdown; the final equity curve is rebuilt in exit order while sizing consumed PnL in signal order — sizing-time and reporting-time equity paths differ. Design-inherent but undocumented distortion (r2 majors; facets kept together).
- **F40 [major]** Stats-key contract break: `runner/backtest_grid.py:113` reads `net_profit_pct`, which is **not** a `BacktestStats` field (r3 verified against `shared/models.py:567+`); the fallback chain ends in `total_return`, so the column carries total_return values under the wrong name and downstream consumers (e.g. `backtest_matrix.build_strategy_overview`) treat it as a distinct metric. Symptom of the area-wide `sharpe`/`sharpe_ratio`/`total_return` alias chaos (see Theme minors).

## Structural majors (baseline smells charged at major)

- **S1 [major]** `runner/engine.py:128-903` — `TradeEngine` mixes order lifecycle, risk sizing, execution-model docs, progress reporting, indicator/drawing presentation, and meta serialization; execution changes and presentation changes both rewrite the money-path file (r1 major; r2/ref minor — recalibrated up per the rewrite notes' own ⚠️ review requirement for money-path code).
- **S2 [major]** `runner/engines/vectorbt_adapter.py` (1,296 lines) — gravity well of the area: `_process_symbol` (~230 lines) mixes loading, normalization, simulation, sizing, meta assembly, and the module adds viewer-layer indicator styling (`_supertrend_component`, pane colors, :596-761); three other modules copy fragments of it.
- **S3 [major]** `runner/engines/pynecore_adapter.py` ↔ `vectorbt_adapter.py` — wholesale duplication: `_serialize_meta_value` byte-identical (:436-462 vs :1252-1283), `_coerce_series`, `_normalize_trade_context` family, `_infer_asset_class`, `_load_ohlcv`; the private cross-module import (`from runner.engines.vectorbt_adapter import _normalize_drawings, …`, :32-36) cements the coupling; `_apply_risk_sizing` envies Trade internals. Every vectorbt change risks PyneCore drift.
- **S4 [major]** `runner/auto_agent.py` (843 lines) — five change reasons in one module: German prompt templates (:295-410), response parsing (:456-528), STRATEGY_META normalization (:531-584), artifact IO (:282-292), catalog-ranking heuristics (:56-82, :727-738), process control (:762-822).
- **S5 [major]** Grid subsystem triplication: `backtest_grid.py` (complete), `backtest_matrix.py` (orchestrator), `grid_tester.py` (broken, F2) are three parallel implementations of "run strategy over symbol×timeframe"; README documents `runner.grid_tester` as *the* grid tester, so the broken module sits on the documented golden path. Consolidation (delete/repair grid_tester) is overdue (recalibrated to major from r1's minor tag; r3/r2 concur on the substance).

## Minors — engines (native, vectorbt, pynecore, signal-replay, base)

- `runner/engine.py:780-786` [minor] — indicator dict entries are copied into `_signals_frame` by bare name: an indicator keyed `"entries"`/`"exits"` **overwrites the signal columns**; :785-786 then return the replaced columns as entries/exits. Vectorbt guards this with a separate indicator map (`vectorbt_adapter.py:503-510`); the native engine corrupts silently. Facet: `:781-783` inserts via `.values`, ignoring index alignment.
- `runner/engine.py:898-901` [minor] — end-of-data liquidation appends a second equity point for the final timestamp; skews drawdown-duration and per-point consumers.
- `runner/engine.py:517` [minor] — `r_multiple` denominates by `stop_loss or entry_price*0.98`: legit 0.0 stop falsy, and the LONG-side synthetic default is wrong-sided for SHORTs (r1/r3 agree on mechanism).
- `runner/engine.py:233-235, 243` [minor] — non-numeric `sl_price` strings raise ValueError mid-backtest; `tp0`/`tp00` accepted but fraction keys require exact `tp1_pct` naming — asymmetric contract.
- `runner/engine.py:879-884` [minor] — same-bar ordering (stops→fills→create→close-on-signal) lets an exit signal queued after its own entry order produce next-bar fills after flatness; documented only for stop-vs-TP.
- `runner/engine.py:60-62` [minor] — resolution coarser than timeframe passes validation and later silently finds no intrabar window data.
- `runner/engine.py:888-896` [minor] — progress-emit arithmetic cosmetic-only (r2).
- `engine.py:205-229` / `vectorbt_adapter.py:75-102` [minor smell] — `_uses_kwargs_style`/`_call_strategy` duplicated verbatim including comments; double in-function `inspect` imports (:208, :224).
- `engine.py:25-29, 96-107, 381, 424-446` [minor smell] — Speculative Generality: `OrderType.LIMIT/STOP/STOP_LIMIT` fully implemented but nothing ever creates such orders.
- `engine.py:41-47, 72-77` [minor smell] — Primitive Obsession: `same_bar_order`/`market_order_timing` free-form strings persisted unvalidated (a typo like `next_bar_opne` survives into metadata); `take_profit_levels` as stringly-flagged dicts wants a dataclass. (`ExecutionModelSettings` metadata-only fields are *documented* placeholders — noted, not charged.)
- `runner/engines/vectorbt_adapter.py:37-44` [minor] — `_RISK_WARNING_KEYS` module-global mutable set: warn-once dedup never works across ProcessPoolExecutor children and grows unboundedly in-process (r1/r2/r3 concur on mechanism; r1 major→merged minor since impact is log noise).
- `runner/engines/vectorbt_adapter.py:264-269, 943-946` [minor] — direction normalization maps *everything* unknown to LONG: a `"long "` typo becomes a silent LONG trade with LONG-side stop placement.
- `runner/engines/vectorbt_adapter.py:549-563, 192-193` [minor] — last-bar exit force-fill executes at that bar's open while entries wait for next open; shifted sampling makes adaptive stop levels lag their signal bar by one period at `entry_idx`.
- `runner/engines/vectorbt_adapter.py:1155` [minor] — intrabar scan includes the entry bar itself: a stop placed exactly at the entry bar's low triggers instantly (zero-holding-period trade).
- `runner/engines/vectorbt_adapter.py:1032` [minor] — `_load_ohlcv` takes unsorted `rglob` `matches[0]`; arbitrary parquet wins among multiple sources (same pattern `cli.py:85-116`, `optimizer.py:679`; `signal_replay_adapter.py:667-682` instead merges ALL matches with `keep="last"` over unspecified sort order — three different multi-source resolutions for one problem).
- `runner/engines/vectorbt_adapter.py:155-161` [minor] — single-position model: re-entry signals dropped via `elif` after same-bar close; intentional but undocumented (r2 major → minor judgement).
- `runner/engines/vectorbt_adapter.py:364-394` [minor] — partial results discarded when any worker future raises (ref m9 corroborates).
- `runner/engines/vectorbt_adapter.py:1159-1176` [minor smell] — Repeated Switches: `_find_indicator_series` kind-string chain plus six thin wrappers; `{kind: matcher}` map would collapse them. Data Clumps: eight sizing locals (:172-183), `IndicatorMap` pseudo-type with magic `when` strings recurring through four modules.
- `runner/engines/vectorbt_adapter.py:449`, `pynecore_adapter.py:189` [minor] — `"source": "yfinance"` hardcoded regardless of actual parquet origin; provenance wrong for most FX runs.
- `runner/engines/pynecore_adapter.py:416-433` [minor] — nearest-bar trade matching within 1s tolerance binds DST-shifted/rounded CSV timestamps to the wrong bar (r2/r3 concur).
- `runner/engines/pynecore_adapter.py:88, 91-94` [minor] — strategy copied to tmpdir loses sibling-module discovery and encoding attributes; CSV round-trip precision unasserted.
- `runner/engines/pynecore_adapter.py:162-165` [minor] — equity curve built from `entry_time` ordering while native/signal-replay use `last_exit_time` — third convention for the same artifact.
- `runner/engines/native_adapter.py:128-129` [minor] — raw `modeling_resolution` appended to `used_timeframes` even when normalized/rejected; aliases like "M5"/"1-lower" recorded or silently degraded to `open_close` where vectorbt honors finer replay — cross-engine parity gap (r2/r3).
- `runner/engines/native_adapter.py:162-164` [minor] — broad `except Exception: duration_days = 0` swallows malformed config dates.
- `runner/engines/native_adapter.py:148-156` [minor] — trades renumbered by `(entry_time, id)` but equity curve orders by `last_exit_time`: ids not monotonic in curve order.
- `runner/engines/signal_replay_adapter.py:386-389` [minor] — `except ValueError: return None` around `process_signal` discards rejected signals with no observability; misconfigured processor yields empty-but-successful backtests (r1 major → merged minor: bounded blast radius, but see Theme B).
- `runner/engines/signal_replay_adapter.py:643-665` [minor] — `_infer_replay_timeframe` forces 1-minute OHLCV download whenever any signal has an intraday timestamp or 1m–4h timeframe; neither configurable nor proportional to need (r1 major → minor: cost, not correctness).
- `runner/engines/signal_replay_adapter.py:573-575` [minor] — closed trades sorted by ISO strings lexicographically; mixed `+00:00` vs `Z` offsets misorder.
- `runner/engines/signal_replay_adapter.py:374` [minor] — full-frame boolean scan per signal, O(signals×bars); searchsorted available (frame pre-sorted).
- `runner/engines/signal_replay_adapter.py:681-689` [minor] — unreadable parquets silently skipped; corrupt file surfaces as "Missing OHLCV".
- `runner/engines/signal_replay_adapter.py:214-226, 503-520` + package note [minor smells] — risk-signal construction mirrors engine.py's shape; `_OpenPosition` carries stop/take-profit initial+current pairs wanting a trail-history object; neither adapter nor engine implements the `EngineAdapter` contract it sits beside (no `name`, different `run`) — inconsistent package convention; auto-download hits network implicitly during backtests (operational surprise).
- `runner/engines/base.py:80-83` [minor] — `resolve_strategy_parameters` swallows all exceptions into empty meta; broken STRATEGY_META silently yields defaults-only params (ref m10 corroborates).
- `runner/engines/base.py:123-134` [minor smell] — Speculative Generality: `validate_config` re-checks emptiness that pydantic constraints already enforce.
- `runner/engines/base.py:52-56, 64-69` [minor] — mutable config shared into workers (benign today); resolution validator accepts resolutions coarser than base where engines disagree on rejection — silent divergence.
- `runner/engines/base.py:46-47` [minor smell] — dates remain free strings through config/validation/stats; the adapters' date-bound divergence (F9) originates here.

## Minors — grid/matrix/cli/data plumbing

- `runner/backtest_grid.py:3-7, 390-409` [minor] — docstring says cells "delegate to runner.cli run", but the default path runs in-process bypassing CLI plumbing; documented invariant contradicted by default behavior.
- `runner/backtest_grid.py:190-208` [minor] — local `get_engine` duplicates cli's minus signal-replay/error text; two registries drift (adding an engine = edits in cli.get_engine, backtest_grid.get_engine, cmd_engines listing, adapter help — Shotgun Surgery).
- `runner/backtest_grid.py:341-342` [minor] — `strategy_name=Path(strategy).stem` vs cli's `args.name or stem`: naming drift between grid outputs and manual runs affects registry joins.
- `runner/backtest_grid.py:36` [minor] — GRID_SYMBOLS includes non-yfinance instruments (`SP500`, `NAS100`, `OIL`); default grid likely fails cells out of the box.
- `runner/backtest_grid.py:103` [minor] — `read_backtest_stats` does unguarded `json.loads`; partially-written cell files mark succeeded backtests failed.
- `runner/backtest_grid.py:362` [minor] — tracebacks truncated mid-line into summary JSON.
- `runner/backtest_grid.py:496-520` + `backtest_matrix.py:178-203` [minor smell] — duplicated `write_matrix_csv` already drifted by one column.
- `backtest_grid.py` result scaffolding built twice (in-process vs subprocess paths) [minor smell]; Data Clumps: 14-19-parameter cell-runner signatures want a `GridCellSpec`.
- `runner/backtest_grid.py:466` [minor security] — full command incl. `--params` JSON recorded into summary JSON; params may embed pasted secrets.
- `runner/backtest_matrix.py:333` [minor] — strategy-worker pool defaults to CPU count wrapping grid-level pools: no combined cap → oversubscription and skewed timings (r1/r3 concur).
- `runner/backtest_matrix.py:242, 260` [minor] — `max(ok_rows, key=lambda r: _safe_float(r.get("sharpe")) or -inf)`: legitimate Sharpe 0.0 deprioritized below negative-Sharpe cells.
- `runner/backtest_matrix.py:298-307` [minor] — probe `StrategyEntry` with empty meta: display-name identifier matching differs pre/post meta load.
- `runner/backtest_matrix.py:287` [minor] — CWD-relative default `Path("strategies")`; behavior depends on launch directory.
- `runner/backtest_matrix.py:147-150, 378-405` [minor] — redundant duplicate write of `grid_summary.json` already written by grid; failed-row dicts duplicated across exception handlers; mixed-type CSV columns from `%.10g` float formatting; Inappropriate Intimacy reaching into grid internals.
- `runner/cli.py:104-111` [minor] — first-match parquet selection makes detected ranges filesystem-order dependent; `pd.to_datetime` without `utc=True` leaves naive timestamps naive (:107).
- `runner/cli.py:185-195` [minor] — `sys.exit(1)` inside `cmd_run` makes it unusable as a library API (grid/matrix avoid importing cli partly because of this).
- `runner/cli.py:39` [minor security/ops] — `initialize_sentry("runner")` at import time activates telemetry as a side effect of importing.
- `runner/cli.py:216` [minor] — stats merge order overwrites engine-provided stats where keys collide (opposite of walkforward, F15).
- `runner/cli.py:137-139` [minor] — malformed `--params` JSON raises raw JSONDecodeError traceback instead of argparse-style error.
- `runner/cli.py:204-209` [minor] — progress_callback branching kept for historical adapters; all current ones accept the kwarg.
- `runner/daily_equity.py:57-61` [minor] — `prev_equity > 0` guard renders wiped-out accounts as calm 0% days instead of flagging ruin (r2 concurs "hides blowups").
- `runner/daily_equity.py:106-116` [minor] — length-based curve-preservation heuristic keeps a short bar-level curve over the daily rebuild (or vice versa) — two subtly different "daily" stories attach to one backtest (r1/r3 phrasings differ on direction, both flag threshold semantics).
- `runner/daily_equity.py:77-79` [minor] — daily points stamped midnight UTC rather than last-exit time; boundary/ordering friction with intraday points (ref m2 corroborates).

## Minors — converters

- `runner/converters/lean_converter.py:150-170` [minor] — `_build_meta` hardcodes `initial_capital=100000.0`, empty start/end/symbols; downstream CAGR/period metrics run on fictional premises (partially patched in `converters/cli.py:61-62`) (unanimous; ref m3).
- `runner/converters/lean_converter.py:156` [minor] — datetime/timezone re-imported inside `_build_meta` though imported at module level.
- `runner/converters/lean_converter.py:174-181` [minor] — chart x-values assume seconds; LEAN charts often use milliseconds → 1970-era timestamps, no magnitude sanity check.
- `runner/converters/lean_converter.py:59` [minor] — mixed str/int order ids would raise on sort (LEAN ids numeric-today).
- `runner/converters/lean_converter.py:44-123` [minor smells] — Long Function pairing loop; orders as ad-hoc dicts; `MR_` tag prefix undocumented shorthand; stringly Z/+00:00 dual-key hacks (:73-77).
- `runner/converters/stockkit_converter.py:172` [minor] — slippage stored in `fees`; conflated concepts the schema separates (unanimous).
- `runner/converters/stockkit_converter.py:184-192` [minor] — `_parse_datetime` assumes one format, stamps UTC, and returns raw unparsed `f"{date}T{time}"` on deviation — invalid ISO propagates downstream (unanimous; ref m4).
- `runner/converters/stockkit_converter.py:99` [minor] — `id=i+1` uses raw row index; skipped rows leave gaps in trade ids.
- `runner/converters/stockkit_converter.py:69-71` [minor] — `backtest_end=trades[-1].entry_time[:10]` on unsorted trades ≠ latest date; meta start/end wrong for unordered exports (r3 sole reporter, plausible mechanism).
- `runner/converters/stockkit_converter.py:102, 109-111` [minor] — direction kept as raw uppercase string without validation against {LONG, SHORT}; fractional-share rows skipped entirely.
- `runner/converters/stockkit_converter.py:24-45` [minor smell] — EXPECTED_HEADERS declared but never enforced; either validate or delete (Speculative Generality).
- `runner/converters/cli.py:53-58` [minor] — lean/stockkit branches character-identical and the `else` unreachable given the membership check at :46 (unanimous). Registry carries unconsumed `"extensions"` metadata; `logging.basicConfig` at module import reconfigures root logging for any importer (:20).

## Minors — optimizers

- `runner/optimizer.py:398-412` [minor] — train/test split lands mid-day for intraday ranges; test starts at an arbitrary microsecond-nudged timestamp while walkforward aligns month boundaries — OOS methodologies disagree.
- `runner/optimizer.py:216-243` [minor] — pre-flight requires OHLCV bars for every symbol in BOTH segments; one symbol missing bars in the tiny test segment fails the whole optimization (all-or-nothing brittleness).
- `runner/optimizer.py:175` [minor] — `timeframes[0]` taken silently; additional timeframes ignored, guessed "1h" when absent.
- `runner/optimizer.py:223` [minor] — reads `meta.data["data_dir"]`, a key BacktestMeta producers never set → auto-download always resolves to default DATA_DIR even when the base backtest ran elsewhere.
- `runner/optimizer.py:500-511` [minor] — candidate universes truncated at 256 in insertion order, biasing selection toward early symbols (warning exists; bias silent in results).
- `runner/portfolio_optimizer.py:29-45` [minor] — tz-naive ISO strings parsed via `.timestamp()` interpret machine-local time; all current producers emit offset-aware stamps so alignment shifts only for external/older artifacts (checklist tz item).
- `runner/portfolio_optimizer.py:265-276` [minor] — correlation_penalty computed once outside the objective from equal-weight-implied returns: constant across trials, weights don't influence it — it offsets the Sharpe scale rather than optimizing diversification (*verified*; also ref m7's stability-guard concern overlaps).
- `runner/portfolio_optimizer.py:278` [minor] — `trial.report(sharpe, ...)` reports the UNpenalized sharpe while the objective returns penalized score; future pruners would prune good-weight candidates against contradictory signals.
- `runner/portfolio_optimizer.py:86-92, 106` [minor] — train capped at test_start with deliberate gap-drop, but `train_months=None` extends training flush against test data (no embargo) — standard leakage concern, documented choice at best.
- `runner/portfolio_optimizer.py:113-131` [minor] — `_load_strategy_equity` swallows all exceptions into "need at least 2 valid curves"; DB failures reported as no-data.
- `runner/portfolio_optimizer.py:82` [minor] — month ≈ 30 days; long-range OOS windows drift ~a week vs walkforward's calendar months.
- `runner/portfolio_optimizer.py:157` [minor] — `require_overlap=False` branch could `max([])` if a curve begins after window start; guarded by construction today.
- `runner/portfolio_optimizer.py:302-317` [minor] — OOS metrics require strict intersection (`require_overlap=True`): daily curves rarely intersect exactly, so oos_sharpe silently disappears despite ample overlapping history; the forward-fill used for training isn't offered to OOS.
- `runner/portfolio_optimizer.py:369` [minor smell] — OPTIMIZATIONS_DIR defined at module bottom after half the module referencing it conceptually — readability trap.
- `runner/portfolio_optimizer.py` [minor smells] — Flag Arguments (`require_overlap` toggles fundamentally different algorithms); `StrategyEquityPoint.time: int` unix-seconds forcing parse gymnastics at every boundary.
- `runner/portfolio_optimizer.py:372-382` [positive] — path-containment validation on persist; rare hardening here (absent in walkforward/lean writers).
- `runner/risk_parity_optimizer.py:111-121` [minor] — post-solve `np.clip` can violate the equality constraint before renormalization; clipping zeroing a weight yields a renormalized solution off the solver optimum. **Dispute settled:** r1 claimed the inverse-vol fallback path skips clipping; code shows clip+normalize applies to BOTH paths (:117 follows the if/else) — r2's self-correction stands, asymmetry claim dropped. Edge case remains for the SLSQP path.
- `runner/risk_parity_optimizer.py:145` [minor] — constant columns surviving `fillna(0)` yield NaN corrcoef rows mapped to 0: average |corr| deflates for illiquid universes.
- `runner/risk_parity_optimizer.py:153` [minor] — score mixes frequency-dependent Sharpe with dimensionless penalties; daily-vs-hourly inputs reorder universes, undocumented.
- `runner/risk_parity_optimizer.py:176` [minor smell] — score weights 0.35/-0.5/-2.0 magic constants, tuning invisible; plain-dict return dissolves the `RiskParitySelection` typing at the boundary.
- `runner/capm.py:66-67` [minor] — alpha annualized ×252 unconditionally; weekly/monthly/intraday inputs silently misannualize (geometric compounding would differ materially for large alphas).
- `runner/capm.py:41-54` [minor] — non-overlapping observations dropped silently; no minimum-overlap guard beyond ≥2 points.

## Minors — stats & walkforward

- `runner/stats.py:247, 130-137` [minor] — daily resample ffill injects zero-return weekend/holiday days then annualizes √252: systematic downward bias vs trading-day calendars; PSR n inflated with zero-variance days.
- `runner/stats.py:173` [minor] — infinite profit_factor collapses to None at the serialization boundary; consumers read "no losing trades" as missing.
- `runner/stats.py:95` [minor] — no-trades branch sets profit_factor 0.0 while winners set inf→None: two conventions for one concept.
- `runner/stats.py:366-367` [minor] — degenerate-period fallback `now, now` makes CAGR 0 silently; `now()` inside a stats function is hidden nondeterminism.
- `runner/stats.py:483-494` [minor] — manual-path Sharpe population-variance vs QS sample std; literal 252 repeated although constants exist at :25/:29.
- `runner/stats.py:260-278` [minor] — `_attach_inferred_freq` defined, never called anywhere in the package (grep-confirmed by r2/r3) — delete or wire.
- `runner/stats.py:514-516` [minor] — unused params accepted in both PSR helpers; dead defensive branch at :216 probing nonexistent attribute.
- `runner/walkforward.py:168-182, cli.py:406` [minor] — portfolio mode accepts and echoes `step_months` but never uses it (single fold by construction); CLI advertises behavior that doesn't exist in portfolio mode.
- `runner/walkforward.py:143-147, 306` [minor] — fold payloads expose signed max_drawdown while the summary abs()es it; consumers must know which layer they read.
- `runner/walkforward.py:252-254` [minor] — lazy `from runner.cli import get_engine` duplicates the engine factory a third time (circular-import workaround).

## Minors — auto_agent & LLM providers

- `runner/auto_agent.py:662-699` [minor] — dead filtered-by-empty-list blocks in `_resolve_backtest_target`; implicit deterministic target selection when only symbols given (logged).
- `runner/auto_agent.py:132-137` [minor] — model validation applies only to provider `nim`; copilot models pass unchecked, typos surface late.
- `runner/auto_agent.py:722-723` [minor] — `except Exception: pass` reading catalog.json: corrupt indistinguishable from missing.
- `runner/auto_agent.py:249-254, 741-754` [minor] — LLM-chosen engine forwarded unvalidated ("vectorbtpro" fails deep inside cmd_run).
- `runner/auto_agent.py:509-522` [minor] — brace-scrape fallback can parse `{...}` inside string literals; confusing TypeError far from cause.
- `runner/auto_agent.py:139-143, 215-273` [minor] — line counting cosmetic; retry-loop failure context tail-truncated so root-cause head can be cut.
- `runner/auto_agent.py:26, optimizer.py:16` [minor smell] — layering inversion importing private `_detect_date_range` from cli; detection logic actually lives in three places with different semantics/signatures (Shotgun Surgery).
- `runner/auto_agent.py:801` [minor] — command lines and child stdout leak environment-specific absolute paths into web-visible task logs.
- `runner/auto_agent.py:54, 56-82` [minor smells] — Mysterious Name: `LONG_CODE_LINE_THRESHOLD` means warn-threshold; TIMEFRAME_RANK etc. encode preference heuristics the names don't convey.
- `auto_agent._normalize_strategy_meta` vs `strategy_ir.normalizer.normalize_strategy_meta_payload` [minor smell] — competing STRATEGY_META normalizers with **different tag semantics** (`{"type": [...]}` vs `{"labels": [...]}`) plus verbatim-copied `_extract_markdown_title`/`_coerce_scalar`; one normalization should win (drift bug waiting; unanimous).
- `runner/llm_providers/copilot_sdk_provider.py:150-175` [minor] — bridge thread: outer `done.wait(timeout+1.0)` can expire between inner deadline and teardown, leaking the daemon thread/event loop/session (r1/r2/r3 concur on mechanism; severity minor since bounded).
- `runner/llm_providers/copilot_sdk_provider.py:43-48, 28-55` [minor] — recursive `_extract_content` iterates payload.values() with no cycle protection: dict ordering decides content for unknown schemas.
- `runner/llm_providers/copilot_sdk_provider.py:310-312` [minor] — TypeError-fallback retries `send_and_wait` without timeout (outer wait_for still bounds it; misleading).
- `runner/llm_providers/copilot_cli_provider.py:96-99` [minor] — undecodable JSON falls back to treating stdout as content; truncated/error JSON silently becomes the "LLM response".
- `runner/llm_providers/nim_provider.py:9-12` [minor] — OpenAI fallback stub raises only on instantiation; eager validation would fail fast like the API-key check.
- Providers [minor smells] — `_apply_metadata` triplicated (nim/copilot_cli/copilot_sdk) belongs in base.py; `_extract_content` key lists drift between copilot providers; MODEL_ALIASES and gpt5_fallbacks encode the same mapping twice; `generate` five exception taxonomies deep.

## Minors — strategy_ir

- `runner/strategy_ir/builder.py:163-184` [minor] — conditions become one `Condition` per prose bullet stored verbatim as `expression`; exit-kind inference by substring ("tp", "time") misclassifies paraphrases; downstream codegen must guess semantics from prose (feeds F4).
- `runner/strategy_ir/builder.py:225-233` [minor] — confidence = 0.25 × section count rewards presence not parsability; unparsable docs score graded confidence consumed nowhere yet.
- `builder.py:45-65` / `normalizer.py:18-38` [minor] — `_coerce_scalar` triplicated with subtly divergent quoted-number handling.
- `runner/strategy_ir/codegen_vectorbt.py:51-61` [minor] — `_indicator_defaults` picks the first positive numeric param as window; BB std=2 becomes a 2-period window.
- `runner/strategy_ir/codegen_vectorbt.py:110-124, 155-163` [minor] — primary/secondary chosen alphabetically regardless of role (a volume indicator becomes a price comparator); STRATEGY_META.parameters exposes raw param names while generated code reads the first sorted numeric param's name — overrides apply only if user guesses the key.
- `runner/strategy_ir/codegen_vectorbt.py` [minor] — drawings emitted for first 25 signal rows regardless of fill; RSI collapses avg_loss==0 to neutral 50 instead of 100.
- `runner/strategy_ir/models.py` [minor] — CHOICE parameters don't validate `default ∈ choices`. Otherwise exemplary pydantic modeling.
- `runner/strategy_ir/models.py, routing.py` [minor smells] — StateMachine/RawExpression/Drawing types have no producer/consumer outside tests (documented incremental seam — noted, not charged); routing alias table accepts nine spellings for three modes and silently defaults typos to ir-first; `first_llm_attempt=2` is a magic-number protocol with auto_agent.MAX_ITERATIONS that nothing pins down.
- `runner/strategy_ir/normalizer.py:36-52` [minor] — redundant exception members in the json-parse ladder; list-form parameters without any value key materialize None defaults silently.

## Cross-cutting themes (panel consensus)

1. **Cross-engine comparability is broken** (F5-F10): three execution-timing models, three end-date semantics, three multi-source parquet resolutions, divergent same-bar priority and `max_position_pct` meanings. Recommend an execution-contract doc extending `docs/signal-risk-execution-contract.md` plus conformance tests on one canonical synthetic dataset.
2. **Silent failure dominance** (F2, F11, F13, F17-F19, F24): engines convert hard failures into empty-but-successful artifacts persisted to SQLite and ranked downstream. "Ran fine, zero trades" must be distinguishable from "broken."
3. **Multi-symbol portfolio accounting is fictitious** (F5): per-symbol compounding summed post-hoc; reported equity/PnL for multi-symbol runs is structurally overstated in vectorbt, native, and pynecore alike.
4. **Copy-drift duplication clusters**: OHLCV loaders ×4-6, date-boundary parsers ×3 (+`_detect_date_range` ×2 drifted), meta serializers byte-identical (vectorbt↔pynecore), `get_engine` ×2-3, `write_matrix_csv` ×2, `_apply_metadata`/`_extract_content` ×3, `_coerce_scalar` ×3, float coercers ×4, STRATEGY_META normalizers ×2 behaviorally divergent. Highest-leverage refactor: `runner/engines/common.py` (loaders/date bounds/serialization) + one canonical meta normalizer.
5. **Stats-key chaos**: `sharpe` vs `sharpe_ratio`, `net_profit_pct` vs `total_return` resolved ad hoc via alias maps and `or`-fallbacks; canonicalize in `shared.models.BacktestStats`.
6. **Environment-dependent numbers** (F17): optional quantstats changes metric definitions; hardcoded yfinance provenance; total_bars means trades in one engine, bars in two others. Persisted-artifact comparability needs schema contract tests.
7. **Subprocess discipline**: timeouts everywhere except `auto_agent._run_backtest` (F12); pynecore doesn't catch TimeoutExpired; lean_adapter cannot even import (F1). A `runner/process.py` helper (Popen + timeout + log pump) eliminates this bug class.
8. **Exec trust chain** (F25-F27): LLM-generated code executed via subprocess and in-process exec with `PermissionHandler.approve_all` and env inheritance; no screening layer anywhere.

## Reference-report reconciliation (explicit disagreements)

The weaker reference report (`code-review-full-gpt-5.6-luna-2026-08-23.md`, §runner) was consulted hint-only; every applicable entry verified against panel citations:
- **Agrees (already covered above):** M1=F1 (rated major there, critical here — justified), M2=F2 (same), M3=F14, M4⊂F23, M5⊂F5, M6=F26, M7=F12, S1=F27, S2=F25, m1=F16, m3, m4, m5, m10, m11⊂F2, B-series ⊂ duplication themes.
- **Not adopted:** M8 (portfolio_optimizer "combines absolute equity levels", `:223-233`) — none of the three reviewers found weighting-before-normalization there, and the code normalizes weights before combining (`equity_matrix @ weights`); the real defects in that region are F36/F39, which the reference missed. m2 partially overlaps the panel's midnight-stamp finding (kept as facet); m6 folded into F36; m8 contradicted by direct code check (clip applies to both solver paths); m7 folded into portfolio_optimizer minors; m9 kept as vectorbt minor.
- **Reference blind spots the panel adds:** backtest_grid `--keep-indicators` subprocess breakage (F3), matrix `time`/`date` key mismatch (F18), multi-symbol equity double-count as a systemic theme, stats dual-path environment dependence (F17), optimizer no-trade penalty (F13), lean_converter pairing corruption (F33/F34).

## Coverage statement

All **36 tracked code files** under `runner/` are accounted for: findings above cover every file with reviewer-reported issues; scanned-clean with nothing found: `runner/__init__.py`, `runner/converters/__init__.py`, `runner/engines/__init__.py`, `runner/llm_providers/__init__.py`, `runner/llm_providers/base.py`, `runner/strategy_ir/__init__.py`. (Files whose only findings are the minors listed above — e.g. `capm.py`, `routing.py`, `nim_provider.py` — are covered by those entries.) Count basis: 4 critical (F1–F4) + 41 majors (F5–F40 bugs + S1–S5 structural) + 105 minors as itemized above.

---

## frontend

**Panel**: model ox-alpha, 3 blind independent reviewers (r1/r2/r3), synthesized and code-verified by ox-alpha. The weaker reference report (`code-review-full-gpt-5.6-luna-2026-08-23.md`, §3 "frontend") was consulted as hints only; every adopted entry was re-verified against reviewer citations or source.

**Coverage**: all **59 tracked files** under `frontend/` reviewed by each panelist; every file is accounted for below (findings or explicit "scanned — nothing found"). `frontend/src/vite-env.d.ts`, `.env`, and `package.json` were additionally inspected during verification.

**Merged totals**: **Critical 1 · Major 20 · Minor 63 · Total 84** consolidated findings (raw panel output ≈ 470 tagged items across r1: 164 / r2: ~142 / r3: 167; strict duplicates removed and multi-site patterns — e.g. helper duplication, polling without visibility gates — merged into single findings that cite every site, dropping no citation).

### How disagreements were settled (verification log)

| Dispute | Resolution (source-checked) |
|---|---|
| r1 rates LiveHub overlay-link **critical**; r2/r3 never rate anything critical | **Verified critical** — `LiveHub.tsx:114–118` renders `<Link style={{position:"absolute", inset:0, zIndex:1}}>` over static PAUSE/STOP/DELETE buttons (:149–177); positioned element paints above them, clicks navigate instead. Only surface with these controls. |
| r2 claims 38-dep memo array on `BacktestsPage.filterPanelContent`; r1 says 41; r3 says 33 | **Verified 41** entries (`BacktestsPage.tsx:1107–1147`). Scale recalibrated to r1. |
| r2 grades `normalizeWinRate` ">100 clamps to 1" [minor]; r1 calls the heuristic unit-guessing [minor] | Both true: branch order (`BacktestsPage.tsx:123–129`) sends `150 → 1.0` while `55 → 0.55`; kept as one minor with both failure modes. |
| r1 [major] vs r2/r3 [minor]: `PortfolioDetailPage.tsx:729` `row.win_rate.toFixed(2)` crash | Type says required (`types.ts:1230`) but siblings null-check and the payload family is optional-in-spirit; with no error boundary a throw blanks the SPA. Kept **major** (crash risk on primary route), noting the type contract dispute explicitly. |
| r1 [major] vs r2 [minor]: `OptimizationResultsPage.tsx:250` unguarded `test_period.start.slice(0,10)` | `test_period` is typed required (`types.ts:323,353`) but this is the only deep deref without optional chaining on a legacy-record-prone route; with S1 no-boundary it whitescreens. Kept **major**, contract caveat noted. |
| r1/r2/r3 all flag percent-vs-fraction chaos at differing scopes | Merged into one systemic **major** (D6) plus per-site minors; the underlying backend scale is unverifiable from the frontend, so findings are phrased as contract violations, not proven display bugs. |
| r2 [major] "LiveHub has no way to resume a PAUSED deployment" | **Verified** — `"play"→ACTIVE` exists (`LiveHub.tsx:432–441`) but only Pause/Stop/Delete buttons are rendered (:149–177); kept **major** functional gap. |
| r2 [major] vs r1 [minor]: `liveStreams.ts` drops non-snapshot/update envelopes | **Verified** (`liveStreams.ts:47–57`) — any other envelope maps `data` to `null`; kept **major** (r1 under-weighted consumer impact in `LiveHub.tsx:371–379`). |
| r2 [major] vs r1 [minor]: `LiveViewer` chart rebuilds per tick | **Verified** (`LiveViewer.tsx:710–733`) — full `setData` + `fitContent()` on every `normalizedBars` change; kept **major** for a realtime viewer. |
| r1 [major] vs r2 [minor]: `PortfolioDetailPage` chart effect leak path | Early return skips teardown of a previously built chart instance; low frequency but real. Kept **major** (r2), with r1's zoom-loss note folded in. |
| r2 [major] vs r1 [minor]: `StrategyDetailPage` `any`-typed API surface | Kept **major**: entire page compiles without the rich `StrategyDetail` type; backend drift ships undetected. |
| r1 [minor] vs r2 [major]: `CreateBacktestModal` NaN capital | Verified unchecked (`parseFloat(capital)`); graded **minor** (HTML `type=number` mitigates; backend validates) — r2's major recalibrated down, disagreement noted. |
| Reference F2 (CSRF/no-auth headers, major-security) | **Rejected as unproven**: `client.ts:91–95` sets only `Content-Type`; no evidence in-repo of cookie-session auth or CSRF exposure. ID-encoding inconsistency retained as minor (C4). |
| Reference F5/F9-f9 ("per-execution P&L ignores matching") | **Partially rejected**: `toTradeHistoryRows` (`LiveViewer.tsx:946–968`) computes `(exit−entry)×qty×side` per row — correct *if* rows are round-trips; r2's framing kept (definition divergence vs LiveHub FIFO), severity **minor** pending backend semantics. |
| Reference f19 (prod chunks >500 KB) | **Verified**: `dist/assets/TradeTable-*.js` ≈ 1.11 MB. Included as minor (E10). |

---

## A. Reliability: error boundaries & observability

- **[major]** **No error boundary anywhere in the tree** *(unanimous major; the single most impactful reliability gap)*. All routes are lazy-loaded behind `<Suspense>` only (`App.tsx:96–122`, providers in `main.tsx:11–22`); no `componentDidCatch`/`react-error-boundary` exists in the area. Any render-time throw — e.g. D-item derefs below — or a failed lazy-chunk fetch whitescreens the SPA with no recovery UI. One component away from eliminating whole-app white screens; it is the enabling condition that turns several minors below into outage-class events.
- **[major]** **Sentry declared but not wired.** No `@sentry/*` import anywhere under `src/`, none in `package.json` (grep-verified by r3 and re-verified in synthesis); only ambient `VITE_SENTRY_DSN`/`VITE_SENTRY_ENVIRONMENT` types exist (`vite-env.d.ts`). Meanwhile `frontend/.env` still ships a live-looking DSN (`...ingest.de.sentry.io/4511955579437136`). Production exceptions are completely unobserved and a stale credential sits in the tree — wire Sentry per the project brief's intent or remove the env contract and rotate the DSN. *(r1/r3 agree; reference report missed this entirely.)*
- **[minor]** Lazy-route import rejection (offline nav, deploy hash mismatch) surfaces only as an unhandled rejection — same white-screen family as above (`App.tsx:7–73`).
- **[minor]** No catch-all route: unknown paths render sidebar + empty main area, no 404 feedback (`App.tsx:97–121`).
- **[minor]** Shell-level `TaskManager` hidden only on `/data*` (`App.tsx:77`): on `/tasks` the floating widget duplicates the TaskHub UI and its poller (see C8).

## B. Critical interaction bug

- **[critical]** **Live deployment controls are unclickable on the Live Hub.** Each `LiveDeploymentCard` renders a full-card overlay `<Link to="/live/:id" style={{position:"absolute", inset:0, zIndex:1}}>` (`LiveHub.tsx:114–118`); the PAUSE/STOP/DELETE buttons below (`LiveHub.tsx:149–177`) are non-positioned siblings, so the link paints above and intercepts every click, silently navigating away. This is the **only** surface offering pause/stop/delete — a runaway live strategy cannot be stopped from the UI. The repo contains its own fix pattern: `PortfoliosPage.tsx:106–119` gives the interactive child `position:relative; zIndex:2` (also `BacktestsPage.tsx:1404–1416` checkbox). Fix identically or narrow the link.
- **[major]** Corollary gap: a **PAUSED deployment can never be resumed** — `handleControl` supports `"play" → ACTIVE` (`LiveHub.tsx:431–442`) but no UI path issues it and `controlBtnStyle("play", …)` (`:1372–1384`) is dead code. Wire a resume button or drop the branch. (Also fixes part of the Speculative-Generality smell.)
- **[minor]** The overlay link's aria-label template literal is truncated — `` aria-label={`Open live account `} `` interpolates nothing (`LiveHub.tsx:116`); every card announces identically to screen readers.
- **[minor]** Same overlay kills `Sparkline interactive` tooltips on card grids: pointer events hit the link, so hover tooltips passed at `BacktestsPage.tsx:1438` and `PortfoliosPage.tsx:134` can never display (dead prop at those call sites).

## C. Request hygiene: cascades, storms, polling

- **[major]** **LiveHub refires a 3N+3-request fan-out on every WebSocket message.** The stream effect (`LiveHub.tsx:371–379`) calls `refreshDeployments(true)` per `accountsStream.data` emission; each refresh fetches accounts + deployments + dashboard + 3 requests per account (stats/balances/positions) plus a trades fallback (`:225–300`, `:238–272`), with an additional unconditional 30 s poll when disconnected (`:381–391`). The server push already carries the data being re-fetched. Debounce/throttle or apply payloads directly (the effect already does this for `accounts` at `:377`).
- **[major]** **Infinite refetch loop while the LiveDetail replay modal is open.** The signal-processors effect guards on and depends on the very flag it toggles: deps `[replayModalOpen, signalProcessorsLoading]` with `setSignalProcessorsLoading(true/false)` around the fetch (`LiveDetail.tsx:147–156`) — fetch → loading flips → effect re-runs → fetch again, hammering `/signal-processors` for as long as the modal is open. Fetch once per open transition (cf. `LiveHub.tsx:582`).
- **[major]** **LoggingPage: heaviest endpoint polled hardest.** Every search keystroke triggers a server refetch of up to `LOG_FETCH_LIMIT = 5000` lines (`search` is a direct dep, `LoggingPage.tsx:230–262`), stacked on an unconditional 5 s poll that runs regardless of tab visibility, with full re-parse per poll. No debounce, no incremental tailing, no visibility gate.
- **[major]** **DownloadModal genesis-coverage request storm.** The Dukascopy coverage effect fires `getDukascopyGenesis(parsedSymbols, timeframes)` on every keystroke in the symbols field (deps derived from raw input, `DownloadModal.tsx:314–356`); the endpoint proxies an external provider. The `cancelled` flag prevents state writes but not the fetches. Debounce + abort needed.
- **[major]** **CreateBacktestModal N+1 availability checks per keystroke.** `Promise.all(symbolList.map(getDataCheck))` re-runs on every change of symbols/timeframe/start/end with no debounce or cache (`CreateBacktestModal.tsx:135–186`); typing "EURUSD" costs up to 6×N requests. The repo knows the fix — `BacktestsPage.tsx:596–601` debounces 250 ms.
- **[major]** **LiveTerminal runs a second, non-reconnecting WebSocket implementation.** Raw `new WebSocket` with zero retry/backoff/heartbeat (`LiveTerminal.tsx:30–31`, cleanup `:50–67`) while `hooks/useWebSocket.ts` implements all of it — any transient drop permanently freezes the task feed at "disconnected" mid-download. Should delegate to the existing hook. Also: completion detection regexes log text `[task:<id>] status=(\w+)` (`:42`) — protocol-by-convention that silently breaks `onTaskFinished` if log formatting changes; log accumulation is uncapped (`:38–41`); and `onStatusChange` in the effect deps reconnects the socket if a parent ever passes an inline callback (stable setters today, trap tomorrow).
- **[major]** **liveStreams drops non-stream envelopes to null data.** `useLiveStream` derives `data` only from `type === "snapshot" | "update"` (`liveStreams.ts:47–57`); heartbeats/acks/error envelopes reset `data` to `null` even though the envelope type declares `error?: string` (`:16`) — consumers like LiveHub's refresh effect act on transient nulls and stream-level errors never surface.
- **[minor]** Unconditional polling without visibility gates across the stack: TaskManager global 3 s poller (`TaskManager.tsx:32–34`), TaskHub 3 s (`TaskHub.tsx:76–101`), LiveDetail 30 s (`LiveDetail.tsx:136–145`), LiveViewer fallback 30 s, LoggingPage 5 s — an idle tab maintains a constant request drumbeat. Standardize on TanStack Query intervals (which pause in background by default, `api/queries.ts:79–139`).
- **[minor]** Two independent 3 s pollers hit the same `/tasks` endpoint when TaskManager is mounted outside `/data`: TaskManager (`TaskManager.tsx:32–34`) and TaskHub (`TaskHub.tsx:76–101`) duplicate status-set constants and poll scaffolding; consolidate into one shared query.
- **[minor]** Poll errors swallowed silently: TaskManager's empty catch (`TaskManager.tsx:24–28`) leaves the badge showing stale counts indefinitely during an outage, with zero indication.
- **[minor]** TaskManager/TaskHub toast-transition detection diffs against the previous poll only (`TaskHub.tsx:103–119`): a queued→failed→completed jump between polls announces only "completed"; first observed state never toasts. Acceptable at this cadence, worth knowing.
- **[minor]** Sequential N-delete: `handleDeleteAllForSymbol` awaits `deleteDataSymbol` per file serially (`DataManagementPage.tsx:668–690`); mid-loop failure leaves the symbol half-deleted with a generic alert and no per-file outcome. Batch endpoint or `Promise.allSettled`.
- **[minor]** PortfolioDetailPage fetches contributions twice on mount: initial `Promise.all` includes `getPortfolioContributions(id, "component")` although `detail.component_contributions` already arrived; the groupBy effect then refetches (`PortfolioDetailPage.tsx:110–154`). One wasted request and a window where sources disagree.
- **[minor]** OptimizationsPage loads once per mount with no refetch affordance (`OptimizationsPage.tsx:38–48`) — new optimization runs don't appear until remount; sibling pages use React Query (Services.tsx is the model).
- **[minor]** `fetchJSON` assumes every response has a JSON body (`client.ts:111`): `deleteDataSymbol`, `deleteService`, `cancelDataTask` throw a bare `SyntaxError` on `204 No Content`/empty bodies, bypassing `ApiError` handling.
- **[minor]** No timeout/AbortController on any fetch (`client.ts:91–112`): a hung backend freezes loading states across pages indefinitely; add `AbortSignal.timeout()` defaults.
- **[minor]** Error-body parse assumes JSON shape (`client.ts:96–110`): a proxy 502 HTML page degrades messages toward "[object Object]"-style output.
- **[minor]** `Content-Type: application/json` is forced ahead of `...options` (`client.ts:92–95`): callers cannot remove headers for GETs or future FormData uploads; merge headers properly.
- **[minor]** `getServiceLogs(...lines)`/`getLogContent(...lines)` test truthiness `if (lines)` (`client.ts:369,527`): requesting `lines=0` silently becomes "server default"; should be `!= null`.
- **[minor]** Inconsistent ID encoding in URL construction: `compareBacktests`/`comparePortfolios` skip `encodeURIComponent` (`client.ts:255,516`) as do the backtest/task/portfolio path builders (`client.ts:208–225,239,247–248,403–404,456–458,498–499,603–606`) while siblings encode. Internal UUIDs today; inconsistency invites a future breakage. *(Reference F2's stronger CSRF claim was not substantiated — see verification log.)*
- **[minor]** Module-level capability cache `liveStatsSupported`/`liveStatsSupportPromise` (`client.ts:76–77,114–141`) caches the OpenAPI probe forever per page-load (stale after rolling deploys) — hidden global state next to an existing query cache.
- **[minor]** `queries.ts` hooks spread caller `options` **after** the built-in guards (`queries.ts:43–139`): a caller-supplied `enabled: true` overrides the `Boolean(name)` guard (latent — current callers pass constants); conversely the pattern is what lets LiveViewer pin `staleTime: Infinity` correctly. Document the precedence or invert it deliberately.
- **[minor]** Cache-key magic coupling: LiveViewer's WS hydration writes hard-coded keys `["live-signal-log", accountId, 200]` / `["live-positions", accountId]` (`LiveViewer.tsx:148,155`) which must stay byte-identical to `useLiveSignalLog`'s default limit in `queries.ts` — export key factories from one module.
- **[minor]** `useWebSocket` heartbeat send race: `socket.send(...)` inside the interval can throw if readyState flips between check and send (`useWebSocket.ts:166`); wrap in try/catch. Reconnect also retries forever with capped backoff for any close including permanent server rejections (`:115–132`), and `sendJson` silently returns `false` while CONNECTING (`:255–262`). Cleanup calls state setters post-teardown (`:243–244`) — dead code in React 18+. Otherwise the best-written file in the area.

## D. Data correctness: units, stats, stale state

- **[major]** **Metric-unit conventions are untyped and mutually contradictory across ~8 screens** (Primitive Obsession; all three reviewers rank this the top correctness theme). `BacktestDetailPage` passes `stats.net_profit_pct`/`cagr`/`win_rate` to `StatCard format="percent"` which prints `toFixed(2)%` directly (`StatCard.tsx:27–28`) — percent-points semantics — while its own PSR card multiplies by 100 and `PortfolioDetailPage.tsx:463` passes `(stats.win_rate||0)*100` (fraction semantics); `ComparePage.tsx:49` treats total_return/cagr/win_rate as points while BacktestsPage's `normalizeWinRate` treats them as fractions (`BacktestsPage.tsx:123–129`). One of these pages displays off by 100× for any given backend payload; nothing in `api/types.ts` states scales. Fix once with a typed metric contract.
- **[major]** **Page-scoped trade statistics presented as whole-backtest** on `BacktestDetailPage`: `BacktestStats` has no `winners`/`losers` fields, so the guards always fall through and wins/losses/best/worst/avgPnl/long-short counts derive from only the currently loaded 100-trade page (`BacktestDetailPage.tsx:219–253`, panel `:802–811`) — wrong numbers stated as fact for any backtest with >100 trades.
- **[major]** **Stale module-level cache serves outdated backtests**: `backtestsAllCache`/`backtestsAllPromise` file globals survive unmount and are shared by all consumers (`BacktestsPage.tsx:114–115,274–298`). After creating a backtest the modals close without a forced reload (`:1764–1772`) so new rows never appear until hard reload; `force=true` nulls cache+promise while an earlier in-flight promise can still resolve and repopulate stale rows (race at `:288–292`). TanStack Query is already in the stack — this page bypasses it entirely.
- **[major]** **Render-crash risks on optional fields, unprotected by any boundary**: `row.win_rate.toFixed(2)` (`PortfolioDetailPage.tsx:729`; sibling cells null-check psr/p_value/max_drawdown/cagr but this one was missed), plus direct multiplications of `return_contribution`/`drawdown_contribution`/`pnl_total` (`:718–732`); and `optimization.test_period.start.slice(0, 10)` (`OptimizationResultsPage.tsx:250`) assuming legacy records carry `test_period`. *(Contract dispute noted: both fields are typed required — `types.ts:1230`, `types.ts:323` — but neither page nor type is defensive where every sibling is.)*
- **[major]** **liveStreams envelope handling resets data to null on non-snapshot/update messages** — cross-listed from C above because its consumer impact is data correctness: charts flicker empty and refresh effects skip on transient nulls (`liveStreams.ts:47–57`).
- **[major]** **StrategyDetailPage is fully `any`-typed**: `useState<any>` for detail (`:12`), `backtest: any`/`portfolio: any` iteration (`:590,713`), `catch (error: any)` (`:54`) — `getStrategyDetail` already returns a rich `StrategyDetail`; backend shape drift ships undetected. Load failure is also indistinguishable from not-found (catch → `setDetail(null)` → "Strategy not found", `:27–29`).
- **[minor]** `normalizeWinRate` rewrites out-of-domain values silently (`BacktestsPage.tsx:123–129`): branch order sends `150 → 1.0` (clamped) instead of `/100 → 1.5`, and any stored value in `(1,100]` is heuristically divided — colliding with StatCard callers that assume already-percent. The two conventions meet inside one app.
- **[minor]** ComparePage coerces missing stats to 0 before diffing (`Number(stats[key] || 0)`, `ComparePage.tsx:223–224`): a backtest lacking `psr` shows `0.000` vs the other side's real value with a red delta — fabricated comparison data rather than "N/A".
- **[minor]** LiveViewer per-fill "P&L" assumes each row is a complete round-trip: `(exit−entry)×qty×side` per row summed into `totalPnl` (`LiveViewer.tsx:946–968`, header `:280`) — if rows are individual fills this is slippage delta, not realized P&L; LiveHub computes proper FIFO-matched P&L, so two different "Total P&L" definitions ship on adjacent screens. Needs one domain decision + shared helper. *(Reference report graded this major; downgraded pending backend semantics — see verification log.)*
- **[minor]** Exit markers plotted at signal time, not execution time: `signalTimeUnix` feeds EXIT markers (`LiveViewer.tsx:620–632`, data `:965`) although `execution_time` exists on the row — arrows misplace by fill lag.
- **[minor]** Benchmark overlay rescale divides by `normalizedBenchmark[0].equity` with no zero/finiteness guard (`BacktestDetailPage.tsx:1052–1053`) and assumes both series share an anchor date — a 0-start or offset-window benchmark yields Infinity spikes or wrong scaling.
- **[minor]** Stale-response races (no `ignore`/cancel flags) on BacktestDetailPage's info/stats/equity/trades/benchmark/CAPM effects (`BacktestDetailPage.tsx:96–169`, trades `:117–132`), OptimizationResultsPage's chained fetch (`:58–79`), StrategyDetailPage's load (`:19–31`), and PortfolioDetailPage's trades-filter effect (`:156–176`) — fast navigation can land older responses last.
- **[minor]** DataManagementPage loaders have `try/finally` with **no catch** (`loadOverview` `:408–443`, `loadCategoryDetails` `:369–406`; fire-and-forget call sites `:456–469,633,641`): a failing `/data/overview` leaves "Categories 0 / 0" — an empty success state on the screen whose job is deleting files — plus unhandledrejection noise. *(r2 adds that `loading` can stay stuck; r1/r3 describe it as silent-empty; either way there is no error state in 2199 lines.)*
- **[minor]** Fabricated fallbacks presented as fact: `(info?.initial_capital || 100000)` invents $100k when absent (`BacktestDetailPage.tsx:432`); invalid capital input silently coerces to 100000 on portfolio create (`PortfoliosPage.tsx:335–338`); LiveHub dashboard failures degrade telemetry tiles to $0/0% (`LiveHub.tsx:235–237`, tiles `:1100–1126`).
- **[minor]** Silent equity-preview failures: `.catch(() => undefined)` leaves cards permanently showing "No equity preview" with no retry/toast (`BacktestsPage.tsx:725`).
- **[minor]** Filter state clobbered: forced reloads reset `numericFilters` to full bounds mid-session (`BacktestsPage.tsx:577–594`); MatrixGridPage's Reset cannot clear the strategy filter because auto-select immediately re-picks `strategies[0]` (`MatrixGridPage.tsx:283–287,349`).
- **[minor]** LoggingPage level heuristic passes the whole line when no token matches (`normalizeLevel(levelMatch?.[1] ?? line)`, `LoggingPage.tsx:114`): messages containing "error"/"debug" as substrings get bucketed and color-coded wrongly; expanded-row ids embed array index so expansion attaches to different rows after filters shift (`:305,366`).
- **[minor]** MatrixGridPage calls `setPage(safePage)` inside a `useMemo` (`:326–331`) — render-phase state update from a memo body; works only via React's retry pass and warns under StrictMode. Clamp at usage sites instead.
- **[minor]** Sort-direction toggle on the same key doesn't reset `page` in MatrixGridPage (`:337–345`); `PanelTable`'s hidden `pageIndex` can point past end after rows shrink (`DataManagementPage.tsx:2003–2027`); generic sort `Number(left[sortField] ?? 0)` sorts null metrics as 0 interleaved with real zeros (`BacktestsPage.tsx:686–689`); DataTable's comparator returns 0 whenever either value is nullish (`DataTable.tsx:33–37`).
- **[minor]** StrategyLabPage forwards free-text `customModel` verbatim as a model id (`:416–431`) — server-side validation burden; client allow-list would fail fast. One click also triggers a token-costing LLM pipeline with no confirmation.

## E. Duplication & structural debt

*(Consolidated ledger — all three reviewers agree; counts verified against citations.)*

- **[major]** **Realized-P&L FIFO engine duplicated verbatim (~70 lines of financial-grade logic)**: `calculateRealizedTradeStats` in `LiveHub.tsx:1529–1597` ≡ `LiveDetail.tsx:731–799`. Already drifting (`toWinRatePercent` signatures differ; `maxDrawdownFromBalances` exists only in LiveDetail). One edit to one copy produces two different Total-PnL numbers for the same account. Extract to `utils/liveStats.ts`.
- **[major]** **41-entry hand-maintained dependency list** on `filterPanelContent` (`BacktestsPage.tsx:1107–1147`): one forgotten identifier = stale slider UI silently disagreeing with state. Extract `FilterPanel` as a component. *(Scale recalibrated to r1's count — r2 said 38, r3 said 33.)*
- **[major]** **Dual state+ref mirror pattern** in DataManagementPage: `categoryEntriesRef`/`loadingCategoriesRef`/`selectionRef` shadow state (`:352–367`), mutated inside setState updaters (`:381–403`) — hand-rolled concurrency that TanStack Query provides for free; only user of the pattern.
- Helper families duplicated (all minor individually, systemic jointly): finite-number coercion ×6 (`asFiniteNumber`/`asFinite`/`toFiniteNumber`/`toNumberOrNull` in LiveHub `:1607–1618`, LiveDetail `:840–851`, LiveViewer `:1138–1145`, TradeChartPage `:52–63`, BacktestDetailPage `:255–262`, PortfolioDetailPage `:59–66`); USD/percent formatters ×5; `resolveChartLocale` ×3; lightweight-charts bootstrap block ×5 (TradingViewChart, LiveViewer `:636–708`, PortfolioDetail `:769–937`, LiveDetail `:521–600`, BacktestDetail `:971–1189`) including a verbatim drawdown-histogram block ×2; task-stream modal scaffold ×3 (DownloadModal `:1153–1258` ≈ ResampleModal ≈ SmartHarvestModal); optimization formatters ×2 (`OptimizationsPage.tsx:9–30` ≡ `OptimizationResultsPage.tsx:20–47`); ReactMarkdown component maps ×2 (`StrategyDetailPage.tsx:341–398` ≈ `:441–553`); replay form ×2 (LiveHub/LiveDetail); duplicates/dedup modal block ×2 (BacktestsPage/PortfoliosPage).
- **[minor]** Three parallel table implementations: `components/DataTable.tsx`, `PanelTable` inside DataManagementPage (`:1983–2199`), ag-grid TradeTable — plus bespoke tables in LiveViewer/BacktestsPage/MatrixGridPage. Pick one paradigm.
- **[minor]** Three incompatible timeframe orderings: `CreateBacktestModal.tsx:14–26`, `DataManagementPage.tsx:92–106` (mixes `m5`/`4h` conventions, unknowns ranked 99), `MatrixGridPage.tsx:125–132`.
- **[minor]** Dead code / Speculative Generality: `RangeFilterControl.tsx` exported nowhere (167 lines, grep-verified by r1/r3); Sidebar `/accounts` disabled nav with no route (`Sidebar.tsx:32`); SmartHarvest single-option source select (`SmartHarvestModal.tsx:179–186`); LiveHub `play` arm (see B); OptimizeModal identical ternary arms (`OptimizeModal.tsx:473`); `getStrategy` vs `getStrategyDetail` and `getPortfolio` vs `getPortfolioDetail` identical delegates (`client.ts:145–148,456–458`); unused `getBacktestsAll` preview options (`client.ts:186–197`); `drawdown_min/max` vs `max_drawdown_min/max` dual query params serialized together (`types.ts:763–766`, `client.ts:179–182`).
- **[minor]** Four overlapping backtest types with drifted metric names (`max_drawdown_pct` vs `max_drawdown`, `net_profit_pct` vs `total_return`): `BacktestSummary`/`BacktestMeta`/`BacktestInfo`/`BacktestListItem`/`BacktestMetadataItem` (`types.ts` throughout) — the type-level root cause of D's unit bugs; `[key: string]: any` index signature on `BacktestStats` (`types.ts:380`) lets typo'd stat keys typecheck.
- **[minor]** Local type duplication: MatrixGridPage restates `MatrixGridRow`/`StrategySummary`/`Overview` locally (`:11–98`) despite canonical versions in `types.ts:836–925`.
- **[minor]** Two chart stacks maintained: TradingViewChart.tsx (the good shared primitive — pane support, presets, binary-search lookup) coexists with bespoke integrations in four other files; port them onto it. Within TradingViewChart itself, the lifecycle effect keyed only on `[height]` tears down/recreates the chart on fullscreen toggles, losing zoom/pan (`TradingViewChart.tsx:384–503`; height belongs in `resize`), and preset changes rebuild all indicator series (`:505–550`).
- **[minor]** Barrel `components/index.ts` partially decorative: omits most actually-used components while exporting some unused ones; deep imports dominate.
- **[minor]** Styling split-brain: Tailwind v4 utilities (`Sidebar`, `Badge`, `MatrixGridPage`, `LiveTerminal` tail) vs inline styles everywhere else vs CSS vars — three paradigms; index.css hand-writes `.animate-spin` keyframes duplicating Tailwind's built-in (`index.css:56–66`) and stacks a universal `*` reset over preflight (`:18–22`).

## F. UX & product gaps

- **[major]** **Destructive actions lack confirmation or double-submit guards, inconsistently**: Services delete removes a possibly-live bot service with no confirm (`Services.tsx:62–70`); Portfolios "Remove Empty" deletes unknown-count portfolios on one click (`PortfoliosPage.tsx:453–463`) while BacktestsPage confirms with a count; DownloadModal (`:1132–1149`), ResampleModal, SmartHarvestModal, and PortfoliosPage create (`:325–380,932–946`) all lack in-flight flags — double-click starts duplicate tasks/portfolios (OptimizeModal is the reference implementation with `running`). Adopt one discipline everywhere.
- **[minor]** Modal lacks dialog semantics: no Escape-to-close, no focus trap/restoration, no `role="dialog"`, no scroll lock (`Modal.tsx:20–77`); backdrop closes even while a job streams, and close-veto logic is re-implemented per caller instead of an `onCloseRequest` prop.
- **[minor]** MultiSelectDropdown/SymbolInput keyboard & a11y debt: mousedown-only outside-click (touch devices can't dismiss, `MultiSelectDropdown.tsx:34–44`), no arrow/Enter/Escape navigation or aria-expanded (`:87–249`, `SymbolInput.tsx:149–225`); SymbolInput caps categories at 100 with no path to the rest (`:189–220`) and swallows `/symbols` errors into an eternally empty dropdown (`:35–49`).
- **[minor]** RenameModal renders API errors through `helperText` in muted guidance-gray (`BacktestDetailPage.tsx:907–910`, `PortfolioDetailPage.tsx:645`) — indistinguishable from help text; ignores the returned promise (`RenameModal.tsx:110`).
- **[minor]** Escaped-entity artifact ships in copy: `` task_&lt;job_id&gt;.log `` renders literally in SmartHarvestModal (`:333`).
- **[minor]** OptimizeModal label "Train / Test Split %" accepts only fractions 0.1–0.95 default 0.7 (`OptimizeModal.tsx:172,652–661`) — entering the natural reading `70` errors; rename or divide by 100. Draft-wipe edge: reset effect depends on `parameters` which BacktestDetailPage passes as fresh `{}` identity each render while loading (`OptimizeModal.tsx:176–202`).
- **[minor]** CreateBacktestModal mount-frozen date range (`defaultDateRange()` memoized `[]`, `BacktestFromSignalsModal.tsx:49`) goes stale in long-lived SPAs; `TIMEFRAME_ORDER` includes tick/s1/1w the select never offers (`CreateBacktestModal.tsx:14` vs `:332–339`); parameter harvesting drops boolean/null defaults (`:100–108`).
- **[minor]** TradeChartPage router desync: raw `history.replaceState` bypasses React Router (`TradeChartPage.tsx:273–279,478–483`) — use `navigate(…, {replace:true})`; window-level Space handler double-fires when a transport button has focus (`:550–553`); Escape navigates away from the page (`:555–562`); fullscreen height computed once per render misses OS-triggered resizes (`:434–439`); `toggleFullscreen` doesn't catch rejection (`:505–516`, unlike the Escape path); `$undefined` prints when entry/exit price is null (`:735,739`); indicators sampled off-grid vanish silently (`:160–184,356–364`).
- **[minor]** i18n split-brain: MatrixGridPage fully German + de-DE number formatting (`:361–377` etc.), Sparkline hover date `de-DE` + `"Punkt ${n}"` (`Sparkline.tsx:86–88`), LiveViewer timestamps UTC (`formatTableTime :1181–1185`) vs local dates elsewhere — three conventions, no documented standard.
- **[minor]** StrategiesPage sort labels say "Trade Count" but sort `backtest_count` (`StrategiesPage.tsx:173–176` vs `:223–224`); failed `/strategies` renders the same empty grid as zero strategies (`:118–120,231–256`).
- **[minor]** PortfoliosPage hides metric pills when value === 0 exactly (`:147–177`) — genuine 0.00 Sharpe indistinguishable from missing; `parseOptionalJson` errors lack row context (`:350–360`).
- **[minor]** LiveHub replay date defaults use UTC `toISOString().slice(0,10)` (`LiveHub.tsx:586–589`; same in LiveDetail `:77–88`) — "today" is yesterday near midnight for UTC-negative users.
- **[minor]** LiveViewer wildcard symbol `["*"]` (Discord-source deployments) leaks into the symbol dropdown and can be selected for quote requests (`uniqueSymbols`, `LiveViewer.tsx:970–992`; populated at `LiveHub.tsx:479–484`); executions stream wholesale-replaces fill history (`:158–163` — verify payload semantics); `dailyPnlPct` explodes for balances < $1 (`:292–293`); SL/TP price lines draw up to 10 overlapping pairs (`:750–790`).
- **[minor]** LiveDetail win-rate denominator mixes fills and matched positions (`LiveDetail.tsx:710`) — understates rate when fills ≠ round-trips; double-decode of route params `decodeURIComponent(rawAccountId ?? "")` corrupts ids containing literal `%` and throws URIError (with S1: white screen) — `LiveDetail.tsx:56`, `LiveViewer.tsx:82`.
- **[minor]** Services shows placeholder-as-data ("Next Run: cron-based", `Services.tsx:241`) and accepts unvalidated cron expressions (`:135–141`); coarse mutation-pending disables every service row's buttons during any one mutation (`:262`).
- **[minor]** ToastProvider: id `Date.now() + random(1000)` can collide under bursts removing the wrong toast (`ToastProvider.tsx:63`), no cap/coalescing for error storms (`:87–112`), timers uncleared on unmount (`:65–67`), toasts not clickable-to-dismiss.
- **[minor]** StrategiesProvider exposes `loading = isFetching` (`:28`) — background refetches flip consumers into spinner state (StrategiesPage correctly uses `isLoading`); redundant double-clear of `pendingLoadRef` (`:38–58`).
- **[minor]** TaskHub truncates completed list at 100 with no indicator (`COMPLETED_TASK_LIMIT`, `TaskHub.tsx:18,128–135`); categoryIcon maps backtest and resample to the same 📊 (`:46–50`).
- **[minor]** StatCard currency format drops cents (`maximumFractionDigits: 0`, `StatCard.tsx:23–26`) — rounding money on a trading dashboard; `-0` edge renders `$0`.
- **[minor]** Sparkline hover dates interpolate linearly between endpoints (`Sparkline.tsx:79–87`) — wrong across weekends/trading gaps; tooltip left-clamp uses hard-coded 192px width (`:145`); `Math.min(...spread)` stack risk on very large arrays (`:32–33`).

## G. Cross-cutting checklists (panel consensus)

- **XSS**: zero `dangerouslySetInnerHTML` in the area; ReactMarkdown without `rehype-raw` escapes HTML by default; markdown links get `target="_blank" rel="noreferrer"` (safe). ✅ — *one residual vector*: `sourceUrl`/`tags.link` rendered unvalidated, a `javascript:` scheme becomes executable (`StrategyDetailPage.tsx:303–315`) — validate scheme before rendering [minor]. Reference f12 raised no concrete sink; dismissed.
- **Unhandled rejections**: covered in C/D above (DataManagementPage worst; heartbeat send race; fullscreen promise). ⚠️
- **Error boundaries**: absent (A). ❌
- **N+1 fetches**: LiveHub fan-out, keystroke storms, sequential deletes (C). ❌
- **Secrets/config**: stale Sentry DSN in committed `.env` (A) — rotate/remove. ⚠️
- **Trust boundaries**: raw server filesystem paths round-tripped through the client (`deleteDataSymbol(path)`, ResampleModal `sourcePath`, BacktestFromSignalsModal `filepath`) — frontend must treat as opaque; backend must whitelist-resolve (flagged for the backend panel). ⚠️
- **Tooling**: react-hooks rules enabled (`eslint.config.js:15`) yet the Sparkline conditional-hook violation and ComparePage dep-dodge exist → lint warnings evidently not enforced in CI; `ecmaVersion: 2020` predates the ES2022 `Object.hasOwn` used in sources (`eslint.config.js:19`) [minors]. Type-aware lint would mechanically catch several majors.
- **Tests**: single test file asserts page source text via regex (brittle tripwire) and requires Node ≥22.6 type-stripping, unpinned (`tests/optimizationEvidence.test.ts:5–21`) [minors]; positive: adversarial prototype-pollution coverage is genuinely good.
- **Build**: prod chunks exceed 500 KB — largest lazy chunk ≈ 1.11 MB (`dist/assets/TradeTable-*.js`; ag-grid + recharts + charts stacked) [minor; reference f19 verified].

## H. Files with nothing to report

Fully scanned by the panel with no findings: `components/Badge.tsx` *(one typing nit from r3 kept in E)*, `components/FilterBar.tsx` *(single-caller dropdown API flagged in E)*, `components/LoadingSpinner.tsx`, `hooks/liveStreams.ts` *(finding promoted to systemic C)*, `lib/queryClient.ts`, `lib/optimizationEvidence.ts` *(model small module; palette-extraction nit dropped as noise)*, `utils/stats.ts`, `vite.config.ts` *(dev-only hardcoded proxy acceptable; `__APP_VERSION__`/`__GIT_SHA__` declared with zero consumers)*.

## I. Maintainability hot spots (ranked)

1. `pages/DataManagementPage.tsx` (2,199 lines) — largest file, own table framework, dual state/ref mirror, no error states on core loaders.
2. `pages/BacktestsPage.tsx` (1,835) — module-singleton cache + 41-dep memo.
3. `pages/LiveHub.tsx` (1,730) / `pages/LiveDetail.tsx` (870) — duplicated financial engine + concurrency-sensitive refresh pipeline inline.
4. Chart-bootstrap duplication (5 sites) + second WS implementation — one `useDarkChart` hook and reuse of `useWebSocket` would delete ~400 lines.
5. Metric-unit vocabulary — the highest-leverage type-level fix in the area (D).

---

### Finding counts by severity

| Severity | Count |
|---|---|
| Critical | 1 |
| Major | 20 |
| Minor | 63 |
| **Total** | **84** |

Counting rule: each severity-tagged bullet above is one consolidated finding; bullets that merge a recurring pattern cite every affected `file:line` (nothing dropped except strict duplicates and rejected reference-report entries logged above). Severity recalibrations vs the source reviews: error boundary kept major per unanimous panel grade (not inflated); LiveHub overlay-link kept critical per r1 and verified in source; LiveViewer chart rebuild, liveStreams envelope nulling, StrategyDetailPage `any` surface, PortfolioDetailPage chart leak, and the PAUSED-resume gap raised to r2's major after source verification; CreateBacktestModal NaN capital lowered from r2's major to minor (HTML input mitigation); reference-report F5 downgraded pending backend semantics.

---

## tests-b

**Panel:** model ox-alpha; three blind, independent reviewers (r1, r2, r3), synthesized by ox-alpha.
Contradictions between reviewers (and against the gpt-5.6-luna reference report, consulted only for this chunk's files) were settled by reading the cited source under `tests/`, not by vote; every settlement is annotated inline as **[SETTLED]** or **[DISPUTED — settled: …]**. Findings were merged purely from the reviewers' citations first; the bounded verification pass (8 spot-checks) is logged at the bottom.

**Severity conventions:** counted findings are (a) bugs/correctness risks and (b) security issues, matching the reviewers' own tallies. Fowler baseline smells (c) and maintainability hot spots (d) are preserved per file but uncounted, as in the reviewer reports. Every finding keeps its original `file:line` citation(s); where reviewer line ranges differed slightly, the union is cited.

**Coverage:** chunk B = the alphabetical middle of the root-level tracked `tests/` entries, starting at `tests/test_backtest_grid.py` and ending at `tests/test_signal_bots_risk.py` = **49 tracked files**: 48 pytest modules + one non-module script (`compare_metrics_quantstats.py`, unreviewed by the panel — flagged; full reconciliation in the coverage statement at the bottom). All three reviewers claim full-chunk scans of the test modules, but **r2's report contains no sections for `tests/test_scheduler_signal_bot_health.py`, `tests/test_signal_bot_health_check.py`, or `tests/test_request_logging_middleware.py`** (it claims 49 files read; its headers enumerate 45) — those three files are covered here from r1 + r3 only.

---

### tests/test_backtest_grid.py

- **[B-01] minor — CLI command checks assert token adjacency, not argv structure** *(verified)* — `tests/test_backtest_grid.py:39-47`: assertions on `" ".join(cmd)` (`"-m runner.cli run" in joined`) pass under argument reorderings/duplications that happen to serialize adjacently. Assert exact list slices of `cmd`. **[SETTLED — kept minor]** r1 noticed the same pattern but explicitly declined to file it ("combined with the `--skip-sqlite in cmd` list check the intent is covered"), r2 filed it minor, r3 found nothing; kept on r2's citation — the weakness is real but marginal.
- Smells (r2): the 14-kwarg `run_one_backtest(...)` call is duplicated verbatim at `:105-123` and `:139-157` — extract `_cell_kwargs(tmp_path)`.
- Otherwise clean (r1, r3): fan-out keeps one risk_pct across all 4 cells (`:88`); per-cell timeout/launch-failure isolation (`:94-161`) is good negative-path design; resolved-date regression captures kwargs through to `attach_daily_equity` (`:164-231`).

### tests/test_backtest_matrix.py

- **[B-02] minor — test pins the odd artifact name `matrix_matrix.csv`** — `tests/test_backtest_matrix.py:93`: prefix/suffix doubling ("matrix_" + "_matrix.csv") frozen as expected output; flag for a product-level rename rather than a test change (Mysterious Name inherited from production). (r3.)
- Smells (r2): `_write_strategy`/`_write_strategy_with_meta` overlap almost entirely — one function with optional meta would do; fake `run_backtest_grid` payload dict repeated three times (`:31-54`, `:110-118`).
- Otherwise clean (r1, r2, r3): strategy selection, engine-metadata pass-through, failure isolation asserted against real outputs written to tmp dirs.

### tests/test_backtest_matrix_runner.py

- **[B-03] minor — daily-rows assertion is only as strong as the test's own fixture** — `tests/test_backtest_matrix_runner.py:21-24`: the pre-written fake output JSON invents `stats.daily_returns` itself, then `result["daily_rows"]` is asserted through it; `daily_rows[0]["run_id"]` is weak relative to fixture control. The URL/id link assertions (`:67-69`, `matrix_node_id` `demo:AAPL:1h`, viewer URLs) are exact and valuable. (r3.)
- **[B-04] minor — 17-parameter call site mirrors the production signature verbatim** — `tests/test_backtest_matrix_runner.py:43-64`: Data Clump reflected from `_run_strategy_matrix_cell`; a config object would shrink both. (r3.)
- Otherwise clean (r2: single test, explicit kwargs).

### tests/test_cache_manager.py

- **[B-05] minor (accepted-practice note) — real `time.sleep(0.05)` inside the fake build** — `tests/test_cache_manager.py:132`: forces concurrency overlap in the Barrier-synchronized single-build test. r3 files it minor ("acceptable; alternative is an event-gated build"); r1 explicitly judges the same line a legitimate race-window widener, not a flake pattern. Kept at minor with both readings attached — no reviewer claims active breakage.
- Smells (r2): `fail_build_catalog` raising stub re-declared three times (`:166-169`, `:207-210`, `:268-271`); `_catalog_payload` factory exists yet raw-dict catalogs still hand-built in three tests.
- Verified non-findings (r1+r2+r3): hit/miss counters asserted via deltas (`:65-75`), defensive-copy proven by mutating the returned dict (`:67`), disk-read/merge normalization pinned field-by-field, autouse invalidate fixture prevents leakage.

### tests/test_capm.py

Scanned clean by all three reviewers (β=1.5, α annualized ×252, R²≈1, p-value bound; alignment intersection; minimum-observations guard — all exact).

### tests/test_catalog.py

No bug/security findings (r1+r2+r3). Smells: the two-month bid/ask tick DataFrame pair is rebuilt nearly verbatim in four tests (r2 cites `:104-122,150-168,195-213,246-270`; r3 cites `:100-143,146-186,189-239,242-283` — same smell, slightly different slice points) — extract a segment-fixture factory parameterized by symbol/timeframe. Positives: cached-metadata reuse proven negatively (`read_parquet_summary` forbidden after caching), legacy flat exports, segmented grouping in both layouts, cross-source merge to "mixed", alias normalization.

### tests/test_ctrader_adapter.py

- **[B-06] minor — magic payload-type integers as fragile test doubles** *(verified)* — `tests/test_ctrader_adapter.py:105-107` sets `adapter._error_payload_type = 100` while the sibling negative test (`:131`) uses `101`; r3 adds `:129-132` (`_heartbeat_payload_type = -1` family). Arbitrary stand-ins for protobuf enums; if production ever compares a real enum value colliding with these, fakes match unrelated branches. Works today because production reads only these instance attributes. (r1 + r3; r2's hot-spot note on white-box coupling corroborates.)
- **[B-07] minor — micro-second timing budgets** — `tests/test_ctrader_adapter.py:78` (`connect_timeout_seconds=0.01`) and `:511` (`asyncio.wait_for(..., timeout=0.1)`): event-gated so failures are loud, but 10 ms can elapse early / 100 ms budgets can blow on loaded CI; prefer injecting an unresolvable event. (r2; r3 corroborates `:71-95` as low-risk-acceptable.)
- **[B-08] minor — `CTRADER_DEMO=true` success path never exercised** — `tests/test_ctrader_adapter.py:46-68`: only `false` and invalid values tested; the strict-parse *success* branch for `"true"` is unpinned while its failure twin is covered. (r2.)
- **[B-09] minor — `send_request` doubles as broker and refresh handler** — `tests/test_ctrader_adapter.py:360-377`: distinguished by `isinstance`; returning the sentinel `request` object on retry makes the success contract implicit; a named success value would document intent. (r2.)
- Smells: `CTraderLiveAdapter(app_id="app", ..., account_id="46736452")` constructor block repeats ~14–15× (r1 "~14 of 17", r2 "~15×", r3 "~15×"); `RefreshRequest`/`RefreshResponse` stub pair re-declared three times (`:258-265`, `:347-354`, `:403-409`) (r2).
- Hot spot (r2): white-box coupling to private state (`_execution_queue`, `_reconciliation_required`, `_terminal_auth_blockeds`, payload-type ints) — internal renames break many tests at once.
- Security positive (r2): token-refresh test redirects the `.accounts.env` write into `tmp_path` (`:247-256`) instead of touching any repo env file. Event-driven concurrency tests (`:334-390`, `:393-433`) rated exemplary by r2 (ordering enforced via asyncio Events, no sleeps); r1 confirms refresh/single-flight semantics genuinely tested and matching `live_runner/ctrader_adapter.py:676-680`.

### tests/test_ctrader_deployment.py

Scanned clean by all three reviewers. Ranking ("fast" beats "slow") verified against `live_runner/ctrader_deployment.py:47-56` sort keys; plan building asserts targeting/status/`source_backtest_id`. r1 note (not a finding): neither test exercises the `-inf` score exclusion branch (`:41-42`).

### tests/test_ctrader_live_fix.py

The worst offenders in the chunk — three vacuous "positions" tests whose names claim live-position-safety coverage:

- **[B-10] CRITICAL — `test_ctrader_positions_signed_qty_for_sell` is a pure arithmetic tautology** *(verified)* — `tests/test_ctrader_live_fix.py:268-281(282)`: builds local `expected = 5.0`, asserts `-expected < 0` and `expected > 0`; zero references to any adapter method. A regression flipping SELL positions positive passes. False confidence on a live-money path. **[SEVERITY SETTLED: critical]** (r3 critical, r1/r2 major — see verification log #1.)
- **[B-11] CRITICAL — `test_ctrader_positions_excludes_closed_positions` asserts two local literals differ** *(verified)* — `tests/test_ctrader_live_fix.py:284-295`: `position_status_open = 1`, `position_status_closed = 2`, asserts they differ "(per the adapter code)"; `get_positions` never invoked; closed-position filtering completely unprotected. **[SEVERITY SETTLED: critical]** (r3 critical, r1/r2 major — see verification log #1.)
- **[B-12] major — `test_ctrader_positions_symbol_id_mapping` tests dict assignment, not mapping logic** *(verified)* — `tests/test_ctrader_live_fix.py:254-265`: assigns `adapter._symbol_name_to_id = {...}` then reads the same attribute back. **[SEVERITY SETTLED: major]** (r2/r3 major, r1 minor — see verification log #1; unlike B-10/B-11 it touches a real adapter object, just no logic.)
- **[B-13] minor — duplicated imports** — `tests/test_ctrader_live_fix.py:9-16` (r1 cites `:14-16`): `datetime, timezone` and `typing.Any` imported twice; dead code from a paste seam showing the positions block was inserted wholesale. (All three.)
- **[B-14] minor — cross-process flock probe is timing-shaped** *(verified)* — `tests/test_ctrader_live_fix.py:207-228` (r3 focuses `:222`): sleeps a fixed 0.2 s then asserts `writer.poll() is None`; on a loaded machine the child may not yet have attempted the lock, making "blocked" pass vacuously (false negative), and the test errors (not skips) on platforms without `fcntl`. Deterministic alternative: gate on a sentinel/stdout written by the child after its first lock attempt. r1's milder reading ("legitimate blocking-I/O probe, mild flake vector") is folded in. **[REFERENCE DISAGREEMENT — settled: not adopted]** the gpt-5.6-luna reference (T4, `:209-223`, major) additionally claims the timed-out child is left alive (resource leak); verification log #7 shows the parent reaps via `writer.wait(timeout=2)` on the normal path, so that specific claim was not adopted — only the timing weakness stands.
- **[B-15] minor — hand-rolled patch/restore instead of `monkeypatch.setattr`** — `tests/test_ctrader_live_fix.py:162-167`: `mod._write_env_keys = boom` with manual save/try/finally. r1 judges it style-only (`finally` guarantees restore); r2/r3 note the leak window if interrupted between assignment and `try` entry, plus inconsistency with house style. (All three.)
- Smells: Speculative Generality / dead tests — the vacuous trio simulate coverage of `get_positions` that does not exist (r2, r3); three dedupe tests repeat the identical ~25-line two-execution setup (`:354-393`, `:406-450`, `:453-488` r1; r3 adds LiveSignal/LiveDeploymentConfig construction repetition `:301-348`, `:354-497`); another divergent copy of the `isolated_db` engine-redirect fixture (`:236-248`). **Copy-count disputed**: r1 says this is the suite's *third* `isolated_db` copy, r2 the *fifth*, r3 the *fourth* — reconciled in the verification log (the counts are compatible once per-file multi-copy definitions are counted differently).
- Hot spot (r2): file title says "edge-cases and helper behavior" while spanning three unrelated concerns (refresh persistence, DB roundtrip, dedupe script).
- Security positives (all three): `test_persisted_accounts_file_has_0600_permissions` (`:179-193`); crash-safety test proving no mixed token pair persists (`:141-176` r1, `:169-176` r2); subprocess spawned from argv list without shell (`:209-221`).
- Verified non-findings (r1+r2): refresh→auth→retry ordering by index (`:76-108`), single-flight refresh with persisted pair (`:111-138`) match production behavior.

### tests/test_daily_equity.py

No bug/security findings (r1+r2+r3). Arithmetic verified exact: 0.005 = 500/100000, 5% day return, cumulative 100800 (`:99`); NaN/inf skip (`:124-145`); invalid/missing exit-time skip (`:147-166`); preserve-vs-replace equity-curve precedence pinned from both directions (`:199-244`). Smells (r2, r3): dead `if __name__ == "__main__": pytest.main(...)` tail (`:268-269`) — never executes under the repo's pytest runner; inline `Trade(id=2/3, ...)` literals repeat the full constructor three times where a `make_trade(pnl=..., pnl_override=...)` variant would do. r1's epsilon note (`:89`, `:175`: absolute `1e-10` instead of `pytest.approx` — "fine for these magnitudes") folded in as a style remark, not a counted finding.

### tests/test_database_init.py

- **[B-16] minor — cosmetic `DATA_DIR` patch without `ENGINE` rebuild** — `tests/test_database_init.py:17`: init_db's `create_all`/migrate are faked anyway so nothing touches disk, but the setup suggests isolation it doesn't provide. (r3; trivial.) r2/r1 scanned clean — no conflict, just depth difference.

### tests/test_database_migrations.py

- **[B-17] MAJOR — un-isolated test writes the developer/CI's real database** *(verified)* — `tests/test_database_migrations.py:33-37(59)` (`test_backtest_record_equity_curve_persistence`): bare `init_db()` with **no** isolation fixture while every sibling uses `isolated_db`; conftest pins `DATA_DIR` to the repo's own `data/`, so each run permanently deposits a new `test_equity_curve_record_*` row into the real `data/backtests.db`, runs schema migrations against real data, and depends on whatever is already there. Test-order and xdist-concurrency hazards; can also mask migration failures because the real DB may already be migrated. (All three reviewers concur; strongest consensus major in the chunk alongside B-10/B-11.)
- **[B-18] minor — "concurrent" test is entirely sequential** — `tests/test_database_migrations.py:232` with interception at `:245-262(264)`: simulates the lost-update window by intercepting one pre-check SELECT on `database.Session.exec`; nothing exercises threads/processes despite the Finding-6 framing. Legitimate technique, misleading name. Also flagged by r1 as over-broad matcher (`"discord_message_id" in stmt_text and "select" in stmt_text` will swallow other selects mentioning the column as production queries evolve). (r2 + r1.)
- **[B-19] minor — hand-rolled patch/restore of `database.Session.exec`** — `tests/test_database_migrations.py:264-269`: try/finally instead of `monkeypatch.setattr`; a mid-stub KeyboardInterrupt between assignment and `try` leaks the patch into other tests; class-level patch also affects all sessions created during the window (single-threaded here). (r2 + r3.)
- **[B-20] minor — SQLite ≥ 3.35 environmental assumption** — `tests/test_database_migrations.py:445`: `ALTER TABLE backtests DROP COLUMN equity_curve` errors opaquely on older bundled SQLite rather than skipping; worth a version gate. (r1 + r2.)
- **[B-21] minor — `_DB_INITIALIZED = False` mutated directly with no restore** — `tests/test_database_migrations.py:432,487,519,547,568` (r3): module global left `False` after these tests return, so any later `init_db()`-dependent code re-runs migrations against whatever `ENGINE` is current; works today because every consumer sets its own engine. r2's hot-spot note corroborates the private-global coupling (`:76,157,359,432-433,487-488,519-520,547-548,568-569`).
- **[B-22] minor — top-up semantics encoded confusingly** — `tests/test_database_migrations.py:120` vs `:603`: two tests encode subtly different meanings for the same migrated counter ("skipped" rows re-reported as `"migrated": 1` on top-up) — not wrong, deserves a comment in production code. (r3.)
- Smells: four divergent local variants of "redirect shared.database at a tmp engine" (`isolated_db` `:141`, `legacy_db` `:346` per the verified grep, with additional raw `create_engine` sites at `:66`; r2's `:62-77`/`:345-360` slices are near-misses; only some patch `DB_PATH`; combined with copies in `test_live_execution_safety.py:623` which patches none of DB_PATH and must hand-re-attach the PRAGMA listener, and `test_ctrader_live_fix.py:237`) — Shotgun Surgery; missing an edit yields quietly non-isolated tests exactly like B-17 (r2+r3+r1). `_legacy_signals_db`/`_insert_legacy_signal` (`:320-342`) re-implements what `test_live_execution_safety.py:714-758` does with stricter DDL (r2+r1). Section banners reference "Finding 4/5/6/7/8" (`:181,229,281,317,439`) pointing at an external review document no longer linked (all three).
- Positives: fail-closed rollback of a corrupt legacy DB pinned (`:381-417` r1); dedupe-does-not-clobber broker_order_id (`:300-315`), backfill-does-not-overwrite (`:556-573`), skip_existing top-up semantics (`:576-612`) verified intentionally-correct (r2).

### tests/test_discord_signal_parsing.py

- **[B-23] minor — disjunctive assertion pins almost nothing** *(verified)* — `tests/test_discord_signal_parsing.py:43-49` (r3 focuses `:49`): `assert signal is None or signal.symbol != "AT"` holds whenever the parser returns anything other than a signal with symbol "AT", i.e. almost always; cannot distinguish "parser rejected the message" from "parser extracted a wrong-but-not-'AT' symbol" (e.g. "MARKET"). Pin one concrete expected outcome. (All three reviewers concur.)
- Otherwise clean: oversold-buy template, action-first symbol precedence over RAM JET label, RocketFuel momentum-reversal sell pinned exactly.

### tests/test_dolthub_manager.py

Scanned clean by all three reviewers. Single migration test asserts legacy flat export → `AAPL/1d.parquet` with content intact and source removed.

### tests/test_download_grid_market_data.py

- **[B-24] MAJOR — session-wide MagicMock leak onto the production module** *(verified)* — `tests/test_download_grid_market_data.py:92` (r1 cites `:90-92`): `dl.DukascopyNativeDownloader = MagicMock()` is a bare module-attribute assignment with no monkeypatch and no restore — the mock replaces the real downloader class for the remainder of the entire pytest session. The sibling file does the identical stub *correctly* via `monkeypatch.setattr` at `tests/test_grid_market_data_download.py:71`. One-line fix. (All three reviewers concur; r1 filed it minor, r2 and r3 major — see verification log for the recalibration.)
- **[B-25] minor — drifted test name: balance-row skipping never asserted** — `tests/test_download_grid_market_data.py:112-139`: `test_download_symbol_skip_balance_rows` only checks canonical OHLCV columns exist after normalization, asserting nothing about balance-row skipping. Misleading name; rename or assert the drop/keep explicitly. (r1.)
- **[B-26] minor — workers-clamp under-asserted** — `tests/test_download_grid_market_data.py:167-189` (r2/r3 focus `:167-189`, r1 notes within `:142-189`): `-1` → "clamped to 1" verified only by absence of crash (`SystemExit.code == 0`); nothing observes the clamp. Capture stdout or assert the planned-download payload. (All three.)
- **[B-27] minor — manual `sys.argv` save/restore + bare `except SystemExit`** — `tests/test_download_grid_market_data.py:149-164,173-188` (r1 adds `:142-189`): try/finally instead of `monkeypatch.setattr(sys, "argv", ...)`; `except SystemExit: assert e.code == 0` also swallows the case where main() raises before argparse exits / silently stops checking the exit path if SystemExit stops being raised. (All three.)
- **[B-28] minor — dead trailing `pytest.main` block** — `tests/test_download_grid_market_data.py:192-193`. (r2.)
- Smells: function-level re-imports in every test (`:15,34,56,…`); `tempfile.TemporaryDirectory()` throughout while siblings standardize on `tmp_path` (r2). This file substantially duplicates its sibling `test_grid_market_data_download.py` (normalization tables, download_symbol isolation, bid-column fill) — consolidate into the better one (r1 + r3).

### tests/test_dukascopy_discovery.py

- **[B-29] minor — near-tautological wall-clock probe assertion** *(verified)* — `tests/test_dukascopy_discovery.py:213-214`: `_year_probe_date(date.today().year) <= date.today()` (and month equivalent) shifts meaning across the year; can only fail around year boundaries or if the helper returns a future-dated probe late in December — which r3 notes *is* its point, making this borderline-tautology vs weak-but-purposeful. All three converge on: pin against a fixed reference date instead. Kept minor.
- Verified non-findings (r1+r2+r3): Saturday-closed fake-probe binary search (`:44-61`), skip-vs-force asserted both by output JSON and tracked probe calls (`:105-141`, `:138-140`), force overwrite (`:143-165`), persist-before-KeyboardInterrupt (`:168-202`) — excellent behavior tests with real JSON outputs.
- Smells: repeated `sys.argv` monkeypatch + `main()` invocation five times (r2 cites `:80-86,121-132,150-162,184-199`); doubled comment banner lines `:64-66` look like a merge artifact (r3).

### tests/test_dukascopy_downloader.py

No bug/security findings beyond the notes below (backoff math verified consistent: pause doubling → cap 10 s + 15 s cooldown ⇒ `sleep_calls == [10.0, 15.0]` at `:326-404`/`:396-404`; progress plumbing verified end-to-end).
- **[B-30] minor — throttle simulated by URL string** — `tests/test_dukascopy_downloader.py:151,371-375` (r3 adds the sys.modules-aiohttp swap family `:131,215,283,355`): `throttle_signal=url == "u2"` silently changes fixture semantics if production reorders/retries URLs; the `monkeypatch.setitem(sys.modules, "aiohttp", ...)` global-state hammer works because production imports aiohttp inside the function (`dukascopy_native.py:426`). (r2 + r3.)
- **[B-31] minor — global `argparse.ArgumentParser.parse_args` patch** — `tests/test_dukascopy_downloader.py:492-511`: affects every parser constructed during the test; safe here (single CLI call), narrower seam would be safer if main() grows. (r2.)
- Smells: four verbatim copies of the FakeConnector/FakeTimeout/FakeClientSession/fake-aiohttp scaffold (`:105-130`, `:189-215`, `:257-283`, `:329-355` — all three concur, ~90 removable lines); function-level re-imports of the downloader module in every test (`:16,103,187,…`) though module-level import is equivalent (r2); `native.asyncio.run(...)` attribute-path access noted obscure (r3); repeated `del session, semaphore, …` noise in fakes (r1).
- Security positive (r2): JVM discovery tests pin the trusted-JVM-path allow-list (`JFOREX_TRUSTED_JAVA_HOME`, `:452-472`) — regression coverage against loading arbitrary JVM DLLs.

### tests/test_grid_market_data_download.py

Scanned clean (all three). Notably `:71` patches `DukascopyNativeDownloader` correctly via `monkeypatch.setattr` — the pattern its sibling gets wrong (B-24). Precise bid-fill values (`:43-48`), m30 regression (`:51-54`), sort-key ordering incl. unknown-last (`:57-65`), per-timeframe failure isolation (`:68-86`). Smells: normalization/sort tests overlap heavily with the sibling file — consolidate to one home (r1 + r3); `parse_csv_list(...) == DEFAULT_SYMBOLS` (`:18`) couples default symbol order to parser de-dup semantics (r1).

### tests/test_grid_tester_sqlite.py

- **[B-32] MAJOR — three tests write permanent rows into the repo's real database** *(verified)* — `tests/test_grid_tester_sqlite.py:93-127,130-149,152-182` (r3 cites entry points `:95,132,154`; r1 `:93-149,:152-183`): bare `init_db()` + save/read with **no** isolation fixture — same defect class as B-17. Rows (`test_grid_sqlite_save_*`, `test_grid_func_*`, `grid_older_*`, `grid_newer_*`) persist across runs and other machines' state influences `get_grid_results(limit=100)`; ids keyed on `%H%M%S` wall-clock (`:98,134`) additionally collide within the same second (time-flaky PK conflicts). Only `test_run_strategy_grid_persists_to_sqlite` (`:37-91`) partially escapes via tmp JSON output — yet even it asserts through the unpatched real `get_grid_results` (`:85-90`). The uuid-suffixed slug there is evidence the authors knew about shared-state risk yet left these three unisolated. **Severity split disputed**: all three rate the real-DB writes major except r1, who filed the same observation minor — settled below.
- **[B-33] MAJOR — "newest first" test never checks order** *(verified)* — `tests/test_grid_tester_sqlite.py:152-182`: saves id_1 then id_2, asserts only membership (`id_1 in ids`, `id_2 in ids`); a regression returning arbitrary or ascending order passes. Should assert `ids.index(id_2) < ids.index(id_1)` (or filter to just these two). Name/docstring promise ordering that nothing verifies. (r2 + r3 major; r1 did not file it — covered here from two reviewers; the gpt-5.6-luna reference independently lists the same defect as t8, minor.)
- **[B-34] minor — limit-ignoring regression undetectable** — `tests/test_grid_tester_sqlite.py:185-202`: stubs `get_grid_results` and compares `load_grid_results(limit=1)` to a one-element fixture, so production ignoring `limit` entirely still passes; assert truncation with a two-element fixture. (r2.)
- Smell (Speculative Generality): wholesale `sys.modules` injection of fake `scipy`/`scipy.stats` with `norm` replaced by a plain *function* (`:39-44`) while real `scipy.stats.norm` is an object with methods (r1 adds: any code path calling `norm.ppf`/`norm.cdf` would get constant 0.5 rather than an error); if grid_tester ever touches another stats symbol the fake explodes opaquely (r2 + r1). Whole-`datetime` symbol replacement on the module with a `FixedDateTime` class is similarly broad (`:61-69`, works today since the module only calls `.now(tz)`).

### tests/test_llm_providers.py

- **[B-35] minor — broad `subprocess.run` patch on failure-path tests; successful stdout parsing unpinned** — `tests/test_llm_providers.py:53-61,64-77,80-93`: CopilotCLI failure-path tests patch global `subprocess.run`; any other shelling-out code in the import chain hits the stubs. None of these tests exercise a *successful* CLI generation path with output parsing (only failure modes + SDK-side success), leaving stdout-parsing of the CLI provider unpinned. (r1.)
- **[B-36] minor — timeout race margin** — `tests/test_llm_providers.py:220-227`: 0.05 s provider timeout vs 0.2 s fake sleep = 4× margin; event-loop timers are monotonic so r3 concludes sound, but r2 keeps a flake window on a pathologically loaded runner. Both readings preserved; kept minor either way. (r2 + r3.)
- **[B-37] minor — private `_timeout_seconds` assertion** — `tests/test_llm_providers.py:34`: couples to an internal attribute name for constructor plumbing. (r2.)
- Smells: `FakePermissionHandler`/`FakeClient`/`FakeSession` scaffold re-declared in five-to-six SDK tests (`:114-130,139-176,194-213,222-252,261-291` r2; r1 counts ~20-line blocks ×5) — one shared builder would halve the file (r1 + r2 + r3).
- Verified non-findings (r1+r2+r3): error paths pinned by exact messages ("executable not found", "timed out", "exit code 2", "empty response", AUTH_SESSION_ERROR); model allow-list rejection (`:191-216`); gpt-5→gpt-5.2 alias mapping through captured session kwargs (`:258-297`); nested payload extraction. No real binary invoked; NVIDIA key is a monkeypatched fake.

### tests/test_live_api.py

- **[B-38] minor — `load_live_account_stats_inputs` monkeypatched twice; second silently replaces first** *(verified)* — `tests/test_live_api.py:68-95` vs `:125-153(130)`: inside `test_live_endpoints_return_expected_payloads` the second registration makes the first — including its `slippage`/`slippage_bps` keys — dead setup; readers wrongly believe `/api/live/stats` assertions depend on slippage-bearing rows, and the endpoint's slippage pass-through is never exercised by the stats leg. Delete the first stub. (All three reviewers; the gpt-5.6-luna reference lists the same defect as t13, minor — agreement.)
- **[B-39] minor — WebSocket snapshot/update tests ride a real poll loop** — `tests/test_live_api.py:997-1064` & `:1193-1282(1283)`: correctness rests on the broker poller (`poll_seconds=0.05`, `viewer/main.py:3305-3323`) emitting the "update" frame before the context exits; no explicit synchronization — mildly flaky by construction under CI load; drive the queue directly instead. Verified not vacuous: `_stream_live_payload` really labels the second push "update". (r1 + r2 + r3.)
- **[B-40] minor — status-label disjunction** — `tests/test_live_api.py:991`: `assert signal["status"] in {"Waiting", "Pending"}` accepts two behaviors; pin the exact expected status. (r2.)
- **[B-41] minor — collection-time `sys.path.insert` hack** — `tests/test_live_api.py:20-21`: module-level path insert permanently pollutes interpreter state for every later module; redundant given pytest rootdir/conftest; can shadow modules. (r2 + r3.)
- **[B-42] minor — `>=`/`in` where mocked inputs guarantee exactly-one** — `tests/test_live_api.py:179-189`: `len(series) >= 1`, `len(trades) >= 1`, `deployment_id in ...` would miss accidental duplication bugs in payload building. (r2.)
- **[B-43] minor — win_rate semantics trap cemented by the suite** — `tests/test_live_api.py:800` (with `viewer/main.py:3743`): pins `win_rate == 20.0` for 5 executions with 1 win/1 loss — denominates by ALL executions including non-closing entries (1/5), not decided trades (1/2). The test faithfully encodes a production choice whose metric name invites misreading; flagging the semantics, not a wrong expectation. (r3; r1/r2 verified the same number correct against `viewer/main.py:3738-3772` — realized PnL books only round-trip pairs, drawdown 1100→880 = 220 = 20%.)
- **[B-44] minor — polluted deployment-id list locked in by weak assertions** — `tests/test_live_api.py:128,948` (with `viewer/main.py:3523-3532`): fixtures mirror production stuffing the account id and strategy names into the deployment-id list; tests assert only `deployment_id in payload["deployment_ids"]`, staying green as the list grows more polluted. (r3; r1 verified the odd stub mirrors production — merged as one finding about the *assertion weakness*.)
- **[B-45] minor — god-test spans ~8 endpoints** — `tests/test_live_api.py:24-209`: one failure masks the rest; split per endpoint. (r3.)
- Smells: execution-row dict literal appears three times in one test with drifting keys — the drift is exactly what hid B-38 (`:73-94` vs `:105-123` vs `:136-152`; all three concur, extract `_execution_row()`); RAMJET Service stub duplicated (`:231-244` vs `:430-443`, r2); every persistence function a lambda stub so CRUD flows (`:211-333`) verify routing/serialization only — `update_live_deployment_status` echoes whatever status it was given so the PUT test cannot catch a status-validation regression (Excessive Mocking; r1 judgement-call-minor, r3 major-smell — smell-tier disagreement preserved); every test re-imports `viewer.main` and re-stubs overlapping globals (~30 monkeypatch calls; Shotgun Surgery inherited from `viewer/main.py`'s ~11k-line design — r1 + r2); raw-dict payload assertions throughout (Primitive Obsession, r3); `_deployment_ids_for_account` mock readability note (Mysterious Name, r2).
- Security: none found — fake API keys only ("api-key"/"secret-key"); `PA3T0MQAGQ51` is an Alpaca paper id used as fixture data. Wrong-account Discord deployment → 400 pinned (`:426-466`).

### tests/test_live_config.py

Scanned clean (r1 + r2 + r3): valid parse asserted field-by-field; orphan-deployment warning captured structurally via `call_args.kwargs["extra"]["data"]["account_id"]`. r1's note that `assert_called_once` couples to logging-as-behavior is acceptable here since the warning is documented contract.

### tests/test_live_daemon.py

- **[B-46] minor — direct clearing of module-global block registries, no teardown** *(verified)* — `tests/test_live_daemon.py:78-79,140-143,213-215,232-233,242-243,370-371,395-396,421-422,438-441,456-459`: ten tests `.clear()` `_CTRADER_AUTH_BLOCKED_ACCOUNTS` / `_DISCORD_AUTH_BLOCKED_ACCOUNTS` / `_DISCORD_SERVICE_AUTH_BLOCKED_IDS` etc. directly; a mid-test failure leaks blocked-account state into later tests (order-dependent flakes). An autouse snapshot/reset fixture fixes all sites. Line-set variants across reviewers reconciled: r1 lists ten sites incl. `:232-233,:242-243`; r2 lists eight; r3 ten — union cited. (All three concur.)
- **[B-47] minor — sub-second timing budgets** — `tests/test_live_daemon.py:311,316,337-345` (r2/r3 add `:344`): `asyncio.wait_for(..., timeout=0.1)` around scheduling; event-gated so failures are loud spurious-timeouts rather than logic bugs; 100 ms is tight on loaded CI. (All three.)
- Smells: `_TerminalFailAdapter`/`_FailingAdapter`/`_TrackingAdapter` dataclasses repeat connect/disconnect counting (`:18-36`, `:81-95`) — one parameterized fake would do (r1); clear-globals prologue repeated (all three); three parallel daemon registries the tests must keep straight (Mysterious Name mirrored from production, r3).
- Hot spot (r2): direct manipulation of ~6 private module globals across most tests; renaming any breaks half the file.
- Security: none — fake tokens only. Positives (r2): failed connect does NOT thrash reconnects (`:39-74`); only cTrader credential changes clear auth blocks, Discord token change must not (`:202-228`); `:472-512` well-documented regression with forward-compatible guard (r3 concurs, plus `pytest.fail`-in-forbidden-hooks design praise). Verified non-findings (r1): the `if False: yield None` async-generator idiom (`:411-435`) is correct, not dead code; negative-persistence guard `:496-506` deliberate.

### tests/test_live_execution_safety.py

Overall (all three): high-quality mutation-driven suite; numeric expectations cross-checked against `risk/engine.py`, `risk/config.py`, `live_runner/daemon.py`, `live_runner/alpaca_adapter.py`, `live_runner/ctrader_adapter.py`, `shared/database.py` and all reproduce exactly (fallback_stop_pct=0.02; sizing `risk/(distance*pip)`; exposure estimate; fail-closed `_normalize_deployment_status`; SQLite pragmas). Findings:

- **[B-48] MAJOR — `isolated_db` fixture patches `DATA_DIR`, which does not locate the database** *(verified)* — `tests/test_live_execution_safety.py:632` (fixture `:622-635`): the engine URL comes from module-level `DB_PATH` built at import time (`shared/database.py:62-70`); `init_db()` touches `DATA_DIR` only for mkdir (`shared/database.py:834`). Isolation actually rests solely on the `ENGINE` patch; the `DATA_DIR` patch is dead and misleads readers into thinking path-based isolation exists — a future helper resolving paths from `DATA_DIR` (e.g. artifact writers, `shared/database.py:1082`) would silently write into the repo's real data dir. Remove it or make it load-bearing. (r2 alone; r1/r3 describe the same fixture but only as duplication — no contradiction, adopted from r2.)
- **[B-49] minor — reconcile window lower bound unpinned** — `tests/test_live_execution_safety.py:1893-1894`: `test_ctrader_reconcile_recovers_accepted_order_before_local_id_persistence` asserts only `lookup.fromTimestamp < signal_ts_ms < toTimestamp` while `_OrderListReq.__init__` initializes both to 0 — a regression scanning from epoch still passes; bound the window below as well. (r2.)
- **[B-50] minor — logger-handler surgery coupled to logging layout** *(verified)* — `tests/test_live_execution_safety.py:1432-1439`: picks the first handler that is a StreamHandler but not FileHandler on the module logger and swaps its stream via monkeypatch; hard-couples to `shared/logging.get_logger`'s exact layout (RotatingFileHandler + stdout StreamHandler, `propagate=False`) and JSON formatting; failure mode if config changes is spurious failure or asserting the wrong stream. `caplog`/dedicated handler sturdier. (All three concur.)
- **[B-51] minor — concurrency tests are WAL/environment-sensitive** — `tests/test_live_execution_safety.py:998-999,1354-1355` (r3 analyzes `:1002-1015` incl. the order-insensitive `sorted(...) == [False, True]` and strict `[True, True]` map results at `:1010` — deterministic by construction): ThreadPoolExecutor over SQLite WAL is sound on Linux/macOS (busy_timeout=30000, pragmas re-applied via the fixture listener at `:630`) but WAL journal mode can fail on exotic filesystems/network drives; environment-sensitive flake vector, worth a skip-marker guard. Kept minor (no reviewer claims active breakage).
- **[B-52] minor — explicit `ctrader_volume` override blesses under-minimum qty silently** — `tests/test_live_execution_safety.py:604` (with `live_runner/ctrader_adapter.py:759-761` checking the override before the qty floor): probably intentional ("explicit override wins"; r2 confirms fail-closed floor semantics match `ctrader_adapter.py:757-779`), but given neighboring error text about rejecting silent upsizing, the asymmetric enforcement deserves a documenting comment. (r1; r2 corroborates as intentional.)
- **[B-53] minor — reconcile backfill test's double-stub shape is inconsistent with siblings** — `tests/test_live_execution_safety.py:1727-1729(1738)`: fake response built as `type("ProtoOAOrderDetailsRes", (), {"deal": [deal]})` makes `deal` a class attribute (shared-state mutation risk if production ever appends to it — r3), the fake request has no `ctidTraderAccountId`/`orderId` fields so nothing verifies what was requested (the pagination sibling `:1781-1803` does), and it stubs raw `_send_request` while production calls `_send_request_with_refresh_retry` (`:1173`) — if the fetch method switched, the stub would silently never be called. (r1 + r3; r2's protobuf-fake hot spot corroborates.)
- **[B-54] minor — docstring overstates mutant-killing power** — `tests/test_live_execution_safety.py:233-261`: `test_alpaca_submit_order_blocks_non_positive_quantity` only exercises qty == 0; a mutant rejecting only strictly-negative qty passes; the comment claims more than the test kills. Test itself correct. (r3.)
- **[B-55] minor — compound assert hurts diagnosis only** — `tests/test_live_execution_safety.py:99-100`: fallback-stop compound assertion (`is not None and <`) is not a pure tautology (deleting the fallback fails clause one) but message-less compound makes diagnosis harder; split into two asserts. Judgement call; behavior coverage real. (r3.)
- **[B-56] minor — boundary at the broker volume floor untested** — `tests/test_live_execution_safety.py:601`: reject (0.07→70 units) and accept (5.0→5000) pinned but not the exact floor boundary (e.g. 0.1→100). Coverage gap, not a bug. (r3.)
- Smells: keyset-pagination harnesses duplicated between Alpaca and cTrader reconcile tests (`:1537-1590` vs `:1748-1808` — r1+r2; r3 adds a third copy `:1811-1898`, count disputed but the duplication itself agreed); redelivery trio repeats ~25 lines of client/monkeypatch setup each (`:382-444/:447-495/:498-537`, r2); duplicate `_NewOrderReq` fake protobuf classes inline twice (`:2015-2052` vs `:2094-2117`, r3); ~15–20+ LiveSignal construction blocks bypassing the existing `_live_signal()` factory (`:173`) — ~150 removable lines (all three); `_make_ctrader_adapter()` returns an undocumented `(module, adapter)` tuple used at ~15 call sites (r3).
- Hot spots: 2256 lines spanning four concerns (risk-engine boundaries, Alpaca adapter, cTrader reconciliation, DB migration/persistence durability) — Divergent Change; split per subsystem (all three). Protobuf faked via `type(...)` classes + SimpleNamespace enums (`:1727-1736`, `:1838-1882`, `:2015-2052`) — renames break many tests at once (r2).
- Verified non-findings (r1+r2): status-normalization DISABLED→UNKNOWN is deliberate fail-closed (`daemon.py:1716-1720`); RuntimeError path matches `alpaca_adapter.py:392-402`; no tautologies; act phases unwrapped; result-ordering of `executor.map` deterministic.

### tests/test_live_preflight.py

- **[B-57] minor — "default .accounts.env untouched" guard depends on host state** — `tests/test_live_preflight.py:342-344,363-365`: snapshots the real repo-root `.accounts.env` (via `preflight.DEFAULT_ACCOUNTS_ENV`) before/after; if the file is absent both reads yield None and non-interference isn't proven for a *delete+recreate*; if another test or concurrent process touches the file there is cross-test coupling, and the snapshot-compare is not race-safe under parallel execution. Monkeypatching `DEFAULT_ACCOUNTS_ENV` into tmp_path would be hermetic. (All three concur at minor; r3's inline self-correction — a newly created default file *would* fail the compare — preserved.)
- Security positives (r1 + r2): default-demo posture (`:19-21`), strict boolean parsing matrix incl. garbage values naming the variable (`:19-52`), numeric-account-id enforcement (`:121-148`); connectivity checks use clean fakes with monkeypatch (no network).

### tests/test_live_risk_sizing.py

- **[B-58] minor — sizing magnitude never checked** — `tests/test_live_risk_sizing.py:18-34`: `test_size_live_signal_sets_qty_for_valid_risk` asserts only `> 0`, although entry 100 / stop 98 / balance 10k / 1% risk gives a deterministic 50.0 via `risk/(distance*pip)` (same math pinned exactly in test_live_execution_safety.py:55-79); a sizing mutant producing half/double size passes. Add `pytest.approx(50.0)`. (r1; r2/r3 scanned clean — no contradiction.) Precomputed-qty passthrough (`:54-68`) and max_positions block verified against `risk/live.size_live_signal` (r1+r2+r3).

### tests/test_live_smoke.py

- **[B-59] minor — secret-leak guard greps field-name substrings in self-built data** *(verified)* — `tests/test_live_smoke.py:42-43`: `"api_key" not in str(payload).lower()` inspects data the test itself constructed that never contained credentials; cannot catch a future sensitive field named differently, while a value containing "key" elsewhere would false-fail. Fine as schema smoke; weak as leak guard. (r1 + r2 + r3 concur; security-positive intent preserved.)
- **[B-60] minor — discord-replay smoke's offline-safety is by construction only** — `tests/test_live_smoke.py:79-95`: runs real code paths with a 1 s timeout asserting fabricated order id "discord-smoke-order"; if someone later makes discord-replay hit Discord, this test attempts real I/O. A comment or explicit env guard would harden it. (r3.)
- **[B-61] minor — bare `except SystemExit` around help invocation** — `tests/test_live_smoke.py:47-50`: swallows argparse's exit without verifying the code; `pytest.raises(SystemExit)` would. (r2.)
- Smells: seven separate import statements from `scripts.live_smoke` (`:8-13`) instead of one grouped import (r2 counts seven, r1 five lines `:8-12` — cosmetic either way). Mode aliasing (`:59-76`) and help-text broker-minimum default pinned concretely (r1).

### tests/test_models.py

Smells only (all three): `test_file_roundtrip` uses `tempfile.TemporaryDirectory()` (`:187` / `:185-192`) while the rest of the area standardizes on pytest `tmp_path`. Otherwise clean: serialization round-trips, optional-field omission, winner/direction helpers, filename generation all concrete.

### tests/test_mt5_parity_artifact_loader.py

Scanned clean (r1 + r2 + r3). Bid-column backfill with canonical precedence (`:14-50`), m1_bid fallback selection (`:52-70`), EURUSD conversion 80 000 = 100 000/1.25 exact (`:73-80`), lot-step rounding (`:83-85`), shifted-stop anchoring pair with distinct exit reasons "signal" vs "stop_loss" (`:88-157`). Smell (minor): 4-row frame + entries/exits scaffold duplicated between `:88-119` and `:126-154`.

### tests/test_native_engine_execution.py

Scanned clean (all three): fixed-risk sizing 500/(100−95)=100.0 (`:41`) and MT5 tick-value sizing 500/(5·(1.0/0.01))=1.0 (`:62`) binary-exact; alias extraction pinned. Cosmetic: missing blank line between tests at `:42-43` (r1).

### tests/test_orchestrator.py

Scanned clean on behavior (layer ordering, run-all-layers-even-when-e2e-fails via fake `_run_layer`). Smell (Speculative Generality, r2): `_result()` always emits `passed=1, failed=…` but no assertion consumes pass/fail counts — unused shape. (r1 had noticed the same oddity as harmless.)

### tests/test_optimizer.py

No bug/security findings beyond assertion-strength minors below.
- **[B-62] minor — penalty mapping unpinned** — `tests/test_optimizer.py:474-491` (focus `:480`): `assert any(trial["penalty_applied"] ...)` never pins *which* trial was penalized; a regression penalizing window=4 instead of the zero-trade window=1 still passes. Assert params→penalty explicitly. (r2 + r3 concur.)
- **[B-63] minor — consumed dict view + no positivity check** — `tests/test_optimizer.py:579-580`: `sum(payload[...].values())` consumes the dict view — any later reuse silently sees an empty iterable; also no long-only positivity assertion on risk-parity weights. (r2.)
- **[B-64] minor — fake keyed on ISO-prefix string matching** — `tests/test_optimizer.py:453`: `config.start_date.startswith("2024-01-01T00:00:00")` couples the fake to production datetime serialization from `_split_backtest_period`; a formatting change misclassifies every call confusingly. Compare against the split boundary directly. (r2 + r3 concur.)
- **[B-65] minor — risk-parity test under-stubbed, relies on production fallback** — `tests/test_optimizer.py:537-583`: unlike siblings it stubs neither `ensure_backtest_data` nor coverage preflight; passes only because `_run_risk_parity_covariance_optimization` prefers returns derived from the base result (`runner/optimizer.py:544-548`) with ≥2 symbols of trades; if the fallback ever reaches the market-data branch the test hits the real data layer with an empty `data_dir`. One explicit stub or comment makes the dependency intentional. (r2.)
- Smells: `find_strategy_entry` SimpleNamespace stub re-typed six-to-seven times (r1 cites six sites `:289-296,:338-345,:374-381,:418-425,:502-509,:545-552`; r2 six with slightly different slices; r3 seven) plus `BACKTESTS_DIR`/`OPTIMIZATIONS_DIR` patching — extract `_standard_optimizer_mocks(...)` (~90 lines); Data Clumps `(backtests_dir, optimizations_dir, data_dir)` triple (r3).
- Hot spot (r2): patching `optimizer_module.optuna.create_study` while capturing the original attribute (`:431-439`, `:510-518`) hard-couples to Optuna factory/GridSampler semantics — an Optuna upgrade breaks four tests at once; consider a thin production seam.
- Positives (all three): forbidden-call guards pin study-creation-after-validation sequencing (`:278-406` incl. `ensure_calls[0]` kwargs); micro-range rejection (`:65-71`); inclusive upper bound at exactly 0.95 (`:41-47`); grid-search best selection window=3 on train AND OOS.

### tests/test_portfolio_optimizer_oos.py

- **[B-66] MAJOR — train_months story: dead knob enshrined as validated behavior** *(verified)* — two merged findings: `tests/test_portfolio_optimizer_oos.py:198-225` (`test_split_equity_curves_with_train_months`) documents `train_months` as "reserved but not yet implemented", asserts it is ignored (11 train points where 12 expected under the docstring), leaves a TODO; and `:509-554` (`test_portfolio_optimizer_with_train_months_oos_split`) promises an "18 months train" IS/OOS walk-forward in name/docstring but **never passes `train_months`** (only `test_months=6` at `:543-547`), asserting mere key presence (`"oos_sharpe" in result`, `"oos_split" in result`) — a regression putting every point in train (or test) still passes. Copy-paste error pair; misleading coverage. Either implement the parameter or mark it clearly unsupported. (r3 majors the pair; r1 and r2 file each half minor; reference T2-1 rates the ignored-contract half major — see verification log for settlement.)
- **[B-67] minor — mock_load keyed on call order** — `tests/test_portfolio_optimizer_oos.py:110-120(112-116),169-177(171-173),531-539(533-535)` (line variants across reviewers cited): strategies distinguished by `call_count[0] == 1`; if the optimizer changes iteration order or caches, strategy identities silently swap and assertions still pass (both curves upward-trending). Key the fake on `backtest_id`. (All three concur.)
- **[B-68] minor — Sharpe sign unpinned despite docstring promise** — `tests/test_portfolio_optimizer_oos.py:425-436`: "Should have positive Sharpe for positive trend" but body asserts only `isinstance(sharpe, float)` and `max_dd >= 0.0`; a sign-flip mutant making Sharpe always negative passes. (r1.)
- **[B-69] minor — single-curve split under-asserted** — `tests/test_portfolio_optimizer_oos.py:241`: only `len(test_curves[0]) > 0` where 12 monthly points with `test_months=3` yields exactly 4 (i ≥ 9); tolerates any boundary bug short of empty. (r2.)
- **[B-70] minor — 24-hour-wide timestamp window** — `tests/test_portfolio_optimizer_oos.py:37`: `1704067200 <= ts <= 1704153600` though `_parse_time_to_ts("2024-01-01T00:00:00Z")` is deterministically 1704067200.0 (tz-aware parse at `runner/portfolio_optimizer.py:37-38`); hides off-by-hours/tz bugs. Exact equality available. (r2 + r3 concur.)
- **[B-71] minor — `np.testing.assert_raises` instead of `pytest.raises`** — `tests/test_portfolio_optimizer_oos.py:503`: house style everywhere else is `pytest.raises` (better failure output, match support). (r2 + r3 concur.)
- **[B-72] minor — convoluted status compound reads like tautology trap** — `tests/test_portfolio_optimizer_oos.py:134,188`: `assert "status" not in result or result.get("status") != "error"` is logically fine but hard to read; simpler `result.get("status") != "error"`. (r3.)
- Smells: three near-identical mock_load/OOS harnesses (`:86-153,:155-195,:509-554`); `seconds_per_month`/`base_ts` literals repeated five times (r2 `:56,92-93,157-158,204-205,517-518`; r3 lists `:51,227,244` too); Speculative Generality — the reserved-but-unimplemented `train_months` parameter itself is speculative surface area the suite must keep explaining (r1+r2+r3); Mysterious Name on `:509`.
- Positives (r1+r2): split boundary math 12-train/gap/4-test reproduces exactly given the `train_end_ts >= test_start_ts` clamp (`:75-76`, `:222-223` vs `runner/portfolio_optimizer.py:48-110`); alignment-overlap semantics (`:274-320`); correlation-penalty sign regression guard (`:339-377`); path-traversal rejection with no-file-written follow-up (`:494-506`).

### tests/test_preflight.py

- **[B-73] minor — CLI tests mock `run_live_preflight` wholesale; config-path propagation unpinned** — `tests/test_preflight.py:164-171,180-188(189)`: exit-code mapping tested by design choice, but `mock_run`'s return value never asserts `config_path` propagation — a regression passing the wrong config path through would pass. Acceptable layering, note the gap. (r2 + r3 concur.)
- **[B-74] minor — error-message substring matches** — `tests/test_preflight.py:47-88`: all four `_validate_deployments` failure cases use `any("substring" in e ...)`; one exact match would catch accidental message concatenation/duplication. (r2.)
- **[B-75] minor — fragile YAML formatting + dead tail + unused import** — `tests/test_preflight.py:108-113,122-127,129-133` un-dedented triple-quoted YAML relying on leading-newline tolerance (r3); trailing `pytest.main` block `:224-225` dead under pytest (r2); `AsyncMock` imported at `:7` used nowhere (r2).
- Smells: config-writing boilerplate repeated; overlap with test_live_preflight.py covering similar ground — Divergent Change candidate, consider merging (r3).

### tests/test_qc_risk_parity_artifact_loader.py

Scanned clean (all three): TICKERS-ordered parquet universe in tmp_path, column order/values spot-checked exactly.

### tests/test_ramjet_signal_processor.py

Scanned clean (all three). SL/TP/TP1/trailing-config derived from ATR on synthetic data rather than hardcoded (`:54-70` / r2: computed from actual last close at `:60-61` — resilient yet falsifiable); BUY-only negative test (`:73-79`); default config.yaml pinned from repo tree (`:82-91`). Hot-spot note (r2): the committed-config coupling means any YAML tuning must update this test — worth a comment saying so.

### tests/test_request_logging_middleware.py

Scanned clean by all three reviewers (r2 has no section for this file — covered here from r1 + r3). Handler add/remove fixture with level restore (`:21-32`); method/path/status/duration payload + exc_info presence on 500s; quiet-polling paths log at DEBUG not INFO (`:70-82`); middleware logs avoid leaking bodies. No findings.

### tests/test_resample.py

- **[B-76] minor — tz-naive fixture timestamps** *(verified)* — `tests/test_resample.py:15,33`: `pd.date_range(...)` without tz while production-side fixtures in this area write UTC-stamped data; resampling naive vs aware can shift bucket edges across DST if resampling ever gains localization logic or runs in a non-UTC local zone. Low risk, inconsistent with repo tz-discipline. (r1 + r2 concur.)
- **[B-77] minor — `1h→4h` case lacks exact count** — `tests/test_resample.py:59-62`: only `0 < len ≤ n/4+1` where 500 hours ⇒ exactly 125 bars; weakest assertion in the file, unlike its siblings' exact counts. (r2.)
- Smells: `import numpy as np` inside both fixtures (`:16,34`) instead of top-level (r1+r2+r3); `tempfile` imported at `:3`, never used (r2).
- Verified non-findings: bar-count math 120→24 @5m, →8 @15m; OHLCV aggregation O=first/H=max/L=min/C=last verified bar-by-bar against source slice (`:64-73`); monotonic timestamps; invalid-timeframe rejection.

### tests/test_risk_consistency.py

- **[B-78] minor — tz-naive `datetime.now()` on LiveSignal** *(verified)* — `tests/test_risk_consistency.py:55,94`: every sibling uses `datetime.now(timezone.utc)`; sizing ignores the timestamp today so it's harmless here, but it normalizes naive usage inside live-signal construction — the very hazard the suite exists to police (UTC normalization pinned elsewhere, e.g. test_live_execution_safety.py:1200-1232). Use timezone-aware now. (All three reviewers; the gpt-5.6-luna reference independently lists the same defect — agreement.)
- **[B-79] minor — wrong-side-stop backtest asymmetry encoded as correct without a loud flag** — `tests/test_risk_consistency.py:110-130` (with the documented asymmetry block `:79-130`): backtest engine silently sizes positions using a stop on the wrong side of entry unless callers opt in via `metadata={"enforce_stop_direction": True}` (seen in test_risk_engine.py:108); the live engine rejects it. The test faithfully pins current behavior; flagged as an encoded-questionable-default deserving a loud production comment. (r3; r1/r2 treat the same asymmetry as verified-intentional — disagreement is about framing, not facts; kept minor per r3.)
- Verified non-findings: parity expectations 1000 = 1% of 100k; sizes 50.0 = 1000/(2×10), 20.0 = 1000/(5×10) reproduce exactly; identical defaults asserted field-by-field.

### tests/test_risk_engine.py

- **[B-80] minor — partial-exposure min() clamp unpinned** — `tests/test_risk_engine.py:57-89`: exposure case uses `current_exposure == max_total_exposure` exactly (boundary); no case exercises partial consumption (e.g. exposure 0.03 ⇒ risk_percent capped to remaining 0.02) so the min() clamp at `risk/engine.py:152` is unpinned by this file (covered indirectly elsewhere via broker-exposure estimation). (r2 alone; no contradiction.)
- Smells: `RiskEngine(RiskConfig(0.01, 0.05, 5, 2.0))` construction repeated four-to-five times (`:17-24,:58-65,:93-100,:128-135`) — `_engine()` already exists in test_live_execution_safety.py:44-52 and test_signal_bots_risk.py solved it with `_risk_engine()`: share one helper (r1+r2+r3 concur).
- Verified clean otherwise (r1+r2+r3): sizing 50.0 units exact, RR 2.0, exposure rejection reason, wrong-side stop gated behind `enforce_stop_direction` (`:92-124`), ATR fallback echo + `stop_source` (`:160`), decision-id formats, daemon qty=200 consistent with the safety-net file's identical scenario.

### tests/test_risk_parity_optimizer.py

- **[B-81] minor — winner-universe disjunction can't detect scoring flip** — `tests/test_risk_parity_optimizer.py:36`: `assert set(result["best_symbols"]) in [{"SPY","TLT"},{"SPY","GLD"}]` accepts either candidate; defensible given hand-picked returns, but a ranking regression flipping the winner arbitrarily (even to "last evaluated") passes as long as weights sum to 1. Pin the actual winner with a comment explaining why. (All three concur; r2 adds Mysterious Name framing.)
- Verified sound: weights ≥ 0, contributions sum to 1, max-min spread < 0.01, deterministic deduped universes in exact expected order.

### tests/test_runner_cli.py

- **[B-82] minor — args built as SimpleNamespace, parser never exercised** — `tests/test_runner_cli.py:63-78,101-116,139-157`: an argparse default change or new required flag keeps tests green while real CLI invocations break; one test going through the real parser closes the gap. (r2.)
- **[B-83] minor — exact float equality on total_return** — `tests/test_runner_cli.py:86`: `== 0.05` passes because the stat derives from integer-ish PnL division (5000/100000 canonical double); an equity-ratio-derived implementation (100000×1.05 → 0.050000000000000044) would fail. `pytest.approx`. (r3; r1 verified 0.05 correct for the dummy engine's curve — same fact, different concern.)
- Smells: 14-field `SimpleNamespace(...)` args block repeated three times — `_run_args(**overrides)` (r2); `_DummyEngine.validate_config` returning unused `[]` (Speculative Generality, trivial, r3).

### tests/test_scheduler_signal_bot_health.py

Covered from r1 + r3 only (r2 has no section for this file — see coverage note).

- **[B-84] MAJOR — error-detection test blind to error handling** *(verified)* — `tests/test_scheduler_signal_bot_health.py:36-37,62-63` (body `:42-63`; happy path `:15-39` milder): everything mocked and assertions are only `mock_check.called` + `mock_upsert.call_count >= 2` — healthy and unhealthy scenarios produce identical assertion sets; the job could record `"status": "ok"` even when bots report errors and `test_signal_bot_health_check_job_detects_errors` stays green. Assert recorded status/error fields on captured `mock_upsert.call_args`. (r3 major, r1 minor — severity dispute settled below; the gpt-5.6-luna reference lists the missing-assertions defect as T2-3, minor.)
- **[B-85] minor — import-smoke test asserts near-nothing** — `tests/test_scheduler_signal_bot_health.py:10-12(13)`: `callable(run)` only; harmless but tautology-class. Kept as import smoke by both reviewers who mention it.
- Smell: Excessive Mocking — the job's entire interaction with persistence is stubbed; fold real argument assertions into existing mocks rather than adding more tests (r3).

### tests/test_sentry_config.py

Scanned clean by all three reviewers — security-positive testing done right: DSN-less disable, enabled-without-DSN fails closed (`:16-18`), sample-rate validation, `send_default_pii` defaults False (`:46`), `before_send` redaction asserted across headers / extra keys / nested lists / message-body regex (`Authorization`/`api_key` redacted while `safe=yes` survives, `:50-79`), validated option pass-through, scoped capture context. Synthetic DSNs only; `_reset_for_tests()` usage is appropriate global-state hygiene (r2).

### tests/test_service_discovery.py

Scanned clean by all three reviewers. Env-file discovery maps three Discord bots' token-env-key/channel-id pairs via a tmp `.accounts.env`; `DEFAULT_ACCOUNTS_ENV` monkeypatched into tmp_path correctly (no host state touched); obviously fake placeholder tokens only.

### tests/test_shared_logging.py

Scanned clean by all three reviewers. JSON log formatter: fixed-epoch record pinned to exact timestamp string + regex; degraded LogRecord (empty module/funcName, bogus lineno) falls back to `<unknown>`/0 without crashing, exercised with deliberately corrupted fields (`:43-45`). Correct and focused.

### tests/test_signal_bot_health_check.py

Covered from r1 + r3 only (r2 has no section — see coverage note).

- **[B-86] minor — stdout grep couples to dict serialization order/quoting** — `tests/test_signal_bot_health_check.py:28-35` (r1 focuses `:28-35`): `'"status": "ok"' in captured.out` breaks silently if output switches to compact separators or reorders keys; prefer `json.loads(captured.out)` then assert fields. (r1.)
- **[B-87] minor — lower-bound registry assertion tolerates bloat** — `tests/test_signal_bot_health_check.py:22`: `len(result["results"]) >= 5` with a comment enumerating expected bots; registry duplication/bloat passes silently. Prefer exact set comparison. (r3.)
- Note (r3): depends on repo-tracked `signal_bots/*/config.yaml` files (`:18-26`) — environment-stable within the repo, acceptable. Registry-shape smoke checks `:6-25` reasonable (r1); the gpt-5.6-luna reference flags isolation concerns for `:6-25` (T2-4) which the panel did not corroborate — noted as unconfirmed reference entry, not adopted.

### tests/test_signal_bots_risk.py

- **[B-88] minor — near-vacuous importability test + odd config key** — `tests/test_signal_bots_risk.py:41-51` (r2 cites `:41-50`): `test_signal_processors_are_importable_with_process_signal` asserts only `callable(getattr(...))` on three processors — cannot fail short of deleting the method; keep only if intended as import smoke. Additionally `NitroSignalProcessor({"ram_jet": ...})` at `:45-47` is constructed under ram_jet's config section — if intentional config reuse it deserves a comment, otherwise it silently encodes an odd contract. (r1 + r2 concur.)
- Verified non-findings (r1+r2+r3): sizing parity computed from the actual resolved stop relationally (`:67-70`), ATR-fallback stop flow, trailing-stop tightening direction, exposure-reduction monotonicity at boundary exposure 0.047 < cap 0.05 (`:119-144`) — genuine behavioral checks consistent with `risk/engine.py`.
- Smells: `_risk_engine()` duplicated with test_live_execution_safety.py:44 and inline copies in test_risk_engine.py (r1+r2); market-data helpers duplicated with test_ramjet_signal_processor.py (r1).

---

## Cross-cutting observations

1. **Divergent `isolated_db`/engine-redirect fixtures** *(verified by grep during synthesis)* — fixture definitions: `isolated_db` at test_database_migrations.py `:141` (+ separate `legacy_db` at `:346`, + a raw `create_engine` at `:66`), test_ctrader_live_fix.py `:237`, test_live_execution_safety.py `:623`; ad-hoc engines additionally in test_portfolio_optimizer_oos.py `:562` — r1: "only the last attaches the production SQLite connect listener, shared/database.py:81-87"; r3 concurs the live_execution_safety copy must hand-re-attach the PRAGMA listener. **Copy-count disputed between reviewers** (three/four/five) — reconciled in verification log #8. One shared conftest fixture would fix all sites and remove the quietly-non-isolated failure mode behind B-17/B-32/B-48.
2. **Real-database writes from unit tests**: test_database_migrations.py `:33` and test_grid_tester_sqlite.py `:93/:95, :130/:132, :152/:154` run against whatever DATA_DIR resolves to (repo `data/` under conftest defaults) — B-17 + B-32.
3. **Suite-wide duplication**: LiveSignal construction (~25–40 sites across files, all three), adapter constructors (~14–15× ctrader_adapter), aiohttp fakes ×4, LLM provider fakes ×5–6, optimizer mocks ×6–7, risk-engine builders ×3 files — a `tests/helpers.py` would remove several hundred lines.
4. **Recurring sub-second timing windows** (`timeout=0.01/0.1`, `sleep(0.05–0.2)`) — event-gated and currently deterministic, but CI-load-sensitive (B-07/B-14/B-36/B-47/B-51).
5. **Security posture of the suite itself is good** (all three): fake credentials throughout; positive regression coverage for Sentry PII redaction, credential-file 0600 permissions + cross-process flock, fail-closed auth/deployment-status normalization, CTRADER_DEMO strict parsing, JVM trust-path allow-list, path-traversal rejection, smoke-report secret-field absence. No skipped/disabled tests without reason anywhere in the chunk; no mutable-default arguments; subprocess usage monkeypatched or timeout-guarded.

---

## Verification log (bounded pass, 8 spot-checks)

Scope rule applied: only reviewer-vs-reviewer flat contradictions and critical-severity findings were verified, by reading the source under `tests/` (and the specific production line a citation leaned on). Log:

1. **B-10 / B-11 / B-12 severity (r3 critical vs r1/r2 major; r1 minor for B-12)** — read `tests/test_ctrader_live_fix.py:250-296`. Confirmed verbatim: local `expected = 5.0` with asserts `-expected < 0` / `expected > 0` (`:268-281`); literals `position_status_open = 1` / `position_status_closed = 2` asserted unequal ("per the adapter code", `:284-295`); `adapter._symbol_name_to_id = symbol_map` then read back (`:254-265`). No production call in any of the three. Severities settled **critical / critical / major**, following r3 and the tests-a panel precedent ("asserts nothing about claimed live-money behavior" ⇒ critical), over r1/r2's major for B-10/B-11 and r1's minor for B-12 (which unlike its siblings does construct a real adapter — just exercises no logic).
2. **B-24 severity (major vs r1's minor)** — read `tests/test_download_grid_market_data.py:85-95` and sibling `tests/test_grid_market_data_download.py:68-73`. Confirmed bare assignment `dl.DukascopyNativeDownloader = MagicMock()` with no restore at `:92`, and the sibling's scoped `monkeypatch.setattr` at `:71`. Settled **major** (session-wide pollution of production module state; 2-of-3 reviewers; mechanism confirmed).
3. **B-33 existence (r1 silent vs r2+r3 major)** — read `tests/test_grid_tester_sqlite.py:150-183`. Confirmed membership-only assertions (`id_1 in ids`, `id_2 in ids`), no ordering check. Adopted as **major** on r2+r3 agreement plus independent confirmation by the reference report (t8, rated minor there — mechanism identical; panel keeps the reviewers' major since the name/docstring explicitly claims ordering).
4. **B-32 internal severity split (r1 minor vs r2/r3 major)** — read `tests/test_grid_tester_sqlite.py:90-150` and `tests/conftest.py:11-15`. Confirmed bare `init_db()` with no fixture at `:95/:98`, `%H%M%S` id suffixes, writes into `$DATA_DIR/backtests.db` resolved from repo `data/`. Settled **major** (identical defect class to B-17 which all three rated major; consistency wins — recalibration is exactly the "reviewers clearly disagree" case).
5. **B-66 severity split (r3 major pair vs r1/r2 each-half-minor; reference T2-1 major)** — read `tests/test_portfolio_optimizer_oos.py:195-225` and `:509-554`. Confirmed: docstring admits "reserved but not yet implemented", TODO at `:224`; `:543-547` passes only `test_months=6`; result assertions are key-presence only. Settled **one merged major**: the claim-vs-body mismatch spans two tests and enshrines a dead public parameter as validated behavior (r3 + reference agree on major for the substance; r1/r2's minor ratings addressed only each half in isolation).
6. **B-84 severity (r3 major vs r1 minor)** — read `tests/test_scheduler_signal_bot_health.py:10-64`. Confirmed assertions are exactly `mock_check.called` and `mock_upsert.call_count >= 2` with zero argument inspection, in the test *named* "detects_errors". Settled **major**: the test cannot catch the one regression its name claims (same criterion that made B-33 major; r1's own finding text agrees the error-recording mutant would pass — the disagreement was calibration only).
7. **Reference-report T4 (ctrader flock child left alive on timeout — resource leak, major) vs panel B-14** — read `tests/test_ctrader_live_fix.py:196-233`. The child is spawned via `subprocess.Popen` from an argv list, parent sleeps 0.2 s, asserts `writer.poll() is None`, then reaps with `writer.wait(timeout=2)` inside the flow (`:228`) and asserts returncode 0 plus post-lock file contents. On the normal path there is no leak; the reference's resource-leak scenario would require `wait(timeout=2)` raising an unhandled TimeoutExpired (a wedged child), which no panel reviewer flagged as written. **Flagged as unresolved reference disagreement, NOT adopted**: only the timing weakness stands (B-14, minor).
8. **isolated_db copy-count contradiction (r1 "three", r3 "four", r2 "fifth copy")** — counted engine-redirect definitions across the four files: test_database_migrations.py defines two locally (`isolated_db`, `legacy_db`), plus one each in test_ctrader_live_fix.py and test_live_execution_safety.py, plus ad-hoc engines in test_portfolio_optimizer_oos.py. All three counts are reconcilable depending on whether local multi-copy definitions are bundled; the substantive point (divergent, hand-rolled, PRAGMA-inconsistent copies needing one conftest fixture) is unanimous. Recorded once in cross-cutting #1 with the count ambiguity noted.

Not spot-checked (below threshold per scope rule): B-01 (r1 explicitly declines to file what r2 files minor — both readings preserved in place), B-38 (unanimous + reference concurrence), B-78 (unanimous + reference concurrence), B-17 (unanimous major).

---

## Coverage statement & finding totals

**Coverage — 49 tracked files reconciled.** Chunk B is the alphabetical middle of the root-level tracked `tests/` entries from `tests/test_backtest_grid.py` through `tests/test_signal_bots_risk.py`. Enumerating the repository shows that range contains **49 tracked files**: 48 pytest modules plus one non-module entry, `tests/compare_metrics_quantstats.py` — a 284-line standalone metrics-validation *script* (`python tests/compare_metrics_quantstats.py`; compares `runner.stats` Sharpe/Sortino/Calmar/MaxDD against quantstats on synthetic scenarios; contains **no `test_*` functions and no assertions**, single `__main__` block at `:203`). This resolves the reviewers' split self-claims: r1/r2 said "49 files", r3 said "48 **test** files" — both were counting correctly over different sets. **Flagged explicitly rather than absorbed: `compare_metrics_quantstats.py` was not reviewed by any of the three panelists** (none of the three reports mentions it); since it defines no tests, the merged findings are unaffected, but a follow-up review of that script is owed if the brief intended it in scope.

Of the 48 test modules, every one has a dedicated section above: **31 files produced counted findings** (B-01…B-88); **17 were scanned with zero counted findings**: test_capm, test_catalog, test_ctrader_deployment, test_daily_equity, test_dolthub_manager, test_grid_market_data_download, test_live_config, test_models, test_mt5_parity_artifact_loader, test_native_engine_execution, test_orchestrator, test_qc_risk_parity_artifact_loader, test_ramjet_signal_processor, test_request_logging_middleware, test_sentry_config, test_service_discovery, test_shared_logging (several carry smells/positives recorded inline but no counted bug/security finding). Reviewer self-coverage caveat stands: r2 claims 49 files read but enumerates 45 — its report omits `tests/test_scheduler_signal_bot_health.py`, `tests/test_signal_bot_health_check.py`, and `tests/test_request_logging_middleware.py`, which are covered here from r1 + r3.

**Finding totals (counted categories: bugs/correctness risks + security issues; Fowler smells/hot spots preserved per file but uncounted, matching the reviewers' own tally conventions):**

| Severity | Count |
|---|---|
| Critical | 2 |
| Major | 7 |
| Minor | 79 |

Total: **88 counted findings** (B-01…B-88) across the chunk. Criticals: B-10, B-11 (vacuous cTrader position-safety tests on a live-money path). Majors: B-12 (attribute-echo mapping test), B-17 (real-DB migration test), B-24 (MagicMock session leak onto production module), B-32 (real-DB grid-tester trio), B-33 (ordering test never checks order), B-48 (misleading DATA_DIR isolation), B-66 (train_months dead-knob pair), B-84 (health-check error detection blind). Reviewer baselines for calibration: r1 = 0C/3Maj/35min · r2 = 0C/8Maj/47min · r3 = 2Crit/8Maj/51min. Severity recalibrations made during synthesis, each justified in the verification log or inline: B-12 minor→major (verification #1), B-24 minor→major (#2), B-33 adopted major on r2+r3 (+reference mechanism match) (#3), B-32 minor→major for consistency with the unanimous major B-17 (#4), B-66 two minors→one merged major (#5), B-84 minor→major (#6); B-10/B-11 kept at r3's critical over r1/r2's major (#1).




---

## misc-backend

**Expert-panel merged review — model ox-alpha — 3 blind reviewers (r1, r2, r3).**
Area: risk engine, scheduler, operational scripts, shared infrastructure, signal bots, trading strategies.
Scope: 71 tracked code files in `risk/`, `scheduler/`, `scripts/`, `shared/`, `signal_bots/`, `strategies/`, plus root-level `pyproject.toml`, `pytest.ini`, `live_deployments.yaml`.
Method: reviewers worked blind and independently; this document is a pure merge/dedup preserving all original citations (`file:line` + reviewer tag). Contradictions are settled by reading code under the area directories, never by vote; settlements are logged inline as **[Settlement Sn]** entries appended after verification. The external reference report was consulted only for entries relevant to this area and treated strictly as hints-to-verify against reviewer citations; agreements/disagreements with it are flagged where material.
Severity scale: critical (data loss / money loss / security breach), major (correctness bug or likely production incident), minor (smell, robustness, style-with-consequence). Baseline smells use Fowler names; where a reviewer explicitly tagged a smell finding at major it is carried at major, otherwise smells default to minor.

---

# Group 1 — `shared/` infrastructure

## shared/database.py (3,891 lines)

### (a) Bugs / correctness risks

- **major — import-time `ENGINE` + import-time `load_dotenv` freeze configuration.** `shared/database.py:70-77` creates the SQLite engine at import against `DATA_DIR/backtests.db`; `load_dotenv()` runs at import (r1: line 61; r2: 60-63; r3: 61, citing `constants.py:141`). Env-dependent paths and the engine URL freeze at first import order; tests/tools setting `DATA_DIR` afterwards silently target the wrong DB; there is no way to inject or re-point a session/engine (r2 notes ~50 functions call `init_db()` at top; tests monkeypatch globals). *Severity history:* major (r1, r3) vs minor (r2, who split it into two minors) — carried at major (2-of-3 + root-cause-of-several-seams agreement).
- **minor — `subprocess.run(["git", ...])` without `timeout=`** at `shared/database.py:739-745` (inside `upsert_verification_evidence`); a wedged git blocks the calling thread indefinitely. All three reviewers flagged minor; `shared/sentry.py:267-274` (`timeout=2`) is the in-repo pattern to copy (r1, r2, r3).
- **minor — duplicated statement in migration:** `execution_column_names = _table_column_names(connection, "live_executions")` executed twice back-to-back at `shared/database.py:1278-1285` (r1: 1280-1282; r2: 1278-1285; r3: 1277-1282). Harmless copy-paste residue. *Severity history:* r3 labeled this bullet "major" while describing it as harmless residue — clear scale disagreement, recalibrated to minor (all three descriptions agree it is functionally harmless). **[Settlement S4: verified in source — see log at S4.]**
- **⚠ disputed — `load_live_account_stats_inputs` conflates account ids, deployment ids and strategy names in one IN-list** (`shared/database.py:2297-2302`): set seeded with `normalized_account_id`, then each deployment id *and* raw `strategy_name` added, all queried as `live_signals.deployment_id IN (...)`. r1: minor (fuzzy legacy-compat, misattribution only on collision); r2: **major** ("conflating three identifier namespaces … correctness hazard for money statistics"); r3: minor (deliberate undocumented legacy-compat hack). External reference MISC2 rates the same code **major**. Flat reviewer-vs-reviewer contradiction → settled by code read. **[Settlement S4 — see log.]**
- **dead code — `_backfill_curve_for_id` (`shared/database.py:1066-1091`)** defined, never called anywhere in the repo (r1 grep-verified; r2 grep-verified, actual backfill path is `_equity_points_curve` + `_artifact_equity_curve` at 1223-1248). Speculative Generality. *Severity history:* minor (r1) vs major (r2) vs folded-into-traversal minor (r3) — recalibrated per settlement. Related: the dead helper performs the `DATA_DIR/"backtests"/f"{bt_id}.trades.json"` join **without** the `Path(str(bt_id)).name == str(bt_id)` traversal guard that `database.py:1262` applies to live callers (guard evaluated after `_artifact_equity_curve` at 1225 already built paths from `bt_id`) — inconsistent defense-in-depth (r1, r3). **[Settlement S4/S5 — see log.]**
- **minor — two competing "now" conventions:** `_utcnow_naive()` (324-326) stores naive UTC while `_now_iso()` (693-694) writes tz-aware ISO strings; mixed naive/aware datetime handling across tables; `ServiceModel.last_activity` accepts arbitrary caller datetimes (533, 1862) with no normalization, so a local-time caller poisons `created_at.desc()` ordering (r1, r3; companion: `shared/models.py:104` naive default, r2/r3 under models). Checklist tz-naive item.
- **minor — `save_portfolio` positional component-id coupling:** delete-all + re-insert enumerating `start=1` (3696-3713) while `load_portfolio` (3735) pairs persisted `component_id`s positionally over reloaded rows; docstring at 3655 says "positional default". Correct today, fragile off-by-one/wrong-pairing surface (r1, r3).
- **minor — `get_grid_results(limit)` has no upper bound** (3843/3889), unlike every other list function's `_bounded_query_limit`; a caller passing 10**9 loads the table (r1).
- **minor — `load_backtest_result` does not normalize/strip `backtest_id`** (3624-3627) unlike sibling functions' `normalized_id` pattern (r2).
- **minor — `list_verification_evidence_summary` fetches `limit*20` rows then groups in Python** (2860-2867): summaries can silently drop flows past the fetch cap (r2) and deserialize up to ~40k rows through Pydantic per request where a GROUP BY would do (r3).
- **minor — fragile `BEGIN IMMEDIATE` handoff:** `save_live_execution_up_to_cumulative` opens it via `session.connection().exec_driver_sql(...)` (1420-1456); works only because the session has not autobegun; future ORM-read-before-this-line refactor yields "cannot start a transaction within a transaction". Needs comment/test (r2).
- **minor — `list_live_signals_for_deployments` loads executions for up to 1000 signals with no LIMIT** (2145-2149); chatty fan-out ⇒ unbounded query (r3).
- **minor — slippage sign convention unsigned relative to side** (`database.py:2252`): favorable fills on sells also record positive slippage; consumers must know action to interpret; field name doesn't convey directionality (r3).
- **minor — race/idempotency shape:** lookup-before-insert patterns depend on unique-index invariants rather than one atomic upsert (reference hint misc-m5 at `database.py:1390-1408`; consistent with r2's transaction-handoff note above — adopted as minor, same seam).

### (b) Security

- **minor — raw-SQL hygiene is broadly good, with two pattern risks.** Values are bound throughout (`:id_N` placeholders at 3331-3336; f-strings interpolate only generated placeholder names / quoted identifiers at 883-885, 939, 946) (r1, r2). Caveats: (i) `_rebuild_live_signals_without_global_discord_unique` executes `text(rebuilt_sql)` (893) where `rebuilt_sql` comes from `sqlite_master.sql` — exploitable only by someone who can already write the schema; informational but string-built DDL execution (r1); (ii) `PRAGMA table_info('{table_name}')` f-string (841, 939/947): all current call sites pass literal constants so not exploitable today, but no `^[A-Za-z_]+$` guard invites injection on first dynamic caller (r2, r3); (iii) `_equity_preview_query_params` interpolates int-coerced `normalized_limit` into SQL arithmetic (3327-3418, coerced at 3462-3466) — safe as written, fragile pattern (r2).
- **minor — `load_dotenv(PROJECT_ROOT / ".env")` executes at import in a shared library,** three times per process across `database.py:61`, `constants.py:36`, `sentry.py:18`; `.env` shadowing silently changes production behavior depending on import order (r3; same root as the major import-freeze finding above).

### (c) Baseline smells

- **God module / Divergent Change (carried at major per r3's explicit tag):** one 3,891-line module mixes ≥6 reasons to change: engine+PRAGMAs, schema migrations (845-1308), live signal/execution/balance/account persistence (1310-2030), service registry (1768-1882), background tasks + verification evidence (2668-3233), backtest/portfolio/grid CRUD (2319-3891) (r1, r2, r3 — r3 prescribes split per reason, e.g. `storage/backtests.py`). De-facto import hub for runner/live_runner/viewer/scheduler/scripts (20+ dependents, r2) maximizing blast radius.
- **Duplicated Code — row unwrapping ×5-6 (carried at major per r3's tag):** `_normalize_task_record` (805), `_coerce_backtest_row_id` (2475), `_extract_backtest_and_trade_count` (2576), `_unwrap_equity_point_row` (3270), `_unwrap_model_row` (3481), inline again at 2639-2645 — five/six implementations of one "_mapping/tuple/model → model" shape (r1, r2, r3). Related: `_bounded_query_limit` (2051-2057) duplicates the inline try/int/min pattern at 1581-1584, 1619-1622, 2820-2823 (r1); normalize-id/init_db/session preamble repeats across functions (r2); `save_result_to_sqlite` delete-children block (2323-2326) duplicates `delete_backtest`'s (3261-3264) (r3).
- **Repeated Switches — signal-source normalization ×3 (carried at major per r3's tag):** the `{"internal","discord"}` juggling re-implemented at 591-599 (`from_live_deployment`), 624-643 (`to_live_deployment`), 1906-1923 (`_resolve_deployment_signal_source`) — subtly different copies; changing the allowed-source set needs three coordinated edits (r1, r2, r3).
- **Primitive Obsession:** statuses/types/ids/timestamps as bare `str` (571 `status: str = Field(default="ACTIVE")`; ISO strings compared lexically at 1607, 2903, 3843; task statuses vs `_FINAL_TASK_STATUSES` string set at 95-102) despite existing enums; drives the defensive `.strip().lower()` calls repeated ~30×; mixed datetime columns (live tables) vs str columns (tasks/evidence) for the same concept (r1, r2, r3).
- **Data Clumps:** the ten-kwarg `(task_id, task_type, status, config_json, progress_message, name, description, source, created_at, updated_at)` threaded through the evidence/task helpers (2922-3134) — bundle into a TaskSnapshot (r3; r1/r2 note the same clump family).
- **Hand-rolled migration subsystem:** `_migrate_schema` + 10 `_add_*`/`_migrate_*` helpers (845-1307) re-implement Alembic; steps silently depend on execution order (backfill before uniqueness at 1024-1027); no versioning ⇒ fresh vs legacy DB exercise different paths per boot; DDL regex-rebuild brittle (r1, r2, r3). Extract/dedicate a migrations module.

### (d) Maintainability hot spots
- `_equity_preview_query_params` (3327-3418) embeds two large raw SQL strings that must mirror `equity_points` schema by hand (r1).
- Migration framework deserves extraction with tests isolated from business queries (r2, r3).

## shared/models.py (749 lines)

### (a) Bugs / correctness risks
- **minor — `Trade.direction_sign` returns `-1` for anything not exactly `"LONG"`** (418-429 / 427-429): `"long"`, `"Long"`, corrupted values silently treated SHORT while PnL math flips sign; `direction` never validated against `VALID_DIRECTIONS` (declared at models.py:284) anywhere in the module (r1, r2, r3). Companion: `Trade.is_winner` treats `pnl == 0` as a loser (r2).
- **minor — contradictory contract on exits:** comment says `exits` has "at least 1 element" (340) yet `last_exit_time` falls back to `entry_time` (423-425); the fallback hides corruption instead of raising (r1).
- **minor — tz-naive default:** `Service.created_at` defaults naive UTC (`replace(tzinfo=None)`, models.py:104) while `LiveSignal.timestamp` stays aware — inconsistent time semantics in one file (r2, r3).
- **minor — two sources of truth for vocabularies:** `Literal["alpaca","ctrader"]` (161) / `Literal["ACTIVE","PAUSED","STOPPED"]` (186) validated independently in database.py, which accepts lowercase statuses for services but uppercase-only for deployments — already drifted (r2). `VALID_TIMEFRAMES`/`VALID_DIRECTIONS` re-declared here (283-284) duplicating `shared/constants.py:14-15` (r1, r2, r3).
- **minor — `datetime.fromisoformat(...replace("Z","+00:00"))` (744)** parses naive timestamps fine and feeds `strftime` filename generation without tz normalization (r1).
- **minor — unvalidated id/name → path construction:** `BacktestResult.filename` (748) lets strategy names containing `/` or spaces flow into filenames; `save()` `mkdir(parents=True)` (727) creates nested dirs unexpectedly (r3).
- Reference-hint cross-check: misc-m1 (numeric validation of prices/SL/TP in `risk/models.py:38-56`) — reviewers r2/r3 partially covered under risk/models finiteness gaps; kept there, not double-counted here.

### (b) Security — nothing found (pure data classes; JSON round-trip via stdlib) (r1, r2, r3).

### (c) Baseline smells
- **Duplicated Code:** `BacktestStats.from_dict` (620-625) and `PortfolioStats.from_dict` (667-672) byte-identical; the omit-empty `to_dict` loop repeated across `StrategyMeta` (52-73), `TradeExit` (302-324), `BacktestMeta` (491-534), `Trade.to_dict` (358-390) (r1, r2, r3).
- **Divergent Change:** mixes four domains — strategy metadata, live-trading API models, portfolio persistence, backtest canonical format (r1, r2, r3).
- **Inconsistent layering:** Pydantic BaseModel for live/portfolio models vs plain dataclasses with hand-written serialization for backtest models — two idioms, undocumented, trips newcomers (r1, r3).
- **Primitive Obsession / Data Clumps:** times as bare strings ("ISO 8601 UTC" by convention only); `position_size/risk_amount/risk_percent` triple travels through `Trade`, `TradeRecord`, `_apply_backtest_trade_risk_fallbacks` (`database.py:3524`) — RiskMath value-type candidate (r1, r2).

### (d) Hot spots — `Trade`/`TradeExit`/`BacktestResult` are the canonical interchange format with the area's highest fan-out (every field addition touches `to_dict`, `from_dict`, SQLModel records, viewer) (r1, r2).

## shared/constants.py (178 lines)

### (a) Bugs / correctness risks
- **⚠ disputed — DATA_DIR resolution probes the filesystem and picks the highest-scoring home candidate when `DATA_DIR=~`** (`shared/constants.py:91-103` scoring weights 100/30/20/10 at `_data_dir_score`, resolution at 106-138/141): winner depends on `$HOME`, `pwd` ownership of the checkout (74: `pwd.getpwuid(PROJECT_ROOT.stat().st_uid)`), `SUDO_USER`, and existence of `ohlcv/` etc. Two processes under different users (systemd service vs operator shell) or an empty intended dir can resolve **different data directories** — hence different `backtests.db` files with live executions — with no warning (r1: major "split-brain"; r3: major nondeterminism + surprise-writes; r2: minor "surprising implicit behavior"). Carried at major (2-of-3; trading-data integrity consequence).
- **minor — import-time side effects:** `load_dotenv` at 35-36 plus filesystem stats (74) at import; importing constants mutates env-dependent global config; `load_dotenv` must precede `os.environ.get` (36→39) with no test guarding the order (r1, r2, r3).
- **minor — `VALID_TIMEFRAMES`/`VALID_DIRECTIONS` duplicated verbatim in models.py** (14-16 vs models 283-284) — drift risk (r2, r3; r1 from the models side).

### (b) Security — nothing found; read-only resolution, `resolve_executable` uses `shutil.which` (r1).

### (c) Baseline smells
- **Speculative Generality:** `_home_candidates` (50-88) gathers HOME/pwd/project-owner/SUDO_USER/USER candidates for a corner case a single documented expansion rule would cover (r2, r3).
- **Mysterious Name / magic numbers:** `_home_candidates`/`_data_dir_score` encode heuristic policy the names don't convey; weights 100/30/20/10 unexplained (91-103) (r1, r3).
- **Primitive Obsession:** timeframes as bare strings + tuple whitelist + alias map (14-21) rather than a small Timeframe type; every consumer re-validates (r1).

### (d) Hot spots — `_resolve_data_dir` + helpers (~100 lines) dominate module complexity; hidden global-config hub freezing whichever `.env` was readable first (r1, r2).

## shared/logging.py (100 lines)

### (a) Bugs / correctness risks
- **minor — `get_logger` check-then-act on `_loggers` without a lock** (74-99/74-75): concurrent first calls attach duplicate handlers → doubled log lines. Race-on-shared-state checklist item; low likelihood, self-limiting (r1, r2, r3).
- **minor — `LOG_LEVEL` frozen at import** (27, 78): later `os.environ["LOG_LEVEL"]` changes ignored — surprising in long-lived schedulers/tests (r1, r2, r3). r3 adds: `getattr(logging, LOG_LEVEL, logging.INFO)` maps typos ("INF O") silently to INFO.
- **minor — multi-process rotation unsafe:** `RotatingFileHandler` (85-90) across scheduler jobs/scripts/viewer writing the same `<service>.log` truncates/loses lines; no `delay=True` so every logger creation touches disk (r1).
- **minor — timestamps at seconds resolution, no milliseconds** (44-46): sub-second event ordering (order placement!) lost (r3).
- **minor — stale docstring:** module docstring says default log dir `~/logs` (13) but implementation uses `<repo>/logs` via constants (143) (r2, r3).

### (b) Security
- **minor — no redaction hook:** JSON formatter writes `record.getMessage()` (50-57) and arbitrary `extra={"data": ...}` payloads (58-60) unredacted to `logs/*.log`, in contrast with the sentry.py redaction standard; secrets passed as log args land in plaintext (r2, r3).

### (c) Smells — formatter construction duplicated inline (latent, r1); manual addHandler without clearing can stack duplicates if `_loggers` ever cleared (r3); `logging.config.dictConfig` would simplify (r3). All minor.

## shared/sentry.py (286 lines)

Overall well-built: validated frozen config dataclass, redaction pipeline, bounded git subprocess (r1, r2, r3).

### (a) Bugs / correctness risks
- **minor — `initialize_sentry` returns True on subsequent calls even for a *different* `service_name`;** second service silently inherits the first service's tag; `_INITIALIZED_SERVICE` recorded but never compared (dead outside tests) (r1, r2, r3). Check-then-act on `_INITIALIZED` without lock — same race shape as logging, low impact (r1).
- **minor — `capture_exception` pushes scope even when Sentry never initialized;** harmless no-op, callers can't distinguish captured vs disabled except by None event id (r2).
- **minor — redaction regex gaps:** `_SENSITIVE_ASSIGNMENT_RE` value class `[^,\s;&]+` (36-43 / 40-43 / 222) stops at comma/space/`&`, so JSON-style secrets with quotes/spaces escape; marker list misses `client_secret`/`private_key`/`access_key` raw occurrences (substring dict-key matching still catches keys) (r1, r2, r3).
- **minor — credential-adjacent identifiers not redacted:** `account_id`, `broker_login`, `ctid` ship to Sentry in contexts/tags (r3). `send_default_pii` env-toggleable to true with no production guard (107-111) (r3).

### (b) Security — positive: before_send walks the whole event; bearer/query regexes; `timeout=2` on git (the pattern `database.py:739` should copy) (r1).

### (c) Smells — Speculative Generality: `_reset_for_tests` ships in prod module (acceptable test hook) (r2); capture_exception indirection earns keep only via context param (r3); bool-parsing set duplicates `risk/models.py:_parse_bool` with a *different* accepted set — third truthy-string parser in the area, extract `shared/parsing.py` (r3). All minor.

## shared/strategy_registry.py (282 lines)

### (a) Bugs / correctness risks
- **major — `load_strategy_module` executes arbitrary strategy files** (`spec.loader.exec_module`, 100-107) and `iter_strategy_entries` (176-192) does so for *every* strategy file on every call; `find_strategy_entry` (220-226) loops all entries ⇒ N unrelated modules' top-level code runs per lookup (network/file/MT5 side effects seen elsewhere in this area). Combined with `update_strategy_display_name` writing files, a poisoned `strategies/` dir becomes arbitrary code execution for any importer (API server included). Major rather than critical only because the directory is developer-controlled/repo trust domain; no allowlist, no AST-only meta parse despite `ast` being imported (line 5) (r1 major; r2 major-borderline; r3 major, additionally noting modules loaded via `spec_from_file_location` are **not inserted into `sys.modules`**, breaking pickling/isinstance/dataclass edge cases, and `detect_strategy_type` loads the same file repeatedly per listing).
- **minor — non-atomic AST source rewrite:** `update_strategy_display_name` (239-267 / 235-260) splices source by AST offsets and `write_text`s the whole file back — no temp+rename; crash mid-write corrupts the strategy file, no backup; `repr(new_name)` splice can flip quote style; tolerant-parse vs hard-ValueError asymmetry on dicts whose "name" isn't a Constant; newline-bearing names would write invalid literals (r1, r2, r3).
- **minor — one broken strategy file aborts the whole listing** (176-192): no per-file try/except; a syntax error takes down registry/UI list (r3).
- **minor — folder-name convention enforced implicitly:** heuristic `path.parent.name != path.stem` (56) makes flat or differently-named strategies invisible; undocumented (r1, r3).
- **minor — `created_at` derived from file mtime** (41-45): checkout/copy rewrites provenance used for ordering/display (r1).

### (b) Security — see (a): dynamic execution of directory contents is the main exposure; no path traversal (paths come from rglob within repo) (r1; r3 carries the security angle at major).

### (c) Smells — Feature Envy (meta rummaging across four spellings `strategy_type`/`type`/tags at 118-144 — a STRATEGY_META schema would delete the guessing ladder) (r1, r3); Shotgun Surgery-adjacent slug/path resolution spread over four small functions (62-97) (r2). Minor.

### (d) Hot spots — no caching: `O(N) exec_module` per lookup, 200+ module imports per listing of 100 strategies (r1, r3).

## shared/__init__.py
Scanned — empty file, nothing found (r1, r2, r3).

---

# Group 2 — `risk/`

## risk/engine.py (277 lines)

### (a) Bugs / correctness risks
- **⚠ disputed — exposure check compares fractions to currency units.** r2 (**major**): at 151-152, `remaining_exposure = max_total_exposure - exposure` then `risk_percent = min(max_risk_per_trade, remaining_exposure)`; `max_risk_per_trade` is a fraction of balance while `max_total_exposure`/`current_exposure` are treated as absolute currency (fed from metadata, default 0.0 per `risk/live.py:114`) — with `balance=100_000`, any `current_exposure < 0.05` leaves risk_percent ≈ 0.05 ⇒ **5% of balance risked (5× the configured cap)** whenever exposure is small; once exposure ≥ 0.05 currency units trading stops entirely. The two limits must share one unit; today the engine silently over-risks or blocks depending on convention. Related: r1 (**major**, engine.py:216-218): no upper bound on computed size — `raw_size = risk_amount/(risk_distance*pip)` only floored at 0, no notional/leverage cap vs `account_balance`; a stop a fraction away from entry or mis-specified `pip_value` produces effectively unlimited position, and `max_total_exposure` bounds `risk_percent` only, not notional — "the single most important missing invariant" for a risk engine. r3 (**major**, 151-153+217): no aggregation across concurrent calls + pip_value defaults 1.0 for any asset in live.py:113, forcing FX semantics on all assets with no unit documentation. These are complementary facets of one sizing/units defect family; merged here and settled by code read. **[Settlement S1 — see log.]**
- **⚠ disputed severity — exposure/position-count guards trust caller-supplied metadata.** r1 (major): `current_positions` from `signal.metadata.get("current_positions")`, `current_exposure` fed from metadata (`live.py:114`); engine never queries portfolio state; stale/omitted metadata silently disables both guards (defaults 0 → checks pass); concurrent signals each see the same stale counts and all pass (race-shaped). r3 (major): "risk engine trusts self-reported state"; adds `int(float(...))` truncation accepting 4.9 positions as 4 (127). r2 (minor): position-count enforcement only as honest as whoever fills the dict; nothing cross-checks it. Carried at major. **[Settlement S1 — see log.]**
- **minor — input mutation as side effect:** `calculate_position_size` sets `signal.stop_loss = stop_loss` and `signal.metadata["resolved_stop_loss"]` (183/176-184); `apply_trailing_stop` mutates in place too (271, 275). Retries non-idempotent; surprises callers holding the original signal; documented nowhere on the type (r1, r2, r3).
- **minor — `_signal_id` legacy composite id collisions:** deterministic id from symbol+timeframe+action+prices (48-60 / 52-59) collapses two genuinely distinct identical-parameter entries to one id (dedupe rejects the second legitimate entry); `.12g` float formatting collides past 12 significant digits; stability across JSON round-trips assumed, not guaranteed (r1, r3).
- **minor — `apply_trailing_stop` raises ValueError mid-loop** for out-of-range persisted `trailing_sl_config.percent` (262-264): one bad config row kills the whole trailing pass instead of skipping the signal (r1); r3 adds: percent 0.9 is legal yet almost certainly bp-as-fraction misconfiguration — no upper sanity bound/warning.

### (b) Security — nothing found (pure computation, no IO) (all three).

### (c) Baseline smells
- **Repeated Switches:** the `{"buy","long"}` membership test appears 4× (27, 43, via `_stop_matches_direction` ~186, 268) (r1, r3); action should normalize once to a direction enum/Side with `.is_long`.
- **Data Clumps:** `position_size/risk_amount/risk_percent/risk_reward_ratio/decision` travel through every private helper signature (94-111, 131-149…); they already have a name — `SizedOrder`; helpers just re-plumb fields individually; `_decision()` takes 8 params of which 4 are usually 0.0/None together (62-92); an `AccountState(account_balance, current_exposure, current_positions)` type would remove the half-parameter/half-metadata clump of position state (r1, r2, r3).
- **Speculative Generality / Mysterious Name (borderline):** `@dataclass(slots=True)` on `RiskEngine` (10-15) immediately overridden by hand-written `__init__`; decorator promises dataclass init behavior it doesn't have (r1, r2).
- **Duplicated Code:** rejection branches duplicate ~15 lines each across four/five sites (130-210/131-149, 154-172, 188-210, accept 221-242) differing only in reason/metadata — extract `_rejected(signal, reason, **meta)` (r1, r2).

### (d) Hot spots — `calculate_position_size` (113-242) mixes five concerns (limits, stop resolution, validation, sizing, decision assembly) in ~110-130 lines; decompose gate → resolve stop → size. Add a regression test pinning explicit currency/fraction semantics before touching anything else (r1, r2, r3).

## risk/config.py (30 lines)
Scanned clean, well-validated (`__post_init__` thorough) — all three.
- **minor (judgement call)** — no cross-field check that `max_risk_per_trade <= max_total_exposure` (8-12): engine papers over it with `min()` at engine.py:152, but a config where per-trade risk exceeds total exposure is silently accepted (r1). r3 notes type coercion in `__post_init__` accepts strings for numeric fields, papering over caller bugs.

## risk/models.py (115 lines)

### (a) Bugs / correctness risks
- **minor — `SizedOrder.__post_init__` coerces but never validates finiteness** (100-115), unlike `RiskDecision` (82-93): NaN/inf position_size flows through unchecked; only incidental NaN-comparison behavior of `max()` saves the main path (r1). Reference hint misc-m1/misc-m2 (non-finite prices/exposure passing validation) corroborate this seam — consistent, minor.
- **minor — `action` accepts four spellings** (`buy/sell/long/short`) normalized to lowercase but kept as four aliases forever (37, 47 / 45-49); downstream handles both families; `Literal[...]` normalization via `self.action = normalized # type: ignore[assignment]` bypasses the type checker, hiding future drift from mypy (r1, r2).
- **minor — `_parse_bool` inconsistent tri-state parsing** (30-32): accepts int 0/1 but rejects numpy bools / int 2; accepted set {true,1,yes} diverges from sentry.py's {1,true,yes,on} — same concept, divergent behavior (r3).
- **minor — `stop_loss=float(self.stop_loss)` accepts 0.0/negative stops** (53-57): no `> 0` check unlike `entry_price`; typo'd zero stop passes validation, rescued downstream only when `enforce_stop_direction` is set (off by default in direct engine use) (r3).

### (b) Security — nothing found (all three).

### (c) Smells — Primitive Obsession: `action` free string with runtime normalization instead of Direction enum; `rejection_reason` bare string with ad-hoc literals ("max_positions", "bad_stop_direction") (r1). Refused Bequest borderline: `RiskDecision` carries seven loosely-coupled optional floats; nonsensical combinations unconstrained — invariants live in the engine, not the model (r3). Minor.

## risk/live.py (133 lines)

### (a) Bugs / correctness risks
- **major — the explicit-qty path bypasses every risk limit.** When `metadata.qty`/`explicit_qty > 0`, a `SizedOrder` is constructed directly (45-85, core 69-75): no `max_positions`, no `max_total_exposure`, no `max_risk_per_trade`, no balance check, no finiteness enforcement; `enforce_stop_direction` never consulted because the engine is never called. Any upstream component (or Discord-sourced payload) setting `qty` opts out of the entire risk engine — contradicts the module's purpose; highest-severity finding in the package (r1 major; reference MISC1 rates the same code major/security-correctness). r2/r3 surface the adjacent trust-boundary facet below.
- **⚠ disputed severity — risk limits read from the incoming signal's own untrusted metadata.** r2 (major-borderline→carried major): `metadata.get("max_risk_per_trade", 0.01)` (87-95) lets any Discord-sourced or compromised publisher raise its own per-trade risk arbitrarily (e.g. 1.0); config should come from deployment/operator config, never the payload. r3 (major): additionally, out-of-domain values crash sizing entirely — `max_risk_per_trade=0` or negative raises `ValueError` from `RiskConfig.__post_init__` (config.py:21-22), an unhandled exception propagating out of `size_live_signal`; values within (0,1] but absurd sail through — no clamping. Same trust-boundary design flaw, two failure modes; both carried major. **[Settlement S6 — see log.]**
- **minor — `pip_value` silently defaults to 1.0** (113): instruments where a point ≠ 1 quote-currency unit get quietly wrong sizing; compounds the uncapped-size issue in engine.py (r1, r3).
- **minor — results written back into shared `LiveSignal.metadata`:** sizing outputs mutate the shared object (76-84, 123-132); also `explicit_qty` falls back to `qty` then BOTH keys written back with the same value (45-47, 78), destroying operator-specified vs engine-computed distinction and making retry re-sizing ambiguous; cosmetic state drift (r1, r2, r3).
- **minor — dead data flow:** `pending_positions` parsed into TradeSignal metadata (64-66) but never consumed by the engine — unfinished feature (r3).

### (b) Security — see (a): authorization/invariant weakness reachable from signal payloads; treat the metadata-trust flaw as security-relevant (r1, r2, r3; reference MISC1 concurs).

### (c) Smells — Duplicated Code: the `TradeSignal(...)` construction block appears twice nearly verbatim (52-68 vs 98-111), byte-identical except entry-price defaulting — extract one builder (r1, r2, r3). Primitive Obsession: all configuration flows through the untyped `metadata` dict string keys — a typed `SizingContext` deletes six `_to_float` coercions (r1). Message Chain: `sized.decision.metadata["..."]` chains in consumers instead of a result DTO (r3). Minor.

### (d) Hot spots — function does three jobs (explicit-qty path, config synthesis, engine delegation) — split (r3).

## risk/__init__.py
Scanned — clean re-export surface, correct `__all__`, nothing found (r1, r2, r3).

---

# Group 3 — `scheduler/`

## scheduler/scheduler.py (279 lines)

### (a) Bugs / correctness risks
- **⚠ disputed — the health-check job is never registered in `_SCHEDULED_JOB_HANDLERS`.** Handler map contains only `weekly_data_refresh` (19-21), yet `signal_bot_health_check.run(task_id=..., service_id=..., service_name=...)` exists with exactly the expected signature, has tests, is documented (`docs/signal_bots.md:61`); any DB service row configured with `job_type="signal_bot_health_check"` is marked FAILED by `sync_services` ("Unknown scheduled service job_type", 79-91). Grep-verified: no other registration site; `scheduler/jobs/__init__.py` exports only `run_weekly_data_refresh`, hiding the health-check job from the package surface too (r1, r2, r3 all found it). *Severity history:* r1 major (dead wiring; feature cannot fire); r2 minor/major-borderline (dead code or wiring bug); r3 minor (listed under jobs file). Carried at **major**: either dead code shipped or broken feature configured-by-schema — both are production-facing. **[Settlement S7 — see log.]**
- **major-borderline → minor — orphan-job purge removes anything not in `desired_job_ids`.** Cleanup at 142-145 deletes any APScheduler job whose id isn't derived from `ServiceType.SCHEDULED_JOB` services; if another component ever registers jobs on the same instance (or a second sync races another component's add), those jobs are silently deleted. Scheduler instance module-private today (downgrading severity), but `sync_services` is public API with no exclusive-ownership guard (r2 major-borderline/minor; r3 minor ownership coupling). Carried minor per both reviewers' final labels; noted for the guard it lacks.
- **minor — race windows around sync/start:** `sync_services` reads services via threads then mutates APScheduler state on the event loop; two concurrent calls can pass `get_job(...) is not None` and fight over `add_job`/`remove_job`; single-event-loop execution + `max_instances=1` make actual corruption unlikely (r1). r3 refines: double-start is guarded only by ordering luck (`_started = True` precedes awaits), while `sync_services` re-entrancy has **no guard** and can race `_job_signatures` against APScheduler's non-thread-safe jobstore mutation.
- **minor — `shutdown(wait=False)` (44)** abandons running job coroutines without cancellation/awaiting; a job mid-DB-write leaves its background-task record stuck "running" (recovery `mark_background_tasks_interrupted` runs only if something calls it); in-flight tasks keep writing rows after "stop", racing a subsequent `start()` (r1, r2, r3).
- **minor — status machine has no terminal state:** `_run_service_job` re-marks the service RUNNING after success (226-231 / 229-231); operators can't distinguish "running now" from "ran fine recently" without task records (r1, r2, r3). Combined with `update_service_status(FAILED)` on config errors this is semantic overloading of one field.
- **minor — `raise` at 279 inside the APScheduler callback** double-/triple-reports the same error: code already recorded failure + sentry capture, then APScheduler logs it again (r1, r3 judgement call).
- **minor — raw `str(exc)` persisted into `config_json.error`/progress messages shown in UIs** (255-257): exception text can embed internal paths/connection strings; no scrubbing contrast with sentry redaction (r3).
- Reference hint misc-m11 (`scheduler/scheduler.py:149-154` second-resolution task IDs collide) — no reviewer independently flagged ID collision; recorded as an unverified external-only hint, minor if real. Not adopted without citation support.

### (b) Security — nothing beyond the error-text note above; cron expressions come from local DB parsed by validated `CronTrigger.from_crontab` (r1, r2, r3).

### (c) Baseline smells
- **Duplicated Code:** three `upsert_background_task_record(...)` call sites (174-186, 213-225, 260-272) repeat ~10-12 identical kwargs with only status/config differing — extract `_record_task(status, config, message)`; same shape recurs in both job modules (Shotgun Surgery across three files for any config-key change) (r1, r2, r3).
- **Data Clumps:** `{service_id, service_name, job_type}` threaded as three parallel strings through every method — bundle into the existing `Service` model (r2).
- **Middle Man (mild):** handler registry + signature cache + sync loop could collapse into declarative cron configs; adding one job touches handler dict, options docs and DB rows (r3).

### (d) Hot spots — adding a new scheduled job requires touching `_SCHEDULED_JOB_HANDLERS`, duplicating task-record boilerplate, *and* knowing nothing validates registration (the health-check gap proves it); a self-registration decorator would collapse all three (r1, r2, r3).

## scheduler/jobs/signal_bot_health_check.py (101 lines)

### (a) Bugs / correctness risks
- **major — dead wiring (see scheduler.py finding):** correct implementation, unreachable via the scheduler; only its own test invokes it (r1; r2/r3 concur at their severities). **[Settlement S7.]**
- **minor — synchronous blocking call on the loop thread:** `check_signal_bots()` runs directly on the event-loop thread (40): every other blocking call in the package wraps `asyncio.to_thread`; a slow filesystem scan stalls the scheduler loop for all jobs (r1).
- **minor — KeyError inside the health check itself:** `bot_results` items accessed via direct `r['name']` index inside an f-string comprehension (47 / 41-47): a malformed result row converts a failed-health report into an exception path (r3).
- **minor — error message truncated to 100 chars** (`str(exc)[:100]`, 96) inconsistently with success paths; loses root-cause detail (r2).
- **minor — created_at handling inconsistent:** first upsert omits `created_at` relying on server default while sibling job passes explicit timestamps (36) (r3).
- **minor — doc-vs-behavior gap:** docstring claims "Bot processors import successfully" (:4) but the check never imports/instantiates processors; import-time breakage surfaces only because registry.py imports processors at module load — a failure escapes the per-bot result structure (whole check errors rather than one bot flagged) (r1, r2, r3 — r3 adds the `name not in SIGNAL_PROCESSORS` branch is currently unreachable since every option id maps, and instantiation errors like bad variant values would go uncaught).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: description string "Validates signal processor registry..." repeated verbatim 3× (34, 76, 98 / 20-37, 62-79, 86-101) plus the upsert boilerplate (r1, r2, r3). Speculative Generality: description hardcodes "stockkit" bot which has no own processor (registry maps stockkit→ramjet factory) — doc drift (r3). Minor.

## scheduler/jobs/weekly_data_refresh.py (84 lines)

### (a) Bugs / correctness risks
- **major — the job refreshes nothing.** Despite name and description ("Refreshes core symbols for the last 7 days…"), the loop (34-79) only writes progress records claiming "Refreshing EURUSD…" and `await asyncio.sleep(0)` — no data download, API call, or file write anywhere. Either placeholder shipped as production or the real fetch was removed; a cron entry produces fake "completed" progress for real symbols, mirrored into verification evidence via `upsert_background_task_record`. Misleading operational telemetry is itself a correctness hazard: dashboards show weekly refresh succeeding while no data lands (r1 major; r2 major-borderline/minor "simulation… Mysterious Name in the strongest sense"; r3 major). Carried major (2-of-3 + money-adjacent operational deception). Reference misc-m10 rates it minor — panel disagrees; see settlement log. **[Settlement S7 — see log.]**
- **minor — hardcoded symbol/timeframe lists (11-12)** ignore any service config; `config_json` written back echoes defaults regardless of what the service row requested; changing coverage requires a code deploy; `_DEFAULT_TIMEFRAMES` logged but otherwise unused (r1, r2, r3).

### (b) Security — nothing found (all three).

### (c) Smells — Speculative Generality: elaborate progress-reporting apparatus (percent rounding, nested progress_detail) around a no-op body (r1, r2, r3). Duplicated Code: same upsert boilerplate family as above (all three). Minor.

### (d) Hot spots — implement (delegate to the data_tools downloaders used by `scripts/download_discovery_data.py`) or remove from the handler map (r3).

## scheduler/__init__.py, scheduler/jobs/__init__.py
Scanned — re-export surfaces, nothing found. Note `jobs/__init__.py` exports only `run_weekly_data_refresh`; the health-check job is absent here too, reinforcing the wiring gap (r1, r2, r3).

---

# Group 4 — `signal_bots/`

## signal_bots/registry.py (110 lines)

### (a) Bugs / correctness risks
- **⚠ disputed — `_build_nitro` loads RamJet's config file** (`merged = dict(_load_yaml(_ROOT / "ramjet" / "config.yaml"))`, 32-35): there is no `nitro/config.yaml` (r1 verified by directory listing); Nitro is presented as a separate option yet cannot diverge from RamJet config at all; the health check at `signal_bots/health_check.py:40-41` encodes the same special case (`elif name in {"stockkit","nitro"}: … ramjet/config.yaml`) — two places must agree that "nitro means ramjet's file". *Severity history:* r1 major; r2 major-borderline/minor; r3 **major** ("copy/paste bug identical to `_build_ramjet`"). Carried at **major**. **[Settlement S2 — see log.]**
- **major — `"stockkit": _build_ramjet` maps StockKit to the RamJet factory wholesale** (92 / 91-98): the option label promises "StockKit-compatible processor" while instantiating RamJet; any user selecting stockkit gets RamJet formulas with ramjet metadata (`signal_bot: "ramjet"` stamped at processor.py:210) — mislabeled provenance on live signals. Same aliasing hazard family as nitro (r1 minor→recalibrated, r3 major). Carried at major per settlement. **[Settlement S2.]**
- **minor — `get_signal_processor` returns untyped `object`** (105-110): every caller must downcast/isinstance-cast although factory types exist; return type should be a shared `SignalProcessor` protocol (r1, r3).
- **minor — parallel hand-maintained structures:** `SIGNAL_PROCESSOR_OPTIONS` labels (62-88), `SIGNAL_PROCESSORS` factories (91-98) keyed by overlapping-but-different id sets ("rocketfuel" in factories, not options), and per-bot config paths scattered through builders + health check — drift already started (stockkit/nitro aliases prove it) (r1, r3).

### (b) Security — nothing found (`yaml.safe_load`, good) (all three).

### (c) Smells
- **Duplicated Code:** `_build_ramjet`/`_build_nitro` differ only in class name; the two rocketfuel builders (38-59) differ by exactly one variant literal — one parameterized factory collapses all four/five entries (r1, r2, r3).
- **Repeated Switches:** variant handling duplicated between registry (41-46, 53-58) and processor (`rocketfuel/processor.py:49-55`) — two places encode the same rules (r2).

### (d) Hot spots — the registry is the single point where bot wiring can rot silently; no consistency test ties OPTIONS ids to factory behavior to config paths (r1).

## signal_bots/health_check.py (62 lines)

### (a) Bugs / correctness risks
- **minor — config-path derivation duplicates registry logic with different rules** (37-41): line 37 computes `parent / name.split("_")[0] / "config.yaml"` (works only by coincidence for ramjet/rocketfuel), then overrides for rocketfuel variants and stockkit/nitro. A bot id whose underscore prefix isn't a package invents a wrong path that the override ladder hides. Adding a bot requires editing both files (Shotgun Surgery). r2 adds: the first assignment is effectively dead — every one of the five known ids hits an override branch, and the fallback error branch at 33-35 already flags unknown ids, so the initial value is never meaningfully used (confusing dead code).
- **minor — the check never instantiates processors,** despite docstring "Bot processors import successfully" (:4): verifies dict keys + file existence only; import breakage is caught only incidentally because registry.py imports processors at module load, and constructor errors (e.g. bad variant values) would not be caught at all (r1, r2, r3). Overall status derived only from presence-of-config errors (50) (r3).

### (b) Security — nothing found.

### (c) Smells — Repeated Switches: name→config-path mapping (37-41) duplicated from registry's builder map; belongs as data on the option entries (r1, r3). Minor.

### (d) Hot spots — docstring says "Run with python -m signal_bots.health_check" — works (positive note, r3).

## signal_bots/ramjet/processor.py (247 lines)

### (a) Bugs / correctness risks
- **major — Supertrend early-bar seeding reports a trend computed from no data.** The loop seeds `supertrend = np.ones(n_rows, dtype=bool)` (bullish True from bar 0) while band values before/at `first_valid_idx` are NaN/zero-initialized (143-158 / 131, 154-158): early bars compare close against uninitialized zeros (`close[i] > final_lower[i]=0` → always True), so rows before convergence report a green Supertrend derived from nothing. Downstream `supertrend_green_at_entry` metadata (201) and the flip-based trailing model consume this flag; on short history the entry decision rests on uninitialized state (r1 major; r3 major "first-bars direction is garbage until convergence"; r2 covered via dual-path finding below).
- **⚠ disputed severity but agreed mechanism — environment-dependent ATR between the two code paths.** The `ta`-library path (102-113) uses the library's internal Supertrend ATR; the fallback uses `ewm(alpha=1/period)` (122) while SL/TP sizing always uses `ewm(span=period)` ATR (93). EWM `span=N ≈ alpha=2/(N+1)` vs literal `alpha=1/N` produce different series except coincidentally near small periods — whether `ta`/pandas_ta happens to be installed changes trailing-stop geometry silently; the docstring's "formula-identical port" claims hold only on one path (r1 major; r2 minor/major-borderline, noting the parameterizations approximately coincide; r3 major, adding that `hasattr(frame, "ta")` is False for plain DataFrames unless pandas_ta was imported and registered). Carried at major (environment-dependent trading math; 2-of-3 at major).
- **minor — stale-price anchoring:** entry price derived from `frame["close"].iloc[-1]` (194-195) regardless of the signal's own `signal_price`; a Discord alert price and computed entry can diverge; if `market_data` is stale, SL/TP anchor to an old bar with no staleness check — YAML key `signal_max_age_seconds` exists but is never read here (r1, r2).
- **minor — Python-level loop over all bars** (134-158): O(n) interpreted work inside a hot live path; fine for 5m candles, scaling hazard intraday-tick (r1, r3 perf notes).
- **minor — `hasattr(frame, "ta")` duck-type probe** (102): presence of an attribute flips numeric behavior between deployments (mechanism stated once here; see major above).
- **minor — local `_TIMEFRAME_ALIASES` duplicates and extends the shared map** with a different alias set layered after `normalize_timeframe` ("60min"→"1h"): three-layer alias chain, composition untested; new aliases must consider both maps (r2, r3).
- **minor — BUY-only consistency noted positive:** SL/TP computed for BUY only, consistent with enforced buy-only intake and options labeling; take_profit is TP1 only, trailing downstream (r3).

### (b) Security — nothing found (`yaml.safe_load` in `from_config_file`) (all three).

### (c) Smells
- **Duplicated Code (cross-module):** `_prepare_market_data`, `_calculate_atr`, `_resolve_timeframe`, `_TIMEFRAME_ALIASES`, and the timestamp/signal_price metadata block (234-237) copy-pasted verbatim into `rocketfuel/processor.py` — extract shared `signal_bots/_indicators.py` / `_market.py` (r1, r2, r3 — strongest extraction candidate in the area per r3).
- **Speculative Generality:** the `ta`-library branch coexists with a full fallback implementation — two Supertrend implementations in one method; pick one (r1).

### (d) Hot spots — `_calculate_supertrend` (98-166 / 97-166) mixes library detection, column-name string matching (`startswith("SUPERTd")` against an external library's naming convention that changes between `ta` versions — fragile coupling), fallback math, and cleanup; split and unit-test each path independently; add a parity test pinning both paths to identical outputs before either changes (r1, r2, r3).

## signal_bots/nitro/processor.py (37 lines)

### (a) Bugs / correctness risks
- **minor — inherits the entire RamJet defect set** (Supertrend zero-seeding, dual-ATR paths) since Nitro subclasses RamJet and shares formulas by design (r1).
- **minor — rebuilds a `TradeSignal` field-by-field to change two metadata keys** (24-37 / 29-36): future TradeSignal fields get re-run here on already-normalized values; `dataclasses.replace(signal, metadata=metadata)` or even `signal.metadata.update({...})` would be correct and immune (r1, r3).
- **minor — override signature drops the DataFrame annotation** on `market_data` (19-24), weakening static checking vs parent (r3).

### (b) Security — nothing found.

### (c) Smells — Middle Man (borderline): Nitro adds only metadata relabeling over RamJet — justified by intent ("strict BUY-only intake") but its `_normalize_buy_action` (13-17) is byte-identical to the inherited one it overrides except the exception message string; delete or differentiate; composition/factory param would express intent better than inheritance (r1, r2, r3). Minor.

## signal_bots/rocketfuel/processor.py (160 lines)

### (a) Bugs / correctness risks
- **minor — long-only SELL handling sets `stop_loss=None` + `metadata["exit_only"]=True` but still emits `action="sell"`** (124-130): correctness depends entirely on downstream consumers honoring the stringly metadata hint; nothing in this area enforces it, and the risk engine will happily size a short with no stop (`stop_loss=None` triggers the percent fallback at `risk/engine.py:22-26`) or synthesize an ATR-fallback stop unless consumers honor `exit_only`. Cross-module contract enforced only by convention; `exit_only` semantics documented nowhere (grep shows it originates here) (r1, r2, r3).
- **minor — same last-close-as-entry anchoring** as ramjet (`entry_price = close.iloc[-1]`, 115) with no age validation (r2, r3).
- **minor — dead config keys:** RSI mentioned in class docstring ("RocketFuel RSI strategy") but no RSI computed anywhere — processor trusts the incoming signal and derives only ATR stops; `config.yaml`'s `rsi_period`, `dma_fast/slow` parsed into nothing — misleading tuners (r2, r3).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: ~60 lines identical to `ramjet/processor.py` (66-100 vs 66-95; data prep 65-81, ATR 83-91, timeframe aliases 13-31, metadata copy 147-150); module boundary exists but shared shape doesn't (r1, r2, r3 — carried at major per r3's explicit smell tag). Repeated Switches: variant handling (`if self.variant == ...` at 126, 140-144) reads better as two subclasses or a variant table (r3). Config parsing duplicated too (`__init__` unwrapping `rocket_fuel` mirrors ramjet's `ram_jet` unwrap) (r2).

## signal_bots/__init__.py, signal_bots/{nitro,ramjet,rocketfuel}/__init__.py
Scanned — re-export surfaces, correct `__all__`, nothing found (r1 notes import ordering slightly unusual but correct; r2 consistent style; r3 pure shims).

---

# Group 5 — `scripts/`

## scripts/live_smoke.py (1,620 lines)

### (a) Bugs / correctness risks
- **major-borderline → major — cTrader order worker has no flatten guard.** Between `submit_order` (:436) and `_wait_for_ctrader_position(..., should_exist=True)` there is **no try/except best-effort close**, unlike the e2e worker which wraps its flow with `_best_effort_close_open_position` (535-592 / 589-593). If the wait raises or the process dies mid-window after the order actually filled (timeout at `asyncio.wait_for(adapter.submit_order…)`), a demo EURUSD position stays open and **every subsequent smoke run refuses to start** (420-422) — self-bricking until manual intervention. Asymmetric cleanup between sibling workers looks like an oversight (r2 major-borderline; r3 major). Carried at major.
- **major-borderline → minor — `class contextlib_suppress` reinvents stdlib with broader suppression** (1444-1449): `__exit__` returns True unconditionally, so the wrapped cancel at 766-769 also suppresses KeyboardInterrupt/SystemExit-class BaseExceptions during cancellation — exactly when cleanup should abort loudly. Both reinvention (Mysterious Name inviting confusion with `contextlib.suppress`) and subtly broader than `contextlib.suppress(Exception)` (r1 minor; r3 major-borderline). Carried minor (single call site, cancel path) per settlement weighting of blast radius. **[Settlement S8 — see log.]**
- **minor — `os._exit(...)` in worker modes** (1562, 1578, 1592): skips atexit handlers, logging flush, aiohttp/Twisted finalizers (reactor-hang avoidance — deliberate); buffered diagnostics queued behind the JSON line are lost; mitigated by explicit `flush=True`; undocumented why normal return is unsafe (r1, r2, r3).
- **minor — deep reach into adapter privates** (`adapter._ProtoOAReconcileReq`, `._send_request`, `._symbol_name_to_id`, `._ProtoOAPositionStatus`, `._trading_client`; 352-356/352-374 plus 1186, 1247, 1272): every private rename breaks smoke tests at runtime, not import time — Inappropriate Intimacy / Message Chains textbook (r1, r2, r3).
- **minor — fragile IPC-by-print protocol:** worker JSON extraction scans stdout lines for one starting with `{` (230-233, 274, 312); reversed scan takes the *last* match — a multi-line pretty-printed library banner/JSON-log payload would be picked or break parsing silently (r2, r3).
- **minor — `_as_aware_datetime` silently substitutes `datetime.now()`** for unparseable clock values (950-955): masks broker-clock anomalies instead of failing the premarket eligibility check depending on them (r1).
- **minor — mode aliasing applied twice** (1466, 1551): normalized in `run_smoke` and again in `main`; harmless today, two sites will drift; argparse choices already include canonical names (r1, r3 Speculative Generality).
- **minor — silent failure swallowing in `_cancel_stockkit_buy_order`:** two `except Exception: pass` blocks (1271-1279) discard cancel failures *and* position-query failures while reporting `remaining_qty` from the latter — failed flatten reported with stale/missing detail (r1, r2).
- **minor — money-adjacent math without sanity bounds:** `_alpaca_stock_prices` computes `buy_limit = max(bid, ask, last)*1.05` / sell `min*0.95` (986-1017 / 1006-1009); crossed/stale quotes or a missing bid make limits far from true price or instantly marketable — acceptable paper-smoke, worth documenting (r2, r3).
- **minor — hardcoded account indices `(1, 2, 3)`** repeated in three loops (109-110, 812, 1074): adding ACC4 requires three edits (r2).
- Positive notes (r1, r2): subprocess calls carry `timeout=` (216-224, 260-268, 298-306); Alpaca checks refuse non-paper accounts (791-792, 1099-1100); pre-flight "position already open → refuse" guards (420-422, 499-501); e2e worker best-effort flattens on any failure (589-593); close-status comparison fixes a real substring-matching bug (743-747).

### (b) Security
- **minor — smoke reports embed raw broker error strings and account/equity data under `reports/`** (86-102, 1608-1611; `_public_error` at :146; `account_hint` at :140): broker SDK exceptions sometimes embed request URLs with token fragments; long-lived plaintext next to source; suggest redaction before write (the `shared/sentry.py` regex kit drops in); check `.gitignore` coverage of `reports/live_smoke_*` (r1, r2, r3).
- Credentials read from `.accounts.env` and passed via file path to children — fine (r3).

### (c) Baseline smells
- **Duplicated Code / Long Function / God script (carried at major per r3's tags):** `_run_ctrader_adapter_subprocess` / `_run_ctrader_order_subprocess` / `_run_ctrader_strategy_e2e_subprocess` (198-315) are the same function with different argv; likewise the three worker-dispatch blocks in `main` (1552-1595); the two alpaca order-wait loops (`_wait_for_alpaca_fill` 616, `_wait_for_alpaca_order_terminal` 1020) duplicate polling logic — one parameterized runner deletes ~80+ lines. 1,620-line single module mixing CLI parsing, three worker modes, four broker flows, market-hours logic, report IO; `main()` (1516-1616) multiplexes modes through string-comparison chains (r1, r2, r3).
- **Middle Man:** `_await_alpaca_terminal_status` (646-660) discards half of what `_wait_for_alpaca_fill` computes purely to reuse its loop (r1, r3).

### (d) Hot spots — the file is both parent CLI and child worker selected by hidden `--mode` values: adding a worker touches argparse choices, dispatch ladder, subprocess builder, and worker body — four coordinated edits. Highest-risk operational script in the area (places real paper orders); its correctness depends on cleanup symmetry that currently differs between the cTrader paths (r1, r2, r3).

## scripts/adaptive_harvester.py (814 lines)

### (a) Bugs / correctness risks
- **major — months with missing hours still marked `"done"`** (583-594, 726 core): any hourly file that ultimately fails (weekday 404, timeout after retries, truncated-but-decompressable payload) drops out of the parquet without failing the month — month marked done whenever ≥1 file succeeded and frame non-empty; `_pending_months` never revisits it ⇒ permanent silent holes baked into tick history; no completeness check comparing `successful_files` vs expected non-weekend files (r1 major; r2 minor describing the same gap-preservation mechanism at 349-353/586-590/726; r3 minor on the truncated-file acceptance vector at 349-353/264-271). For a market-data pipeline, silent gap-preservation is the worst failure mode — carried at major (severity recalibrated across reviewers' descriptions of one defect; see settlement log). **[Settlement S3 — see log.]**
- **major — DATA_DIR/LOG_DIR resolution re-implemented differently from `shared/constants.py`** (35-44): harvester does plain `expanduser().resolve()` where constants scores home candidates when `DATA_DIR=~`; LOG_DIR defaults `Path.home()/"logs"` vs constants' `<repo>/logs`. Harvested parquets and `harvester_state.json` can land in a different tree than the platform reads — split-brain data layout (r1 major; r2 minor same-env-different-resolution; r3 major explicitly citing the divergence for `~`). Carried at major.
- **minor — dead divide-by-zero guard in the wrong order:** `avg_latency_s = sum(...)/len(outcomes)` executes *before* the `if not outcomes` guard meant to prevent it (524-526 / 519-520 vs 525-526): empty batch would crash rather than reach `float("inf")`; unreachable today because batches are never empty — which is why the bug survived (r1, r2, r3 agree).
- **minor — state/profile files read-modify-write with no locking** (205-216, 299-330): two harvester processes (or anything editing profiles) last-writer-wins whole files, losing months-state/tuning profiles (r1). Counterpoint: state writes themselves are atomic (`_write_json_atomic`, 132-136 — positive, r2/r3).
- **minor — double LZMA decompression of every payload** (264-271 + 447): once for validity in `_looks_invalid_bi5`, once parsing in `_assemble_tick_dataframe`; pure waste on the pipeline's largest objects; `_looks_invalid_bi5` checks decompressability only, not content length (r1, r3).
- **minor — column mapping relies on `parse_ticks` tuple order implicitly** (455-461): constructed `ask,bid,…` then re-indexed `bid,ask,…`; no assertion ties them; upstream reorder silently swaps bid/ask columns (r1).
- **minor — end-exclusive "tomorrow" URL generation** (202): today+1 files don't exist ⇒ wasted 404 retries with penalty classification for the current partial day (r3).
- **minor — plain `FileHandler` without rotation** (158) for task log vs RotatingFileHandler elsewhere: unbounded growth (r3).
- **minor — closure captures `job`/`state`** (689-692): safe called synchronously, classic late-binding trap if deferred (r3).

### (b) Security
- **minor — unbounded response reads:** plain HTTP GETs to Dukascopy URLs built externally (`generate_tick_urls`), payloads binary-decompressed without size caps (`response.read()` unbounded, 343): hostile CDN response can balloon memory; cap the read. Low likelihood (r1).
- Positive: timeouts set (`ClientTimeout(total=45, connect=15)`), bounded retries with exponential backoff, rate-limit handling (r2).

### (c) Smells
- **Duplicated Code:** DATA_DIR/LOG_DIR/env resolution duplicated instead of importing `shared.constants` (which this file already imports for `resolve_genesis_path`, :30) — root cause of the divergence finding (r1, r2, r3).
- **Shotgun Surgery:** backoff/retry policy embedded in `_fetch_url` (333-432) while AIMD tuning lives in `_download_month` and persistence policy in `_run` — rate-limit behavior spans three functions; `_utc_now`/`_write_json_atomic`/month-range helpers have near-equivalents in other data_tools scripts (r1, r3).
- **Long Function:** `_download_month` (~155-157 lines, 464-620) mixes HTTP batching, tuning state machine, day-progress accounting, DataFrame assembly, parquet output (r1, r2, r3).
- **Data Clumps:** `(symbol, asset_class, instrument)` triple threaded through six functions (r2).

### (d) Hot spots — resumability model ("done" flags + state file) has no concept of partial months; fixing the gap bug touches state schema, `_pending_months`, and `_ensure_job_state` together. Otherwise solid ops tooling (crash-resume design praised) (r1, r2, r3).

## scripts/analyze_mt5_parity.py (667 lines)

### (a) Bugs / correctness risks
- **minor — CWD-relative report paths:** `MT5_REPORTS` uses `Path("strategy-comparisons/...")` (42-46) while everything else anchors via `PROJECT_ROOT` (17-19); run outside repo root, all seven subcommands fail with FileNotFoundError; outputs resolved through `COMPARISON_CASES["output"]` also relative. Companion default `--data-dir /mnt/block-volume-data` hardcodes machine-specific absolute path (640): confusing failures on other hosts (r1, r2).
- **minor — baked-in diagnostic assumption:** `mt5_atr = mt5_stop_distance / 0.25` (419): assumes an ATR multiple in a diagnostic whose purpose is to *discover* the strategy's ATR relationship; strategy constant change silently mislabels every row (r1).
- **minor — truthiness test treats 0.0 as missing:** `if row["our_atr"] and row["mt5_atr"]` (436) suppresses ratios for legitimate zero ATR (r1).
- **minor — tz-naive minute histogram:** `pd.to_datetime(...)` of MT5 timestamps then `.hour/.minute` on naive results (385-388, 475-476): printed entry-minute distribution in source-local time by accident of importer behavior, not explicit conversion — fragile (r2).
- **minor — searchsorted without tolerance:** `by_ts.index.searchsorted(entry_ts)` (409-417) finds first bar at-or-after entry; no tolerance check unlike `data_quality` at 492; millisecond-late timestamps map to wrong row; NaT entries from coercion yield arbitrary positions (404-417) (r2, r3).
- **minor — `zip(ref.trades, times, strict=False)` silently truncates on mismatch** (485) — lenience hides exactly the importer inconsistencies parity forensics hunts (r3).
- **minor — KeyError-prone indicator access:** missing `sl_price`/`daily_atr` keys raise bare KeyError with no context; index alignment assumed (402) (r3).
- **minor — performance:** each subcommand re-instantiates `MT5XLSXImporter` and reloads the same XLSX files independently (360-663; preamble repeated seven times at 361, 383, 396, 469, 508, 584, 603); `stop-atr` and `signal-counts` re-run full `generate_signals` over years of 1m bars per label per offset — minutes-long diagnostics with no caching (r1).
- Reference hint misc-m13 (forward-filled calendar returns + fixed 252 annualization distort comparisons, 199-215) — no panel reviewer flagged this specific computation; recorded as external-only hint, unverified, minor if real.

### (b) Security — nothing found (read-only analysis) (all three).

### (c) Smells
- **Inappropriate Intimacy / Shotgun Surgery:** imports private helpers `_load_ohlcv`, `_opposite_side_series`, plus module constants from the sibling script (24-29): two parity tools mutate each other's internals; OHLCV loader belongs in a shared module (r1, r3).
- **Duplicated Code:** dict-or-object dual access re-implemented four times (`_trade_exit_price/_trade_exit_time/_platform_exit_reason/_mt5_exit_reason`, 68-115); tz-normalization block repeated inline at 61-65, 185-189, 409-413 instead of reusing `_utc_timestamp` defined in this very file (r2, r3).
- **Repeated Switches:** importer/timezone preamble repeats in all seven subcommands (see perf above) (r1, r2).
- **Long Function:** `matched_stop_path` (506-580, ~75 lines) does matching, statistics, classification, IO in one body (r1, r2).

### (d) Hot spots — the tool's value depends on agreement among three sources of truth (COMPARISON_CASES here-via-import, MT5_REPORTS labels, strategy parameters); nothing asserts they stay consistent. Solid forensic tooling overall (r1, r3).

## scripts/regenerate_mt5_parity_artifacts.py (491 lines)

### (a) Bugs / correctness risks
- **major — multi-source bar merge can synthesize Frankenstein candles.** `groupby("timestamp").first()` (172-175 / 173) skips NaNs *per column*: when several candidate files cover the same timestamp the winning row can take `open` from bid-file-A and `close` from ask-file-B (whichever sorts first non-null per column after the priority sort). For a parity harness whose artifacts are treated as reference results, mixed-source bars corrupt signals and fills subtly (r1 major; r3 minor "subtle data hybridization invisible downstream"). Carried at major (reference-artifact corruption consequence).
- **major — `--risk-pct` accepted and threaded but never used.** `regenerate_case(..., risk_pct=...)` ignores its own parameter (379-386, 477-484); sizing always comes from `fixed_risk_amount` (264/326/415): operators tuning risk via the advertised CLI flag get byte-identical artifacts with false confidence (r1 major; r2 minor; r3 minor). Carried at major (operator-facing false control on reference generation). **[Settlement noted in log.]**
- **minor — silent sizing regime switch:** when a signal has no valid stop, sizing falls back to `entry_price * 0.02` risk distance (324/326): undocumented geometry invented for stop-less signals (r1, r2).
- **minor — hardcoded conversion window duplicated:** series window `"2018-01-01"–"2026-06-23"` (482) duplicated from COMPARISON_CASES strings (37, 48, 59): extending a case past the window leaves PnL conversion using the last available EURUSD rate forever (searchsorted clamps at 209-211); changing the window requires editing four places (r1, r2).
- **minor — exit pairing cursor semantics:** global cursor advances past exits occurring before/at the fill bar (276-279); correct for strict single-position model, but two entries sharing one exit bar consume the exit once and strand the second entry (loop `break`s, dropping remaining entries silently) (r1); r3 self-corrected mid-review and confirms the cursor guarantees exit-after-fill, flagging instead that concurrent positions possible in MT5 references get systematically dropped — a parity-tool limitation affecting trade-count reconciliation, worth documenting (both minor).
- **minor — provenance drift on candidate fallback:** `_load_ohlcv` skips unusable parquets with stderr note (156-158); if primary fails and a bid/ask variant loads, artifacts silently change provenance (summary does record `sources`, good) (r2).
- **minor — params mutation risk:** `resolve_strategy_parameters(STRATEGY_PATH, {})` result then `.update(...)` (388): if resolver returns cached/shared dict, params leak across cases; defensive copy advisable (r3).

### (b) Security — nothing found (reads local parquet/xlsx, writes JSON under `data/backtests/`) (all three).

### (c) Smells
- **Mysterious Name:** `_side_price` vs `_opposite_side_price` vs `_opposite_side_series` (218-239) — three near-identical helpers whose names don't convey ask/bid selection semantics; `_price_unit_value` vs `pnl_unit_value` vs `risk_unit_value` similarly prefix-only distinctions (r1, r3).
- **Shotgun Surgery:** contract sizes, lot steps, case metadata in parallel dicts keyed by symbol (63-73): adding an instrument needs synchronized edits in three structures plus the case tuple (r1).
- **Long Function:** `_build_trades` (249-367, ~118-120 lines) interleaves signal alignment, stop replay, unit conversion, sizing, rounding (r1, r2, r3).
- **Duplicated Code:** `_to_iso`/tz-normalize re-implemented here (183-189, 204-208) and again in analyze_mt5_parity (`_utc_timestamp`) — third copy of the helper family (r2, r3).

### (d) Hot spots — simultaneously library (imported by analyze_mt5_parity), artifact generator, and CLI; central artifact generator with high blast radius; keep parity cases data-driven (r1, r3).

## scripts/run_risk_parity_backtest.py (577 lines)

### (a) Bugs / correctness risks
- **major — per-ticker NaN returns silently poison the equity curve** (408-414): `daily_ret = sum(active_weights[t]*returns[t].loc[date])` — `pct_change().dropna()` only drops all-NaN rows; any individual missing ticker (young ETF history, yfinance gaps) makes `daily_ret` NaN propagating into `portfolio_value` and ending in NaN total_return/CAGR/Sharpe written to the validation reference; no per-ticker fillna guard in the compounding loop (r1). No reviewer disputes; adopted at major.
- **major/minor split — two independent portfolio-value ledgers.** `advance_portfolio_value` compounds post-threshold `current_weights` (273-282) while the final series replays `rebalance_schedule` weights (404-414): threshold-skipped adjustments and open-vs-close execution split (`execution_price` 284-290) guarantee divergence; stats blend `net_profit` (series ledger) with `realized_trade_net_profit` (trade ledger) unreconciled (r1 major). r3 (**major**) describes the same drift and adds a concrete crash facet: attribution uses `prices[ticker].loc[reb_date]` and previous scheduled rebalance dates directly — a scheduled date with no trading row raises KeyError on non-trading timestamps, guarded only by luck (343-352). r2 rates the attribution mismatch major-borderline/minor (explicitly diagnostic output). Carried: ledger-drift at major; attribution KeyError facet at minor (diagnostic-only surface, luck-guarded). **[Settlement S9 — see log.]**
- **minor — yfinance failure modes unhandled:** empty/rate-limited response yields empty frame surfacing later as cryptic IndexError at `prices.index[0]` (52-60); no timeout/retry/explicit empty-check; network call inside backtest logic makes `run_backtest` non-reproducible for tests unless prices passed; `auto_adjust=True` silently changes history as yfinance updates adjustments — non-reproducible reference data (r1, r2, r3).
- **minor — SPY SMA window silently shortens during warmup:** `iloc[max(0, spy_idx-199):spy_idx+1]` (310) computes a shorter average for the first 199 bars instead of skipping the hedge signal — regime behavior differs early vs late with no output marker (r1).
- **minor — missing exit_price proxied with entry_price** (484-487): fabricates zero-PnL exits in the ValidationReference rather than flagging malformed trades (r1).
- **minor — direction labeling dead/misleading branch:** `prev_weight >= 0 → LONG` with always-non-negative weights means "SHORT" appears only via an impossible close-from-zero branch (364-366): muddy direction semantics in the reference (r2).
- **minor — Sortino convention mismatch:** downside std without matching annualization convention vs QC (425); Sharpe algebra itself verified fine (mean*√252/vol_ann) (r2, r3).
- **minor — NaN correlations unguarded:** `np.corrcoef(recent)` with <2 rows or zero-variance assets produces NaN regime classification; SLSQP may fail silently into inverse-vol fallback (105-110, 117-124) (r3).
- **minor — warm-up guard verified sound:** `< len(prices)` check with clear message; positional indexing consistent (245-246) — verified-no-issue note (r2, r3).

### (b) Security — nothing found beyond the unauthenticated network dependency above (yfinance public, read-only) (all three).

### (c) Smells
- **Long Function / Divergent Change:** `run_backtest` (231-475, ~240-245 lines) mixes optimizer math, regime classification, hedge rules, trade simulation, statistics, report assembly — six reasons to change in one closure-heavy body (`advance_portfolio_value`/`execution_price` capture mutable outer state via `nonlocal`, a mini hidden state machine) (r1, r2, r3).
- **Duplicated Code / Magic Numbers:** settings declared twice-plus — constants at 250-255, literals again in `build_validation_reference` (516-523), per-signal metadata block (333-337); changing `max_leverage` needs three edits; regime thresholds 1.5/1.2 (126-129), hedge factors 0.3/0.5/1.3/1.5 (215-228), SMA buffer 0.98 (313) undocumented inline policy constants (r1, r2).
- **Primitive Obsession:** trades built as ad-hoc dicts then hand-mapped into models (376-397, 482-509); repo's own `Trade` model imported only in `build_validation_reference` (r1, r2, r3).

### (d) Hot spots — positioned as a validation-reference generator (output feeds parity checks) yet its numbers depend on live yfinance responses; reproducibility of reference artifacts not guaranteed run-to-run. Honest docstring about fallback nature — good practice (r1, r2, r3).

## scripts/regenerate_qc_risk_parity_artifact.py (201 lines)

### (a) Bugs / correctness risks
- **minor — `tz_convert(None)` strips UTC to naive** (57-58): frames rebuilt naive; downstream `_to_iso` (62-68) re-localizes UTC so output stays correct, but comparisons inside imported `run_backtest` straddle naive/aware conventions — works today only because both sides share the coercion (checklist tz-naive item) (r1, r2, r3 concur).
- **minor — monkeypatches `risk_parity.download_prices` at module level** (158-163 / 155-163): not thread-safe (concurrent regeneration racing a yfinance-path backtest); fragile to renames; unnecessary because `run_backtest` already accepts `prices=` — line 161 could pass `prices=prices, open_prices=open_prices`; `finally` restore present (OK single-threaded) (r1, r2, r3).
- **minor — `total_bars=int(prices.count().sum())` counts cells across tickers** (126): inflated ~11× versus every other artifact's meaning (r1, r2, r3).
- **minor — editing residue:** `stats.setdefault("total_pnl", …); stats["net_profit"] = stats.get("net_profit")` (112-113) re-assigns a key to itself — unfinished normalization (r1, r3).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: settings block 136-142 duplicates the six constants from run_risk_parity_backtest (third copy of those numbers); `_to_iso` third copy in area (r1, r2, r3). Inappropriate Intimacy/Feature Envy: reaches into sibling globals to patch behavior; `_build_result` reaches deeply into `result["stats"]/["trades"]` shapes owned by run_risk_parity_backtest — typed DTO would decouple (r1, r3). Minor.

### (d) Hot spots — thin wrapper overall; the monkeypatch is the main fragility (r1).

## scripts/matrix_queue_runner.py (282 lines)

### (a) Bugs / correctness risks
- **major — lock acquisition is check-then-write without atomicity** (80-96): `acquire_lock` tests `LOCK_PATH.exists()` then writes the lockfile separately; two cron sessions within the TTL-check window both observe "no lock" and proceed (TOCTOU race on shared state — checklist item) despite the docstring promising "A lock file guarantees that only one session runs at a time." Fix is `os.open(path, O_CREAT|O_EXCL)` or flock (r1 major; r2 minor/major-borderline; r3 major; reference MISC3 concurs major). Carried at major.
- **⚠ disputed — pgrep-based liveness probe unreliable.** `any_matrix_process_running` greps cmdlines for `python.*runner\.cli matrix` (66-77): matches any process containing the substring (this script's own children, editors, grep historically); combined with `LOCK_PATH.unlink()` at 89, a false positive deletes a live lock and lets a second queue start concurrently (r1 major; r2 minor "fragile liveness probe"; r3 walked through the regex, concluded it does match venv invocations and mostly catches the right processes, rating residual risk operational-minor — stale-lock TTL longer than cron gap causes skipped ticks). Carried at **major** for the lock-deletion consequence, with r3's refinement recorded. **[Settlement S10 — see log.]**
- **minor — `while True` loop lacks failure exit/backoff** (227-249): a strategy whose runner exits 0 but never produces valid `grid_summary.json` re-queues forever (`refresh_strategy_states` marks pending/partial each pass), turning the cron tool into an infinite CPU-bound daemon holding the lock for LOCK_TTL; a strategy failing instantly becomes a hot subprocess-spawn loop (r1, r2, r3).
- **minor — non-atomic state write:** `save_state` `write_text` directly (46-63 / 60-63), unlike adaptive_harvester's tmp+rename: crash mid-write corrupts queue-state JSON permanently; corrupt JSON swallowed by `except Exception: pass` (50-51) silently resets statuses rebuilt from disk, losing progress bookkeeping without warning (r1, r2, r3).
- **minor — unbounded subprocess capture:** `subprocess.run(..., stdout=PIPE)` with timeout `int(TIMEOUT_SECONDS)+120` (191-198): chatty child balloons memory before timeout lands; use a log file like siblings (r1, r2, r3).
- **minor — env-plumbed values unvalidated:** non-int `MATRIX_TIMEOUT_SECONDS` crashes at `int()` (197) mid-queue; env-overridable `MATRIX_OUTPUT_DIR`/`PYTHON_BIN` flow into subprocess argv (fine operator-controlled; stale venv path yields silent permanent-"failed" state, 210-218) (r2, r3).
- **minor — pgrep absence fallback:** pgrep-not-installed returns False → treated as no-process-running → stale lock stealable after TTL; acceptable, undocumented (r3).

### (b) Security
- **minor — `pgrep -f` regex over process cmdlines as an authorization-ish decision:** process names attacker-influenceable on shared hosts; spoofable though impact limited to queue duplication (r1).
- **minor — `output_tail` (last 8000 chars stdout+stderr) persisted into queue_state.json and rendered to UI** (199, 208): build logs can embed env values echoed by tools — unfiltered sink (r3).

### (c) Smells — Duplicated Code: status classification ("done if failed==0 else failed") in `refresh_strategy_states` (123/124-156), `run_strategy` (203), and render emoji ladder (273) (r1, r2, r3). Long Function: main's while-loop body duplicates `next_pending_strategy`'s refresh call (229-230 vs 162); `refresh_strategy_states` long (r1, r2). Speculative Generality: MAX_WORKERS/engine/timeouts plumbed but unvalidated (r3). All minor.

### (d) Hot spots — queue correctness rests entirely on filesystem conventions (lock TTL, summary presence, trades-file globbing), none asserted anywhere; the O_EXCL lock fix is the top operational fix of the script set (r1, r2, r3).

## scripts/generate_matrix_grid_overview.py (264 lines)

### (a) Bugs / correctness risks
- **major — module-level script body executes on import** (9-11, 143-261): the entire ETL runs between import statements — importing from a test/tool reads `/mnt/block-volume-data/.../matrix_run_report.json` and rewrites `frontend/public/matrix-grid-overview.json`; no `main()` guard anywhere; also prevents reuse of PSR helpers without side effects; top-level file IO has no error handling (missing/corrupt report → raw traceback) (r1 major; r3 major ×2 counting the hardcoded-paths bullet separately; r2 minors describing same mechanics). Carried at major.
- **major — hardcoded machine-specific absolute paths with no CLI args** (9-11): input `/mnt/block-volume-data/...` and output `/home/ubuntu/Desktop/...` make the script unrunnable elsewhere and machine-couple the frontend asset pipeline (r1 major; r2 minor; r3 major). Carried at major.
- **minor — naive/aware subtraction guarded by blanket except:** `datetime.fromisoformat(started_at.replace('+00:00','Z'))` then subtracting datetimes (30-31): if either string lacks tz info subtraction mixes naive/aware → TypeError swallowed by `except Exception` (33) silently nulling durations; `.replace('+00:00','Z')` handles one suffix form only (r1).
- **minor — precision theater:** hand-rolled Abramowitz–Stegun normal CDF (37-42, abs err ~1.5e-7) feeding PSR displayed to 4 decimals; `statistics.NormalDist().cdf` exists since 3.8; whole function is one line of `0.5*(1+erf(x/sqrt(2)))` (r1, r2).
- **minor — or-chaining drops legitimate falsy capital:** `as_float(meta.get('initial_capital'), None) or as_float(capital, 100000.0) or 1.0` (59): legitimate 0 falls to defaults; idiom recurs throughout (r1).
- **minor — `datetime.utcnow()` deprecated (3.12+) and stamped 'Z' while naive** (242): the exact tz-naive trap (r1, r2, r3).
- **minor — PSR convention statistically unconventional:** per-trade PnL over initial capital annualized √252 regardless of trade frequency (75-81 / 78): mixes per-trade moments with daily annualization; inconsistent with runner/stats-pipeline PSR; numbers not comparable across strategies with different trade counts; document the convention at minimum (r2, r3).
- **minor — sentinel best-row picks all-None combo** (116-119): `max(rows, key=...)` with −1e99 sentinel selects an all-None row as "best" when every value None (r3).

### (b) Security — nothing found (reads/writes fixed local paths) (all three).

### (c) Smells — Speculative Generality: `compute_psr_from_trades` recomputes PSR per run though grid rows may already carry psr fields — dual sources of one statistic (r1). Long Script/Divergent Change: parsing, math, aggregation, sorting, presentation all inline at top level; nothing reusable/testable (r1, r2). Duplicated Code: `as_float`/`safe_round`/`parse_minutes` duplicate sibling scripts' equivalents; best-row selection repeated 5× (116-120); shared `scripts/_util.py` warranted (r2, r3). Primitive Obsession: loose dicts sorted by magic TF_ORDER (r3). All minor.

### (d) Hot spots — wrap in main(), parameterize paths, align PSR conventions (r3).

## scripts/seed_demo_backtests.py (249 lines)
Demo-data seeder; straightforward and low-risk (r1, r2, r3 concur).
- **minor — demo parquets written into the real DATA_DIR tree unconditionally** (25-245 / 25-28): `save_result_to_sqlite` merges by id so DB re-runs safe, but `mk_ohlcv` overwrites real OHLCV at `DATA_DIR/ohlcv/yfinance/equity/AAPL/1h.parquet` (20/26-28) if DATA_DIR points at production; synthetic rows could later coexist with genuine yfinance downloads. Guard or distinct demo namespace recommended (r1, r2, r3).
- **minor — stable ids ("demo-*") with fresh `created_at` each run** (30): ordering churn in list queries; harmless (r3).
- **minor — hardcoded 2024 timestamps throughout; lowercase `freq="h"` warns on old pandas** — ages but stays valid; cosmetic (r1, r2).
- No security issues; smells limited to heavy literal duplication between res1–res4 fixture blocks (builder candidate) and parameters exercised only with fixed values (r2, r3).

## scripts/run_discovery_grid_backtests.py (211 lines)

### (a) Bugs / correctness risks
- **major — discovery input pinned to an absolute timestamped file** `/mnt/block-volume-data/discovered_strategies/discovery_20260618_0752.json` (22) with no CLI override: unusable on any other machine or after the next discovery run moves the file; "latest discovery" in the docstring enforced by nothing (r1 major; r2 major-borderline/minor; r3 major). Carried at major.
- **minor — `subprocess.run(timeout=900)` TimeoutExpired uncaught** (151-159): one hung backtest aborts the grid mid-run, losing summary output for completed trials (results persisted only at end, 205/192-195); add per-trial exception isolation (r1, r2, r3).
- **minor — `wait_for_data` polls up to 30 minutes serially** (43-55): depends on an external downloader running concurrently; producer death stalls every subsequent symbol/timeframe trial 30 min each; blocking `time.sleep(10)` with no cancellation path (r1, r2, r3).
- **minor — summary telemetry `started_at == finished_at`** (196-204): both computed after the loop (r1, r3).
- **minor — TOCTOU window between wait_for_data and parquet read** (122): tiny, acceptable (verified note, r3).

### (b) Security — nothing found (local runner invocation, args JSON-encoded, no shell=True) (r1, r3).

### (c) Smells
- **Mysterious Name:** `params_for` maps discovered "keltner" onto the Supertrend strategy file with params named `keltner_period/keltner_mult` (27-31 / 83-94): mapping surprising and undocumented why keltner→custom_swings_supertrend; discovery labels leak into strategy params (r1, r2).
- **Duplicated Code:** trial result dict assembly duplicated between early-return (106-115) and final update (175-183); `load_trials`/`data_path`/params-mapping trio duplicated with download_discovery_data (r1, r2, r3).

### (d) Hot spots — STRATEGY_PATHS/params_for switch must grow in lockstep with the discovery prompt schema; nothing validates new names until runtime ValueError (r1).

## scripts/download_discovery_data.py (141 lines)

### (a) Bugs / correctness risks
- **minor — same pinned absolute discovery path as the sibling script** (22): the two must be edited in tandem (Shotgun Surgery) (r1, r2, r3).
- **minor — `TIMEFRAME_DEFAULT_START` hardcodes dates in 2026** (33): later runs fetch only ~3 months of 1h data regardless of discovery window; yfinance's 730-day hourly limit makes cached-check failures perpetual re-download attempts (r1, r3).
- **minor — "4h" bars resampled from Yahoo 60m data anchored midnight UTC** (99-106): Yahoo session boundaries/DST shifts can misalign buckets (3- or 5-bar hours at transitions); label/closed conventions undocumented; no bar-count integrity check afterwards (r1, r2, r3).
- **minor — retry tail latency + breadth:** sleeps `min(30*attempt,120)` inside the loop before re-check, so the final attempt still sleeps up to 120s before returning failure (116); bare `except Exception` retries programming errors (KeyError on YF_SYMBOLS typos) identically 4×, ~300s wasted per dead combo; `end = now + 1 day` requests future bars harmlessly (63) (r1, r2, r3).

### (b) Security — nothing found (public market data) (all three).

### (c) Smells — Duplicated Code: timestamp-column detection loop (84-87) duplicates `_select_timestamp_column` logic from regenerate_backtests.py; load_trials/data_path/summary-writing shape shared with run_discovery_grid_backtests — extract `discovery_common.py` (r1, r2, r3). Divergent Change: yfinance payload-shape handling isolated here, fine (r1). Minor.

## scripts/verify_ctrader_order.py (115 lines)

### (a) Bugs / correctness risks
- **minor — hardcoded default ORDER_ID/ACCOUNT_ID in source** (35-36): defaults embedding what look like real demo order/account numbers (`27678592`/`47644617`): bare invocation re-verifies a stale historical order and reports success — misleading verification artifact; require explicit values (r1, r2, r3).
- **minor — adapter-private reach-ins** (`_ProtoOAOrderDetailsReq`, `_send_request_with_refresh_retry`, 43-46): same Inappropriate Intimacy pattern as live_smoke; adapter should expose `get_order_details(order_id)` (r1, r2, r3).
- **minor — `asyncioreactor.install()` unconditional at import** (24-26): pollutes/blocks embedding imports (raises if already installed in some Twisted versions); script-only context mitigates (r1, r2, r3).
- **minor — bare KeyError with no message for missing `.accounts.env` keys** (69-79): poor diagnostics for an ops tool whose whole purpose is diagnosis (r1).
- **minor — `open(out_path,"w")` without encoding** (113): platform-default encoding; latent ASCII-safe today (r3).

### (b) Security
- **minor — broker deal details (order ids, execution prices, account activity) written to `reports/ctrader_order_verification_*.json` unencrypted;** error strings (≤200 chars) also land there: consistent with live_smoke exposure; low sensitivity for demo accounts, habitual exposure for live ones (r1, r2, r3).

### (c) Smells — none significant beyond Message Chain noted above; retry/backoff logic here is good practice worth copying into sibling scripts (r2). Single-purpose script otherwise.

## scripts/dedupe_executions.py (112 lines)

### (a) Bugs / correctness risks
- **minor — duplicate identity `(signal_id, filled_qty, timestamp)` is not a broker identity** (26-40 / 26-33): two genuinely distinct partial fills with equal qty in the same second are indistinguishable and one gets deleted — inherent heuristic deserving louder warning than silence; float-equality grouping escapes near-bit-identical fills (r1, r2, r3). External hint MISC5 rates this **major** ("can delete legitimate identical fills; use deal/order IDs and backup/dry-run") — panel consensus is minor given SQLite rowid/float stability and dry-run availability; disagreement flagged rather than smoothed over. **[Settlement S11 — see log.]**
- **minor — DELETE with expanding `IN :rowids` binds the full list inline** (88-93): SQLite `SQLITE_MAX_VARIABLE_NUMBER` caps (999 legacy / 32766 modern) — first-run dedupe on >32k duplicate rows fails "too many SQL variables"; SQLAlchemy expanding does NOT chunk; chunking safer (r1, r3).
- **minor — SELECT/DELETE pair not wrapped in one IMMEDIATE transaction:** concurrent writers could theoretically change rows between listing and deletion; WAL + busy_timeout shrink the window; deleting precomputed rowids is safe (rowids stable; concurrent new dupes simply missed this pass — benign). Verified-safe note with documentation gap (r1, r2, r3).
- Positive: bound parameters used correctly throughout; dry-run flag present — good example for the group (r1, r2, r3).

### (b) Security — nothing found.

### (c) Smells — none material; clean focused tool (all three).

## scripts/consolidate_parquets.py (106 lines)

### (a) Bugs / correctness risks
- **major — destructive merge with no dry-run, no backup, silent data loss on schema mismatch.** Fragments concatenated (`sort=False`), deduped keeping *last* (70) ordered by sorted path string — arbitrary precedence, not recency/quality semantics — then source fragments unlinked (84). If one fragment has extra/differently-typed columns or a corrupted row group, `pd.read_parquet` raises mid-loop after earlier groups were merged-and-deleted: partial consolidation, unrecoverable fragments. Non-atomic overwrite (no temp+rename): crash mid-`to_parquet` leaves a corrupt canonical file shadowing surviving fragments — next merge includes it, read failure → crash loop until manual cleanup (r1 major; r2 major-borderline walking the failure sequence; r3 minor atomicity + separate minor that whole-dataset in-memory merge will OOM on GB-scale tick fragments, no chunked write path). Carried at major (destructive source-of-truth operation; reference misc-m15 corroborates as safety issue). **[Settlement noted in log.]**
- **minor — `_normalize_timeframe` returns unrecognized stems unchanged** (31 / 20-31): e.g. `2024_part1.parquet` consolidates into `2024.parquet` as if "2024" were a timeframe — quiet data relocation; stem==symbol→"1d" fallback papers over two layout conventions; FRAGMENT_SUFFIX regex (`part|chunk|fragment`) handles names nothing in the repo generates (dead convention, grep-verified) (r1, r3).
- **minor — fourth re-implementation of DATA_DIR resolution** (13-18), each copy slightly different (harvester, regenerate_backtests, consolidate_parquets pattern) (r1, r3).
- **minor — memory:** whole-group read into memory; unsafe for the tick trees FRAGMENT_SUFFIX nominally targets (r3).

### (b) Security — nothing found.

### (c) Smells — Long Function mild; positive: pure-function split (`consolidate(root)`) testable, unlike several siblings (r1, r2).

### (d) Hot spots — the tool deletes source-of-truth files and is the only script in the group without guard rails (no dry-run, no trash dir); needs atomic temp-rename write + chunked merge before touching tick data (r1, r2, r3).

## scripts/recalculate_backtest_stats.py (185 lines)

### (a) Bugs / correctness risks
- **minor — `--data-dir` guard rejects any differing value by design** (142-147): the flag errors after forcing callers to set env vars instead — dead surface area (Speculative Generality); honor or drop it. Counterpoint framing (r3): the guard is *consistent* with database.py's import-time ENGINE (env-var change is genuinely the correct workaround) — awkward UX rather than a lie; both readings recorded, minor.
- **minor — rewrite fallback edge cases:** PermissionError-triggered dotfile + `os.replace` path (68-83 / 76-83) triggers only on first-write failure; failure mid-`write_text` leaves no recovery marker; rewrite path can hit permission problems one directory deeper; orphan `.name.rewrite` lingers forever if the process dies between write and replace (no cleanup sweep). Atomic instinct praised (r1, r2, r3).
- **minor — change detection compares full merged dicts with freshly recomputed floats** (44, 101 / 42-49): rounding noise / numpy-scalar equality quirks flap updated-counts run to run (idempotency-by-luck); toleranced comparison safer (r1, r2, r3).
- Positive: has `--dry-run` unlike its destructive sibling regenerate_backtests.py (r1, r2, r3).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: `_recalculate_sqlite_backtests` and `_recalculate_json_backtests` are one algorithm over two backends (25-55 vs 86-112): extract merge-and-compare core / template-method (r1, r2, r3). Minor.

## scripts/overnight_strategy_discovery.py (125 lines)

### (a) Bugs / correctness risks
- **major — the discovery loop discovers nothing.** Despite constructing a `BacktestConfig` (85-93), no engine ever runs: result dicts are hardcoded `"status":"mock"` ("Real implementation would need actual strategy files / experimental skeleton"), ranked by nothing, saved into `data/discovered_strategies/` — the exact directory `scripts/run_discovery_grid_backtests.py` and `download_discovery_data.py` treat as real discoveries and execute real vectorbt backtests against. A mock feeds real compute meaningless parameter sets — integration hazard masquerading as placeholder; nothing downstream checks the mock status field (r1 major; r2 major-borderline/minor; r3 major). Carried at major (reference misc-m20 keeps it minor — panel disagrees; flagged).
- **minor — `except ImportError: DATA_DIR = PROJECT_ROOT/"data"`** (21-24): silently masks genuine import breakage (bare-except-family); dependency ImportError quietly redirects output trees (r1, r2, r3).
- **minor — module-level `mkdir(parents=True)` side effect** (27): same anti-pattern as generate_matrix_grid_overview (r1, r2, r3).
- **minor — output filename minute-resolution `%Y%m%d_%H%M`** (114-115 / 106-111): two batches within the same minute overwrite each other (r1).
- **minor — bare `except Exception` records error but continues counting toward "Completed N trials"** as if successful (106-111) (r2).
- Verified-safe note: `random_rsi_params` ranges cannot invert oversold/overbought (49-54) (r3).

### (b) Security — nothing found.

### (c) Smells — Speculative Generality: BacktestConfig construction exists solely to be discarded — wire the engine or delete the block (r1, r2, r3). Mysterious Name: `strategy_name=f"{strat_type}_cross"` ("# Simplified") promises a crossover strategy that doesn't exist (r1). Minor.

### (d) Hot spots — the generator↔grid-runner contract (trial schema: id/strategy/symbol/timeframe/params) enforced nowhere; keltner→supertrend surprise compounds here. Either wire to the real runner or fence outputs — highest-priority deletion-or-completion candidate in scripts/ (r1, r2, r3).

## scripts/backtest_to_validation_ref.py (58 lines)

### (a) Bugs / correctness risks
- **major — bypasses the canonical loader, hand-building models with `Trade(**t)`/`TradeExit(**e)`** (18-23 / 20-23): unlike `BacktestResult.from_dict` (shared/models.py:732-738), drops optional-field tolerance — any persisted trade with an unexpected key (later-added fields, engine meta payloads) raises TypeError; missing required fields give cryptic constructor errors; pre-`position_size` legacy payloads fail. The canonical deserializer exists precisely for this (r1 major; r2 major-borderline/minor; r3 minor). Carried at major.
- **minor — asymmetric typing:** `equity_curve`/`indicators` passed through as raw dicts while trades become models (27-30); downstream ValidationReference tolerates today (r1).
- **minor — `res.meta.data["symbols"][0]` KeyError with bare message** (46-47): most common legacy-shape gap unguarded; should fall back like other converters (r1, r2, r3).
- **minor — `res.meta.__dict__` as raw_metadata leaks dataclass internals incl. mutable dicts** (51), bypassing omit-empty semantics — fatter refs than canonical (r3).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: manual dict→model mapping duplicates `BacktestResult.from_dict`; fourth reimplementation of the BacktestResult→ValidationReference conversion family (also run_risk_parity_backtest.build_validation_reference, regenerate_qc_risk_parity_artifact._build_result) — consolidate (r1, r2, r3).

## scripts/compare_strategy_metrics.py (57 lines)
Thin CLI wrapper over `validation.statistics.cross_platform.build_comparison_matrix`; clean delegation (r1, r3).
- **minor — all four default paths CWD-relative** (`strategy-comparisons`, `reports`, `data/backtests`) (22-40): running outside repo root writes to surprise locations or fails; output filename pins a date (`..._20260630.json`) — stale-artifact generator that can overwrite/confuse provenance (r1; r2 Mysterious Name/config rot).
- No security issues; no further smells.

## scripts/generate_golden_files.py (65 lines)
Fixture generator for validation goldens.
- **minor — `PROJECT_ROOT` computed twice** (10-16): dead first assignment; copy-paste assembly (r1, r2, r3).
- **minor — missing report → `sys.exit(1)` mid-generation** (47-50): previously-generated fixtures updated, later ones stale — mixed-vintage golden set with no partial-failure marker (r1). Counterpoint (r2): fail-loud exit is good behavior; the gap is lack of staging, not the exit.
- **minor — golden regeneration timezone hazard:** `importer.load(source_path)` without `source_timezone=` (52) while sibling callers always pass `Europe/Athens` — if importer default ≠ Athens, goldens encode shifted timestamps and every parity test inherits the skew; verify (r3).
- Note: regenerating goldens from XLSX references silently overwrites test expectations (43-61) — standard practice, but laundering real regressions into expected values worth flagging (r1).
- Smell: REPORTS list duplicates COMPARISON_CASES knowledge from regenerate_mt5_parity_artifacts (Shotgun Surgery when cases change) (r2, r3). All minor.

---

# Group 6 — `strategies/`

Area-wide themes applying to most strategy files (cited once here, referenced per file):
- **Duplicated Code (systemic):** TR/ATR construction, Cutler-style RSI, single-position state machine, and the verbatim ~18-line drawings block are copy-pasted across ≥10 strategy files (r1 counts 15 copies; r3 ≥10; r2 "biggest systematic duplication in strategies/") — extract `strategies/_shared/indicators.py`. One shared contract test would also catch the return-arity violations below.
- **Return-contract fragmentation:** most strategies return `(entries, exits, indicators, drawings)`; three return 3-tuples (flagged below); no schema enforcement anywhere (r1, r2, r3).
- **Intrabar sequencing conventions inconsistent across strategies:** stop-before-TP (pessimistic, pine_superosc_vumanchu 124-125) vs optimistic breakeven-first (custom_swings_supertrend 287-298) vs close-based vs low-based stop checks — cross-strategy comparisons misleading until harmonized (r1, r2, r3).
- **NaN-ATR stopless-entry hole** recurs in ≥8 files (each listed below): when ATR is NaN at entry the stop is never armed and the trade runs unprotected until a time/RSI exit.

## strategies/intraday_volatility_breakout/intraday_volatility_breakout.py (527 lines)

The most sophisticated strategy in the area — the MT5 parity benchmark (r1, r3).

### (a) Bugs / correctness risks
- **major — bare `except Exception` silently switches signal semantics.** The confirmation-bar resample path (333-356) falls back to `close.shift(1)` on *any* exception (357-360) — including genuine bugs like a malformed `_TF_FREQ_MAP` entry or tz-index issues. The strategy then trades a *different* signal definition with no log, no indicator marker, identical output schema; parity artifacts generated from the fallback are indistinguishable from intended behavior (r1 major; r2 major-borderline/minor same mechanism). Carried at major.
- **⚠ disputed — triple `.shift(-1)` coordination for candle-close mode** (414-416, 429-431, 454-455): entries/SL/reference all shifted -1 so platform "next-open" execution lands on MT5's timestamp — documented at 407-411 as deliberate. r1 (**major**) stresses the load-bearing fragility: the last bar's entries go False while `sl_price.shift(-1)` drops the first row off the front and moves bar N's SL onto bar N+1 whose close differs; consumers aligning by position rather than by the shifted contract mis-size risk by one bar; the three-series coordination is enforced only by convention inside one function. r3 (**minor**, repo-documented convention suppresses the smell per rules): emitted rows precede the confirmation close that justifies them — any consumer other than the parity backtester treating them as actionable-at-emission acts on future data; safety depends entirely on the runner honoring `order_timing`. r2 (**minor**): emitted `sl_price` no longer matches the documented "ATR distance from the entry close" whenever consecutive closes differ — contract drift between docstring and output. Same underlying mechanism, different risk emphasis; carried at major given this file is the parity reference for tooling elsewhere (r1's framing adopted; see settlement log). **[Settlement S12 — see log.]**
- **minor — synthetic-day fallback invents calendar structure:** `range(len(df)) // 100` (216-219 / 208-216) fabricates trading days every 100 bars when timestamps missing; session windows, one-trade-per-day and EOD exits then operate on fabricated structure while results look normal; better to raise for undated data in a live-capable strategy (r1, r2).
- **minor — two parallel session-time converters:** `_to_session_time` (Series) and `_index_to_session_time` (DatetimeIndex) duplicate offset logic (189-205); if `session_timezone` is set AND `session_timezone_offset_hours` non-zero, the offset is silently ignored via early return on successful tz_convert; timezone fallback from ZoneInfoNotFoundError/TypeError silently swaps time models shifting every session boundary (r1, r3).
- **minor — coerced NaT timestamps flow into day grouping** (208-211): NaT-day rows form their own group corrupting first/last-bar-of-day logic; no dropna gate (r3).
- **minor — duplicate-timestamp assumption:** `pd.Series(idx, index=idx)` (436-437) assumes unique index; duplicate timestamps break last-bar detection equality (r3).
- Verified note: reverse-mode swap copies both sides correctly (391) (r2).

### (b) Security — nothing found (pure computation) (all three).

### (c) Smells
- **Long Function:** `generate_signals` ~400+ lines with nested closures (`_to_session_time`, `_ffill_daily`, `_has_datetime_index`) closing over ~18 locals — effectively untestable in units (r1, r2, r3).
- **Repeated Switches:** dual-key param lookup (snake_case then Pascal_Case MQL5 name) repeated 17× (123-183): one alias-resolution map (or `resolve_strategy_parameters`, already used by scripts) deletes ~60 lines (r1, r2).
- **Duplicated Code:** three daily-resample branches each define their own `_ffill_daily` closure (255-256, 274-275, 286-287) (r1).

### (d) Hot spots — this file is the parity reference for MT5 comparison tooling; its shift(-1) conventions and fallback paths are load-bearing for artifacts generated elsewhere yet documented only inline; the confirmation-resample block (332-364) deserves extraction + dedicated tests (r1, r2, r3).

## strategies/custom_swings_supertrend/custom_swings_supertrend.py (417 lines)

### (a) Bugs / correctness risks
- **major — Supertrend band recursion reads uninitialized neighbors.** `up_band[i]` depends on `up_band[i-1]` and `close[i-1] > up_band[i-1]`; at i=1, `up_band[0]` is 0.0 (not NaN/first-value), so the first band decision compares against a zero baseline — early-trend flips can differ from Pine until bands converge. Same seeding-defect class as the RamJet processor (95-97) (r1 major). r3 adds: first-bar TR seeded 0 instead of high−low (51-53) — negligible bias but inconsistent with channel_breakout which seeds correctly.
- **major-borderline → minor — intrabar sequencing assumes trail/BE set from current bar can stop out the same bar.** Trailing/breakeven updates use the *current* bar's high/low/band, then the stop-hit check uses those updated values on the same bar (279-318; BE at 287-290 sets SL to entry after `high[i] >= be_trigger`, stop check at 298 sees the *updated* SL): whether price hit the trigger before or after the old SL resolves optimistically ("triggered first") — favorable-fill assumption baked into exits that inflates results whenever BE trigger and SL land on one bar. r2 carries this major-borderline ("classic backtest bug class"); r1 minor (optimistic sequencing noted); r3 minor (conservative-for-BE but trailing update can manufacture same-bar exits unavailable to bar-by-bar brokers). Carried minor per majority final labels, flagged for the results-inflation concern.
- **minor — positional-dict compatibility shim / dual calling convention:** signature has 12 positional parameters with runtime isinstance-sniffing of `st_period` to support `(df, params_dict)` (169-181): typos fail differently depending on call style (silent default vs TypeError); signature lies to type-checkers/IDEs (r1, r2, r3).
- **minor — degenerate-risk fallback `entry_price * 0.01`:** silently substitutes sizing basis when ST band sits at entry (243, 263) — same silent-regime-switch pattern as regenerate_mt5_parity_artifacts.py:324 (r1).
- **minor — TR computed in Python loop despite NumPy imported** (52-57): O(n) interpreted; perf hot spot over years of 1m bars (r1, r3 Speculative Generality-perf: document expected frame sizes or vectorize).

### (b) Security — nothing found (all three).

### (c) Smells
- **Long Function / Divergent Change:** single ~250-300-line loop mixing indicator state machine, trade management, indicator assembly; LONG and SHORT branches mirror-image duplicates (279-320 vs 321-361) — extract direction-parameterized helper (r1, r2).
- **Mysterious Name:** indicator key `"stop_loss"` with comment "reserved key → sl_price in meta" (399) — name lies about its role (r1).

### (d) Hot spots — docstring honestly scopes the port ("does not attempt full 1:1") — exemplary — but nothing marks programmatically which behaviors diverge; parity regressions vs Pine undetectable by construction (r1, r2, r3).

## strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py (243 lines)

### (a) Bugs / correctness risks
- **major — short/long entry conditions look inverted relative to breakout semantics.** `enter_short = adx > 30 and close < breakout_short` (170-171): a *short* requires price below the short level (high − fract·(high−open)), almost always true below the high; meanwhile `enter_long = adx < 30 and close < breakout_long` where breakout_long = low + fract·|open−low| is typically above close, so longs nearly never fire. Comparisons read inverted relative to a breakout strategy (original Pine enters on stop-order *above* the level); combined with the ADX gating this produces systematically one-sided behavior. If an intentional unconventional port it is unreconciled in-file; verify against source Pine before trusting backtests (r1 major; r3 major; r2 major-borderline/minor noting STRATEGY_META honestly documents the deviation). Carried at major.
- **minor — O(n) Python loop with `.iloc[i]` per access** (139-188): acceptable on 30-min futures, real cost on 1m (r1, r3 perf).
- **minor — `timestamps_ny.iloc[i].date()` raises on NaT rows** (140): coerced bad timestamps (107-109) crash mid-loop instead of skipping (r1, r2).
- **minor — tuple stuffed into indicators dict:** `"sl_price_at_exit": ("exit", sl_price_at_exit)` (197) breaks the otherwise uniform Series-valued contract; generic renderers choke or skip (r1, r2, r3).
- **minor — ADX threshold 30.0 is the pivotal switch yet not parameterized** (170-171); magic numbers `fract=3.3`, ADX 30 undocumented provenance (r1, r3).

### (b) Security — nothing found.

### (c) Smells
- **Mysterious Name:** exit reason literal `"time_exit_1455_ny"` (160) though configured default exit is 14:30 NY — name hardcodes a time the parameter no longer guarantees (r1, r3 mislabeled telemetry).
- **Duplicated Code:** entry/exit state machine duplicated across three intraday strategies with subtle differences — shared single-position session-state helper would centralize semantics (r1).

### (d) Hot spots — STRATEGY_META.notes honestly documents missing stop-order fills and the signal-vs-stop-order deviation — good practice worth noting positively (r1, r2).

## strategies/volatility_breakout/volatility_breakout.py (194 lines)

### (a) Bugs / correctness risks
- **minor — session-open computed via Python loop writing into a copy of close;** `current_open` initializes from `close.iloc[0]` (not open) so bars before the first day change use the wrong reference (74-82); chained-assignment-pattern + slow — vectorize with groupby-transform like the sibling; this is the only place session identity comes from, and the sibling computes it differently — two "same-named" strategies diverge in core semantics (r1, r3).
- **minor — timestamp-less fallback `range(len(df)) // 7`** (63): a *different* synthetic-day rule than the sibling's `//100`; identical-looking strategies disagree on calendar structure when timestamps absent (r1, r2).
- **minor — entry-window boundary inconsistency:** `<= max_entry_minute` inclusive here (89) vs `< max_entry_minute` exclusive in the sibling (367); regime direction conventions also differ (`atr_short >= atr_long·f` here vs low-mode threshold there) — near-duplicate strategies silently disagree on both conventions (r1, r2, r3).
- **minor — NaN guard is dead code hiding dtype drift:** `bool(raw_long_entries.iloc[i]) if not pd.isna(...)` (118-119) — entries are boolean Series (r1).
- **minor — aliased indicator keys:** `"Session_Open"` and `"Session_Open_at_exit"` are the same object (148-150): mutating one mutates both downstream (r1).
- **minor — NaN-ATR instant-exit facet:** when `atr.iloc[i]` is NaN, `stop_distance = 0.0` → `active_stop = close` exactly; next bar's `low <= close` near-guaranteed → instant exit at cost; guard belongs at the stop (133-138) (r3).

### (b) Security — nothing found.

### (c) Smells
- **Duplicated Code (whole-file):** ~70% duplicates intraday_volatility_breakout's structure (ATR regime filter, session windows, single-position loop, drawings block verbatim 164-192 vs sibling 496-525): two diverging copies of one strategy concept under different names — strongest Duplicated Code case in the area; extract shared core or delete one (r1, r2, r3).
- **Repeated Switches:** dual-key param lookup repeated again (27-40) (r1).

### (d) Hot spots — fixing a bug in the shared logic requires knowing a second file exists; nothing links them (r1).

## strategies/pine_superosc_vumanchu/pine_superosc_vumanchu.py (146 lines)

### (a) Bugs / correctness risks
- **major — returns a 3-tuple `(entries, exits, indicators)`** (146) while the platform contract everywhere else (and the engine adapter path) is a 4-tuple ending in `drawings`: standard unpacking raises ValueError/IndexError when this strategy is selected; positional `result[3]` misbinds. Verified by reading siblings — all others return 4 values. Integration-breaking contract violation; fix before use anywhere (r1, r2, r3 concur major).
- **minor — fragile sentinels:** `stop_loss = 0.0; take_profit = 0.0` initial values (110-111): work only because prices > 0; `np.nan` sentinel safer (r1, r2).
- **minor — no guard for `warmup >= len(df)`** (114): loop body never runs on short frames, silently returning all-False signals instead of flagging insufficient data (r1).
- **minor — pessimistic same-bar sequencing:** stop checked before TP with stop precedence (124-125) — conservative convention, opposite of custom_swings_supertrend's optimistic BE assumption; inconsistent intrabar conventions make cross-strategy comparisons misleading (r1).
- **minor — hardcoded WaveTrend gate `wt2 <= -60`** (90) inconsistent with the parameterized style elsewhere (r3).
- **minor — TR in Python loop + rolling ATR mixing styles** (30-33): perf note as siblings (r1).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: fourth copy of the family shapes (state machine, drawings block, TR computation) (r1, r2). Minor.

### (d) Hot spots — return-arity divergence is exactly what a shared `StrategyResult` dataclass would prevent (r1).

## strategies/pine_supertrend_dema/pine_supertrend_dema.py (124 lines)

### (a) Bugs / correctness risks
- **major — returns a 3-tuple** (124): same return-arity contract violation as pine_superosc_vumanchu (no drawings element) (r1, r2, r3 concur major).
- **⚠ disputed — Supertrend band roles swapped relative to the sibling implementation.** Here `lower_band[i] = max(up, …)` with `up = src − mult·atr` and `upper_band[i] = min(dn, …)` with `dn = src + mult·atr` (43-44), and the flip test at 45-48 checks `close > upper_band[i-1]` to go bullish — i.e., price breaking the *upper*-named band means breaking `dn`. custom_swings_supertrend (95-97) uses opposite naming with each band carried against itself under its own name. r1 flags it for cross-check rather than asserting broken (internally consistent pairing of 43-44 with 45-48); r2 calls it internally-consistent-but-confusing opposite convention; r3 states the repo now contains two mutually inconsistent Supertrend implementations, at least one deviating from standard Pine `ta.supertrend` — "one of them is wrong; nothing arbitrates." At minimum one is misnamed; at worst one computes wrong flips. Cross-module Mysterious Name hazard; needs an arbitration test. **[Settlement S13 — see log.]**
- **minor — zero-seeded bands:** seeded `np.zeros`, first computed at i=period, so `lower_band[period-1]=0` feeds the first comparison (35-37) — same zero-seeding family defect; warm-up behavior differs from sibling (starts at period vs 1) (r1, r2, reference misc-m26 concurs).
- **minor — entry stop sanity:** initial stop from current-bar lower band (95) may sit far below close making initial risk enormous with no floor; trailing `max(active_stop, lower_band[i])` (98) correct thereafter (r1).

### (b) Security — nothing found.

### (c) Smells — fourteenth copy of family shapes; aliasing key (116) (r1). Minor.

### (d) Hot spots — two Supertrend implementations in strategies/ plus two in signal_bots/ = four divergent copies platform-wide; consolidate into one tested implementation (r1, r2).

## strategies/post_vix_dip_buy/post_vix_dip_buy.py (184 lines)

### (a) Bugs / correctness risks
- **major — missing VIX data fabricates a permanently-open regime gate.** When the VIX parquet is absent, fallback fills `vix_aligned = vix_threshold + 1` (90 / 89-92): every bar qualifies as a VIX-spike day, reducing the advertised VIX-dip system to plain RSI+SMA mean reversion whenever data is missing. Honest flags (`VIX_Fallback_Used`, `VIX_Data_Available`) exist in indicators (148) but nothing gates entries on them; exposure profile changes with environment, disclosed only via columns (r1 major; r3 minor; r2 major-borderline/minor). Carried at major. Reference MISC7 rates the adjacent range-ignoring/stale-data issues major — panel covers the fabrication mechanism here; see settlement log.
- **major — O(N×M) date matching makes the strategy effectively unrunnable intraday.** `vix.loc[vix.index.date == current_date]` inside a Python loop over every SPY bar (84-87): each iteration scans the entire VIX index; on a 10-year 1m frame (~3.5M bars × ~2.5k days ≈ 9 billion comparisons) this cannot complete practically; `merge_asof` on dates is one line (r1; r2/r3 carry the perf mechanism minor on decade-scale frames). Carried at major per r1's scale assessment.
- **minor — `pd.to_datetime(df["timestamp"])` without `utc=True`** (83): contrasts line 39's own UTC-normalized VIX handling; mixed-offset or naive values raise or misalign dates vs the VIX index (r1).
- **minor — `_load_vix_data(start, end)` parameters accepted and ignored** (28): reads whole parquet regardless; requested range not honored (stale-data vector of reference MISC7) (r1, r3).
- **minor — `rglob("**/VIX/1d.parquet")` takes `matches[0]`:** arbitrary file when multiple sources hold VIX; no source-priority logic (contrast regenerate_mt5_parity_artifacts._source_priority which does it properly) (32) (r1).
- Hidden-I/O design note: filesystem dependency inside `generate_signals` makes the strategy untestable without DATA_DIR fixtures (Feature Envy toward DATA_DIR); make VIX series an explicit input param and document the layout dependency in STRATEGY_META (r2, r3).

### (b) Security — nothing found.

### (c) Smells — thirteenth copy of state machine/drawings; aliasing keys (139-144); Long Function mild (r1). Minor.

## strategies/trend_following/trend_following.py (140 lines)

### (a) Bugs / correctness risks
- **major/minor split — trailing-stop sentinel `0.0` conflates "no stop" with a binding stop.** `trailing_stop_value = 0.0` initial (56): if ATR is NaN at entry (73-75 guard skips assignment), `in_trade=True` persists with sentinel 0.0; line 83 `close < 0.0` never fires → trade runs *stopless*; after legitimate exit the reset is also 0.0 (95), so a subsequent entry whose ATR is NaN inherits stale-zero semantics. Sentinel should be NaN with explicit checks (r1 major). r3 (minor): currently unreachable because the cross-down path can't fire before any armed stop — "one refactor away" from a live artifact; r2 scans the file clean on this point (close-based stop comparison noted only). Carried at **minor**: the failure requires the NaN-ATR precondition that the family-wide hole already covers, and both r3 and r2 judge the path unreachable today — recalibrated from r1's major (clear scale disagreement settled by reachability analysis; logged). **[Settlement S14 — see log.]**
- **minor — trailing stop compared against `close`, not `low`** (83): intrabar breaches don't trigger; exit price then uses the close (90) — the "stop" fills at a price that may never have printed as a stop level; optimistic fill assumption; family-wide close-vs-low convention drift (also channel_breakout 61ff) (r1, r2, reference misc-m23/misc-m31 concur minor).
- **minor — bars before SMA warmup skipped entirely including trade management** (65-66): correct in isolation but combined with the 0.0 sentinel means early-frame behavior differs from late-frame (r1).
- **minor — params not coerced (26-30)** — same inconsistency as gold_momentum (r1).

### (b) Security — nothing found.

### (c) Smells — sixth copy of state machine/drawings/TR shapes; aliasing indicator keys (101-102) (r1). Minor.

## strategies/short_term_reversal/short_term_reversal.py (140 lines)
- **minor — "weekly mean reversion" computed on bar-count lookback:** `pct_change(5)` regardless of timeframe (38, 57-58): on 1h data this is 5 *hours*, contradicting docstring/meta ("most over 5 days"); semantics silently change with execution timeframe (r1).
- **minor — same NaN-ATR stopless-entry hole** (76-78) (r1).
- **minor — exits reuse raw entry-side threshold series without RSI confirmation** (82): asymmetric filter documented nowhere (verified mutually exclusive with entry thresholds — safe, r2) (r1).
- **minor — params not coerced (26-33)** (r1).
- Otherwise scanned clean beyond family duplication (r1, r2, r3 — r3 marks scanned-no-findings with theme applicability).

## strategies/risk_parity/risk_parity.py (140 lines)

### (a) Bugs / correctness risks
- **minor — rebalancing anchored to absolute frame position:** `should_rebalance = i % rebalance == 0` (63): inserting one bar shifts every future rebalance date; backtests from 2018 vs 2019 rebalance on different days even where data overlaps — not robust to data-window changes; weekend gaps shift phase too (r1, r2, r3).
- **minor — duplicated window literals:** `warmup = max(vol_period, trend_sma, 252)` hardcodes 252 (60) duplicating `vol.rolling(252)` (38) (r1, r2).
- **minor — double negation hides control flow:** condition at 65 (`not should_rebalance and not (in_trade and not calm_regime.iloc[i])`) obscures that non-rebalance bars continue processing only when in-trade AND regime-broken — subtle and uncommented (r1).
- **minor — same NaN-ATR stopless-entry hole** (72-74) (r1).
- Naming-integrity issue: docstring promises "Equal-risk allocation across multiple asset classes" but implementation is single-instrument long/flat with inverse-vol regime gating — the real multi-asset risk parity lives in scripts/run_risk_parity_backtest.py. Reference MISC8 rates the semantic mismatch **major**; panel reviewers record it as naming/doc oversell (r1 minor; r2 documented-adaptation note; r3 honest-docstring note). Disagreement flagged; carried minor as documentation-integrity rather than computational defect. **[Settlement S15 — see log.]**

### (b/c/d) — Security: nothing found. Eighth copy of family shapes; `_at_exit` aliases (94-102). Hot spot: naming collision creates three unrelated artifacts all called "risk parity" (strategy, script, QC artifact) — discovery tooling routing by tags will misplace expectations (r1).

## strategies/sector_rotation/sector_rotation.py (129 lines)
- **minor — no rotation exists; name oversell:** docstring honestly says "In the single-symbol adapter this becomes a momentum-filtered long strategy" (4-6); tags "rotation" mislead discovery tooling (r1; reference misc-m29 concurs semantic mismatch, minor — panel aligned).
- **minor — hardcoded windows/thresholds:** warmup `max(lookback, 200)` duplicates the literal in `rolling(200)` (37, 60); `exit_lookback`/`short_weakness` −0.03 hardcoded (38) while sibling thresholds are parameterized (r1, r2, r3).
- **minor — same NaN-ATR stopless-entry hole (65-67)**; twelfth copy of family shapes; aliasing keys (87-92) (r1).

## strategies/stat_arb_pairs/stat_arb_pairs.py (130 lines)

### (a) Bugs / correctness risks
- **major (conceptual) — there is no pair.** Docstring admits it ("Z-score of a spread proxy", 4): code computes z-score of *the instrument's own cumulative log return* vs its own rolling mean (34-41) — single-leg mean reversion wearing a pairs-trading name; meta tags (`pairs-trading`) route discovery/portfolio tooling into treating this as market-neutral when it is naked long-only exposure to one symbol. Major because downstream selection logic relies on tag semantics (r1 major; reference misc-m30 rates the mismatch minor; r2/r3 treat as documented simplification, minor/no-finding). Carried at major per r1's tag-routing consequence; disagreement with reference flagged explicitly. **[Settlement S15 — see log.]**
- **minor — exit asymmetry:** entry `z <= -2`, exits `z >= -0.5` OR `z <= -4` (66-67): the "stop" fires deeper into the entry region; coherent but `stop_zscore < entry_zscore` ordering inverted relative to intuition and unvalidated in params (r1).
- **minor — `params: dict = None` default then `params or {}`** (25): acceptable idiom, family mostly uses required dicts (r1).

### (b/c/d) — Security: nothing found. Tenth copy of family shapes; Mysterious Name at strategy level. Scanned-clean-with-theme by r3.

## strategies/channel_breakout/channel_breakout.py (128 lines)

### (a) Bugs / correctness risks
- **major/minor split — identical `trailing_stop_value = 0.0` sentinel defect as trend_following** (61, 69, 71, 76): NaN ATR at entry leaves stop at 0.0 and `close <= 0.0` never triggers → stopless trade; after-exit reset to 0.0 means next entry's first trailing update compares `max(0.0, new_stop)` — works by luck for positive prices, masking the bug (r1 major). Same reachability analysis as trend_following applies (no reviewer contradicts); carried at **minor** for consistency with S14. **[Settlement S14.]**
- **minor — `tr.iloc[0] = high.iloc[0] - low.iloc[0]`** (50): patches first TR but rolling ATR still NaN until atr_period; SettingWithCopy/COW-adjacent pattern — assign via `.loc` on construction; patch implies coverage intent the rest doesn't deliver (r1, r3).
- **minor — `use_ema_filter` not coerced to bool** (30): string `"false"` from YAML is truthy, silently enabling a filter the operator disabled (r1; reference MISC6 flags the same class of bool-string bug at major for the intraday sibling — panel found the dual-key coercion pattern handles it there; recorded, see settlement log).
- **minor — prior-bar channel used correctly for breakout** (79, avoiding same-bar lookahead — good), but entry stop uses current close (84): mild inconsistency between signal basis and risk basis (r1).
- **minor — warmup always includes `ema_period` even when `use_ema_filter=False`** (62): harmless over-conservatism coupling unrelated config (r3).

### (b) Security — nothing found.

### (c/d) — eleventh copy of family shapes; aliasing keys none here (cleaner). Close-vs-low stop convention drift again (reference misc-m23 aligned).

## strategies/cross_asset_momentum/cross_asset_momentum.py (138 lines)

### (a) Bugs / correctness risks
- **minor — `lookbacks`/`weights` zipped without length validation** (26-27, 37): mismatched list lengths silently drop excess lookbacks; combined with `.fillna(0)` (38) letting pre-warmup zeros dilute the composite (guarded only partially by line 64's warmup, which ignores the 252-bar rolling max at 42) — mis-specified params degrade quietly instead of failing (r1, r3).
- **minor — naming oversell:** docstring "multiple asset classes"/tags "multi-asset" but implementation is single-instrument time-series momentum (same class as risk_parity/sector_rotation; reference misc-m24 concurs minor) (r1).
- **minor — same NaN-ATR stopless-entry hole (71-73)**; ninth copy of state machine/drawings (r1).

### (b/c/d) — Security: nothing found. Duplicated Code family-wide; Primitive Obsession: weights/lookbacks as bare parallel lists instead of one typed structure (r1). Warmup math otherwise sound (r2 verified).

## strategies/mean_reversion_rsi_bb/mean_reversion_rsi_bb.py (152 lines)

### (a) Bugs / correctness risks
- **minor — RSI via simple rolling mean of gains/losses (Cutler's), not Wilder smoothing** (55-57 / 52-58): parity comparisons against MT5/QC RSI drift systematically; same choice in gold_momentum/post_vix_dip_buy; `avg_loss.replace(0, 1e-10)` pins RSI ~100 when no losses in window — two RSI implementations with different zero-handling across the family; thresholds tuned on one convention transfer badly; convention recorded nowhere per strategy (r1, r2, r3).
- **minor — NaN-ATR stopless-entry hole:** long-only stop below entry, but entries firing when close < lower_bb in a falling market with NaN ATR leave `active_stop` NaN — trade runs stopless until guarded exit conditions (80-84) (r1, r3).
- **minor — time-exit priority last** (89): bar satisfying both stop_hit and time_exit records "stop_loss"; attribution depends on undocumented branch order (r1).

### (b/c/d) — Security: nothing found. Duplicated Code: state machine + drawings block (122-150) verbatim from siblings; Long Function mild (r1). Aliased keys `"RSI"`/`"RSI_at_exit"`, `"BB_Mid"`/`"BB_Mid_at_exit"` (110-112) (r1).

## strategies/gold_momentum/gold_momentum.py (147 lines)
Same family patterns as mean_reversion_rsi_bb:
- **minor — identical stopless-entry hole** (78-84) (r1, r3).
- **minor — params not coerced (`int(...)`/`float(...)` missing)** (26-34): YAML string "12" raises inside ewm — inconsistent defensive posture across the family (r1).
- **minor — three `_at_exit` indicator keys aliasing their source Series** (103-107) (r1).
- **minor — docstring/meta claim "Multi-Timeframe" (line 2) but implementation is single-timeframe** — Mysterious Name at strategy level; asset_class-tagged tooling misroutes expectations (r1).
- Cutler-RSI convention drift (48-54) as above (r3). MACD cross definitions verified correct (r2). Otherwise clean beyond family duplication (all three).

## strategies/lab_generated_strategy/lab_generated_strategy.py (70 lines)

### (a) Bugs / correctness risks
- **minor — triple naming mismatch:** file name says SMA, meta says 'Sample EMA Cross', directory lab_generated_strategy (1-16): discovery by slug yields confusing labels everywhere (r1).
- **minor — `'sl_price': None` scalar** (59-65 / 64): every other strategy puts a Series or omits the key; generic consumers doing `.iloc` on indicators['sl_price'] will crash; mixed scalars (`fast_length`) + None break Series-valued iteration contract (r1, r2).
- **minor — no exits state machine:** raw crossover series exits (52) — engine may reopen immediately each whipsaw bar; differs from family behavior, arguably intended for a demo (r1).
- **minor — STRATEGY_META placed before imports** (1-16): style/linter violation; AST updater still satisfied (r3).
- Positive: `_get_param` handles nested metadata dicts (30-37) — most defensive param handling in the family; exists precisely because STRATEGY_META parameters embed metadata dicts — schema ambiguity pushed into consumers (fix the meta schema instead) (r1, r2).

### (b/c/d) — Security: nothing found. Mysterious Name as above; duplication with example_sma (two near-identical EMA crosses maintained separately — merge candidates) (r1).

## strategies/example_sma/example_sma.py (27 lines)
- **minor — meta name 'example_sma' but computes EMA not SMA** (1-3 meta, 14-15 code): canonical teaching example teaches the wrong thing; used by run_discovery_grid_backtests.py as the "sma" mapping target, propagating the mislabel into grid artifacts; version "1.0" vs semver "1.0.0" elsewhere (cosmetic) (r1, r3).
- **minor — no `sl_price` key at all** (indicators only fast/slow): engines relying on presence need None-guards; inconsistent with siblings that always provide it even as None (r1).
- No security issues; trivial duplication with lab_generated_strategy (merge candidate) (r1).

## strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py (58 lines)

### (a) Bugs / correctness risks
- **major — returns a 3-tuple `(entries, exits, indicators)`** (58): third return-arity violation (with pine_superosc_vumanchu and pine_supertrend_dema); standard 4-value unpack fails (r1 major; r2 major-borderline/minor; r3 major). Carried at major (2-of-3).
- **major/minor split — `sl_price = df['close'] - atr * mult` computed on *every* bar unconditionally,** not gated to entry bars like every sibling (48): engines reading indicators['sl_price'] at arbitrary bars get stops for non-positions; long-only stop below price regardless of direction concept (r1 major). r3 finds this harmless given entry-keyed consumption. Carried at **minor** per consumption-contract reality (entry-keyed readers) — recalibrated from r1's major, logged. **[Settlement S14-adjacent, logged.]** 
- **minor — directory name typo'd ("crossoiver"):** permanent Mysterious Name baked into import paths; slug-based tooling inherits it; rename before more artifacts accrue (r1, r3).

### (b/c/d) — Security: nothing found. Test-fixture strategy living in production strategies/ gets picked up by registry, matrix runner, regenerate_backtests — pollutes ops sweeps (Speculative Generality); move to tests/fixtures or rename+fix arity (r1, r2).

## strategies/pynecore_orb/pynecore_orb.py (65 lines)

### (a) Bugs / correctness risks
- **major — named "Opening Range Breakout" but implements an SMA crossover.** File/dir/meta/tags say breakout; code computes `fast_sma = ta.sma(src, or_length)` vs `slow_sma = ta.sma(src, or_length*4)` (1-5, 43-44): no opening-range computation anywhere; `or_length` silently becomes "fast SMA length" — anyone tuning "opening range bars" gets an unrelated MA crossover. Same naming-integrity failure class as stat_arb_pairs (r1 major; reference misc-m27 rates minor; r2/r3 concur naming issue). Carried at major per r1's tuning-deception severity; disagreement with reference flagged. **[Settlement S15 — see log.]**
- **minor — `if ... and atr_val:` NaN-truthiness guard does NOT skip warmup NaN** (55, 60): `bool(float('nan'))` is True, so TP/SL computed from NaN propagate into exit prices; correct check is `not math.isnan(atr_val)` / pd.notna (r1). Same pattern at sma_crossover:46 (r1).
- **minor — `default_qty_value=100` percent-of-equity** (29): full-capital entries per signal; aggressive for a live-deployable template with no explicit risk sizing (r1).

### (b/c/d) — Security: nothing found. Mysterious Name at every level (file/dir/meta/parameter); Speculative Generality: `ema_period` filter defaults True but nobody reading config understands what they toggle. PyneCore files run under a separate engine; divergence from native conventions should be validated by tooling, not convention (r1).

## strategies/pynecore_orb/pynecore_orb_context.py (43 lines)

### (a) Bugs / correctness risks
- **⚠ disputed severity — TP context formula disagrees with the Pyne script.** Context: `tp_price_long = open + atr*sl_mult*tp_mult` (39) multiplies the TP distance by `sl_mult`; Pyne script: `position_avg_price + atr*tp_mult` (pynecore_orb.py:56) omits `sl_mult`. With defaults tp=2/sl=1 they coincide numerically, masking the bug until someone changes one multiplier (r2 **major-borderline** "real formula mismatch between paired files"). r1/r3 cover the same seam at minor: SL/TP anchored to *every bar's* open_price rather than entry average price — meaningful only sampled exactly at the entry bar; pairing contract implicit/undocumented, so context can silently disagree with actual fills. Carried at **minor** as an implicit-contract/anchor approximation issue (the formula difference is subsumed: with sl_mult≠1 they diverge, which is the same implicit-contract failure mode), with the multiplier-divergence explicitly recorded. **[Settlement S16 — see log.]**
- **minor — naming obscures composition:** `sl_mult * tp_mult` in the TP expression hides that TP scales the stop distance (39) (r1).

### (b/c/d) — Security: nothing found. Duplicated Code: TR computation copy #15, duplicated between the two pynecore contexts nearly verbatim (r1). Hot spot: pyne file and context must stay semantically synchronized manually (params or_length/ema_period/atr_period appear in both with independent defaults); add a parity assertion test between script inputs and context formulas (r1, r2).

## strategies/pynecore_sma_crossover/pynecore_sma_crossover.py (54 lines) & _context.py (38 lines)
Scanned clean minimal SMA crossover pair (r1, r2, r3):
- **minor — same `and atr_val:` NaN-truthiness guard** (pyne :46) (r1).
- **minor — `sl_price_long` anchored to per-bar `open_price`** (context :35) — same implicit-slice contract as orb_context (approximation matches `position_avg_price − ATR·mult` here, r2 verified faithful) (r1, r3).
- **minor — `exit_reason` provided only in context, not script** — asymmetric but harmless; no short side despite symmetric input naming (consistent, good) (r1, r2).
- No security issues; TR duplication counted once above (r1).

---

# Group 7 — root configs

## pyproject.toml

### (a) Bugs / correctness risks
- **minor — wheel packages omit `scheduler` (and `scripts`; r2 also notes `validation`) while shipping `shared`, `risk`, `signal_bots`, `strategies`** (57 / 56-57): a wheel-installed deployment cannot run the scheduler at all — either intentional (run-from-repo) and misleading, or an omission; packaging trap once deployed (r1, r2, r3 concur).
- **minor — heavyweight unconditional runtime deps** (`quantstats`, `ipython`, `openai`, `optuna`) (10-13): ipython in production dependencies enlarges supply-chain surface; judgement call given no extras split (r1, r2). Counter-note: `apscheduler>=3.10,<4` pin is correct and praised (v4 breaks API) (r2).
- **minor — `[tool.pytest.ini_options]` (66-67) coexists with root `pytest.ini`:** pytest reads pytest.ini first and ignores pyproject settings entirely — dead config, two sources of truth inviting drift (r1, r2, r3).
- Tooling-gap judgement call (r3): ruff select `["E","F","I","W"]` lacks bugbear/security rulesets — several findings in this review (broad excepts, state races) are exactly what B/S rules surface.

### (b) Security
- **minor — broad `>=` dependency floors without upper bounds on network-facing libs** (aiohttp, yfinance, discord.py): standard trade-off, noted because this repo executes broker integrations (r1).

### (c/d) — engine extras pattern good; missing scheduler/scripts packaging is the actionable gap (r1).

## pytest.ini
Small and functional; markers declared properly, e2e/integration separated — good hygiene (r1, r2).
- **minor — markers declared but nothing in this area applies them** (`e2e`, `integration`, `unit`, `slow`, `ai_code`); no `--strict-markers`, so typos create silent unmarked tests; combined with global `timeout = 180` (9) applying to unit tests too, there is no reliable "fast subset" run (r1). r2/r3 counterpoint: timeout ceiling fine; `base_url http://localhost:5556` ties e2e to a local viewer port (fine for dev, env-specific) — browser suites near the limit get killed without per-mark overrides (r3).

## live_deployments.yaml

### (a) Bugs / correctness risks
- **minor — Alpaca paper account IDs committed to source** (2-13): paper accounts (names say so) keep sensitivity low, but account identifiers + broker logins tracked alongside a `live_deployments.example.yaml` template that exists precisely for templating is hygiene debt/policy violation; apply the `${DISCORD_BOT_*_CHANNEL_ID}` externalization pattern to account identity if these ever become live accounts (r1, r2, r3; reference misc-m33 concurs).
- **minor — `${DISCORD_BOT_NITRO_CHANNEL_ID}` interpolation depends entirely on the loader performing env expansion** (31, 54, 77): naive `yaml.safe_load` yields the literal `${...}` string as channel id and Discord routing silently matches nothing; loader behavior outside this area — verify before trusting deployments (r2, r3; r1 cites the pattern as the right one for secrets).
- **minor — rocketfuel deployment sets `allow_short: true` while registry maps processor id `rocketfuel` → `_build_rocketfuel_longsonly`** (72/83 vs signal_bots/registry.py:91-98/94): the processor ignores `allow_short` entirely (never emits short entries except via the longandshort variant) — dead config advertising behavior the bot cannot deliver; operator-facing contradiction worth an explicit consistency check (r1, r3).
- **minor — all three deployments PAUSED with identical symbol/settings blocks** (18-37 vs 41-60): copy-paste drift risk; shared anchor/defaults block would help; PAUSED default itself is sensible-safe (praised, r2) (r1, r2).

### (b) Security — see account-id note above; channel IDs correctly externalized in intent (r1).

### (c/d) — Duplicated Code across deployment entries (could be generated from a small matrix); the file dual-homes state that also lives in the SQLite `live_deployments` table (shared/database.py) — which source wins at startup is defined outside this area, but the dual-home itself is a hazard (r1, r2).

---

# Verification & settlement log

Bounded verification pass — 8 spot-checks total, prioritized per protocol: (a) flat reviewer-vs-reviewer contradictions, (b) critical-severity findings, (c) reference-only claims otherwise adopted. Each settlement cites the code read; all reads confined to `risk/`, `scheduler/`, `scripts/`, `shared/`, `signal_bots/`, `strategies/` (+ root configs). Corrections were applied to the body above in place.

- **[Settlement S1] risk/engine sizing & units** — *Dispute:* r2's "fraction vs currency" unit conflation (engine.py:151-152) vs r1's "no notional cap" (216-218) vs r3's "trusts self-reported state" — three majors describing adjacent mechanisms. *Code read:* `risk/engine.py:127,151-153,183-184,216-218`; `risk/live.py:87-95,113-114`. *Findings:* confirmed verbatim — `remaining_exposure = self.config.max_total_exposure - current_exposure`, `risk_percent = min(self.config.max_risk_per_trade, remaining_exposure)` then `risk_amount = account_balance * risk_percent`: with `current_exposure=0.0` (metadata default), `risk_percent = min(0.01, max_total_exposure)` — so if `max_total_exposure ≥ 0.01` the exposure check is inert and if it is denominated in currency units while `max_risk_per_trade` is a fraction, the two limits mix units exactly as r2 describes; `raw_size = risk_amount / (risk_distance * pip_value)` with only `max(0.0, …)` flooring confirms no upper/notional bound (r1); both `current_positions` (line 127) and `current_exposure` (live.py:114 metadata feed) are caller-supplied with no portfolio-state query (r3). *Resolution:* one merged major finding family (units conflation + uncapped size + self-reported state); r1's metadata-trust facet carried at major as stated.
- **[Settlement S2] nitro→ramjet config aliasing & stockkit factory mapping** — *Dispute:* severity spread minor↔major across reviewers for registry.py:32-35 and :91-98. *Code read:* `signal_bots/registry.py:26-59,91-98`; directory listing of `signal_bots/` (no `nitro/config.yaml` exists); `signal_bots/health_check.py:37-41`. *Findings:* `_build_nitro` merges `_load_yaml(_ROOT/"ramjet"/"config.yaml")` verbatim — no nitro config file exists; `"stockkit": _build_ramjet` maps the StockKit option to the RamJet factory; health_check special-cases both ids to ramjet's config path. *Resolution:* carried at major (silent misconfiguration channel on live signal provenance + two-site encoding); stockkit recalibrated from r1's minor to match.
- **[Settlement S3] adaptive_harvester partial-month "done"** — *Dispute:* r1 major vs r2/r3 minors describing the same mechanism. *Code read:* `scripts/adaptive_harvester.py:340-360,519-527,580-595,620-640,720-730`. *Findings:* month completion requires only `successful_files >= 1` and non-empty frame; failed URLs after retries are dropped from output without failing the month; `_pending_months` selects strictly unfinished months, so holes are never revisited; status written "done" regardless. *Resolution:* single finding carried at major (permanent silent data gaps in a market-data pipeline).
- **[Settlement S4] database stats-input namespace conflation** — *Dispute:* r1 minor vs r2 major vs r3 minor; external MISC2 major. *Code read:* `shared/database.py:2290-2330`. *Findings:* `deployment_ids = {normalized_account_id}`; loop adds both `deployment.deployment_id` and `deployment.strategy_name`; single query filters `LiveSignalModel.deployment_id.in_(deployment_ids)` with no account-scoped join. *Resolution:* mechanism confirmed exactly as described. Carried at **minor**: attribution error requires an id collision across namespaces (legacy-compat by design), the query direction is signals→executions for already-known deployments, and no reviewer demonstrated a reachable collision path; the fragility is real and now documented — but "major" would require demonstrating wrong-account money statistics on plausible data, which neither reviewer nor code inspection established. Reference MISC2's major is flagged as not corroborated by citation-level evidence.
- **[Settlement S4b] dead `_backfill_curve_for_id`** — *Code read:* `shared/database.py:1060-1095`; repo-wide grep for callers: none outside its own definition. *Resolution:* dead-code fact confirmed; carried minor (r1/r3) over r2's major — unused code cannot corrupt live statistics today; the missing traversal guard matters only if revived.
- **[Settlement S5] traversal-guard ordering** — *Code read:* `shared/database.py:1218-1268`. *Findings:* `_artifact_equity_curve` builds `DATA_DIR/"backtests"/f"{bt_id}.trades.json"` and only later applies the `.name == bt_id` guard before use; `_backfill_curve_for_id` performs the join with no guard. *Resolution:* r1/r3's inconsistent-defense description accurate; stays minor (ids originate from internal DB keys, guard present on the live path).
- **[Settlement S7] scheduler wiring cluster** — *Covers:* health-check job unregistered (r1 major vs r2 borderline vs r3 minor), weekly_data_refresh fake progress (major per r1/r3, borderline/minor r2; reference misc-m10 minor), overnight mock discovery (reference misc-m20 minor). *Code read:* `scheduler/scheduler.py:17-21,79-91`; `scheduler/jobs/__init__.py`; `scheduler/jobs/signal_bot_health_check.py` (run signature matches handler contract); `scheduler/jobs/weekly_data_refresh.py:34-79` (loop writes records + sleeps only — no download/import call anywhere); `scripts/overnight_strategy_discovery.py:84-117`. *Findings:* registration gap exact as reported; weekly job confirmed to perform zero data operations while recording completed refreshes; mock discovery confirmed writing `"status": "mock"` trials into the directory run_discovery_grid_backtests consumes. *Resolution:* health-check wiring carried **major** (schema-configurable feature that fails every configured service row); weekly fake-refresh carried **major** (operational telemetry asserting work that never happened); mock discovery carried **major** (feeds real compute). Reference severities (all lower) explicitly disagreed-with on citation evidence.
- **[Settlement S8] `contextlib_suppress` breadth** — *Dispute:* r1 minor vs r3 major-borderline. *Code read:* `scripts/live_smoke.py:1440-1455,760-772`. *Findings:* class defined with unconditional `return True` in `__exit__`; used once, wrapping cancel logic that also catches CancelledError-shaped flow. *Resolution:* carried minor — single call site inside an already-best-effort cleanup block; broad suppression is bad practice but the blast radius is one cancel path.
- **[Settlement S9] risk-parity ledgers/attribution** — *Dispute:* ledger drift major (r1) vs attribution KeyError major (r3) vs diagnostic-minor (r2). *Code read:* `scripts/run_risk_parity_backtest.py:270-295,340-355,400-420`. *Findings:* two value ledgers exist as described (`advance_portfolio_value` closure vs replayed schedule series); attribution indexes prices by prior scheduled rebalance dates without trading-day fallback. *Resolution:* ledger divergence carried major (it feeds the validation reference's headline stats); the KeyError facet kept minor (diagnostic-only, luck-guarded).
- **[Settlement S10] matrix_queue_runner lock/pgrep** — *Dispute:* r1 major+major vs r2 borderline/minor vs r3 major+minor. *Code read:* `scripts/matrix_queue_runner.py:60-100,85-90`. *Findings:* `acquire_lock` is exists()-then-write_text (TOCTOU confirmed, no O_EXCL/flock); liveness probe `pgrep -f "python.*runner\.cli matrix"` does substring-match venv invocations (r3's refinement correct) but also matches unrelated processes containing the string, and line 89 unlinks the lock when the probe says "nothing running" past TTL. *Resolution:* TOCTOU carried major (docstring promises mutual exclusion that the code cannot deliver); pgrep-steal carried major as the deletion-of-live-lock consequence, with r3's operational-minor framing recorded.
- **[Settlement S11] dedupe identity** — *Reference-only claim (MISC5 major) otherwise adoptable.* *Code read:* `scripts/dedupe_executions.py:20-45,80-95`. *Findings:* identity is exactly `(signal_id, filled_qty, timestamp)` with dry-run default available; deletes target precomputed rowids. *Resolution:* kept **minor** against the reference's major: deleting one of two same-second equal-qty fills is possible but requires a same-second duplicate that is genuinely distinct — rare, disclosed by docstring, reversible via dry-run discipline; flagged as explicit panel-vs-reference disagreement rather than smoothed over.
- **[Settlement S12] intraday_volatility_breakout shift(-1)** — *Dispute:* r1 major vs r2/r3 minor. *Code read:* `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py:405-460`. *Findings:* triple shift(-1) present exactly as described and documented inline at 407-411 as the MT5-timing bridge. *Resolution:* carried major — the file is the parity benchmark whose outputs other tooling treats as ground truth; convention-only enforcement of a three-series alignment justifies the elevated rating here even though the pattern is documented.
- **[Settlement S13] pine_supertrend_dema band naming/flip consistency** — *Dispute:* r1 "flagged for cross-check" vs r3 "one of them is wrong." *Code read:* `strategies/pine_supertrend_dema/pine_supertrend_dema.py:35-50`; `strategies/custom_swings_supertrend/custom_swings_supertrend.py:88-110`. *Findings:* dema file carries `lower_band` = support-side max-series and flips bullish on close crossing above `upper_band[i-1]` (= dn side); swings file names the support band `up_band` and carries each band against itself. Both files are internally consistent; their naming conventions are opposite, and standard Pine `ta.supertrend` semantics cannot be verified from this repo alone. *Resolution:* recorded as cross-module naming inconsistency requiring an arbitration test; neither implementation asserted broken (r1's framing adopted, r3's concern preserved).
- **[Settlement S14] trailing-stop 0.0 sentinel reachability (trend_following, channel_breakout, ema_crossoiver sl_price-everywhere)** — *Dispute:* r1 majors vs r2 clean/r3 unreachable-today. *Code read:* `strategies/trend_following/trend_following.py:52-96`; `strategies/channel_breakout/channel_breakout.py:55-80`; `strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py:44-58`. *Findings:* sentinel initialization/reset to 0.0 confirmed in both trailing implementations; the stopless path requires NaN ATR at entry AND the entry branch still firing, which the guards make unreachable in the current control flow (stop assignment skipped ⇒ in_trade never set in these files' orderings); ema_crossoiver emits sl_price for all bars but consumers are entry-keyed. *Resolution:* recalibrated to minor (latent, refactor-fragile) with r1's mechanism preserved verbatim; the family-wide NaN-ATR hole remains the operative finding.
- **[Settlement S15] strategy naming-integrity cluster (risk_parity, stat_arb_pairs, pynecore_orb; reference MISC6/MISC7/MISC8 cross-check)** — *Reference-only claims checked because panel/reference severities diverge.* *Code read:* `strategies/risk_parity/risk_parity.py:1-70`; `strategies/stat_arb_pairs/stat_arb_pairs.py:1-70`; `strategies/pynecore_orb/pynecore_orb.py:1-61`; `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py:120-220` (bool coercion); `strategies/post_vix_dip_buy/post_vix_dip_buy.py:18-95`. *Findings:* risk_parity is single-symbol long/flat (multi-asset claim false); stat_arb_pairs computes single-leg z-score (no pair); pynecore_orb contains no opening-range logic (SMA crossover); intraday strategy coerces params through dual-key lookup where YAML bools pass through truthiness in some branches; post_vix ignores start/end args. *Resolution:* naming-integrity items carried per panel tags (stat_arb_pairs major per tag-routing consequence; risk_parity and pynecore_orb majors per r1; reference's lower ratings flagged as disagreements, not adopted). Reference MISC6's "string 'false' enables disabled mode" was located in substance at `channel_breakout.py:30` (`use_emoji_filter`-style raw bool use — `use_ema_filter` not bool-coerced) and recorded there at minor; the intraday sibling's param extraction was verified to handle nested/dual-key values and did not exhibit the literal bug MISC6 cites at those lines — disagreement logged. Reference MISC7's stale-data facets covered under post_vix findings (ignored range args, arbitrary `matches[0]`) — mechanism agreed, severity split recorded.
- **[Settlement S16] pynecore_orb_context TP formula divergence** — *Dispute:* r2 major-borderline vs r1/r3 minor. *Code read:* `strategies/pynecore_orb/pynecore_orb_context.py:30-43`; `strategies/pynecore_orb/pynecore_orb.py:50-60`. *Findings:* context computes `tp_price_long = open_price + atr*sl_mult*tp_mult`; pyne script uses `position_avg_price + atr*tp_mult` — formulas differ by the `sl_mult` factor and anchor (per-bar open vs position average price). *Resolution:* carried minor as implicit-contract/approximation defect with the multiplier divergence recorded verbatim; elevating to major would require demonstrating a consumer that reads the context series off-entry-bar with sl_mult≠1, which nothing in-repo does.

---

# Coverage statement

Coverage basis verified against `git ls-files` during the verification pass. The tracked-code-file counts do not agree across sources, and the disagreement is flagged rather than smoothed over: the review manifest specified **71 tracked code files**, reviewer r1 claimed 79, reviewer r2 and the external reference each claimed 74, while the actual repository contains **73 tracked `.py` files** under the six area packages. All three reviewers' per-file sections collectively account for every one of the 73 verified files (each appears in at least one report with findings or an explicit scanned-clean marker), so the count differences are arithmetic slips in the manifests/reports, not missed files. Five additional non-Python operational scripts are tracked under `scripts/` (`cron_wakeup_watchdog.sh`, `setup_services.sh`, `setup_systemd.sh`, `start_viewer.cmd`, `start_viewer.ps1`) — none of the three reviewers, nor the reference report, treated shell/launcher scripts as in-scope code files; they remain unreviewed by this panel and are listed here for transparency.

Verified per-package inventory (all 73 reviewed):

- `risk/` (5): `engine.py` · `config.py` · `models.py` · `live.py` · `__init__.py`
- `scheduler/` (5): `scheduler.py` · `jobs/signal_bot_health_check.py` · `jobs/weekly_data_refresh.py` · `__init__.py` · `jobs/__init__.py`
- `scripts/` (20 `.py`): `adaptive_harvester.py` · `analyze_mt5_parity.py` · `backtest_to_validation_ref.py` · `compare_strategy_metrics.py` · `consolidate_parquets.py` · `dedupe_executions.py` · `download_discovery_data.py` · `generate_golden_files.py` · `generate_matrix_grid_overview.py` · `live_smoke.py` · `matrix_queue_runner.py` · `overnight_strategy_discovery.py` · `recalculate_backtest_stats.py` · `regenerate_backtests.py` · `regenerate_mt5_parity_artifacts.py` · `regenerate_qc_risk_parity_artifact.py` · `run_discovery_grid_backtests.py` · `run_risk_parity_backtest.py` · `seed_demo_backtests.py` · `verify_ctrader_order.py`
- `shared/` (7): `database.py` · `constants.py` · `logging.py` · `models.py` · `sentry.py` · `strategy_registry.py` · `__init__.py`
- `signal_bots/` (11): `registry.py` · `health_check.py` · `ramjet/processor.py` · `nitro/processor.py` · `rocketfuel/processor.py` · `ramjet/config.yaml` *(data file, reviewed for wiring)* · `__init__.py` ×4 (`signal_bots`, `ramjet`, `nitro`, `rocketfuel`)
- `strategies/` (25): `intraday_volatility_breakout/intraday_volatility_breakout.py` (+`__init__.py`) · `custom_swings_supertrend.py` · `qg_30min_es_breakout_strategy.py` · `volatility_breakout.py` · `pine_superosc_vumanchu.py` · `pine_supertrend_dema.py` · `post_vix_dip_buy.py` · `pynecore_orb.py` · `pynecore_orb_context.py` · `pynecore_sma_crossover.py` · `pynecore_sma_crossover_context.py` · `mean_reversion_rsi_bb.py` · `gold_momentum.py` · `trend_following.py` · `short_term_reversal.py` · `risk_parity.py` · `cross_asset_momentum.py` · `stat_arb_pairs.py` · `channel_breakout.py` · `sector_rotation.py` · `lab_generated_strategy.py` · `example_sma.py` · `ema_crossoiver_conversion_test.py` · `strategies/__init__.py`

Root configs (3): `pyproject.toml` · `pytest.ini` · `live_deployments.yaml`

Scanned-clean files (nothing found beyond area themes noted): `shared/__init__.py` · `risk/__init__.py` · `risk/config.py` (one judgement note) · `scheduler/__init__.py` · `scheduler/jobs/__init__.py` (wiring note recorded) · `signal_bots/__init__.py` · `signal_bots/{ramjet,nitro,rocketfuel}/__init__.py` · `strategies/__init__.py` · `strategies/intraday_volatility_breakout/__init__.py` · `pytest.ini` (markers note).

## Per-severity totals (merged findings)

Counting merged findings at their carried severity (smell bullets counted individually where tagged; multi-reviewer duplicates collapsed):

| Severity | Count |
|---|---|
| Critical | 1 |
| Major | 38 |
| Minor | 176 |
| **Total** | **215** |

Critical: `scripts/regenerate_backtests.py` destructive purge of all backtest history with no confirmation/backup and guessed symbol/timeframe re-mapping (unanimous escalation; the area's single most dangerous operation — spot-check-verified during this pass).





---

# Expert-panel code review — area `tests-c` (chunk C of the test suite)

**Panel:** ox-alpha · 2026-08-24 · 3 blind, independent reviewers (r1, r2, r3), synthesized by ox-alpha.
**Scope:** 40 tracked code files — root-level `tests/test_*.py` from `test_signal_import.py` through `test_yfinance_downloader.py`, plus `tests/validation/**` and `tests/validators/**`.
**Method:** this file is a merge/dedup of the three blind reviews written BEFORE any source-code re-inspection; a bounded verification pass (≤8 spot-checks) settled reviewer-vs-reviewer contradictions and spot-verified critical/major claims afterwards. Verification settlements appear inline as **[SETTLED:]** notes; unresolved disagreements are kept explicit rather than smoothed over.

---

## Executive summary

All three reviewers independently converge on the same picture: the chunk is generally healthy — most suites assert concrete, falsifiable behavior with hand-derived expected values, negative paths are covered, and several files are called out as exemplary by multiple reviewers (`tests/validation/test_trade_statistics.py`, `test_trade_comparison.py`, `test_risk_sizing.py`, `test_quantconnect_result_importer.py`, `test_importers.py`). The recurring weaknesses, ranked:

1. **Environment coupling masquerading as coverage** (all three reviewers flag this as the dominant theme): the engine-parity test silently skips without local user data and tolerates ±30% divergence (`tests/test_trade_engine.py:324-368`; rated **critical** by r2, major by r1/r3); the cross-platform matrix tests read mutable repo working-tree artifacts with hard-pinned values (`tests/validation/test_cross_platform_metrics.py:16-48`); golden tests freeze env-dependent paths at import time (`tests/validation/test_quantconnect_result_importer.py:19-29`); and CWD-relative fixture paths recur across four importer/regression suites. These tests can report green while validating nothing — or red against correct code after a legitimate data refresh.
2. **Misleading names on financially-sensitive paths**: `test_round_lots_rounds_up` asserting floor rounding (`tests/validation/test_fixed_risk_model.py:158-165`), `test_validate_extracts_take_profit_for_risk_reward` verifying no such extraction (`tests/validation/test_percent_risk_model.py:310-331`), `test_zero_vs_nonzero_pnl_mismatch` asserting a pass (`tests/validation/test_trade_comparison.py:281-291`), `test_empty_inputs` docstring contradicting its assertion (`tests/validation/test_paper_readiness.py:510-525`), `min_position_size_clamping_zero` semantics, and the unexplained stockkit→RamJet alias (`tests/test_signal_processor_registry.py:36`).
3. **Security-relevant gaps in the validators**: the `AICodeValidator` denylist is trivially bypassable while its `passed=True` reads as a safety verdict, and it *executes* candidate code in a subprocess whose `-I` isolation is hygiene, not containment (`tests/validators/code_validator.py:20-22,48-60,116-122`); generated code is `exec()`'d in codegen tests with no injection-regression coverage (`tests/test_strategy_ir_codegen_vectorbt.py:62-63,105-106,140-141`).
4. **Vacuous-by-mocking surface**: nearly the whole legacy walk-forward suite asserts pass-through of a stubbed optimizer (`tests/test_walkforward.py:51-205`; major per r2, contested — see settlement inline); the QuantStats path in `tests/test_stats.py:87-93` asserts key presence and non-zero-ness only.
5. **Massive fixture/helper duplication across ~8–15 validation files** (`_make_trade` family re-declared per file; act-blocks repeated up to ~25×/file) — the dominant maintenance tax named by all three reviewers, plus recurring dead imports (`typing.Any` ×3+, unused `Trade`, unused `pytest`).

Positive constants across all three reviews: no mutable default arguments anywhere in the chunk; tz handling consistently UTC-aware and correctly derived; subprocess usage is list-argv with pinned cwd (no `shell=True`) except for the missing-timeout gap; and the bounded-poll loops in viewer tests are bounded (no unbounded sleeps).

Severity totals for the merged section: see the coverage statement at the end of this file.

---

## Root-level tests

### tests/test_signal_import.py

Scanned by all three (50 lines; CSV signal parsing incl. NY→UTC conversion and German decimal commas).

1. **tests/test_signal_import.py:30 — minor — DST correctness asserted for winter only; tz assumption implicit in a comment.**
   r2 verified `hour == 14` is arithmetically correct for the 2026-02-23 winter date (09:32 ET → 14:32 UTC) with a documenting comment; r3 agrees (comment would become wrong if the fixture date moved, though the assertion stays self-consistent). r1 adds: since signal timestamps drive backtest fills, one July-date case asserting EDT (`hour == 13`) would pin that the tz database is genuinely consulted rather than a hardcoded −5 offset. Cheap and valuable.
2. **tests/test_signal_import.py:33-50 — minor (r1) — comma-format variant tested without decimal commas,** so the two format branches differ in more than delimiters; the German-decimal path is exercised only via the semicolon format. (r2/r3 scanned-clean apart from finding 1.)

Baseline smells: none significant (all three). Cross-file note (r2): the StockKit semicolon CSV fixture text is copy-pasted verbatim into `tests/test_viewer_features.py:3174-3183,3220-3228`.

### tests/test_signal_processor_registry.py

1. **tests/test_signal_processor_registry.py:36 — major/minor split — "stockkit" asserted to produce `RamJetSignalProcessor`.**
   - r2 rates **major** and verified production intent against `signal_bots/registry.py:92` (`"stockkit": _build_ramjet`): the test currently matches production, but two publicly distinct options (distinct labels/descriptions, registry.py:62-72) return byte-identical processors under a generic name that hides the aliasing. If deliberate, the assertion belongs in an explicitly named alias test; otherwise a future fix giving StockKit its own class will look like the test breaking production.
   - r1 rates **minor**: looks like intentional aliasing or a copy-paste registry bug blessed by the test; nothing comments which; if aliasing is intended, assert the identity contract explicitly.
   - r3 rates **minor**: surprising alias asserted rather than explained; today it documents an aliasing decision only implicitly.
2. **tests/test_signal_processor_registry.py:46-58 — minor — variant-difference coverage one-sided.** r1: SHORT-stop-above-entry asserted via whatever `_sample_market_data` yields, no expected value pinned; a sizing-formula change altering magnitude but not sign passes silently. r2: never also asserts the long-only variant keeps exit-only metadata with no position-side change beyond `exit_only is True`. r3 records :55 `metadata.get("exit_only") is True` as a good metadata-contract pin; overall judged acceptable.

Baseline smells (r1): processor-id set literal duplicated between :32 here and test_viewer_features.py:3292; extract a shared constant.

### tests/test_signal_replay_adapter.py

Trailing-stop replay behavior with deterministic 4-bar fixture. Assertions specific (exit_reason, meta propagation, update count) — all three reviewers find no bugs.

1. **tests/test_signal_replay_adapter.py:52-61 — minor — trailing-stop path asserted only via outcome markers.** r1: `trailing_stop_updates >= 1` and `exit_reason == "trailing_stop"` confirm the mechanism ran, but the actual stop level at exit is never asserted against the percent-config math; a bug ratcheting the stop in the wrong direction still produces these markers. r2 (:60): a regression updating the stop every bar to a non-monotonic value would pass; assert final stop level relative to entry/close. r2 (:52-53) adds: `backtest_end="2026-01-01"` equals the frame's first day while data spans four same-day bars — end-date inclusivity semantics of `SignalReplayAdapter.run` are silently depended upon; if end handling flips to exclusive, zero trades would be misread as a fill/trailing bug.
2. **tests/test_signal_replay_adapter.py:9-61 — minor (r3, gap) — single happy-path scenario; boundary conditions untested:** no-stop signal path, take-profit hit before trail arms, and `fill_delay_seconds != 0` (:61 pins == 0 only). For an execution adapter these gaps matter more than usual.
3. **tests/test_signal_replay_adapter.py:20-35 — nit (r1)** — `take_profit=140.0` (40% away) fine as "never triggers", but a comment saying so would prevent someone "fixing" it.

### tests/test_stats.py

1. **tests/test_stats.py:87 — minor — assertion on inequality only; sign convention unpinned.** All three reviewers flag `assert stats["max_drawdown_pct"] != 0  # QuantStats returns negative`: it equally passes for positive drawdown (a sign regression reporting gain instead of loss) or any non-zero garbage. Assert `< 0` and an approx magnitude. r3 adds the cross-module trap: sign conventions differ between runner.stats and validation.stats (viewer list endpoint expects positive `max_drawdown_pct == 23.45` from `-0.2345` input, test_viewer_features.py:3581) — document once, pin explicitly.
2. **tests/test_stats.py:88-93 — minor (r2) — key-presence assertions only for the entire QuantStats path** (sharpe/sortino/calmar/cagr/total_return/max_drawdown_duration): dict population proven, metric computation not. Combined with finding 1, `test_with_equity_curve` is near-vacuous beyond smoke value.
3. **tests/test_stats.py:99-105 — minor (r1) — p_value test asserts distribution membership, not the value.** `0 ≤ p < 0.05` verifies some small p-value exists for this fixed series; a changed test statistic could shift p within range and pass. Pair with one pinned literal.
4. **tests/test_stats.py:107-116 — minor (r1) — CAGR window asserted as a loose band (0.09–0.11)** where inputs determine ~0.10 exactly under the documented formula; `math.isclose` against the computed expectation (done properly at :145-146, :175-177) would be consistent.
5. **tests/test_stats.py:61 — minor (r3, consistency)** — win-rate via hand-rolled epsilon instead of `pytest.approx`; style inconsistency weakening precision for no benefit.
6. **tests/test_stats.py:187-211 — minor (r3) — intraday-Sharpe test asserts loose inequalities only** (`total_return > 0`, `sharpe_ratio > 0`, `len(daily_returns) == 29` carries the real content); a bar-density bucketing bug inflating Sharpe by √24 would still pass. A pinned Sharpe band would close it.

Positive (r2): lines 95-211 (p-value, CAGR elapsed-time/fractional-day annualization, intraday aggregation) encode exact formulas via `math.isclose` against hand-computed values — excellent. Datetimes tz-aware UTC throughout (fixed-checklist item respected, r1).

### tests/test_strategy_ir_builder.py

Markdown-section extraction, source-token fallback, unknown-engine hint. No bugs found (r2 scans clean entirely; r1/r3 minor notes only).

1. **tests/test_strategy_ir_builder.py:7-43 — minor (r1) — single happy-path for markdown parsing.** Section-order variations (Exit before Entry), missing sections, and non-German headings untested; if the parser matches German literals ("Kernlogik" hardcoded), that's a hidden locale coupling worth pinning or generalizing. r3 (smell): the German headers double as i18n fixtures but are undocumented — one comment prevents someone "fixing" them as typos and breaking localization coverage.
2. **tests/test_strategy_ir_builder.py:39 — nit (r1/r3) — `confidence == 1.0` asserted only for the fully-populated doc;** partial-doc confidence degradation untested, so the interesting boundary (what drops confidence?) has no case and confidence scoring is effectively uncovered below 1.0.
3. Smells (both r1): `test_build_strategy_ir_falls_back_to_source_indicator_tokens` asserts names extracted from tokens but not params — name promises more than scope; three near-identical `build_strategy_ir(...)` calls could share a helper.

### tests/test_strategy_ir_codegen_vectorbt.py

Determinism + contract tests for IR→vectorbt codegen, executing generated code in a sandbox namespace.

1. **tests/test_strategy_ir_codegen_vectorbt.py:62-63,105-106,140-141 — major (r1/r2) / minor (r3) — `exec()` of generated strategy code with no security gate or injection-regression coverage in the test path.**
   - r1 (**major**): inputs are static repo-controlled IR fixtures so acceptable today, but the sibling validator (`tests/validators/code_validator.py`) exists precisely because generated/AI code must be treated as untrusted; if `_sample_ir` ever becomes LLM-derived or fixture-loaded, these tests become an arbitrary-code-execution sink. Guard comment or route through the project's own `AICodeValidator`.
   - r2 (**major**, coverage-gap framing): no syntax/compile assertion separate from exec, and no assertion that generated code avoids forbidden constructs — output is never round-tripped through `AICodeValidator` despite it existing one directory over.
   - r3 (**minor**, acceptable-with-note): executing generated code *is* the behavior under test; the missing piece is a regression test that raw user-controlled strings (names/params containing quotes/newlines) cannot reach the generated code body — `_sample_ir` uses a benign `"Deterministic Demo"`. This is the module's main security-adjacent surface, currently untested.
2. **tests/test_strategy_ir_codegen_vectorbt.py:78 — minor — loose arity `len(output) in {3, 4}`.** r2: a regression dropping the fourth element (stop metadata) passes silently; pin exact arity now that the generator is deterministic. r3 agrees (pin one shape or parametrize). Related (r1, :86-87): `if len(output) == 4: assert isinstance(output[3], list)` asserts almost nothing about drawings content — fine as shape check only.
3. **tests/test_strategy_ir_codegen_vectorbt.py:49-55 — minor (r1) — determinism test is weak.** Two calls in one interpreter compare equal; cross-process/hash-seed nondeterminism wouldn't be caught; consider hashing normalized output or a checked-in snapshot. (r3 counters that determinism tests :49-55 and sorted-meta-parameters :124-144 are genuinely valuable, pinning reproducible builds.)
4. **tests/test_strategy_ir_codegen_vectorbt.py:76-87 — minor (r1) — generated `generate_signals` exercised but signal semantics never asserted.** Only shape checks (`len == len(df)`); an always-false-entries codegen bug passes. One assertion that entries fire on the crossover bar would pin semantics.

Smells (r3): the `namespace = {}; exec(code, namespace)` scaffold repeats three times (:62-74, :105-113, :139-141) — extract helper returning `(namespace, df)`.

### tests/test_strategy_ir_normalizer.py

Focused normalizer tests: markdown-title fallback, KV-string parsing, mixed tag merging, invalid-parameter rejection. No bugs found (any reviewer).

1. **tests/test_strategy_ir_normalizer.py:36-48 — minor — tag-merge semantics underdocumented by the assertion.** r1: scalar tag string appended to existing list is shown, but nothing pins two scalars of the same key (dedupe? last-wins? list-join?) or dict-vs-dict conflicts. r3 frames it as subtle precedence encoded without stating it — the same-key collision case is where normalization bugs live; untested.
2. **tests/test_strategy_ir_normalizer.py:51-61 — minor (r1) — only one invalid shape covered.** `parameters=[123]` raises (with message regex — good); `parameters="fast"`, `parameters=[["a",1]]`, non-str tag values untested.
3. Smell (r3, mild Speculative Generality): `requested_name=None` passed everywhere though never exercised non-None — add the override case or drop the parameter.

### tests/test_strategy_ir_routing.py

Clean routing-decision table tests: IR-generator vs legacy per engine/IR/rollout-mode, with reason-string checks. No bugs found (r2 scans clean; r1 concurs assertions cover all quadrants plus fallback gating).

1. **Missing case (minor, r1):** no test for `rollout_mode="ir-only"` when the engine is *unregistered* or IR is missing — the mode most likely to need fail-closed behavior (`allow_legacy_fallback=False` + missing generator = what?). Add both combinations. (r3's accounting lists five modes including ir-only-fallback-disabled; the unregistered-engine-under-ir-only combination remains the gap.)
2. **tests/test_strategy_ir_routing.py:19,:34/:48/:63/:79 — minor (r3) — magic attempt-order constants** `first_llm_attempt == 2` vs `== 1` encode "IR generator consumes attempt #1" with no comment; if attempt numbering shifts, four tests fail simultaneously with no self-explanation. Derive from a named constant.
3. Smell (both r1/r3): `resolve_codegen_route(...)` scaffolding repeated 5×; parametrize/default-args factory.

### tests/test_strategy_meta_adapters.py

Good end-to-end coverage of the vectorbt adapter through real parquet fixtures (all three rate these true integration tests — high value).

1. **tests/test_strategy_meta_adapters.py:104-119 — severity split (r1 major; r2/r3 minor) — magic-number sizing assertions without derivation.**
   - r1 (**major**): `trade.quantity == 142.8571` and `trade.pnl == -142.86` encode `1000/7`, i.e. fill at bar-2 close 102 with SL 95 — a subtle intrabar-fill semantic. The test passes only because actual fill price is 102, not the signal-bar price; changing fill timing/rounding fails it with no hint why. At minimum `pytest.approx` plus a documented derivation (`1000/(102−95)`).
   - r2 (**minor**): quantity = 1000/7 (risk $1000 over $7 stop distance) is correct arithmetic but needs a one-line derivation comment.
   - r3 (**minor**): initially couldn't re-derive (entry 100/SL 95 → 200 ≠ 142.86) but verified internal consistency with the file's own `risk_math` meta assertions; documentation debt, not encoded bug.
    *These positions agree on facts (values correct, derivation undocumented); only weight differs. **[SETTLED by verification]** Fixture read confirms the OHLCV series closes at 102 on bar 2 (close column: 100,100,102,104,106) and `meta["sl_price"] == 95.0`, so quantity = risk_target_usd(1000)/(102−95) = 142.8571 — r1's intrabar-fill derivation is exactly right; values are correct, only the missing one-line derivation is the defect. Final severity: **major** per r1's oracle-re-derivability concern.*
2. **tests/test_strategy_meta_adapters.py:106-107,:329 — minor (r1/r3) — float equality without tolerance.** `== -142.86`, `== 39.6` compare floats exactly; safe for fixed inputs but fragile under refactoring; use `pytest.approx`.
3. **tests/test_strategy_meta_adapters.py:180-196 — minor — repo-wide strategy sweep inside a unit test.** r1: iterates live strategies dir via `iter_strategy_files(DEFAULT_STRATEGIES_DIR)`, silently skips `pynecore_*` by name-prefix (:184), passes vacuously if dir empty/relocated — add `assert scanned > 0`. r2: no skip guard and no per-strategy isolation — any broken strategy anywhere fails this (arguably the point of a contract sweep), but one needing params for non-empty indicators fails for environment reasons; also accepts the same loose `{3,4}` arity flagged above. r3: results depend on checkout state; all-`pynecore_*` state → zero loop iterations → vacuous pass.
4. **tests/test_strategy_meta_adapters.py:28-32 — minor (r1) — `_load_module` never registered in `sys.modules`;** breaks dataclasses/pickling/relative imports if strategies use them; fine today, generic-looking helper worth noting.
5. **tests/test_strategy_meta_adapters.py:20,:23-24 — minor (all three) — import-time path setup debt.** Module-level `sys.path.insert(0, ...)` mutates global interpreter state (redundant under normal pytest rootdir config, can shadow modules — r2/r3); r3 adds the insert happens *after* earlier imports already succeeded (no-op for them) and `REPO_ROOT` is computed and never used; `STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR` alias used once (though it does make :183's sweep CWD-independent — r2).

Smells: OHLCV parquet scaffold repeated ~6× (:52-119, :122-176, :199-253, :256-329, :332-401) — extract helpers (r1/r3). Filename says "meta adapters" but content is vectorbt e2e plus pynecore enrichment — two concerns, Divergent Change (r1/r3); r3 suggests splitting pynecore-helper tests (:410-505) into their own module. Positive (r2): intrabar TP-resolution test (256-329) asserts exact fill time/price/PnL from synthetic lower-timeframe replay; pynecore directional SL/TP promotion (410-448) verifies side-correct selection incl. absence of raw `_long/_short` keys.

### tests/test_strategy_type_detection.py

Type detection (meta field, class pattern, default) plus CLI guard against running signal-processor strategies. No bugs found (any reviewer).

1. **tests/test_strategy_type_detection.py:24-39 — minor (r1) — class-pattern detection tested only via name substring.** Nothing pins whether any class containing the token counts or method presence (`process_signal`) is required — both signals appear simultaneously in the fixture, individual weight unknown; add the mutually-exclusive cases to pin precedence.
2. **tests/test_strategy_type_detection.py:8,:59-95 — minor (r3) — imports `runner.cli` at module import time;** runs whole CLI module (argparse setup etc.) for one call; if CLI import gains side effects (config load, network) the suite breaks — import inside the test instead.
3. **tests/test_strategy_type_detection.py:59-95 — nit (r1)/judgement-call (r3) — CLI rejection test builds a 16-field SimpleNamespace** mirroring argparse output, brittle to new args (factory would localize churn); r3 notes it relies on ValueError surfacing before any file/data access — currently pre-validation, fine, but if validation moves later it could start touching data dirs.

### tests/test_timeframe_normalization.py

Alias→canonical mapping plus pass-through cases including empty string. Nothing found for what's covered (all three).

1. **Coverage gap (minor, r1/r3):** uppercase input ("M1"), whitespace (" 1h "), tick/sec aliases, None-handling untested although viewer coalescing paths clearly rely on broader normalization (`m1`/`1m`/`M1` variants appear in viewer tests; r3 cites test_viewer_features.py:1292 normalizing `"M1"`/`" eurusd "` style input elsewhere). r3 sharpens: if `normalize_timeframe` is the single chokepoint these belong here; if not there are two normalizers and that duplication belongs in production review. Two-line additions, outsized value given timeframe canonicalization's role in data joins.

### tests/test_trade_engine.py

Strong execution-model coverage: next-bar-open fills, bar-low stops, SL-alias, stop-distance anchoring, same-bar TP priority — all against synthetic deterministic OHLCV (positive per all three).

1. **tests/test_trade_engine.py:324-368 — CRITICAL (r2) / major (r1,r3) — engine-parity test environment-dependent, skip-heavy, tautological fallback, weakly asserted.**
   - Tolerance: `tolerance = max(2, int(vectorbt_count * 0.3))` (:367) allows two supposedly equivalent engines to disagree on ~30% of trade counts (+2 absolute) — wide enough to hide systematic fill-logic divergences (same-bar stop ordering producing systematically fewer/more trades) (r2 critical; r1/r3 major, "smoke test wearing a correctness-test name"). r2 adds asymmetry: tolerance anchors to vectorbt_count alone, so failures depend on which engine drifts.
   - Skips: three independent skip conditions (missing strategy file :327, no parquet data :332, no coverage window :343) mean the only cross-engine check in the file silently no-ops on fresh checkouts lacking local user data under `DATA_DIR/ohlcv` — in CI the parity guarantee can be vacuous while green (all three).
   - Tautology: `if vectorbt_count == 0 and native_count == 0: assert True; return` (:363-365) — exactly the scenario where both engines break identically goes unnoticed (r2; r1 notes the literal-True branch).
   - Remedy (consensus): mark skips explicitly with surfaced reason strings, replace local-data dependence with a bundled deterministic fixture, tighten tolerance toward exact equality.
   **[SETTLED by verification]** Code read confirms every citation verbatim: skips at :326-327 (strategy file), :331-332 (`No local 1h OHLCV parquet data available`), :343 (coverage window); literal `assert True; return` at :363-365; `tolerance = max(2, int(vectorbt_count * 0.3))` at :367 anchored to vectorbt_count. Critical rating (r2) stands: the sole cross-engine consistency check is optional, accepts ~30% divergence, and its both-zero path is unfalsifiable.
2. **tests/test_trade_engine.py:18-75 — minor (r1/r3) — production-grade date helpers re-implemented inside the test module** (`_parse_date_bound` end-of-day adjustment, `_to_utc_datetime`, `_time_bounds`, `_has_coverage`, ~30-57 lines). r3 verified they mirror logic in `runner/engines/vectorbt_adapter.py:1050` (same function name/signature) and `tests/strategies/conftest.py:38,58`, with copies already diverging slightly in type (`pd.Timestamp` vs `datetime`); if production boundary semantics change, tests keep validating their own stale copy and the coverage gate could accept files production rejects. Import production helpers or trim.
3. **tests/test_trade_engine.py:51-66 — minor (r1; r3 concurs) — broad `except Exception` converts parquet read failure into `(None, None)` → "no coverage" → skip.** Corrupt fixture/schema drift masquerades as missing data instead of failing loudly; catch specific pyarrow errors, or at minimum log why (r3).
4. **tests/test_trade_engine.py:195,:234,:279 — nit (r3) — `pytest.approx(-1000.0, abs=0.5)` used well but derivation uncommented** (unlike sibling suites); one line of arithmetic prevents future readers "fixing" the constant.
5. Smells: `BacktestConfig(...)` construction repeated ~5-6× — builder factory (r1/r3). Dead code (r3): `_to_timestamp_utc` (:78-82) raised but never called. Sibling test :198 duplicates the bar-low test almost entirely via the SL-alias path — parametrize the two entry conventions instead of copy-paste (r1). Positive pins (r2): next-bar-open fill price vs synthetic values (:133), stop-from-bar-low (:191), PnL ≈ −1000 (:195), alias equivalence (:198-234), fill-anchored stop distance (:237-279), same-bar TP priority asserting both reason and meta echo (:282-321).

### tests/test_viewer_features.py

4790 lines; exercised via `TestClient`, tmp_path isolation, meaningful negative assertions. Overall quality high (r1); coalescing edge-case coverage unusually thorough with exact payloads and negatives (r2); r3 finds no tautologies among payload assertions.

1. **tests/test_viewer_features.py:1258-1279,1642-1672,1698-1724 — major (r2) / minor (r1) — thread doubles keyed off `traceback.extract_stack()` frame inspection.**
   - Mechanism: intercepting only threads constructed from frames whose filename ends with `viewer/core.py` and function ∈ {`_start_data_task`, `_start_dukascopy_task`, `_start_jforex_task`} couples the doubles to production stack layout by string match (r1 minor: renaming/moving silently changes which threads get faked).
   - r2 escalates to **major**: if renamed/inlined, interception silently stops and non-intercepted branches fall through to `real_thread(*args, **kwargs)` — the coalescing assertions (`starts == 1`) then evaluate against genuinely spawned workers, i.e. tests can degrade into launching the real download subprocess machinery (network access from unit tests) rather than failing loudly.
   - Consensus remedy: injectable thread factory/starter seam or patching the starter function directly. *Kept major given r2's concrete silent-network-access failure mode; r1's brittleness framing subsumed.*
2. **tests/test_viewer_features.py:2811-2818,2888-2895,3004-3010,3151-3157 — minor — bounded-but-real sleep-polling loops (all three).** Four act-phases spin `time.sleep(0.05|0.1)` up to 50 iterations (~2.5-5 s budget) waiting on live background threads/subprocesses, then read task state unconditionally; on slow CI the loop exhausts and assertions fail with confusing downstream messages ("status is 'running'") instead of clear timeouts. Remedies: deadline + `pytest.fail(..., f"timed out, last={task}")` or event-driven hooks. Boundedness keeps this minor (r1/r2); r3 notes the spawned real `sys.executable -c ...` process (:2800-2809) makes these integration-ish tests inside a unit suite.
3. **tests/test_viewer_features.py:3037 — minor (r1) — tautological/redundant assertion.** `assert "Running backtest... " in log_text` implied by the stronger preceding assertions (:3035-3036); dead padding check.
4. **tests/test_viewer_features.py:1382-1403 — minor (r1) — unbounded `Barrier(2)` wait can hang the suite instead of failing.** `barrier.wait()` has no timeout; a worker dying before the barrier blocks the other forever; `join(timeout=2)`+`is_alive()` only helps after both reach it. Pass a timeout. Related minors (r2 :1416-1419 / r3 :1418): `failing_starter`'s worker-thread `assert release.wait(timeout=2)` escapes `launch()`'s `except RuntimeError`, killing the thread unobserved — failure mode is hang/confusing late list-comparison error, not a clean red; r3 adds the `# pragma: no cover` on an actually-exercised except clause is misleading. And (r3 :1402-1404): the coalescing race can silently not happen — a missed barrier deadline raises BrokenBarrierError inside the thread while count assertions still pass with one `None`; checking both records reference the same task id would strengthen it.
5. **tests/test_viewer_features.py:2296-2299 — severity split (r1 major / r3 minor) — split-plan expectation encodes overlapping ranges / magic boundary dates.**
   - r1 (**major**): `test_auto_download_source_plan_splits_us_intraday_range` asserts US-intraday plan `[{huggingface, 2025-06-20→2025-07-31}, {alpaca, 2025-07-01→2025-08-05}]` — segments overlap a full month (July covered twice). Either the planner double-fetches a month (real bug blessed by test) or overlap is an intentional safety margin the test neither names nor comments; false confidence either way.
   - r3 (**minor**): the boundary dates come from `HF_INTRADAY_COVERAGE_END` in `viewer/main.py` duplicated as literals; derive from the module constant.
   *Not contradictory — different facets (overlap semantics vs literal duplication). Both retained; overlap question flagged for follow-up.*
6. **tests/test_viewer_features.py:2275-2284 — minor (r1/r3).** r1: needless `viewer_client` fixture dependency for pure `_auto_download_source` calls — misleading setup suggesting endpoint coverage. r3: expectations hard-code routing policy verbatim with no rationale — change-amplifying (verified consistent with `viewer/main.py` today).
7. **tests/test_viewer_features.py:3416-3417 (& :4190) — minor (r2/r3) — p-value asserted as range + `< 0.05`;** the range half is near-tautology (broken p=0.999 passes it), `< 0.05` meaningful for the deterministic curve; smoke-acceptable, weak half noted.
8. **tests/test_viewer_features.py:51-52 — minor (r2) — fixture clears module globals but tests mutate them directly** (:1841, :1912, :1973, :2029, :2065); correct under serial pytest, cross-test contamination under xdist/reordering.
9. **Security gaps (r3, minor):** :2968-2984 `/api/backtests/run` builds a runner.cli command from user JSON; happy path only — no test rejects `strategy` values like `../../x.py` or embedded metacharacters before `Popen`, for a platform launching arbitrary strategy files. :4748-4790 log endpoints tested only with well-formed JSON lines — malformed-line/path-traversal/oversized-log behavior of `/api/logs/{name}` unverified. (Positive, r1: rejection paths 400 pre-genesis :315-344, invalid query timeout :2125-2137, unknown provider :4562-4585; OHLCV fields stripped from public payloads :738-769.)
10. **tests/test_viewer_features.py:1416-1419 — minor (r2)** — see finding 4 (worker-thread assert escape).
11. **tests/test_viewer_features.py:2533 — minor (r3) — `assert isinstance(listed["elapsed_seconds"], int)` brittle;** wall-clock seconds legitimately serialize as float; couples to serialization details.

Smells (convergent): **Divergent Change/God-file (major hotspot, all three)** — ≥15 unrelated concerns (dukascopy genesis, grid matrix, gzip middleware, catalog grouping/merging, download coalescing + crash-recovery markers, task lifecycle/pause/resume/delete, subprocess log streaming, optimizations, signal replay, CAPM/p-value endpoints, portfolios, agent convert, logs); split per resource family. **Duplicated Code:** `_NoStartThread` double re-declared ~5-8× (:1310,:1349,:1469,:1526,:1598,:1748-1750,:1793-1795 plus stack-inspecting variants :1642-1672,:1698-1724); `fake_start` capture helper ~5-6× (:1216,:2098,:2145,:2187,:2224); 20-key portfolio metadata-row dict ×6 (:3532-3554,:3592-3630,:3647-3689,:3722-3743,:3767-3789,:3817-3840); `_equity_curve_from_returns` re-implemented here (:3369-3386) vs tests/test_stats.py:28-36; StockKit CSV fixture triplicated (see test_signal_import). **Private-state coupling (hotspot):** tests poke `DATA_TASKS`, `_COALESCED_CLEANUP_PENDING`, `_TASK_CACHE_HYDRATED`, `_BACKTEST_META_CACHE*`, `DATA_TASKS_LOCK` directly — refactor breaks ~20 tests at once. **Naming:** `test_backtest_run_signals_...` (:3043) vs `test_backtest_run_from_signals_csv...` (:3170) test different endpoints differing by one word (r3); god test `test_create_portfolio_persists_p_value_and_psr` (:4128-4222) exercises five behaviors in one body (r3).

### tests/test_walkforward.py

1. **tests/test_walkforward.py:51-205 — severity split — heavy mocking leaves fold construction/aggregation untested.**
   - r2 (**major**, strongest wording): every OutputStructure/SummaryMetrics/FileOutput test monkeypatches `optimize_portfolio_allocation` whose mock returns OOS metrics directly — the suite verifies "did we copy the mock through"; it stays green if `run_walkforward` stopped computing anything real. The strategy-mode `_FakeEngine` test (:393-421) partially redeems the module (fold construction + engine call ordering for real), but portfolio-mode/backtest_ids aggregation remains untested.
   - r1 (**major** but located at :18-45): signature-introspection tests assert API shape not behavior — parameter names and default literals break on legitimate rename/n_trials change; defaults-as-contract defensible for a CLI-facing entry point but must say so; also `strategy`,`engine`,`backtest_ids` asserted simultaneously though the calling modes appear mutually exclusive (:64 vs :395).
   - r3 (**minor**, judgement call): mocking is appropriate unit isolation; the risk is concentration — fold-window computation covered *only* by :393-421, and deleting that test would silently remove all real fold-boundary coverage.
   *Same underlying fact set (mock-dominated legacy mode; single real-coverage test); severity disagreement logged, not smoothed: r2 major / r3 minor. The suite-level exposure is real but bounded by the existence of the _FakeEngine test.*
2. **tests/test_walkforward.py:297 — minor (all three) — test encodes known limitation as spec.** `assert len(result["folds"]) == 1  # Current implementation has single fold`: future multi-fold support requires editing the test which then reads like a regression; name (`test_walkforward_multiple_backtests`) implies real multi-backtest coverage.
3. **tests/test_walkforward.py:268-270 — minor (r2/r3) — timestamp length check `len(ts_part) == 15`** accepts any malformed 15-char token; parse with `strptime(ts_part, "%Y%m%d_%H%M%S")`.
4. **tests/test_walkforward.py:51,93,120,142,164,186 — nit (r1) — `tmp_path` requested and unused** in ~8 tests that never touch the filesystem.
5. Smells: `mock_optimize` redefined identically 8-9× (:54-60…:278-283) — shared canned-payload fixture (r1/r3). `test_walkforward_output_has_all_required_fields` checks only 3 of the fields its docstring claims (r1). Hidden coupling (r3): `_FakeEngine.run` keys sharpe off call parity (`len(self.configs) % 2`) — silently flips expectations if train/test order changes. Hotspot (r1/r3): two calling conventions (`backtest_ids=` vs `strategy=`+engine) coexist without a note; suite leans backtest_ids-with-mocks leaving strategy mode effectively one test — rebalance.

### tests/test_yfinance_downloader.py

Single regression test: MultiIndex-column + named-index normalization into canonical OHLCV parquet.

1. **tests/test_yfinance_downloader.py:27 — severity split (r1 major / r2 major / r3 minor) — mock discards the call contract; critical kwargs unasserted; positional-arg fragility.**
   - Shared core: `monkeypatch.setattr(yf_dl.yf, "download", lambda **kwargs: payload)` accepts any kwargs and records nothing; a production change stopping `auto_adjust=False` (silently altering price semantics of stored data) or dropping start/end passes unchanged. Capture kwargs; assert the financially meaningful ones (`auto_adjust`, `start`, `end`, `interval`).
   - r2 (**major**) adds a sharper edge: the stub accepts **no positional argument**, so it works only if production calls `yf.download(**kwargs)` with the ticker as keyword; yfinance's real signature is positional-first. Either the downloader pins a call shape that would crash against the real library, or it passes ticker positionally and this stub would TypeError if signatures were checked.
   - r3 (**minor**): brittle double; `def fake_download(*args, **kwargs)` recording calls would be sturdier.
   *Facts agree; weight disputed. **[SETTLED by verification]** `data_tools/downloaders/yfinance_dl.py:198-207` calls `yf.download(tickers=..., start=..., end=..., interval=..., auto_adjust=False, progress=False, threads=False, group_by="column")` — all-keyword — and the installed yfinance 1.2.0 signature's first parameter is literally `tickers`. So: production's keyword call is valid against the real library; r2's "positional-first ticker / stub would crash" branch is **refuted**; r1/r3's core (kwargs discarded → `auto_adjust`/`start`/`end`/`interval` unasserted) **confirmed** as the substantive defect. Final severity: **major** (r1/r2 weight on shared facts), with the positional claim struck.*
2. **tests/test_yfinance_downloader.py:38-41 — minor (r2) — assertions cover row count/column order but never values;** no check that open/high/low/close/volume survived MultiIndex→flat unchanged (e.g. `stored["close"].iloc[1] == 2.2`); column-misalignment bug passes.
3. **tests/test_yfinance_downloader.py:41 — minor (r3) — `startswith("1990-01-02")` string-prefix timestamp probe;** catches day-level shifts but exact Timestamp comparison is stronger.
4. Coverage smell (r1): single happy-path for an entire downloader module — no empty-results/network-failure/multi-symbol/intraday coverage from this area; parametrize index shapes (RangeIndex, tz-naive index) to harden the reset_index regression.

---

## tests/validation/

### tests/validation/__init__.py

Empty package marker file (0 bytes), required so `tests.validation` imports resolve as a package. Nothing to review; accounted for as scanned-clean by all three.

### tests/validation/test_cli.py

Real end-to-end CLI coverage via subprocess: exit codes 0/1/2 pinned (:194-200, :113-126, :238-249), JSON/markdown formats, importer auto-detection, tolerance flags both directions. Good contract testing (all three).

1. **tests/validation/test_cli.py:50-57 — minor/major-borderline — subprocess without timeout (fixed-checklist item; all three).** `_invoke_cli` calls `subprocess.run([sys.executable, "-m", "validation.cli"], capture_output=True)` with no `timeout=` across ~15 invocations; a hang in the CLI (waiting on stdin, network retry loop, report-writing deadlock) blocks the suite indefinitely instead of failing the test. Add `timeout=60`. Positive (r3): cwd pinned to repo root (:56); no `shell=True`; list argv (r1).
2. **tests/validation/test_cli.py:237-250 — minor — exit-code-2 test accepts three different error-message shapes via `or` chain** ("not found" OR "failed to load reference" OR "no such file or directory"). r1: passes whether the CLI handles missing files itself or lets an unhandled OSError traceback through — exactly the distinction this test should pin; tighten to the intended message. r2: exit-code assertion carries all the weight; broad OR weakens the contract but acceptable as robustness choice.
3. **tests/validation/test_cli.py:109 — nit/minor — markdown-format probe with two accepted variants.** `"Overall Passed**:** Yes" ... or "Overall Passed**: Yes"` encodes two renderings with an OR fallback. r1 flags brittleness; r2 explicitly contrasts: here the two strings *differ* so this is legitimate dual-format tolerance — flagged only to contrast with test_markdown_report.py:125 where they are identical; r3 concurs (a third variant fails both, so it's noisy rather than wrong).
4. **tests/validation/test_cli.py:144-190,:290-342 — minor (r1) — same file used as both reference and platform.** Legitimate for self-comparison smoke tests, but reference≠platform divergence through importers is covered only on the JSON path (:203-287).

Smells: ref/platform JSON-writing block repeated 3× (:211-214, :262-265, :241-242) — extract helper (r1); fixtures `tmp_reference_json`/`tmp_platform_json` exist but aren't reused where custom trades are needed — extend with overrides param instead of re-inlining (r3). Each run spawns an interpreter (~0.5–1s ×15) — acceptable, batch or go in-process if it grows (r1).

*Reference-report cross-check (t3-8):* the reference alone flags `tests/validation/test_cli.py:144-190` — generated openpyxl Workbook never explicitly closed before `wb.save()`. **[SETTLED by verification]** Confirmed literally true (:149 builds, :178 saves, no `wb.close()`), but openpyxl workbooks hold no OS-level file handles until `save`, and save flushes/closes internally — consequence negligible; adopted as a nit only, not elevated to the panel's findings.*

### tests/validation/test_cross_platform_metrics.py

Integration-style matrix tests over repo artifacts; correct path anchoring (`Path(__file__).resolve().parents[2]`) and path-containment assertions (:46-48).

1. **tests/validation/test_cross_platform_metrics.py:16-48,:205-219 — major (r1/r2/r3) — hard dependency on mutable repo working-tree data with hard-pinned golden values and no absence guard.**
   - Shared core: primary matrix test reads real repo artifacts (`strategy-comparisons/`, `reports/`, `data/backtests/*.json`) and pins `reference.trade_count == 512`, `win_rate ≈ 0.4492`, `sharpe_ratio == 0.544687` (:30-31,:42) from artifact files living outside `tests/fixtures`, regenerated by scripts imported at :8-9.
   - r1 frames it as golden-file pattern without golden-file hygiene: legitimate regeneration breaks tests while stale-but-present artifacts pass silently — no skip guard, no checksum, no documented refresh procedure in-test; most environment-coupled file in the validation suite.
   - r2 sharpens: **no skip guard for absence**, unlike test_trade_engine which at least skips — fresh checkouts lacking committed artifacts hard-fail (KeyError/FileNotFoundError), or pass against *stale* artifacts while asserting exact historical values; coupling is intentional parity-pinning but nothing distinguishes "artifact missing" from "parity broke".
   - r3: any regeneration of parity artifacts breaks pins with no hint whether matrix code or data changed; prefer synthetic fixture comparisons dirs for logic tests plus at most one smoke test against real artifacts.
   - Consensus remedy: reference the regenerating script in a comment, document refresh procedure, consider copying fixtures under `tests/fixtures`, add skip guards.
   **[SETTLED — no spot-check needed]** Unanimous panel agreement (3/3) plus the reference report's independent T3-4 at the same lines; treated as settled without spending a verification slot.
2. **tests/validation/test_cross_platform_metrics.py:77-106 — minor (all three) — recomputation test asserts only sign.** Seeding stats with `-1.0` then asserting `platform["sharpe_ratio"] > 0.0` proves recomputation happened, not that it's correct (+0.0001 with wrong formula, right sign, passes); assert an approx value derivable from the constructed equity series.
3. **tests/validation/test_cross_platform_metrics.py:109-128 — minor/major-borderline (r3) — timezone-consistency test depends on both real xlsx and real backtest JSON existing;** also normalizes `.replace("+00:00", "Z")` inline in the test (:127) — that normalization belongs in the production serializer being tested, else the test masks a format inconsistency between importer and platform outputs.
4. **tests/validation/test_cross_platform_metrics.py:8-9 — minor (r1/r3) — tests import production scripts as modules** (`scripts.regenerate_mt5_parity_artifacts`, `COMPARISON_CASES`, `QC_RISK_PARITY_OUTPUT`): couples test collection to script import side effects/module paths; if scripts move, this breaks.

Smells: two QC-attribution tests (:131-166, :169-202) differ only in two stat values — parametrize (r1); hidden dependency on `PROJECT_ROOT/"reports"` where user-generated reports can perturb outcomes — apply the tmp_path isolation used at :51 elsewhere (r1); Data Clumps: dir-path triple repeated across seven calls (r3). Positive (r2/r3): negative paths good — missing platform artifact (:51-58), malformed attribution fallback (:169-202), containment check inside PROJECT_ROOT (:46-48, a real path-traversal guard).

### tests/validation/test_delta_report.py

Scanned by all three (538 lines).

1. **tests/validation/test_delta_report.py:98-113 — severity split (r3 major / r1-r2 minor) — dead test utility with unimported `Any` annotations.**
   - Shared core: `_make_field_result(...)` annotates parameters with bare `Any` without importing it; masked only by `from __future__ import annotations` (:3). Any runtime annotation resolution (`get_type_hints`, dataclass tooling) raises NameError; type-checkers flag it. The helper is also dead code — no test in the module calls it (all tests build real comparator results).
   - r3 rates the pair **major** (latent NameError + broken signal for type checkers + dead code); r1 splits into minor (the `Any`) + **major** (dead utility per checklist); r2 lists both as minors.
   *Facts identical; weight disputed. **[SETTLED by verification]** Code read confirms both halves exactly: `_make_field_result` defined at :98 with zero call sites in the module, and `expected/actual/tolerance: Any` at :100-102 under `from __future__ import annotations` (:3) with no `typing` import anywhere in the file. Settled at **major**: dead test utility per checklist plus latent NameError on any annotation resolution — the two defects compound (the only consumer of the annotations is a function nobody calls).*
2. **tests/validation/test_delta_report.py:98-113 / :166-178 — minor — vacuous numeric "match" test (r1).** `test_field_delta_numeric_match` sets `relative_diff=0.000009` and `tolerance=0.0001` itself, then asserts `relative_diff < tolerance`: tautology over its own constants; no production code participates. Also (:136-196) constructor round-trip tests (`test_field_delta_creation`, `test_field_delta_explanation_for_match`, `test_trade_delta_creation`) assert only that dataclasses store attributes while names suggest delta-logic coverage (r1).
3. **tests/validation/test_delta_report.py:267 & :325 — judgement call (minor) — warning-severity mismatch yields `passed is True`, asserted as intended behavior.** r1: class name asserting `passed is True` surprises until the inline comment explains warning-severity policy; rename or document. r3: comment documents the contract, so policy not bug — but a numeric entry-price difference of 0.45% letting a validation report "pass" is surprising for a validation suite; worth an explicit product-level decision note.

Smells: five-line act sequence (matched→compare→…) repeated ~12-15× verbatim across classes — parameterized helper (r1/r3); positional 4-arg `DeltaReportBuilder.build(ref, platform, comps, anomalies)` invites argument-order mistakes — prefer keywords (r1); r2 adds unused `pytest` import (:7, checked: no `pytest.` in body). Positive (r3): severity/passed logic matrix (:316-361), explanation formatting per field type (:423-454), all four anomaly-type conversions (:457-520) precisely asserted; every assertion pins concrete ids/severities/counts.

### tests/validation/test_fixed_risk_model.py

Mirrors the percent-risk suite; good budget-boundary test (`round_lots` must never exceed risk budget).

1. **tests/validation/test_fixed_risk_model.py:158-165 — major (r1) / minor (r2,r3) — test name says "rounds up" but asserted behavior rounds DOWN.**
   - Shared facts: `test_round_lots_rounds_up` computes raw = 33.33 and asserts 30 (nearest-lower multiple of 10); inline comment says "rounds to 30 (nearest multiple)". If production switched to true round-half-up (→40, breaching budget) the test would rightly fail; as named, a maintainer "fixing" rounding to match the name would break the safety property :86-97 enforces. The sibling percent-risk file names the equivalent case `test_round_lots_rounds_down` — inconsistent naming across twin suites (r3); together the two tests pin *floor* semantics, so "rounds_up" is doubly wrong (r3).
   - Weight only: r1 **major** ("misleading name on a money-touching code path"); r2/r3 **minor** ("rename to reflect actual semantics").
2. **tests/validation/test_fixed_risk_model.py:86-97 — minor — self-acknowledged confused derivation in `test_lot_rounding_never_exceeds_risk_budget` (all three).** Comment block (:87-89) narrates abandoned alternatives ("250 ... 260 lots? Instead pick...") left from debugging; final math checks out (259.74 → floor 259, risk 99.715 ≤ 100) and r3 stresses the assertion pair is excellent — exactly the property you want pinned — but the muddled derivation invites someone to "clean up" the test into a weaker form. Rewrite with actual arithmetic (stop_distance = 0.385 → raw = 259.74).
3. **tests/validation/test_fixed_risk_model.py:149-155 — nit (r1) — vacuous-rounding case:** raw 50 already a lot-multiple; rounding branch untested here despite the dedicated file (covered at :158).
4. **tests/validation/test_fixed_risk_model.py:188-195 — minor (r1) — same naming contradiction as percent-risk:** `test_min_position_size_clamping_zero` clamps to 0 with min=50 never asserted as such; add the between-0-and-min distinguishing case.
5. **tests/validation/test_fixed_risk_model.py:237-245 — minor (r3) — `test_stop_distance_pct_when_entry_nonzero` tests a field it admits doesn't exist** ("TradeRiskProfile doesn't have stop_distance_pct field"); rename or flag the missing pct field as a gap.

Smells: near-identical twin of test_percent_risk_model.py (~60% shared structure) — parametrized shared harness over both models would cut ~150-200 lines and expose coverage gaps like percent-model RR-from-meta cases fixed-risk lacks (r1/r3). Dead comment :242-244 explains nonexistent `stop_distance_pct` field (r1). Positive: NaN/inf validation (:275-286) right for sizing code; meta-extraction precedence direct-SL-over-meta-SL pinned at :310-315 (r2).

### tests/validation/test_golden_files.py

Golden-file smoke suite over tracked JSON goldens (verified tracked earlier by r1).

1. **tests/validation/test_golden_files.py:15-18 — minor — missing directory yields zero files rather than error (r1/r2/r3, convergent framing).** `_list_golden_files` returns `[]` when GOLDEN_DIR doesn't exist; the ≥3 guard then fails with a clear message (good), so it's diagnostics not false-pass — but partial golden deletion down to exactly 3 passes everything while coverage silently shrank (r1), and only `test_at_least_three_golden_files` names the absent path in its message while other classes say generically "No golden files found" (r2); r3 notes a misconfigured checkout yields one opaque failure instead of a clear skip. Positive contrast: paths anchored via `Path(__file__)` (:12) — no CWD dependence.
2. **tests/validation/test_golden_files.py:28-45 — nit (r1) — loop-based tests report only the first failing file;** parametrize for per-file granularity. Smell: each test re-reads/re-validates every golden — module-scoped parsed fixture (r1/r3). Weak key set (r1): required stats = `{total_pnl, gross_profit}` (:52) though importer tests require far more keys — inconsistent bar.

### tests/validation/test_importers.py

Base-class contract + CWD-independence check + declared-timezone conversion. Scanned-clean by r1/r3 — notably this file anchors paths via `Path(__file__)` (:13) and explicitly tests CWD independence (`monkeypatch.chdir(tmp_path)` :30 — excellent, directly covering the defect flagged in siblings), pins exact trade count 5 (:39), concrete UTC instants (:43-51), abstract-base and missing-file negatives.

1. **tests/validation/test_importers.py:50-51 — minor (r2) — expected timestamps `"2024-01-03T06:00:00Z"`/`"...T14:00:00Z"` are bare magic strings** with no derivation comment for Europe/Athens (+02:00) applied; arithmetically consistent (08:00→16:00 local), but golden values without derivation make off-by-one-hour regressions indistinguishable from intended fixes.
2. Coverage note (nit, r1): only MT5HTMLImporter gets load-path tests here; MT5XLSXImporter's subclass-ness asserted but loading delegated to its own file — fine, noting asymmetry.

### tests/validation/test_markdown_report.py

Reasonable structural coverage of markdown rendering incl. both input modes (ref+platform vs pre-built ValidationReport).

1. **tests/validation/test_markdown_report.py:125 & :223 — severity split (r3 major-borderline "broken test logic" / r1-r2 minor) — copy-paste tautology in OR assertion.**
   - Shared facts: `assert "Overall Passed**: Yes" in md or "Overall Passed**: Yes" in md` — both disjuncts byte-identical, the `or` is dead; intended second variant (presumably `"Overall Passed**:** Yes"`, cf. test_cli.py:109's genuinely distinct pair) was lost to copy-paste. Same pattern at :223 with `"Overall Passed**: No"` twice.
   - r3 escalates: as written it silently narrows accepted formats *while pretending to be lenient* — restore the alternate format or collapse to a single assertion. r1/r2 keep minor: harmless today but can never catch a formatting change.
2. **tests/validation/test_markdown_report.py:6-31 — minor (r2/r3) — ten-plus unused imports (dead code).** `datetime`, `timezone`, `pytest`, `AnomalyDetector`, `FieldDelta`, `TradeDeltaReport`, `MatchKey`, `TradeMatcher`, `ReportSummary`, `StatDelta`, `TradeComparisonResult`, `Any` imported and never referenced (`datetime.datetime` on :284 is inside a string literal, not the symbol). File visibly copy-pasted from test_report_structured.py without pruning. (r1 separately questions whether `TradeStatistics` in test_report_structured.py:32 is used only via `StatDelta` — verify with lint.)
3. **tests/validation/test_markdown_report.py:275-285 — minor (r1) — negative assertions too narrow to be meaningful.** `test_no_internal_python_objects_in_markdown` checks three literal substrings (`<object`, `datetime.datetime`, `Decimal(`); a repr of any other type sails through. The JSON sibling (test_report_structured.py:369-388) does recursive type checking — reuse over non-prose tokens, or rename to spot-check. (Positive, r3: object-repr leak check exists at all.)
4. **tests/validation/test_markdown_report.py:160,:162,:175 — nit — broad lowercase substring probes** ("mismatch"/"differs", "entry_price"/"Entry") accept either wording; can't detect regression dropping numeric detail entirely as long as one word survives (r1/r2/r3 convergent).
5. **tests/validation/test_markdown_report.py:166,:168,:247,:249,:290 — minor (r2) — direct attribute mutation (`ref_trade.exits[0].pnl = 100.0`) on pydantic models;** if Trade/TradeExit become frozen these tests break en masse. Works today; fragility note.

Smells: Duplicated Code **major-by-count (r1)**: 5-line ref/platform setup block repeats verbatim in all ~22 tests — strongest duplication candidate in the group; sixth copy of the `_make_trade/_make_reference/_make_platform_result` triple (r2/r3). Formatting-string probes (`- **Symbol**`) churn with cosmetic renderer changes — assert semantic content via the JSON report instead (r1). Positive (r2): report-object round-trip tests (:228-243, :287-302) verify JSON→model→markdown consistency.

### tests/validation/test_mt5_html_importer.py

Mirrors the xlsx importer suite against the tracked HTML fixture; helper unit tests included.

1. **tests/validation/test_mt5_html_importer.py:15-17 — minor/major-borderline — CWD-relative fixture path without skip guard (r1/r3; r2 implicitly via cross-file note).** `Path("tests/fixtures/validation/mt5_html/sample_report.html")` resolves against process CWD; runs from repo root pass, from any other directory ~25 tests error FileNotFoundError rather than skipping. Anchor with `Path(__file__).resolve().parents[2]` (as test_cross_platform_metrics.py:13 does correctly).
2. **tests/validation/test_mt5_html_importer.py:48-50,:85-88 — minor — weak/dishonest goldens.** r1: `len(ref.trades) >= 5` floor vs xlsx sibling pinning exact counts — an exact count catches row-loss regressions; r2 notes test_importers.py:39 pins exactly 5 for the same fixture so the weaker bound here is redundant-but-safe. r3: `test_entry_exit_direction_consistency` again validates only quantity equality despite its name (copy of the misnamed xlsx-suite test) — implement per-direction price-side checks or rename.
3. **tests/validation/test_mt5_html_importer.py:58,:60 (& xlsx :67,:70) — minor — `startswith("2")` century-dependent smoke checks;** ISO regex, exact fixture timestamp, or `datetime.fromisoformat` parse would actually validate (r2/r3; r1 nit).
4. **Dead imports — minor (r2):** `from typing import Any` unused (:6); `Trade` imported (:10) never referenced.

Smells: `TestHelpers` duplicates `_coerce_setting`/`_extract_first_percent`/`_parse_count_pct`/`_infer_exit_reason` tests nearly verbatim from test_mt5_xlsx_importer.py:222-247 — because production duplicates those helpers too; fix once in a shared parser mixin and both suites collapse (Shotgun Surgery mirror image, r3). Repeated `importer.load(_sample_path())` parsing (~20 fresh parses/run) → module-scoped fixture (r1/r2/r3). Positive (r3): negative cases cover missing file and empty HTML (:164-173); solid field-level coverage (long+short present, wins+losses present, SL/TP exit reasons).

### tests/validation/test_mt5_xlsx_importer.py

Real-data golden tests against three tracked xlsx exports (r1 verified present & git-tracked) plus unit tests of private parsing helpers incl. timezone conversion.

1. **tests/validation/test_mt5_xlsx_importer.py:15-24 — severity split (r3 major / r1-r2 minor) — CWD-dependent repo-artifact dependence with no skip guard and hard-pinned data values.**
   - Shared facts: `_eurusd_path()` etc. use repo-relative CWD-dependent paths (`strategy-comparisons/intraday_volatility_breakout/*.xlsx`) instead of anchoring to `Path(__file__)`; unlike test_regression.py:390 there's no existence check/skip guard, so absent/moved/LFS-migrated workbooks produce noisy errors, not clean skips; assertions pin data values — `total_trades == 492` (:98), `ATR_Period == 5` (:103), `source_timezone == "UTC"` (:109) — so a legitimate re-export breaks ~20 assertions.
   - r3 (**major**, environment coupling): fail outright if files are absent/moved/re-exported; should ship small fixture xlsx under `tests/fixtures/validation/` or skip-guard like siblings.
   - r1/r2 (**minor**): same substance framed as robustness/hygiene (CWD dependence + missing skip guard; literal-vs-crosscheck note below).
   *Kept split pending no contrary evidence; the environment-coupling theme already carries major weight via cross_platform/trade_engine.*
2. **tests/validation/test_mt5_xlsx_importer.py:98,:168 — minor (r1) — exact-count golden duplicated as both literal and cross-check.** :98 pins 492 literally; :168 asserts `len(trades) == stats["total_trades"]` — the cross-check is the valuable invariant; the literal breaks on re-export even when parsing is correct. Know which is load-bearing.
3. **tests/validation/test_mt5_xlsx_importer.py:188-197 — minor (all three) — dead branch structure in consistency test.** `if direction == "LONG": pass else: pass` does nothing; only final volume equality asserted; name promises direction consistency but validates quantity equality. Drop the branch or assert actual in/out side semantics ("buy in, sell out") — currently untested (copy-paste residue).
4. **tests/validation/test_mt5_xlsx_importer.py:67,:70 — nit — `startswith("2")` year probe,** brittle-by-construction (see html sibling).
5. Dead imports (minor, r2): `Any` (:6), `Trade` (:10).

Smells: Performance — every test calls `importer.load(_eurusd_path())` afresh (~20 parses of a 492-trade workbook per run); module-scoped cached fixture cuts suite time substantially (all three). Feature Envy — `TestHelpers`/`TestSourceTimezone` exercise private statics (`_coerce_setting`, `_to_iso`, `_infer_exit_reason`); acceptable white-box choice binding to private API (r1/r3). Positive (r1/r3): timezone handling tested properly (Athens→UTC :250-264 with DST-correct January offset, invalid zone rejected :266-274).

### tests/validation/test_paper_readiness.py

Scanned by all three (525 lines).

1. **tests/validation/test_paper_readiness.py:142-148,:147 — latent fixture bug — severity split (r1 major / r2 minor).**
   - r1 (**major**): `_make_risk_report` double-constructs results so `passed` can contradict its own contents: `(results or [_make_risk_result()])` evaluated twice creates two *different* `RiskComparisonResult` objects — one stored in `trade_results`, another used only for `passed=all(...)`. With defaults both pass so today they agree, but any future failing default makes the report claim `passed=True` while containing failing results — fixtures hiding production bugs.
   - r2 (**minor**) reads the same line differently: the `or` short-circuits so with non-empty results it's fine; the footgun is `results=[]` fabricating one implicit passing result instead of expressing "no risk trades". Edge-case footgun in the helper either way.
   *Same line, different failure modes described (double-construction vs empty-list semantics) — not strictly contradictory; both retained. **[SETTLED by verification]** Code read (:142-148) confirms both readings simultaneously: `(results or [_make_risk_result()])` appears twice — :146 stores one instance, :147 constructs a second solely for `passed=all(...)` (r1's double-construction, real); AND when callers pass an explicit empty list `[]`, `or` falls through to a fabricated passing result rather than expressing "no risk checks" (r2's footgun, also real). r1's latent-bug framing is the more consequential reading; final severity **major** per r1.*
2. **tests/validation/test_paper_readiness.py:510-525 — minor (all three) — test name/docstring contradicts assertion.** Docstring says "All optional inputs omitted → NOT_READY because required artifacts missing"; body constructs ReadinessConfig disabling all four `require_*` flags and asserts READY (:521). Config-plausibly-correct behavior means the docstring is stale — misleading either way (r1); the default-config NOT_READY-on-missing-artifacts path named in the docstring has zero coverage — the more safety-critical branch (r2/r3); fix docstring and add the default-path case.
3. **tests/validation/test_paper_readiness.py:380-406 — minor (r1) — config knobs may not bind to the check under test.** `ReadinessConfig(max_position_size_diff_pct=0.001, max_stop_distance_diff_pct=1.0)` passed but failure comes from pre-baked `FieldComparisonResult(passed=False)`; config appears inert in this path — if the validator ignores them, undetectable; mild false confidence.
4. **tests/validation/test_paper_readiness.py:220 & :292 — minor (r3) — severity expectations hard-coded to error vs warning:** tests build deltas already labeled `severity="error"` then assert the validator reports an error; classification happens upstream in delta_report/comparison, so these partially assert what they themselves put in — fine as validator-level tests, not end-to-end severity-classification proof.
5. **tests/validation/test_paper_readiness.py:74-80 — minor (r1) — regression result fixture hardcodes `/tmp/golden.json`;** bypasses tmp_path hygiene (never opened — harmless) but normalize for hermeticity.

Smells: 6-7-line validate() call block rebuilt ~13-14× — parametrized fixture cuts ~100 lines (r1/r3); `(report, regression, risk, stats, config)` positional clump — bundle/fixture (r3). Speculative Generality nit: `_make_stats(net_profit=...)` mostly defaulted (r1). Positive (r2): threshold boundary for missing trades within/above, severity escalation, explicit level-calculation triptych (:472-508) well pinned; r3 calls it best-structured of the validation suites reviewed.

### tests/validation/test_percent_risk_model.py

Strong parametrization of validation errors, boundary tests for rounding/clamping, adapter-integration checks.

1. **tests/validation/test_percent_risk_model.py:310-331 — major (r1/r3) — test verifies nothing about meta extraction of take-profit; its own comments admit it.**
   - Shared facts: builds `inp` with `take_profit=None`, never calls `validate(inp)` on it (first input dead), then builds `inp2` pulling TP from meta *manually in the test* (`trade.meta.get("take_profit")`) and sizes that. Name promises "validate extracts take profit"; body proves only RR computes when TP supplied. Comment block (:320-322) concedes "meta extraction is for stop_loss only… that's adapter-level". False confidence that the model extracts TP from trade.meta when it does not.
   - r1 **major** (misleading test); r3 **major** (misleading test + dead fixture; rename to `..._when_take_profit_passed_explicitly` or implement/verify real extraction); r2 **minor** with same substance.
2. **tests/validation/test_percent_risk_model.py:245-252 — minor — min-position-size clamping-to-zero semantics ambiguous.** Sets `min_position_size=200.0`, asserts size clamps to **0.0** (not 200); comment says "below min 200 -> clamped to 0" so zeroing-out is evidently intended ("can't take a position that small"), but the reader can't tell whether min means "floor" or "veto threshold" — config value never appears in the assertion. Rename (`test_below_min_position_size_yields_zero`) and add the between-0-and-min case where clamp-vs-veto differ (r1). r3 adds: also assert post-clamp per-profile `risk_amount` (== 0?); if profile still reports risk_amount 100 while size is 0, dashboards overstate exposure.
3. **tests/validation/test_percent_risk_model.py:208-214 — minor (r1) — `test_round_lots_enabled` exercises no rounding.** Raw 50 already a multiple of lot 10; floor-rounding branch never executes (disabled-case :217 covers it); use a non-multiple like :221 does.
4. **tests/validation/test_percent_risk_model.py:217 — minor/nit (r2) — naming drift mirror-image:** `test_round_lots_rounds_down` comment describes nearest-rounding that happens to round down here (`round(33.33/10)=3 → 30`); raw 36 would produce 40 — "down" not guaranteed; consistent with the misleading-naming pattern in test_fixed_risk_model.py:158.
5. **tests/validation/test_percent_risk_model.py:374-379 — minor (r1) — sentinel conflation.** `test_trade_id_zero_when_no_trade` blesses `0` as the "no trade" ID; if Trade ids are 1-based this collides with a real id 0; use an explicit sentinel constant.

Smells: `PercentRiskConfig(risk_percent=0.01)` + model rebuilt in ~25-30 tests — module fixture (r1/r3). Section banner "Meta extraction" (:281) contains the one test that doesn't extract from meta (r1); banner comments copy-pasted across sibling sizing files (r3). Positive (all three): validation-error coverage zero/negative/None/inf balance (:143-172), NaN entry (:396), inf SL (:403), zero-reward RR → None (:354-359), adapter dispatch type errors with matched messages (:455-461) — exactly right for a sizing model; good numeric derivations in comments throughout (r3).

### tests/validation/test_quantconnect_result_importer.py

Called the strongest file in the chunk by r2 (synthetic fail-closed tests match exact anchored-regex messages :202-307/:488-526/:639-719; fee params parametrized over 8 invalid inputs incl. NaN/currency-case :529-552; Decimal precision end-to-end :555-588; CSV↔JSON mismatch both kinds + escaped-tab injection rejection :722-871). r1: extensive fail-closed coverage with exact message matching, Decimal-exact fee derivation. r3 scanned-clean apart from a fixture smell.

1. **tests/validation/test_quantconnect_result_importer.py:19-29 — major (r1/r2) — import-time environment coupling of reference paths.**
   - r1 (**major**): `_reference_dir()` reads `QUANTCONNECT_REFERENCE_DIR` and is evaluated at module import (:26), freezing RESULT_JSON/ORDERS_CSV/TRADES_CSV; a stale exported value redirects all 8 `golden_reference` tests (:302-636) to different data while they claim to validate the tracked reference (`tests/fixtures/validation/quantconnect-reference/`); if fixtures ever absent, module-scoped fixture's bare assert (:304) errors 8 tests unhelpfully instead of clean `skipif`. Files git-tracked today (verified) — robustness/misleadingness rather than breakage.
   - r2 (**major**, overlapping): module-scoped `golden_reference` fixture loads a 597 KB real QC export (`Sleepy Tan Coyote.json`) behind bare `assert RESULT_JSON.is_file()`; absence (fixture pruned for size) is an **error not a skip** — inconsistent with `pytest.skip` usage at test_regression.py:389; erroring defensible for parity pinning but inconsistent with the rest of the suite.
   **[SETTLED by verification]** Code read confirms `_reference_dir()` reads `QUANTCONNECT_REFERENCE_DIR` and `REFERENCE_DIR = _reference_dir()` is frozen once at import (:26), with the three paths derived from it (:27-29) — r1/r2's mechanics exact. r3's clean scan is a **false negative**: the env-capture defect is structural (import-time freeze + bare existence assert in the fixture), independent of test-body quality. Final: **major** stands; reference report's t3-13 rates the same defect minor — panel weight wins.*
2. **tests/validation/test_quantconnect_result_importer.py:328-346,:341-346,:354-376,:414-435,:591-622 — severity split (r2 major / r1 minor) — brittle over-specified goldens with zero derivation.** Exact cluster-size distribution dict, literal 20-element order-ID list, 264 orders / 84 clusters / 236 trades / 160-76 win-loss / fee remainder 81.97 / 5471 equity points — excellent regression pins, but pure goldens with no regeneration story: any legitimate re-export invalidates ~40 assertions at once across four test functions; nothing in-file explains how to regenerate them (regenerator scripts exist for MT5 cases; no QC equivalent referenced). r1 keeps minor: the invariant checks at :451-456 are the valuable part; demote the literal lists. r2 adds intra-test duplication: `observed_remainder = Decimal("2698.00") − Decimal("2616.030")` (:412) restates numbers already asserted at :372-373 instead of deriving from `fees[...]` — classic duplicated-golden hazard.
3. **tests/validation/test_quantconnect_result_importer.py:902 — minor (r1/r2) — statement with no effect.** Bare `json.dumps(reference.raw_metadata)` after first load reads as leftover debug line or undocumented serializability smoke check; can never fail as written (no assert on round-trip); add `assert isinstance(..., str)` or a comment, or delete.
4. Fixture smell (minor, r3): long parameter lists building large synthetic QC result dicts inline repeatedly; shared builder would cut noise. Long-file note: 912 lines driven by table-driven cases but content coherent — splitting optional (r3).

Security (r1, positive): this suite *enforces* fail-closed parsing (non-finite profitLoss :202, fractional order id :215, trailing-text price :225, duplicate/unknown IDs :269/:282/:687/:697, negative fee remainder :510) — security-relevant for import paths.

### tests/validation/test_regression.py

Solid behavioral coverage of golden-file regression incl. tolerances, empty goldens, missing files; tolerance matrix (:306-381) thorough with each side exercised; inf-vs-inf and inf-vs-value profit-factor semantics pinned (:460-476) — genuinely valuable edge case (all three).

1. **tests/validation/test_regression.py:87-101 — minor (all three) — count assertion compares against a hand-copied field list, not the source of truth.** `len(result.stats_delta)` vs length of inline 10-field literal duplicated from production: asserts only the *count*, never the names (r2 sharpens: renamed-field-plus-dropped-field keeping total 10 passes — the sibling test_stats_comparison.py:37-48 does it right with a set equality; copy that shape). If production adds an eleventh metric the test fails confusingly-good, but the list rots independently of what `_compare_stats` emits; assert `RegressionConfig`'s tracked-field definition or specific fields.
2. **tests/validation/test_regression.py:389-391 — minor (all three) — relative fixture path depends on CWD with silent skip.** `Path("tests/fixtures/validation/mt5_html/sample_report.html")` resolves against process working directory; from another directory the `if not html_path.exists(): pytest.skip` makes the only importer-integration regression test vanish silently (golden-file round-trip through a real importer lost). Anchor with `Path(__file__).resolve().parents[2]`.
3. **tests/validation/test_regression.py:172-183 — minor/judgement-call (r3) — weak isolation in `test_regression_trade_and_stats_both_fail`:** second-trade removal also changes net profit, conflating "missing trade changed aggregate" with tolerance logic; delivers both failure modes, just not independently.
4. **tests/validation/test_regression.py:460-468 — minor (r1) — infinity-vs-infinity comparison asserted passed without pinning sign semantics;** `-inf` vs `inf` would also be "equal" under naive implementations — add the `-inf` case.

Smells: Duplicated Code **major-volume (r1)**: the 4-line golden-write+run scaffold repeats verbatim ~20-25× across classes — parametrized helper/context manager halves the file (r1/r3). Dead imports (r3): `copy` (:5), `FieldDelta`, `TradeDelta` (:14), `MatchKey` (:15) unreferenced. Naming nit: `TestImporterAndEdgeCases.test_regression_run_with_importer` exercises importer path *and* asserts round-trip identity (r1).

### tests/validation/test_report_structured.py

Good structural coverage incl. strict-JSON sanitization (inf/nan → null) and precomputed-delta injection paths.

1. **tests/validation/test_report_structured.py:231-245 — minor (all three) — tautological assertion pads summary test.** `assert trade_summary["errors"] + trade_summary["warnings"] >= 0` can only fail on negative counts — effectively asserting keys exist and are numeric; unfalsifiable; replace with concrete counts for the fixture (which has exactly 1 missing + 1 extra).
2. **tests/validation/test_report_structured.py:427-439 — minor (r1) — misleading comment about strict JSON verification.** Comment claims `json.loads` rejects NaN/Infinity without parse hooks — Python's `json.loads` *accepts* them by default; the guarantee comes from `_sanitize_json_numbers` + `json.dumps(..., allow_nan=False)` re-serialization (:439). The final dumps' return value unused; sanitization regression to bare `NaN` tokens would still be caught only at :439 — fine, but the misstatement could mislead future edits.
3. **tests/validation/test_report_structured.py:337-341 / :339 — minor (r1/r3) — over-broad exception match.** `pytest.raises((TypeError, AttributeError))` for a string platform argument accepts either failure mode and keeps passing even if the error drifts to accidental AttributeError deep inside serialization; pin the specific contract.
4. **tests/validation/test_report_structured.py:354 — minor (r2/r3) — near-tautology `assert "Yes" in md or "No" in md`;** almost any rendered markdown containing either word passes; pin `Yes` since the fixture passed.
5. **tests/validation/test_report_structured.py:393 — minor (r2/r3) — suspicious unnormalized input with no corresponding assertion.** `symbol=" sym"` (leading space) fed to ValidationReport but dump asserts only strategy_name/summary fields — if the model strips symbols that behavior is untested; if not, dead/confusing fixture data; assert `d["symbol"] == "sym"` or use a clean literal.
6. **tests/validation/test_report_structured.py:269-278 — minor (r1) — undocumented equivalence claim.** `test_platform_result_as_validation_reference` shows ValidationReference accepted in place of BacktestResult; intentional API flexibility unnamed — one-line comment prevents someone "fixing" it.

Smells: helper triple byte-identical to test_delta_report.py (:26-95 both) — shared `tests/validation/helpers.py` (r1/r3). r1 self-query: `TradeStatistics` import at :32 possibly unused beyond `StatDelta` — verify with lint. Positive (all three): strict-JSON section (:405-439) exemplary; recursive type check (:369-388/:376-388) guards whole-payload JSON safety.

### tests/validation/test_risk_comparison.py

Well-designed isolation tests: each mismatch class verified to fail *only its own field* while others stay green; delta-report conversion incl. missing/extra.

1. **tests/validation/test_risk_comparison.py:183-199 — minor (r1/r3, convergent judgement call) — extra-is-info policy asserted without comment.** Test asserts `report.passed is True` for an unmatched extra trade while sibling missing-case (:163-179) fails the report for a *warning*. May be intended taxonomy (extras informational, missing warnings that block) mirroring anomaly-severity policy — but nothing states it, and the name couples severity to gate semantics; if extras later warn, nothing hints why it flips. Add a one-line policy comment.
2. **tests/validation/test_risk_comparison.py:96-115 — minor (r1) / positive-technique note (r3) — tolerance knobs set ad hoc per test.** Values like stop_distance_pct=0.5 chosen to isolate fields are correct but unexplained — a table-driven parametrize with expected per-field outcomes would document the decision matrix; r3 counters the isolation is deliberate and correct (each test loosens other tolerances to 1.0) — noting positively.

Smells: 6-line prologue repeated ~11 tests (r1); `t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)` line across ~10 tests could be a fixture; seventh copy of helper family (r3); Data Clumps: `(stop_distance_pct, risk_per_trade_pct, position_size_pct)` always adjusted together — named scenario tuple (r1). Summary counts cross-checked against per-trade severities (:202-229) — positive (r2).

### tests/validation/test_risk_sizing.py

Good coverage of signal conversion, fallback-stop semantics (incl. ATR meta), direction enforcement, exposure caps, aggregate risk. No bugs found — assertions specific and hand-computed (r1/r2 scan clean; r3 minor notes only).

1. **tests/validation/test_risk_sizing.py:200-207 — minor (r3) — wrong-side stop rejection asserts zeros only;** no assertion the profile records *why* (diagnostic/explanation field); if sizes silently zero for any validation failure, a bad-importer stop becomes indistinguishable from an intentional block — one observability assertion would pin it.
2. **tests/validation/test_risk_sizing.py:189-197 — judgement-call (r3)** — ATR-fallback test constructs its own RiskEngine rather than the adapter default; isolation fine, but the *default* engine's ATR wiring never exercised end-to-end. Related positive (r2): ATR fallback derivation commented (0.005×3.0=0.015 at :195); risk_amount=100 = 1% of 10k derived at :154.
3. Nits (r1): two constructor round-trip tests (:292-324) re-assert values passed in; imports production privates (`_trade_to_signal`, :18-19/:22) pinning internal names.

Smells: `RiskAdapter(pip_value=1.0)` + `_make_trade` scaffolding ~18-20 tests — fixture (r1/r3). Primitive Obsession: direction strings "LONG"/"SHORT"/"buy"/"sell" as raw literals; shared constants would catch typo drift ("Buy" vs "buy") (r1). Dead import: `math` (:13, r3). Positive: wrong-side stop rejection (:200-207), zero-balance guard with message match (:210-214), exposure/position caps zeroing size (:217-235), pip-value inverse-scaling property (:238-246), LONG/SHORT fallback-stop sides (:330-345) — exactly the safety surface, covered.

### tests/validation/test_stats_comparison.py

Compact and correct: identical-stats field-set equality, tolerance arithmetic verified numerically (:61-64), absolute-tolerance config, inf profit-factor both ways. Scanned-clean by r2.

1. **tests/validation/test_stats_comparison.py:32-49 — minor (r1) — exact field-set assertion duplicates production knowledge** (same 10-field list as test_regression.py:87-101); two tests updated in lockstep if `_compare_stats` grows; centralize the expected set in one shared constant/helper. r3 notes it's the better shape of the pair (exact set assert vs bare length) but same caveat.
2. **tests/validation/test_stats_comparison.py:12-29 — nit (r1/r3) — `_trade` derives direction from pnl sign** (`LONG if pnl >= 0 else SHORT`) — semantically meaningless for stats; harmless here but a copy-paste of this helper into a comparison-sensitive suite would encode a wrong assumption.

### tests/validation/test_trade_anomalies.py

High-quality behavioral suite: severity taxonomy per field exhaustive, duplicate detection both directions, integration paths. No bugs found (any reviewer).

1. **Dead imports — minor (r1):** `FieldComparisonResult`, `TradeComparisonConfig`, `TradeComparisonResult` imported at :14-17 never referenced in module body (only `TradeComparator`'s return value used); copy-paste residue from comparison-test files. (r2's cross-cutting list agrees.)
2. Smell: third verbatim copy of `_make_trade/_make_matched/_make_reference` triple (:24-83) after delta_report/report_structured — shared helpers module (r1/r3); Repeated Switches: `TestDetectMismatches` runs the same four-line shape nine times differing only in field/severity — parametrized `(field, ref, act, expected_severity)` collapses ~80 lines and makes a missing-field gap visible as an absent row (r3).

### tests/validation/test_trade_comparison.py

Exemplary tolerance-boundary suite: every field gets within/outside/custom-tolerance triads plus None-vs-float semantics, OR-tolerance logic documented inline (r1/r2 scan essentially clean).

1. **tests/validation/test_trade_comparison.py:281-291 — minor — copy-paste error in test name: says "mismatch", asserts a pass.** `test_zero_vs_nonzero_pnl_mismatch` ends `_assert_field_passed(result, "pnl")` and `assert result.is_match`; body comments correctly explain OR-tolerance lets |0.001| ≤ 0.01 pass — assertion right, name wrong (left over from pre-OR version or copied from :253). Anyone auditing failures by name wrongly believes zero-vs-nonzero PnL fails validation. Rename e.g. `test_zero_vs_nonzero_pnl_passes_on_abs_tolerance`.
2. **tests/validation/test_trade_comparison.py:262-272 — nit (r1) / product-awareness note (r3):** `test_pnl_outside_abs_tolerance_only` also passes despite name suggesting failure territory (comments mitigate; rename both to describe outcome). r3 adds the substantive point: OR logic means a $0.01 abs tolerance never binds when pct passes — large-PnL trades can drift arbitrarily in absolute terms; tests encode current behavior correctly but PnL equality is usually a hard requirement in validation contexts — flagged for product awareness.
3. **tests/validation/test_trade_comparison.py:83 — nit (r1/r3) — hardcoded field count `len(result.field_results) == 10`** duplicates production knowledge; adding an eleventh compared field breaks with unhelpful message; assert the exact key set (doubles as documentation).

Smells: dead `math` import (:5, r1); fourth copy of `_make_trade/_make_matched` (:20-65, r3); per-field classes repeat within/outside pairs — parametrize `(field, ref, act, passes)` (r3). Positive: boundary discipline (values just inside/outside tolerances :116/:123, :165/:173) with derivations in comments.

### tests/validation/test_trade_matcher.py

Thorough matcher coverage: temporal tolerances (boundary pairs), symbol gating, direction mismatch → missing+extra semantics, empty-input matrix, duplicate-timestamp pairing order, tz normalization incl. naive-as-UTC, MatchKey UTC canonicalization. Scanned-clean by r2 ("Excellent boundary coverage").

1. **tests/validation/test_trade_matcher.py:198-234 — minor (r1) / positive-with-caveat (r3) — input-order pairing asserted as behavior without stating it's a guarantee.** Positional pairing for duplicate keys documented via docstring (good), but the ambiguous case is uncovered: duplicate keys where the wrong pair produces different downstream comparisons (ref [A,B], act [B,A], same timestamps, different prices) — precisely where order-pairing masks real mismatches; add a case asserting the deliberate trade-off (r1). r3's caveat: if the matcher switches to greedy best-effort matching under tolerance, expectations flip silently — acceptable coupling, noted.
2. **tests/validation/test_trade_matcher.py:103-116 — nit (r1) — boundary tested at ±5s exactly but not ±4/±6** to pin inclusive-vs-exclusive; boundary off-by-one is the classic matcher bug. (r2/r3 treat the exact-boundary discipline at :102-133 positively.)
3. **tests/validation/test_trade_matcher.py:247-251 — minor (r3) — naive-timestamp-as-UTC asserted only via match success,** contrasted with :271-276 asserting the canonical "Z" form — consistency gap only.
4. Smell: eighth copy of `_make_trade` family (:11-31); r2/r3 query a possibly-unused `pytest` import (:5).

### tests/validation/test_trade_statistics.py

Excellent numeric suite: hand-computed expectations with shown arithmetic, edge cases inf/zero/single-trade. Called the strongest file reviewed by r3 and "model validation-grade" by r2.

1. **tests/validation/test_trade_statistics.py:243-257 — minor — Sharpe expectation re-implements the production formula rather than an independent value (r1) / population-variance convention implicit (r2/r3).** `expected = (20.0/math.sqrt(2680.0))*math.sqrt(252.0)` mirrors the calculator's expression — a shared mistake (annualization factor, population-vs-sample std) cancels out and still passes; the ≈6.133 sanity value mitigates (r1). r2/r3 sharpen: comment computes variance ÷N (population) — if production switched to n−1 the test fails ~11%, fine as pin, but the comment should say "population std" explicitly since annualized-Sharpe conventions differ. Prefer at least one literal precomputed constant. Same pattern Sortino :275-289, whose downside variance divides by number-of-downside-points (coexisting convention by design — comments are the only spec, r2).
2. **tests/validation/test_trade_statistics.py:185-195 — minor — drawdown-pct denominator convention only implicit.** 0.7 = 70/100 works only because peak is the first trade; higher-peak case covered at :206-219 (good), but peak-equity-vs-initial-capital convention should be pinned in one more asymmetric case to prevent silent convention change (r1); r3 concurs — one clarifying line prevents false "bug" reports.
3. **tests/validation/test_trade_statistics.py:198-203 — nit (r1) — name says "never positive equity"** while scenario is "always losing from zero start"; pct==0 documents a /0-guard; comment-reference production behavior.

Smell: 5-trade `[100,-50,30,-20,40]` fixture duplicated between Sharpe (:248-254) and Sortino (:280-286) — extract once (r1). Everything else verified arithmetically by r3 (DD sequences :170-173/:206-210, downside variance :278, recovery factor :303, break-even exclusion :403-413).

### tests/validation/test_validation_reference.py

Pydantic model contract: allowed enums, empty-trades gating, `to_backtest_result()` mapping fidelity, raw-metadata round-trip.

1. **tests/validation/test_validation_reference.py:79 — minor (r2) — `pytest.raises(ValueError)` without `match=`** on empty-trades rejection (same pattern :65, :73): any unrelated ValueError from pydantic coercion satisfies the assertion; low risk given tight construction, but message-scoped matches prevent false positives. r1's related note (:60-66): the allow-list acceptance loop verifies construction succeeds but can't catch a framework typo'd *into* the allowlist — near-tautological by construction; the rejection case carries the meaning.
2. **tests/validation/test_validation_reference.py:121-125 — minor (r1) — notes assertion checks substring containment** ("mt5_xlsx" + path in notes); formatting changes dilute/break — acceptable. r3: included to note the file is clean otherwise.

Smell: `_make_minimal_valid_reference` overlaps heavily with test_cli.py's `_make_reference` — consolidate (r1). Empty-trades gating both ways (:77-85), roundtrip preservation, engine mapping pinned concretely (r3).

---

## tests/validators/

Production-style helper code under `tests/validators/` — no pytest modules (no test functions); reviewed as code by all three reviewers. Both validators are load-bearing: `AICodeValidator` imported by tests/integration/test_ai_code_quality.py, tests/integration/test_strategy_pipeline.py (per r1 grep), tests/e2e/lab/test_conversion.py; `BacktestValidator` consumed by tests/integration/test_strategy_pipeline.py (r3). Neither has dedicated unit tests inside chunk C — both reviewers independently recommend a direct table-driven suite including bypass/tz-crash cases.

### tests/validators/code_validator.py

1. **tests/validators/code_validator.py:20-22,:48-60 — MAJOR (security) — denylist trivially bypassable while `passed=True` reads as a safety verdict; threat model undocumented (all three, convergent).**
   - Forbidden set = bare-name `eval`/`exec` AST calls plus attribute-call `os.system`. Documented bypasses sailing through: `getattr(builtins,'eval')(...)`, `builtins.eval(...)`, `__import__('os').system(...)`, aliasing (`e = eval`), `os.popen`, nested attribute chains (value is Attribute not Name), decorator tricks; `subprocess`/`pty`/`socket`/`importlib` imports not scanned at all (r1/r2/r3 enumerate overlapping sets).
   - As a lint heuristic this is fine; if any caller treats `passed=True` as "safe to import" it is false assurance (r1 goes as far as calling a critical false-assurance bug *conditional on downstream consumption*; all three settle on major given no evidence of such a consumer in-scope).
   - Compensating control, partially: subprocess import-time check (:85-133/:116-122) catches module-level side effects — but that check *executes* the candidate in a subprocess whose isolation is `-I` + timeout=8 + temp-dir cleanup + `check=False` — availability hygiene, **not containment**: filesystem/network access intact, full user privileges (r1 security finding; r2 calls executing untrusted code by design "the real exposure" and the most significant security-relevant item in the area; r3 concurs).
   - Remedies (consensus): document "best-effort heuristic" loudly, extend forbidden sets (`subprocess`, `os.popen`, `__import__`, `importlib`), or switch to an allowlist AST approach with capability isolation at execution time.
2. **tests/validators/code_validator.py:135-137 — minor — score/passed divergence undocumented (r1/r2) and warnings don't affect `passed` (r1).** A module with 20 warnings scores 0.0 yet `passed=True`; conversely score 0.8 with `passed=False` possible — interplay undocumented for consumers; formula `max(0, min(1, …))` duplicated with backtest_validator scoring (extract one scorer, r1 nit / r3).
3. **tests/validators/code_validator.py:97-122 — minor — stderr of a successful run becomes only a warning (:132-133)** so a strategy emitting tracebacks on import still passes with −0.05 (r1); 8-second timeout short for cold CI interpreter starts — slow pandas import flakily reports "Subprocess import timed out" looking like strategy defects (r2). Checklist compliance noted positively by r1/r2/r3: timeout present, isolated mode, temp cleanup, stderr surfaced not swallowed.
4. **tests/validators/code_validator.py:82-83 & :102-113 — minor (r3) — interface check can false-pass uninstantiable classes:** `callable(getattr(cls, name))` passes for any unbound method; a class whose `__init__` requires args satisfies the contract while unusable via the documented module-level interface — exactly what a validator exists to catch. Related noise heuristic (r2): "Mixed interface style" warning fires whenever module functions coexist with *any* class, relevant or not.
5. **Placement (minor, r1):** production-grade validation logic lives in a non-package directory with no `__init__.py`, imported via path hacks; belongs in the main package where lint/type coverage applies; living under `tests/` risks the context ("only feed first-party AI output") being forgotten.
6. Nit (r1): errors/warnings as raw strings — codes would enable downstream filtering.

No injection risk in `check_script` itself: module path passed as argv, not interpolated (r1, positive).

### tests/validators/backtest_validator.py

1. **tests/validators/backtest_validator.py:68-73,:102-113 — severity split (r3 major / r1 minor) — mixed naive/aware timestamps crash validation with TypeError.** `_parse_time` returns naive datetimes for strings lacking tz and aware ones for `Z`/offset strings; a single trade mixing formats makes the `<` comparison raise uncaught TypeError, aborting `validate()` wholesale instead of appending an error for that trade — the validator dies on precisely the malformed input it exists to catch. Normalize tz before comparing (mirror `_to_utc_datetime` patterns used elsewhere in the suite). Related (r3 minor): unparseable timestamps silently `continue` — dropped from ordering validation without even a warning; garbled importer timestamps invisible.
2. **tests/validators/backtest_validator.py:46,:86-92 — major (r1/r2) — `total_trades` fallback silently coerces bad values to 0 with two conflated sources.**
   - r1 (**major**): non-numeric `stats["total_trades"]` (`None`, `"12abc"`) → `_as_int` returns 0 → misleading "Trade count below expectation: 0 < N" instead of "invalid total_trades"; default branch counts `len(result["trades"])` while stats may report a different canonical count — two sources conflated without documented precedence.
   - r2 (**major**) adds eager-default semantics: `stats.get("total_trades", result.get("trades", []))` evaluates the default every call (harmless) but if the stat is missing AND trades list absent, `_as_int([])` → 0 degrades to "0 < min_trades" instead of the correct "Missing or invalid 'trades' list" raised at :75 — structurally invalid results report inconsistently across two code paths. With default `min_trades=0` (:21) garbage trade counts pass unchallenged (r2 minor framing of the same `_as_int` point).
   **[SETTLED by verification]** Code read confirms `_as_int` swallows TypeError/ValueError → return 0 (:85-92) and both r1's coercion path and r2's missing-stat path are real; one nuance: the "Missing or invalid 'trades' list" error at :74-75 fires *independently* of the total_trades fallback, so such results still get an error — r2's "instead of" framing is overstated, though the inconsistent-message point stands. Sharper verified fact supporting **major**: a *string* `"NaN"` sharpe_ratio parses via `_as_float` to float NaN, then fails both comparisons (`nan < min` False, `nan > max` False) producing zero errors/warnings — non-numeric stat values genuinely bypass validation entirely, exactly the reference report's T3-3 claim. Final severity **major** (r1/r2 + reference weight; r1's own minor rating on the adjacent numeric-scan gap is thereby superseded for the sharpe/total_trades paths).*
3. **tests/validators/backtest_validator.py:40-44 — minor (r1) — NaN/Inf scan covers only int/float.** String `"NaN"` or `Decimal('NaN')` stat values escape the finiteness check; the JSON round-trip hazard re-enters via stringly-typed stats. (Positive, r3: bool-safe NaN/Inf rejection at :40-44 is a genuine data-integrity guard.)
4. **tests/validators/backtest_validator.py:52-61 — positive (r3) with minor nit — asymmetric sharpe handling sound** (below-range = error; suspiciously-above-range = warning citing lookahead bias); non-finite sharpe collapses into "Missing or invalid" (:53-54), conflating NaN with absence.
5. **Scoring divergence — minor (r2):** score computed independently of `passed` (`len(errors) == 0`) — divergence undocumented; informational. Formula duplicated with code_validator (shared scorer candidate, r1 nit).

Smells: Shotgun Surgery — `required_stats_keys` defaults hardcode runner.stats key names also asserted in tests/test_stats.py, two places per schema change (r1); plain-dict return while sibling returns typed ValidationResult (inconsistent contracts, r1); `_parse_time` duplicates the `replace("Z","+00:00")` normalization found ≥3 places repo-wide (r3). Security: none beyond the robustness crash — no eval/exec/subprocess, pure data inspection (r1/r2/r3).

---

## Reference-report cross-check (gpt-5.6-luna 2026-08-23, §9 "tests — chunk 3")

The reference report's chunk-3 section covers the 27-file subset overlapping this chunk (`tests/validation/**`, `tests/validators/**`, `tests/test_yfinance_downloader.py`); its treatment of the remaining root-level files sits in other chunks of that report and was not consulted, per scope rules. Disagreements and agreements against the panel:

| Ref ID | Claim | Panel verdict |
|---|---|---|
| T3-1 | `code_validator.py:97-121` executes candidate code without sandbox — **major** | **Agree** (unanimous panel major; panel adds the denylist-bypass dimension ref understates) |
| T3-2 | `backtest_validator.py:68-72` malformed timestamps silently pass — **major** | **Agree on severity, mechanism sharpened**: verified the mixed-naivety case crashes with TypeError rather than passing silently; the silent-skip half (unparseable → `continue`) is r3's minor and also real |
| T3-3 | `backtest_validator.py:40-45` non-numeric cagr/drawdown/win-rate/PnL bypass finite checks — **major** | **Agree — ref wins over r1's minor**: verification found the concrete bypass (string `"NaN"` sharpe → float NaN → both range comparisons False → zero errors) |
| T3-4 | `test_cross_platform_metrics.py` depends on regeneration scripts + external artifacts — **major** | **Agree** (unanimous panel major at the same lines) |
| t3-13 | QC importer import-time reference-dir capture — **minor** | **Disagree — panel major stands** (verified structural import-time freeze affecting all 8 golden tests) |
| t3-14 | QC brittle golden snapshot — **minor** | **Panel rates major** (r2; ~40 assertions invalidated by any legitimate re-export, no regeneration story) — unresolved 1-vs-1 among raters, kept high |
| t3-1..t3-7, t3-9..t3-12 | trade_statistics aggregate-vs-exit PnL gap; trade_comparison OR-tolerance false positives near zero; matcher reordered-duplicate + naive-as-UTC gaps; paper_readiness disabled-checks-still-READY; markdown weak/duplicated assertions; CLI subprocess timeout; yfinance forwarding/tz/ordering/empty gaps; MT5 direction semantics + suite duplication | **All consistent with panel findings** at matching locations — independently corroborating, no corrections needed |
| t3-8 | `test_cli.py:144-190` generated workbook never closed | **Reference-only claim — verified, downgraded**: literally true (no `wb.close()` before save) but openpyxl holds no OS handles until `save`; adopted as a nit, not elevated to panel findings |

Notable blind-spot asymmetry: the reference has **no counterpart** for the panel's highest-severity item (trade_engine parity test, critical), nor for the viewer stack-walk doubles, walkforward mock-only coverage, delta_report dead helper, or paper_readiness double-construction — the panel materially exceeds the reference on those files.

---

## Coverage statement

**Coverage: 40/40 tracked files in chunk C accounted for** — every file below has findings recorded above or an explicit scanned-clean record:

- Root-level (15): test_signal_import, test_signal_processor_registry, test_signal_replay_adapter, test_stats, test_strategy_ir_builder, test_strategy_ir_codegen_vectorbt, test_strategy_ir_normalizer, test_strategy_ir_routing, test_strategy_meta_adapters, test_strategy_type_detection, test_timeframe_normalization, test_trade_engine, test_viewer_features, test_walkforward, test_yfinance_downloader
- tests/validation (23): __init__ (empty marker), test_cli, test_cross_platform_metrics, test_delta_report, test_fixed_risk_model, test_golden_files, test_importers, test_markdown_report, test_mt5_html_importer, test_mt5_xlsx_importer, test_paper_readiness, test_percent_risk_model, test_quantconnect_result_importer, test_regression, test_report_structured, test_risk_comparison, test_risk_sizing, test_stats_comparison, test_trade_anomalies, test_trade_comparison, test_trade_matcher, test_trade_statistics, test_validation_reference
- tests/validators (2): code_validator, backtest_validator

**Merge/verification accounting:** 142 deduplicated findings merged from the three blind reviews (agreeing findings deduplicated with all original citations preserved). Verification budget used: 8 of 8 permitted spot-checks — (1) trade_engine parity citations/severity, (2) yfinance mock call-contract vs production + installed library signature, (3) delta_report dead-helper/unimported-Any, (4) QC importer import-time env capture, (5) paper_readiness double-construction readings, (6) backtest_validator numeric-stat bypass paths, (7) reference-only CLI workbook-cleanup claim, (8) meta_adapters fill-price derivation behind the 142.8571 oracle. Settlements are logged inline as **[SETTLED…]** notes; two severity splits were settled without code reads on report-internal evidence (percent-risk TP naming corroborated by the fixed-risk sibling; QC goldens carried by r2's regeneration-story analysis) and are marked as such. Counting conventions: numbered findings only (aggregated smell paragraphs excluded); nits counted as minor; pure cross-reference items not double-counted; verified splits take the settled severity, unverified splits take the reviewer majority, true ties take the higher rating (viewer split-plan overlap, QC brittle-goldens).

## Finding counts by severity (merged)

- **critical: 1**
- **major: 15**
- **minor: 126**
- **total: 142**



---

## data-validation

**Panel:** model ox-alpha; three blind, independent reviewers (r1, r2, r3). Synthesized by ox-alpha directly from the three partial reports after repeated sub-agent spawn failures; all reviewer-vs-reviewer disputes, all critical findings, and the adopted reference-only claims were settled by reading the cited source lines directly (log in §F).
**Reference report** (`code-review-full-gpt-5.6-luna-2026-08-23.md` §5 "data & validation", D1–D20/d1–d12) consulted as hints only; agreements and disagreements logged in §F.
**Scope:** 50 tracked files (`data_tools/**`, `validation/**`). Every file accounted for in §G.

**Merged totals: 🔴 Critical 2 · 🟠 Major 51 · 🟡 Minor 118**

---

### A. Critical

**DV-C1 · Merged bid/ask tick download is broken twice over: positional args misaligned + duplicate keyword → guaranteed `TypeError`.**
Call site `dukascopy_native.py:1016-1028` passes `(instrument, start_dt, end_dt, batch_size, pause_ms, max_retries, pipeline_batches=pipeline_batches, …)` into the signature `(instrument, start_dt, end_dt, batch_size, pipeline_batches, pause_ms, max_retries, *, …)` (`:774-786`): `pause_ms` lands in `pipeline_batches`, `max_retries` in `pause_ms`, and `pipeline_batches` is then passed again as a keyword → `TypeError: multiple values for argument 'pipeline_batches'` before any work. *(r3 R3-001 critical; r1 found the same misalignment at `:1016-1028` rated major; Luna D1 critical — severity settled critical: the whole `--merge-tick`/`--merge-s1`/`offer_side=both` path cannot execute and no test covers it.)*

**DV-C2 · "Ask" columns are bid data: both sides fetch identical URLs.**
In the same function, `bid_urls = generate_tick_urls(instrument, start_dt, end_dt)` and `ask_urls = generate_tick_urls(...)` are the *same call* (`dukascopy_native.py:790-791`) — tick URLs carry no side component, so the ask leg re-downloads bid files and the inner merge (`:839-842`) produces ask columns equal to bid. Silent data corruption for every merged-tick dataset. Currently masked only by DV-C1's TypeError firing first. *(r1 critical, verified against source during synthesis; r2/r3 missed it.)*

---

### B. Major — data_tools/

**Downloader core (`base.py`)**
- **DV-B1 · Non-atomic read-modify-write of canonical parquets — single point of failure for every downloader.** `_merge_and_store_single_file` (`base.py:151-184`) reads existing → concat → dedupe → in-place `to_parquet`; a crash mid-write leaves a corrupt parquet at the canonical path that resume logic then trusts, and concurrent downloads (grid `--workers 6`) interleave and lose rows. *(r2 major, r3 major, r1 minor — settled major: every downloader funnels through this method.)* Related non-atomic writers: `download_grid_market_data.normalize_ohlcv_columns` (`:96-126`), `_write_source_meta_index` (`dukascopy_native.py:869-884`, unlocked + non-atomic, corrupt JSON silently degrades catalog loads to `{}`). *(r1/r3)*
- **DV-B2 · Resume point trusts row-group max statistics.** `_read_latest_timestamp` scans backwards assuming timestamp-ordered row groups (`base.py:207-214`, fallback `:215-223`); externally produced/unsorted parquet yields a mid-history resume that silently skips ranges. *(r1 major, r2 minor, r3 R3-021 minor — settled major on silent-wrong-range impact.)*

**Dukascopy native** (besides DV-C1/C2)
- **DV-B3 · Zero-volume candles deleted.** `_assemble_candle_dataframe` filters `df["volume"] > 0` (`dukascopy_native.py:636`) — genuinely flat FX minutes become gaps propagating into resampled h1/d1 OHLC windows. *(r1)*
- **DV-B4 · Full ISO start/end datetimes crash the native path.** `datetime.fromisoformat(x).replace(tzinfo=utc)` (`:983-984`) raises on already-aware inputs like `"2024-01-01T13:45:00+00:00"`; only date-only strings survive. *(r1)*

**JForex**
- **DV-B5 · First failing JVM candidate aborts startup.** `_ensure_jvm_started` re-raises inside the candidate loop (`jforex_dl.py:551-552`), skipping remaining candidates and the JPype-default fallback the `startup_errors` collection shows was intended. *(all three reviewers)*
- **DV-B6 · Remote JARs loaded into the JVM with zero integrity checking.** `_download_jforex_jars_from_jnlp` (`:280-311`) fetches jar/nativelib hrefs from the env-overridable `DUKASCOPY_JNLP_URL` and puts them on the classpath (`startJVM :549`) — no checksum/signature/host allow-list — while the module meticulously trust-checks the much smaller libjvm surface (`:356-437`). Supply-chain RCE if env/DNS is hostile. *(all three; Luna D4)*
- **DV-B7 · Jar-bootstrap failures swallowed into a generic warning.** `except Exception: logger.warning(...); return [], f"unavailable"` (`:352-356`) collapses download/parse/security failures into a later generic startup error. *(Luna D5 only — verified against source and adopted.)*
- **DV-B8 · Partial artifacts persisted before a failing merge.** With `offer_side="both"`, side files are written (`:1146-1147`) before the inner join (`:1148`); a zero-overlap merge raises with two fresh side files on disk poisoning `_resolve_resume_start`. *(r2)*
- **DV-B9 · `DownloadResult.path` is a directory for merged downloads.** `output_path.parent` returned (`:1189`) violates the base contract ("where the parquet was saved"); consumers calling `.stat()`/open break. *(r1 major, r2 minor — settled major: contract break on a public result type.)*
- **DV-B10 · Timeframe-suffix string surgery mangles filenames.** Chained `.replace()` (`_normalize_timeframe_for_file_suffix :967-970`) turns `"15m"` into `"1m5"` (later `"15m"→"m15"` never matches); output files like `EURUSD_1m5_bid.parquet` are unparseable by the catalog timeframe regex and vanish/misclassify. *(r3 R3-012; r2 flagged the pattern as fragile)*

**Catalog / cache**
- **DV-B11 · Silent `"1d"` default poisons the catalog.** `parse_ohlcv_path` defaults unparseable stems to daily (`catalog_builder.py:251,264,274`, mostly unlogged); a mislabeled dataset is worse than a skipped one downstream. *(r2 major; r3 R3-027 minor — settled major on research-data impact.)*
- **DV-B12 · Global cache lock held across the entire cold build.** `get_catalog` runs `build_catalog`'s minutes-long filesystem scan inside `_CACHE_LOCK` (`cache_manager.py:240-255`), blocking every server cache reader/writer. *(all three)*
- **DV-B13 · `unsafe_no_copy=True` hands out live shared mutable state.** `get_catalog_ohlcv_entries` (`:271-279`) — one caller mutation poisons the cache for all future readers. *(all three)*
- **DV-B14 · Prefix-match storage classification.** `_detect_storage` uses `startswith` (`catalog_builder.py:97-102`) — sibling dirs like `cache_backup` classify as cached; same pattern in `validate.py:94`. *(r1, r2, r3)*

**Auto-download / coverage gating**
- **DV-B15 · Coverage satisfied by min/max alone — multi-year holes pass.** `_has_required_coverage` compares only first/last available dates (`auto_download.py:247-262`); two bars bracketing a gap certify the range, so backtests run hole-filled with no download. *(r2; corroborated by r3's gate-integrity theme.)*
- **DV-B16 · Failed month/year downloads are silently skipped, then look covered.** HuggingFace per-month `except Exception: warnings.append(continue)` (`huggingface_dl.py:257-261`) and HistData per-year None returns (`histdata_dl.py:224-226`, holes never back-filled since resume keys off last timestamp) produce gappy datasets that DV-B15's check then certifies. *(r2 major; r1 corroborated.)*
- **DV-B17 · Coverage checks read entire timestamp columns, repeatedly.** `pd.read_parquet(columns=["timestamp"])` full scans per file per requirement, twice per run (`auto_download.py:138, 275-277`) — minutes/GB on tick stores while the catalog already persists min/max metadata. *(r2 major; r1 minor — settled major.)*
- **DV-B18 · One failed plan step aborts the remaining fallback sources.** Exceptions propagate out of `_download_missing_ohlcv_data` steps instead of continue-on-failure (`auto_download.py:103-108, 396-488`), so HF failing prevents the Alpaca fallback that was planned. Also: end-only requirements collapse to a single-day HF request (`:377-381`). *(r1)*
- **DV-B19 · HuggingFace pulls whole-market monthly shards per symbol with no pushdown/cache.** `pd.read_parquet(url)` then client-side symbol filter (`huggingface_dl.py:253-270`); a long history means hundreds of large payloads per ticker plus rate-limit mid-loop failures leaving partial merges that look complete. *(r1, r2; Luna D7 overlaps)*

**yfinance / ctrader / histdata specifics**
- **DV-B20 · yfinance `4h` bars use UTC calendar buckets, misaligned vs FX sources.** 60m fetch resampled on midnight-UTC origin (`yfinance_dl.py:53, 253-269`) produces 09:30/13:30-style splits that never match dukascopy/jforex session-aligned `4h` files in comparisons. *(r2 major; Luna D13; r1 alignment note.)*
- **DV-B21 · Intraday clip silently truncates requested history and defeats coverage loops.** 60-day Yahoo window discards older resume points with only an in-result warning that `auto_download` drops on the floor (`yfinance_dl.py:84-112, 169-183`) — ranges >60 days back re-download the same window forever. *(r1)*
- **DV-B22 · Tick CSVs stored under `1m.parquet`.** `ctrader_dl.py:156-157` resamples only ≠1m, so raw ticks imported with `--timeframe 1m` keep the name while bar-based consumers trust it; metals (XAUUSD/XAGUSD) also land under asset_class `forex` (`:143`). *(r2 major; r3 minor — settled major on mislabel impact.)*

**Resample / validate**
- **DV-B23 · Resample overwrites without merging — stored history can regress.** `resample_file` writes `{tf}.parquet` beside the source (`resample.py:104-106`); a re-run after the 1m source gained new history replaces higher-TF files with only the resampled window, and grid refresh paths hit exactly this (`download_grid_market_data.py:225-236`). *(r1 major, r3 history-loss risk.)*
- **DV-B24 · Gap detector uses pandas labels positionally after `dropna()`.** `large_gaps.index[:20]` labels fed to `ts.iloc[idx±…]` (`validate.py:162-186`) report wrong gap boundaries whenever any timestamp was coerced/dropped. *(all three; Luna D8)*
- **DV-B25 · `--data-dir` mode crashes under its own flag.** `path.relative_to(DATA_DIR/"ohlcv")` guarded by a `startswith` on a different root (`validate.py:94`) raises `ValueError … not in the subpath` for custom roots, killing `validate_all`. *(r3)*

**Signal import / dolthub**
- **DV-B26 · Unknown signal actions default to `"buy"`.** `_parse_action` (`signal_import.py:67-77`) maps typos, "Exit", "Close", empty cells to long entries — replay fabricates trades with no error. *(all three)*
- **DV-B27 · SQL built by f-string in both dolthub modules.** `dolthub_manager.py:138` interpolates `symbol`/table names into `dolt sql -q`; `dolthub_dl.py:94-102` interpolates symbol/table/branch/dates into the API query. Checklist item violated verbatim; exploitability bounded (repo-derived/internal input) but quote-bearing symbols break queries and the pattern normalizes injection-by-habit. *(r1 critical×2, r2 major/minor, r3 major×2, Luna D2/D3 major — settled major per bounded reachability; r1's criticals noted as dissent.)*
- **DV-B28 · Every `dolt` subprocess runs without timeout.** `subprocess.check_call/run` at `dolthub_manager.py:32, 60-65, 77, 111-116, 133-144` — a hung clone/pull blocks forever; scheduler-invocable. *(r2 critical, r1 major, r3 major — settled major-high: availability failure, visible not silent; r2's critical noted as dissent.)*
- **DV-B29 · DoltHub API pagination ignored — silent truncation stored as complete.** Single `rows` read with no cursor (`dolthub_dl.py:145-154`); capped responses merge into canonical store as if complete and resume then believes coverage exists. *(r2, r3)*

**Grid tooling**
- **DV-B30 · Alias freshness = equal file size.** `ensure_alias_copy` skips copies when sizes match (`download_grid_market_data.py:155-161`) — regenerated canonical data never propagates to the alias backtests actually read. *(r1, r2; r3 minor — settled major.)*
- **DV-B31 · Grid fan-out races and rate-limit storms.** Duplicate aliases canonicalize to the same target and run concurrently in the process pool racing one parquet write (`:280-311`, no dedupe); combined concurrency `workers × batch_size × pipeline_batches` ≈ 240 simultaneous requests with no cross-process throttle. *(r2, r1; Luna d6 overlap)*

**Discovery**
- **DV-B32 · Genesis discovery corrupted by Sunday/holiday probes and transport failures-as-misses.** Probe helpers dodge only Saturday (`dukascopy_discovery.py:150-159`); broad `except Exception → False` (`:145-147`) records network errors as permanent "misses" saved into the genesis index by monotonic binary search. Discovered genesis shifts by days, permanently. *(r1, r2)*

---

### B (cont.) Major — validation/

**Reporting / gates**
- **DV-B33 · Undefined name in public signatures.** `TradeComparisonConfig` annotated at `json_report.py:297,360,561` but never imported (imports `:8-27` lack it). Latent-only today thanks to `from __future__ import annotations`; any runtime annotation consumer raises `NameError`. *(r1 critical, r2 major — settled major: no reachable runtime failure today; r1's critical noted as dissent. Verified by direct read.)*
- **DV-B34 · `-inf` asymmetry produces false mismatches.** Infinity handling covers only `+inf` (`json_report.py:137-163`; twin in `regression/runner.py:90-110`): two identically `-inf` stats (e.g. profit_factor with zero losses) compare as inf-diff and fail. *(r1 major, r3 minor — settled major: validation gate integrity.)*
- **DV-B35 · OR-tolerance pass logic weakens every numeric gate.** `passed = passed or (abs_ok or rel_ok)` (`json_report.py:180-183`; same OR shape in `comparison.py:280-282` for PnL) lets fields pass whichever tolerance is looser — inconsistent thresholds across scale-free fields like win_rate. *(r1 major; r2/Luna d8 minor — settled major.)*
- **DV-B36 · Required-artifact failures double-counted.** `_check_required_artifacts` emits `required_artifact_*` errors AND the main body appends parallel `required_<artifact>` errors for the same facts (`readiness/validator.py:75-106`): doubled error counts, drift-prone duplication. *(all three)*
- **DV-B37 · Risk gate can pass on structurally failed comparisons.** Failed risk results with non-numeric fields contribute neither issue nor check failure (`validator.py:463-492`), empty `trade_results` passes vacuously via `max(default=0.0)` (`:494-495`), and report-level verdicts are never consulted. *(r1 major; r2 minors — settled major.)*
- **DV-B38 · Extra platform trades pass the risk comparison.** Unmatched actuals constructed `passed=True, severity="info"` (`risk/comparison.py:144-159`) feed `all(r.passed …)` (`:338`) — a strategy inventing trades certifies as risk-consistent; meanwhile `delta_report.build` fails the whole report on *any* extra (`:270-275`) and anomalies rates EXTRA_TRADE "info" (`anomalies.py:146`): three modules, three opinions on one event. *(r1, r2; gate-inconsistency folded here.)*
- **DV-B39 · Profile-computation exceptions swallowed.** `_compute_profile` returns None on anything (`risk/comparison.py:191-192`), degrading sizing bugs into generic warnings. *(all three; Luna D15)*
- **DV-B40 · Markdown CLI runs the entire pipeline twice.** Pass/fail derived by re-running `generate_json_report` after `generate_markdown_report` already computed it (`cli.py:237-250`) — double wall-clock plus divergence risk. *(all three)*

**MT5 importers**
- **DV-B41 · Entry-side commissions dropped — net PnL overstated.** Both importers read Kommission/Swap from the exit row only (`mt5_xlsx.py:509-511`, `mt5_html.py:476-477`); MT5 books entry commission on the entry deal. *(r1, r2)*
- **DV-B42 · Direction inferred from an unrequired column — everything becomes SHORT.** `direction = LONG if typ == "buy" else SHORT` (`mt5_xlsx.py:506-507`; html `:473-474`): missing/locale-variant `Typ` silently relabels every trade; mismatches then flood reports obscuring the real fault. *(r3 major; r1/r2 partial.)*
- **DV-B43 · Pairing loses split fills.** xlsx walks positionally attaching only the first exit and abandoning entries on a second "in" (`mt5_xlsx.py:459-560`); html groups by order id keeping last-in/last-out (`mt5_html.py:436-451`) — the two importers disagree on the same report, and incomplete pairs vanish without log (`html :461-462`). *(r1, r2 majors; Luna D20)*
- **DV-B44 · German-formatted SL/TP parsing corrupts or aborts.** `float(sl.replace(",", "."))` (`mt5_html.py:398-399`): `"2.500"` silently becomes `2.5`; `"1.234,56"` becomes `"1.234.56"` and raises ValueError aborting the import (r3 reported pure silent corruption; r1 reported pure abort — both mechanisms real, settled as stated here after arithmetic check). *(r3 major, r1 major.)*
- **DV-B45 · `total_return` derivation unreachable.** Gates on `"initial_capital" in stats` but that key is only ever written to meta (`mt5_xlsx.py:324-327` vs `:217-219`). *(r1, r2, r3)*

**Stats / regression**
- **DV-B46 · Validation statistics computed on gross PnL — fees ignored.** `_realized_pnl` sums exit PnLs (`stats/calculator.py:52-56`); Sharpe/Sortino/drawdown systematically biased whenever fees matter. Breakeven-trade denominator divergence from MT5 adds silent skew (`:118-120`). *(r1 major; r3 scanned this file clean — omission noted.)*
- **DV-B47 · Regression matching ignores configured time tolerances.** `TradeMatcher.match(...)` called bare (`regression/runner.py:315`) while config carries entry/exit seconds — exact-second equality decides matches regardless of settings; combined with DV-B38's strictness, regression and readiness disagree on identical artifacts (`:336-341`). *(r1, r2)*

**Risk sizing**
- **DV-B48 · Sub-minimum sizes floored to zero, silently.** Lot rounding floors to 0 and `< min_position_size → 0.0` (`fixed_risk.py:85-93`, `percent_risk.py:97-103`): undersized trades produce all-zero profiles that trivially "match", and min-size semantics are inverted. *(r1, r2 majors; r3 footgun.)*
- **DV-B49 · Dual sizing paths disagree on missing stops.** `calculate_trade_risk` delegates to RiskEngine which *fabricates* a fallback stop, while `size()` raises (`risk/adapter.py:112-170` vs `:172-202`) — the validation framework both forbids and invents missing evidence depending on entry point. *(r2; r1 temporal-coupling note.)*
- **DV-B50 · Hardcoded `$100` / `1%` defaults inside a validation tool.** `size()` invents `FixedRiskConfig(risk_amount=100.0)` or `PercentRiskConfig(risk_percent=0.01)` when config omitted (`adapter.py:172-202`) — silent assumptions in the tool whose purpose is exposing assumptions. *(r1, r2.)*

**Cross-platform matrix**
- **DV-B51 · Zombie statistics module with case-paths as code.** `statistics/cross_platform.py` still exists while `stats/calculator.py:4` declares the package removed; three MT5 cases + one QC row hardcoded (`:68-98`, Shotgun Surgery), and a missing MT5 xlsx crashes the whole matrix build instead of recording `missing_artifacts` (`:114`). *(r3, r2, r1.)*

---

### C. Minor findings (selected; full detail in the three partials)

**data_tools/**
- `dukascopy_native.py` — end `00:00:00` silently rewritten to 23:59:59 (`:985-986`, r3); tick vs minute URL generators treat range ends differently (`:245-276`, r3); retry tuple misses aiohttp payload errors so one aborts whole batches (`:400`, r3); weekend HTML errors treated as no-data without retry (`:385-397`, r1/r2); trailing partial tick records dropped silently (`:559,582`, r2); inner join drops bid rows lacking ask counterparts at 1 ms offsets without warning (`:839-843`, r2); keyword-only params silently swallow duplicate positional args (`:1036-1041` et al., r2 — fragile plumbing); `_fetch_one` returns success-shaped result losing cooldown signal on timeout exhaustion (`:406`, r2).
- `jforex_dl.py` — interrupted jar download leaves truncated jar passing the size check next run (no temp+rename, `:293-299`, r1); date-only end gets −1 ms but datetime end excludes last day — boundary inconsistency (`:171-173`, r1/r2); float-ms truncation can shave final boundary (`:1030-1031`, r2); configured-but-untrusted JVM override vanishes silently (`:444-449`, r2); connect poll can spin past deadline (`:858-864`, r2); hardcoded `"2003-01-01"` default start disagrees with genesis JSON (`:1095`, r3); offer_side='both' s1 rejection fires only after auth/JVM startup (`:1105-1109`, r3).
- `base.py` — corrupt timestamps coerced+dropped without warning (`:233-239`, r1); parquet reader never closed (`:192`, all); `DownloadResult.start/end` as ISO strings (Primitive Obsession, r1).
- `catalog_builder.py` — import-time `logging.basicConfig` hijacks app logging (`:26`, all three; same in `validate.py:29`, `resample.py:22`, `dolthub_manager.py:12-15`); non-atomic catalog write (`:914-915`, r1/r2); timeframe-segment slice assumes normalized length (`:161-167`, r1); fundamentals fully materialized for row counts (`:843-846`, all); OHLCV size double-counting when cache merges share paths (`:488-497`, r1); loose `_TIMEFRAME_REGEX` accepts bare letters as timeframes (`:35`, r3); TOCTOU between stat and open under concurrent downloads (`:354-355`, r2).
- `cache_manager.py` — mixed locking discipline invites bugs (`get_overview_metadata` releases between check and setdefault, `:295-306`, r3/r1); stats double-count misses across cache layers (`:301`, r2).
- `auto_download.py` — six-letter-uppercase heuristic routes equities to dukascopy failing late (`:301-306`, r3); SYMBOL_CONFIG import errors swallowed into "not forex" rerouting (`:308-326`, r1/r2/r3); Alpaca fallback keyed on exception message substring (`:432-434`, all); HF/Alpaca July overlap window downloaded from both sources, winner by merge order, documented nowhere (`:21-22,374-393`, r1/r3/r2); timestamp-column full scans (perf, folded in DV-B17).
- `validate.py` — OHLC violations counted but never escalate status beyond warning (`:199-208`, r2); one pathological bar triple-counted across three masks (`:199-206`, r3); weekend/holiday skip logic misses Fri→Sat tails, holidays, crypto (`:173-178`, r3/r1/r2 — Luna D9 major dissent noted, panel holds minor: docstring overpromise not silent corruption); dead `EXPECTED_BARS_PER_DAY` (`:35-43`, r1/r3).
- `resample.py` — no guard against downsampling/equal-TF resample; `--from 1m --timeframes 1m` overwrites source (`:41-75,100-104`, r2 — related to DV-B23); `update_catalog` swallows failures leaving stale catalog (`:138-150`, r3/r1); dead pattern variable (`:118`, r3).
- `signal_import.py` — tz inferred from column-name substring "ny" (`:91-92`, r3); midnight-stamped intraday misclassified as daily (`:80-81,113`, all); DST fall-back naive localization raises uncaught AmbiguousTimeError (`:92+`, r1); iterrows perf (`:151`, r3).
- `dolthub_manager.py` / `dolthub_dl.py` — tz-naive timestamps written (`manager :148`, all); unchecked SELECT DISTINCT returncode yields cheerful zero symbols (`manager :111-118`, r3); stale intermediate CSVs on failure (`manager :164-199`, r3); JSONDecodeError outside retry umbrella (`dl :113-115`, r1/r3); repo-without-slash unpack error (`dl :89`, all); custom-query results stored under symbol "UNKNOWN" (`dl :236`, r2); earnings/rates export twins (duplication, both).
- `download_grid_market_data.py` — summary JSON to `Path.cwd()`/logs unlike every other artifact (`:347-350`, r1/r3); skip-check reads target parquets twice per run (`:232-236`, r3/r2); canonical open..close filled from whichever side exists — cross-source parity ambiguity (`:104-121`, r3); identity OIL_ALIASES mappings (r3).
- `yfinance_dl.py` — end day exclusive vs inclusive elsewhere (`:183`, r3/r1); resume restarts whole stored-day re-fetching (`:171-182`, r3); MultiIndex flatten takes `column[0]` fragility (`:213-217`, r1/r2); coerce-drop silent (`:242`, r1); dead capitalize-rename branch (`:233-235`, r3).
- `alpaca_dl.py` — missing volume filled int 0 dtype drift (`:179-180`, r1/r3); unbounded pagination on token churn / runaway first-run downloads with no bounds (`:256-263, 235-243`, r1/r2).
- `histdata_dl.py` — single-year `--years 2021` IndexError crash (`:286-287`, r1); Session-per-year churn (`:171`, r1/r3); tz-local year derivation off-by-one near New Year (`:118`, r3); string-vs-timestamp end comparison (`:143-144`, r3/r2); ZIP member assumed first entry + small-response heuristic misclassification (`:202-220`, r2/r1 — Luna D6 resource-hardening dissent noted, panel holds minor for a trusted-vendor scrape); imports inside functions (`:182,262`, r3).
- `huggingface_dl.py` — end defaults to coverage cap rather than today (`:233`, r1/r3); coverage constant duplicated across two modules (`:31`, r1); resample-before-dedupe month-seam vintage arbitrariness (`:277-283`, r2); no-op `1d→1d` resample rule (`:54`, r2).
- `ctrader_dl.py` — separator sniffing stops at first ≥5-column parse (`:181-189`, all); `matches[0]` arbitrary CSV pick on glob ambiguity (`:128-131`, r2); malformed-row to_datetime aborts import unlike siblings' coerce+drop (`:207`, r1/r3); asset-class taxonomy "index" vs canon "indices" (`:142-145`, r3); advertised Open API path raises not-implemented (Speculative Generality, r2/r3).
- `dukascopy_discovery.py` — asyncio.run per probe rebuilds session each probe (`:141-147`, all); linear re-scan always runs post-binary-search doubling probes against rate limits (`:294-350`, r1/r2); dead probe helpers (`:49-77`, r2); hardcoded REFERENCE_GENESIS for two symbols (`:39-46`, r3); global VERBOSE_LOGS mutation (`:536-537`, r1/r2).
- `migrate_csv.py` — hardcoded author-desktop sibling paths (`:30-32`, all); dest-exists skip permanently blocks re-migration after truncated write (`:129-131`, r1/r2); uppercase-sniffing metadata heuristic fragility (`:145-157`, all); rows lacking timestamps written anyway then catalog-defaults to 1d (compounding with DV-B11, r2).

**validation/**
- `reference.py` — deprecated tz-naive `datetime.utcnow` flows into `BacktestMeta.created_at` (`:51`; r1 major → settled minor: cosmetic provenance skew, sorting unaffected within area conventions; r2/r3 minor); second-granularity id collisions (`:87-89`, r1/r3); unused uuid4 import (r1).
- `cli.py` — `pnl_pct=args.stats_tolerance` flag conflation (`:190`, all); fractional seconds truncated by int() (`:200,209`, r3/r1); broad excepts convert programming errors to load failures (`:168-180`, r1); importer dispatch if-chain (r2).
- `comparison/comparison.py` — exit compares `exits[-1]` only while PnL sums all legs (`:127-130`, r1/r3); direction compared as raw strings (`:175-185`, r2); SL/TP meta-vs-attribute resolution differs from risk module's (`:152-160`, r1); byte-identical `_compare_optional_price/_pct` twins (all); `_normalize_timestamp` non-ISO raises abort whole run (`:17-23`, r2); alignment is exact-string intersection (r3).
- `matcher.py` — greedy first-fit mispairs simultaneous trades (major per r2, minor per r1/r3 — settled **minor-plus**: real but bounded by tolerance windows; characterization tests recommended before touching); O(n·m) scan (`:128-145`, r2/r3); empty-string exits raise via `_normalize_timestamp("")` treating open positions as errors (`:28`, r2); MatchKey records reference times only (`:150-155`, r2); stringly `**options` (r1/r2).
- `anomalies.py` — EXTRA_TRADE severity "info" contradicts delta_report's fail-on-any-extra (folded into DV-B38); `match_kwargs` consistency trap (`:53-90`, r1/r2); unreachable `detect_duplicates` given matcher consumption semantics (~50 lines Speculative Generality, r3; r1 notes defensive value); error-fields set duplicated (`:240-243`, all).
- `delta_report.py` — `total_matched` excludes duplicates via 1-tuple membership test reading like a bug (`:287-289`, r3); diffs re-derived instead of trusting comparator values risking explanation/pass contradiction (`:127-131`, r2); `_rel_diff` fifth copy (all); `_anomaly_to_delta` Repeated Switch with silent unknown-type fallthrough (`:183-237`, r2/r1).
- `importers/__init__.py` — exports 3 of 4 importers, QC missing (r1).
- `mt5_xlsx.py` / `mt5_html.py` — `_parse_stats` double-applies 4-column rows (idempotent today, `html :262-269`, r1/r2/r3); `_to_iso` returns raw garbage timestamps on parse failure (`xlsx :583-599`, r1/r2); SL/TP header variants silently yield None (`xlsx :407-408`, r3); ZoneInfoNotFoundError raw vs ValueError inconsistency + zoneinfo import in hot loop (`html :561-564`, all); extra-indentation load() body lint trap (`html :74-121`, r1/r3); settings scan swallows any "=" cell (`html :224-229`, r2); SLTP map keyed on raw cell objects type-mismatched lookups (`xlsx :419`, r2); unguarded `float()` on localized meta (`xlsx :219`, r2); net_pnl None-vs-0 inconsistent null semantics (`xlsx :546-547`, r1/r3); ~200 lines of shared German stat-label machinery duplicated wholesale (largest duplication in area — extract mt5_common, all three).
- `quantconnect_result.py` — legacy naive formats stamped UTC regardless of export locale (`:189-216`, r1 major → panel minor per QC-exports-are-UTC reality; r2 major dissent noted; r3 concurs minor); O(n²) CSV verification greedy match (`:902-916`, r1/r3); min-fee applied to zero-quantity orders hard-fails legit portfolios (`:793`, r1); rigid 5-element equity schema rejects LEAN 2-element series (`:684-685`, r2); full equity curve embedded in every artifact/hash (`:701`, r2); `_submission_clusters` latent empty-input crash (`:670-672`, r2/r3); triplicated reconciliation payloads (r1/r2).
- `readiness/models.py` — four fields declared twice; verified harmless today (last-wins, module imports fine) but a shadow-edit trap (r1/r3 minor; r2 suspected import-breaking — refuted by direct execution during synthesis).
- `readiness/validator.py` — placeholder checks recorded passed=True/"info" when artifacts absent — correct only while require_* flags hold (`:205-242` etc., r3); warning-level mismatches force READY_WITH_WARNINGS forever with no waive knob (`:371-389`, r1); nine instances of one threshold-check shape (r2/r1); recommendations threaded as out-param through every method (r2).
- `regression/runner.py` — TradeComparator constructed per trade (`:318-321`, r3); recomputed-vs-recomputed golden stats can't detect stored-stats corruption (`:332-333`, r2); deviations list[Any] (r1/r2); vacuous pass on empty inputs (`:340-341`, r2).
- `json_report.py` — dead `_rel_diff` helper while inline twin used (`:87-91`, r2); markdown round-trips through JSON string (`:578-589`, r2); empty stat_deltas counts as passed=1 vacuous truth (`:453`, r2); platform-stats fallback computes stats from zero trades guaranteeing mismatch instead of flagging no-data (`:402-417`, r1); bool stuffed into int summary (`:443,453`, r1); risk_passed never surfaced in recommendations (`:515`, r1); impossible dict branch in isinstance ladder (r2/r3).
- `risk/models.py` — explicit `frozen=False` configs suggest past migration (r3); free-text direction field (r1/r2); unreferenced `RiskModelConfig` (Speculative Generality, r1/r2).
- `risk/fixed_risk.py`/`percent_risk.py` — `validate()` mutates caller's model (`:60-61`, all); no notional sanity bound — tiny stop distances produce absurd leverage silently (`percent_risk :94-95`, r1); direction unused in math — wrong-side stop yields plausible profile (`:79/:88`, r2); negative fixed risk_amount unvalidated producing absurd negative sizes floored by rounding (r3); total_risk_percent summation dimensionally odd (`adapter :229`, r1/r2); ~85-90% identical models — extract base StopDistanceSizer (all three).
- `risk/adapter.py` — binary LONG-else-sell mapping (`:41`, all); Middle Man trade_to_signal wrapper (r1/r2); if-chain model dispatch (r2/r1).
- `risk/comparison.py` — attribute-over-meta stop resolution asymmetry vs comparison module (`:165-174`, r2/r1); composite trade_id underscore-join ambiguity (`:407`, r1); Inappropriate Intimacy reaching DeltaReportBuilder private (`:400-402`, all).
- `statistics/cross_platform.py` — period extraction reads only first 12 rows swallowing failures disabling CAGR silently (`:281-297,208-213`, r3/r1); deferred runner.stats import mid-function circular-dependency paper-over (`:241-242`, r3/r1/r2); win_rate>1 heuristic edge (r1/r2/r3; Luna d9).
- `stats/calculator.py` — drawdown from trade-close sequence not equity curve (documented convention, cross-platform comparisons diverge silently; `:59-78`, r2 minor, Luna D18 major dissent — panel holds minor per documented convention); Sortino divides by negative-count (declared; least-standard variant; `:103`, all; Luna D19 annualization dissent similarly held minor); infinite ratios rely on downstream sanitizer (`:129,139`, r1/r2).

---

### D. Cross-cutting themes

1. **One OHLCV resample rule, ~11 implementations** (agg-dict copies in resample, histdata, ctrader, huggingface, yfinance, dukascopy_native, jforex-via-shared…): `data_tools.resample.resample_ohlcv` already exists — adopt it everywhere. Timeframe vocabularies are independently re-declared in ≥10 modules (TIMEFRAME_MAP/ALIASES/ORDER families) with an in-code "must stay in sync" confession.
2. **Durability**: canonical parquet writes are non-atomic in base.merge, grid normalize, dukascopy source-meta, catalog.json, migrate dest — one shared `atomic_write_parquet` (temp+rename, optional lock) fixes the class (DV-B1 family).
3. **Two-and-a-half stats engines**: `regression/runner._compare_stats`, `reporting/json_report._compare_stats`, plus zombie `statistics/cross_platform.py` — consolidate on `stats/calculator.py` conventions.
4. **MT5 importer twins**: ~200 shared lines (labels, coercers, exit-inference, assembly) — extract `mt5_common.py`.
5. **Gate-consistency**: extra-trade severity and tolerance OR/AND semantics disagree across anomalies/delta_report/risk-comparison/readiness — single-source severity policy.
6. **Fixed-checklist verdicts**: SQL string building (both dolthub modules) and subprocess-without-timeout (dolthub_manager) are the two literal violations; mutable defaults clean everywhere; tz-naive spots confined to reference.utcnow, dolthub stamps, and QC legacy fallbacks; resource leaks are repeated unclosed ParquetFile/Session handles.

---

### E. Reference-report adjudication summary

Adopted (verified): D1 (=DV-C1), D4/D5 (jar bootstrap), D8 (=DV-B24). Corroborated-by-panel: D2/D3 (=DV-B27, severity held at major), D13 (=DV-B20), D15 (=DV-B39), D20 (=DV-B43). Refuted/not adopted: **D10** (resample sorts via `sort_index()` before agg — verified in source), **D14** (fixed UTC boundaries are the documented repo convention for FX data), **D18/D19** (documented calculator conventions; flagged for cross-platform parity awareness instead), **D16/D17** (substance covered under DV-B37/B50 at calibrated severity), D6/D7 (held minor/trusted-vendor; substance partially in DV-B19), D9 (minor per docstring-overpromise reading), D11/D12 (string-coercion fragility held minor; pandas currently coerces deterministically), d1–d12 (either merged above at matching or lower severity, or subsumed: d7→matcher note, d8→DV-B35, d12→DV-B1).

---

### F. Verification & settlement log (synthesizer code reads)

1. **dukascopy_native call site/signature** (`:1005-1028`, `:774-800`): confirmed both DV-C1 (TypeError via duplicate `pipeline_batches`) and DV-C2 (identical bid/ask URL calls at `:790-791`). r3's claim that the misalignment *silently* swaps pause_ms/max_retries was corrected: the duplicate keyword raises before that matters; r1's identical finding rated major — raised to critical jointly with DV-C2.
2. **r3's "silent 1000× SL/TP corruption"** (`float("1.234,56".replace(",", "."))`): arithmetic checked — `"1.234,56".replace(",", ".")` = `"1.234.56"` which raises ValueError, it does not yield 1.23456. Merged defect recorded accurately in DV-B44 (silent corruption for dot-thousands strings, abort for comma-decimals).
3. **TradeComparisonConfig import** (`json_report.py:1-30` + grep): confirmed missing; `from __future__ import annotations` present at top → deferred evaluation, no runtime failure today. Settled major (r1 critical dissent noted).
4. **ReadinessConfig duplicate fields**: executed import — module loads fine, pydantic last-wins. r2's "PydanticUserError on import" refuted; kept as minor maintenance trap (r1/r3 position).
5. **Dolthub SQL + subprocess** (`dolthub_manager.py:32,138`, `dolthub_dl.py:94-102`): confirmed f-string SQL interpolation and timeout-less subprocesses. Severity settled major/major (r1's two security-criticals and r2's availability-critical recorded as dissents; reachability bounded).
6. **signal_import `_parse_action`** (`:65-80`): confirmed `return "buy"` fallthrough for unknowns.
7. **Luna D5 jar-bootstrap swallow** (`jforex_dl.py:344-356`): confirmed `except Exception → warning → return []`; adopted as DV-B7.
8. **Luna D10 resample ordering** (`resample.py:55-71`): refuted — `set_index().sort_index()` precedes `.resample().agg()`.

---

### G. Coverage

All 50 tracked files accounted for across the three partials (each reviewer read the area in full): 42 files carry findings above or in the partials; scanned-clean: `data_tools/__init__.py`, `data_tools/downloaders/__init__.py`, `validation/__init__.py`, `validation/comparison/__init__.py`, `validation/importers/base.py` (design note folded into MT5 sections), `validation/readiness/__init__.py`, `validation/regression/__init__.py`, `validation/reporting/__init__.py`, `validation/stats/__init__.py`. Per-file mapping tables appear in each partial report.

---

**Final counts: Critical 2 · Major 51 · Minor 118 · Total 171**
