# Cleanup 15-B — Design (Sub-Issue #22)

Fixed point: origin/main @ 6caf461. Branch: cleanup/15b, worktree /home/ubuntu/Desktop/wt-22.

## Goal (from AC)

> Credential resolution is deterministic, logged without secrets, and cannot silently
> route multiple accounts through one account's keys.

Decision Record quote: "Credential resolution uses a unique mapping between each live
deployment and one credential block. No fallback to another account's keys and no
ambiguous fallback when multiple blocks exist."

Open proposal (implemented as proposed): env-vs-file precedence is **file-first**
everywhere — `.accounts.env` wins, `os.environ` only fills keys the file does not
define — via one shared resolver.

## New seam: `live_runner/credential_env.py`

One module owns the two things that are currently duplicated across four files:

1. **Boolean env parsing.** `_parse_bool_env` (lenient, returns False on typos) and
   `_parse_strict_bool_env` (raises on typos) exist in daemon; preflight and ctrader
   each have their own strict copies; account_discovery and alpaca_adapter have
   lenient copies. The module exports one strict parser
   `parse_bool_env(name, value, *, default)` and re-exports nothing else.
2. **Env-vs-file precedence.** `load_accounts_env_values(path) -> dict[str, str]`
   loads the dotenv file, then overlays `os.environ` **only for keys the file does
   not define** (file-first). `accounts_env_mtime(path)` moves here too.

## Slice plan (TDD: failing test first in each slice)

### Slice 1 — B-1: hint-less multi-account configs return None (alpaca_adapter)
Current behaviour (`alpaca_adapter.py:157-183`): with >=2 ACC{N} blocks and no hints,
an account whose id/login matches nothing falls through to `blocks[0]` — every account
routes through ACC1's keys. Fix: return `None` (after logging a warning without secrets).
The default pair (`ALPACA_API_KEY`/`ALPACA_SECRET_KEY`) still applies **only** when no
hints exist anywhere and at most one credential block is resolvable — i.e. the legacy
single-account setup. With 2+ hint-less blocks there is no unique mapping, so None.
Tests: `tests/test_alpaca_account_routing.py` (extend).

### Slice 2 — B-3: strict boolean parsing for deployment config (daemon)
`_deployment_wait_for_candle_close` and `_deployment_allows_short` use the lenient
parser; a typo like `wait_candle_close: ture` silently disables candle-close waiting
(forces immediate market orders) and `allow_short: eanbled` silently disables shorts
without any signal. Switch both to the shared strict parser. signal_config values are
typed by pydantic as `Any`, so a typo reaches runtime as a str — the ValueError is
converted to a logged error + safe default by the new typed accessors
(`_deployment_flag(deployment, key, default)`) so one bad deployment cannot crash the
daemon loop; env-sourced values keep raising through the strict parser as today
(daemon/preflight catch and surface them).
Tests: `tests/test_live_daemon.py` (extend).

### Slice 3 — B-9: file-first precedence via shared resolver
- `daemon._load_accounts_env_values` -> shared resolver (file-first).
- `account_discovery.discover_configured_accounts` currently `load_dotenv(accounts_env)`
  then `env = dict(os.environ)` (os.environ overwrites the file — inverted). Replaced by
  the shared resolver.
- `service_discovery.discover_discord_bots_from_env` same inversion; replaced.
- `ctrader_adapter.refresh` writes refreshed tokens back to `os.environ` (486-487) —
  removed; the file is the source of truth and `_resolve_ctrader_credentials` reads the
  file-first mapping, so in-process consumers already get fresh values.
Tests: new `tests/test_credential_env.py` + `tests/test_service_discovery.py`.

### Slice 4 — B-12: one credential-block parser shared by discovery + adapter
`account_discovery._iter_alpaca_credentials` and `alpaca_adapter._alpaca_credential_blocks`
parse the same env keys with different rules (discovery ignores `ALPACA_PAPER_ACC{N}`
and hints; adapter honours them). Discovery now uses the adapter's block parser, so the
reported `is_demo` equals the paper flag the runtime adapter is constructed with.
Tests: `tests/test_account_discovery.py` (extend).

## Discarded alternative

- Making `resolve_alpaca_credentials` raise ValueError for unmatched accounts: daemon
  and preflight catch nothing today and a raise inside `_create_adapter_for_account`
  would abort the whole account loop, not just the misconfigured account. Logging a
  secret-free warning and returning None matches the existing "missing credentials"
  path that both callers already handle.

## Non-goals / out of scope

- cTrader credential blocks are global (one app credential set, multiple ctid trader
  accounts) — no per-account mapping exists there, so B-1/B-12 apply to Alpaca only.
- No changes to Discord signal parsing, PAUSE/SL-TP paths (15-A), or `os.environ`
  non-credential settings (CTRADER_CONNECT_MAX_ATTEMPTS etc.).
