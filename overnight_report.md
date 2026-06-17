# Trading Platform / Provider Work Report
**Date:** 2026-06-17
**Current checked time:** 12:xx MESZ (Europe/Berlin)

## Provider / Hermes Work

### Aerolink
- Added a real Hermes model-provider plugin at `~/.hermes/plugins/model-providers/aerolink/`.
- Registered provider profile:
  - provider: `aerolink`
  - api_mode: `anthropic_messages`
  - base_url: `https://aerolink.lat`
  - fallback Claude models included because `/models` is Cloudflare-protected.
- Imported Aerolink API key into Hermes CredentialPool:
  - `hermes auth list aerolink` shows 1 credential.
- Real tests:
  - `https://api.aerolink.lat` DNS fails.
  - `https://aerolink.lat/v1/models` returns Cloudflare 403.
  - `https://aerolink.lat/v1/messages` with `x-api-key` returns Cloudflare 403 / code 1010.
  - `https://aerolink.lat/v1/messages` with `Authorization: Bearer` also returns Cloudflare 403 / code 1010.
  - Hermes call with `--provider aerolink` reaches Aerolink but gets `HTTP 403: Your request was blocked`.
- Conclusion: Aerolink is now selectable in Hermes, but upstream blocks server-side traffic from this machine. This does not prove credits are gone; likely Cloudflare/WAF/API access setting.

### Duplicate pi provider in model selection
- Removed old `custom-pi-provider` entry from `~/.hermes/config.yaml` `custom_providers`.
- Kept `providers.pi` intact so the local pi gateway still works.
- This should remove duplicate pi/custom-pi rows while preserving the intended `pi/...` provider.

### New model-provider plugins for credit pools
Added user model-provider plugins:
- `~/.hermes/plugins/model-providers/kimchi/`
- `~/.hermes/plugins/model-providers/freemodel/`
- `~/.hermes/plugins/model-providers/bluesminds/`
- `~/.hermes/plugins/model-providers/aerolink/`

Imported existing keys into Hermes CredentialPool:
- `kimchi`: 1 key
- `freemodel`: 1 key
- `bluesminds`: 1 key
- `aerolink`: 1 key

Set pool strategies to `fill_first` for all four providers. This is best for using monthly/free credits account-by-account before falling through to the next account.

Created and enabled operator plugin:
- `~/.hermes/plugins/provider-credit-pool/`
- CLI command: `hermes provider-pool status`
- CLI command: `hermes provider-pool import-json <file> --strategy fill_first`
- CLI command: `hermes provider-pool strategy <provider> <strategy>`

### Provider smoke tests
- `freemodel/gpt-5.4-mini`: OK
- `bluesminds/gpt-5.5`: OK
- `kimchi`: API reachable, but tested model routes currently reported exhausted credits.
- `aerolink`: selectable but upstream Cloudflare blocks with 403/1010.

## Trading Platform Work

### cTrader
- cTrader account discovery shows `CH_ACCESS_TOKEN_INVALID: Invalid access token`.
- 29 relevant tests pass:
  - `tests/test_ctrader_adapter.py`
  - `tests/test_live_execution_safety.py`
- Need a fresh cTrader access token before real live trading can be tested.

### Portfolio / Grid
Branch: `feature/portfolio-optimizer-bayesopt`
- Added `runner/portfolio_optimizer.py`.
- Added `runner/grid_tester.py`.
- Tests/compile pass for touched Python modules.
- Graphify updated.

## Git Branches / Commits
- `trading-platform` branch `feature/portfolio-optimizer-bayesopt`
  - `feat: add portfolio optimizer with bayesian allocation + grid strategy tester`
  - `chore: update graphify graph and overnight report`
- `hermes-pi-provider` branch `fix/openai-tool-calls`
  - `fix: improve tool call parsing with JSON fence stripping`
  - `chore: persist pi SDK local dependencies`
- `pi-multiprovider` branch `test/provider-smoke`
  - `fix: update Aerolink endpoint and add provider smoke test`

## Verification
- Hermes provider plugin import syntax: OK.
- `hermes provider-pool status`: OK.
- trading-platform py_compile touched files: OK.
- trading-platform selected tests: `29 passed, 1 warning`.
- `graphify update .`: OK.
