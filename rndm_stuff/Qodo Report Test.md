









Platform Health & Audit Report — Automated QA & System Audit Agent
Date: 2026-04-21T17:30Z (local test run)

Summary (one-line)
- I executed the unit test suite, ran a set of backend API checks, navigated the frontend with Playwright (Firefox headless), captured screenshots, and performed a static review of a few key backend/frontend files; unit tests passed, UI pages rendered, some backend endpoints returned data while others returned non-JSON or empty responses and a small set of operational issues were observed (see Findings).

Environment & commands I ran
- Working directory: /home/ubuntu/Desktop/trading-platform
- Virtualenv activation used for Python commands:
  - source .venv/bin/activate
- Unit tests:
  - pytest -q --disable-warnings --maxfail=1
- Playwright installation & run:
  - python -m playwright install firefox
  - Playwright script using the playwright.sync_api (Firefox headless) to visit routes and save screenshots.
- API checks:
  - curl -sS http://localhost:5556/api/services -o /tmp/services.json
  - curl against a few other endpoints (see API checks below)
- Files inspected (examples):
  - frontend/src/api/types.ts
  - risk/engine.py
  - shared/database.py (reviewed earlier in the session)
  - viewer/main.py (reviewed earlier in session)
- Note: I did not modify repository files.

Phase 1 — Unit tests (logic layer)
- Command executed:
  - source .venv/bin/activate && pytest -q --disable-warnings --maxfail=1
- Result:
  - 303 passed, 70 skipped, 11 warnings (runtime ~52s)
- Interpretation:
  - The unit and integration test baseline for the code under test is healthy in the current environment; key logic pieces covered by tests are passing (including risk/engine pieces exercised by tests).
- No code modifications were required to make tests pass.

Phase 2 — API & data contract checks (integration layer)
What I called
- /api/services
  - curl stop: produced HTTP/200 with JSON payload (I saved to /tmp/services.json).
  - I inspected the JSON (partial printed output): it lists several services, many of type "discord_bot" with status "failed". Example extracted snippet (truncated):
    - ROCKETFUEL — type: discord_bot — status: failed
    - RAMJET — type: discord_bot — status: failed
    - NITRO — type: discord_bot — status: failed
  - Meaning: the application is exposing service entries; the "failed" status likely means external connectors (discord bot tokens) are missing or failing in this local environment (expected in local test).
- /api/backtests, /api/live/accounts, /api/data/overview
  - I attempted curl to these endpoints and received no JSON output or the responses could not be parsed as valid JSON by python -m json.tool (curl output caused "Expecting value: line 1 column 1 (char 0)").
  - Possible reasons:
    - These endpoints may require query parameters, authentication, or return HTML (SPA) rather than JSON when requested without specific headers.
    - The endpoints may be served but expect a different route (e.g., `/api/backtests` may be behind a prefix or accept POST only).
  - Recommendation: Re-run targeted requests including necessary query params or check server logs.

Phase 3 — UI & visual checks (frontend layer)
- Preconditions:
  - The backend (uvicorn viewer.main:app --host 0.0.0.0 --port 5556) was running and reachable from this environment.
  - The frontend is served by the same process (the server serves a SPA root).
- Playwright run: headless Firefox
  - I launched Firefox in headless mode and navigated to:
    - /
    - /backtests
    - /data
    - /tasks
  - I waited until networkidle then captured full-page screenshots for each route and saved into the repo folder temp/.
- Screenshots saved (files & sizes)
  - temp/screen_.png — root page (265,217 bytes)
  - temp/screen_backtests.png — /backtests (915,746 bytes)
  - temp/screen_data.png — /data (285,561 bytes)
  - temp/screen_tasks.png — /tasks (3,229,712 bytes)
- Playwright textual checks
  - For each route I read page body inner text and looked for specific keywords to confirm the page rendered the expected SPA content.
  - Results (printed during run):
    - / — title: "frontend" — found keywords: ['Backtests', 'Data']
    - /backtests — title: "frontend" — found keywords: ['Backtests', 'Data']
    - /data — title: "frontend" — found keywords: ['Backtests', 'Data']
    - /tasks — title: "frontend" — found keywords: ['Backtests', 'Data', 'Tasks']
  - Interpretation: The SPA is rendering and injecting content for routes; the "frontend" title is used by the app; the pages load static HTML/JS allowing the frontend to render.

Phase 4 — Quick static review and focused file checks
- Files examined during the session:
  - frontend/src/api/types.ts — confirmed type declarations and interfaces (many API contract types present).
  - risk/engine.py — reviewed RiskEngine implementation; the code includes sensible numeric guards (balance > 0, pip > 0), fallback stop-loss logic, trailing stops and risk sizing; tests cover it.
  - shared/database.py — earlier PR diff review had changes to preview extraction and SQL; I flagged possible issues (timestamp falsiness, ordering semantics) in a prior audit and retained that advice (still applicable).
  - viewer/main.py — contribution aggregation and metric_weight handling previously flagged for small-denominator stability — still recommended to guard before dividing by metric_weight.
- Observations from static review (no code edits performed):
  - There are many Date.parse usages across frontend; many are guarded, but some use `Date.parse(x) || 0` pattern which can hide malformed dates by coercing to 0.
  - Some navigator.language usages are not consistently guarded (use `globalThis?.navigator` helper).
  - getBacktestsAll default change in PR set include_equity_preview to true; that increases payload sizes unless opt-in.

Findings & issues (summary)
- Passed / OK
  - Unit tests: 303 passed, 70 skipped.
  - Frontend SPA pages render for the main routes visited: /, /backtests, /data, /tasks.
  - Screenshots were successfully created (see Screenshot Index below).
  - RiskEngine basic guards look correct (balance, pip>0, risk distance check).
- Observed problems / items requiring attention
  1) Some API endpoints returned non-JSON or no-parsable content when called via curl without params (/api/backtests, /api/live/accounts, /api/data/overview). If JSON is expected, verify parameter expectations and auth.
     - Impact: Integration tests or other tools calling these endpoints without proper params/auth may fail.
     - Suggested action: Run targeted API calls with documented query params, or add a small diagnostic route to confirm JSON availability.
  2) /api/services JSON contained multiple "discord_bot" entries in "failed" status.
     - Impact: External connector status is expected in local dev; ensure secrets are not present locally and treat as expected.
     - Suggested action: If these should be "running" in this environment, provide tokens/config or mock connectors.
  3) DB preview logic & viewer aggregation (previously identified in PR) — not directly triggered by tests here, but still high-risk:
     - shared/database.py: reject of falsy `timestamp` can drop valid zero timestamps.
     - viewer/main.py: dividing by metric_weight can cause instability if metric_weight is unusually small.
     - Suggested action: adjust numeric checks to use Number.isFinite-like logic and guard denominators.
  4) Performance / UX: getBacktestsAll default now includes equity previews (payload heavier).
     - Suggested action: consider making it opt-in.
- No automatic code changes were performed by me; I only executed tests and checks and pointed out issues for manual or PR changes.

Artifact index (screenshots)
- All screenshots saved to repository temp/ directory (already created). Filenames & sizes:
  - temp/screen_.png — root page
  - temp/screen_backtests.png — backtests page
  - temp/screen_data.png — data page
  - temp/screen_tasks.png — tasks page
- The screenshots are full-page PNGs; open them locally to visually verify AG Grid tables or charts. Note: tasks page screenshot is larger (3.2 MB) — likely a larger DOM.

Exact commands you can re-run locally
- Activate venv:
  - source .venv/bin/activate
- Run tests:
  - pytest -q --disable-warnings --maxfail=1
- Run backend (if not already running):
  - source .venv/bin/activate
  - uvicorn viewer.main:app --host 0.0.0.0 --port 5556
- Quick API checks:
  - curl -sS http://localhost:5556/api/services | python -m json.tool
  - curl -sS 'http://localhost:5556/api/backtests?page=1&per_page=10' | python -m json.tool
- Playwright screenshot script (example):
  - source .venv/bin/activate
  - python - <<'PY'
    from playwright.sync_api import sync_playwright
    from pathlib import Path, PurePosixPath
    import time
    Path('temp').mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        routes = ['/', '/backtests', '/data', '/tasks']
        for r in routes:
            url = 'http://localhost:5556' + r
            page.goto(url, wait_until='networkidle', timeout=15000)
            time.sleep(1)
            safe = r.replace('/', '_') or '_root'
            path = f'temp/screen{safe}.png'
            page.screenshot(path=path, full_page=True)
            print('wrote', path)
        browser.close()
    PY

Actionable remediation plan (short)
1. Reproduce API non-JSON behavior:
   - Call endpoints with intended query params and validate JSON shape.
2. If endpoints must return JSON without params, adjust route handlers to always return JSON or redirect usage documentation.
3. For DB preview logic: change timestamp truthiness check to numeric parse (do not drop 0).
4. For viewer contributions: guard divisions by metric_weight (if very small, treat as not computable).
5. Reassess getBacktestsAll preview default: revert to false or add parameter.

Limitations / Out of scope
- I did not:
  - Change any code or make automated fixes (per your instruction to confirm code changes first).
  - Run Playwright with a full, logged-in user flow or interactions requiring authentication, nor did I interact with external broker or discord systems (those need external credentials).
  - Validate chart correctness (visual regression), AG Grid content correctness beyond presence of keywords; visual verification requires opening PNGs locally.
- External services not validated here:
  - Real broker connectivity (Alpaca, cTrader), rate-limited Discord bots, or external data provider availability and slippage behavior.
  - Performance/load testing.

Concluding notes
- The system in this environment is in a healthy state at the unit-test level (303 passed).
- The frontend SPA renders the main routes, and Playwright screenshots were captured and stored in temp/.
- Some integration endpoints returned non-JSON when invoked without params — please confirm intended usage (auth/params).
- I flagged a few higher-risk code areas (DB preview timestamp logic and contributions averaging) that are worth immediate attention; these are not failing tests but represent correctness risks in edge-cases.

If you want me to continue
- I can now:
  1) Run targeted API calls with query parameters and validate response JSON shapes against frontend types (I will fetch and diff keys).
  2) Attempt to interact with charts (e.g., open backtest detail page, load equity preview) and capture more focused screenshots.
  3) Produce a formatted Markdown report file in the repo root (I will prepare the content here for you to save), or produce an editable Google Doc if you prefer (requires external integration).
  4) Propose exact minimal code patches (diffs) to fix any of the flagged issues (DB preview, metric_weight guard, navigator/date parsing) for your review and apply them only after you approve.

State which of the above to do next (1/2/3/4) and I will proceed step-by-step.
