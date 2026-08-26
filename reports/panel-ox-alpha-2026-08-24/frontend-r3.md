# Frontend Review — Reviewer r3 (ox-alpha)

Area: `frontend` (React 19 + Vite + TypeScript SPA), reviewed blind and independently.
Scope: all 59 tracked code files listed in the brief. Every file is accounted for below either with findings or as "scanned — nothing found".
Method notes: read-only; no files under `reports/` other than this one were opened. Severity scale: critical / major / minor. Smells are named per Fowler (*Refactoring*, ch. 3) with the two binding rules applied (repo-standard overrides; everything is a judgement call; tool-enforced issues skipped).

---

## Systemic findings (apply across many files; cited once here, referenced per file)

**S1. No error boundaries anywhere in the tree — major**
No `componentDidCatch`, no `react-error-boundary`, nothing. All 21 routes are `React.lazy()`-loaded (`App.tsx:7–73`); a failed chunk fetch, a thrown render error, or any unexpected `null` deref takes down the whole app to a white screen (only `Suspense` covers *loading*). Highest-risk derefs found later: `PortfolioDetailPage.tsx:718–729`, `LiveViewer.tsx:82`. Checklist item "missing error boundaries" — violated.
Cite: `frontend/src/App.tsx:96–122`, `frontend/src/main.tsx:11–22`.

**S2. Unbounced network effects bound to text input — major**
Three pages fire HTTP requests synchronously from every keystroke:
- `LoggingPage.tsx:230–262` — `search` is a direct effect dependency; each keystroke refetches up to `LOG_FETCH_LIMIT = 5000` log lines (and the 5 s poll keeps doing it forever afterwards).
- `DownloadModal.tsx:314–356` — `parsedSymbols`/`requestedTimeframes` memos recompute per keystroke in the symbols field, refetching `getDukascopyGenesis` each time.
- `CreateBacktestModal.tsx:135–186` — `symbolList`/`start`/`end` changes trigger `getDataCheck` per symbol per edit (no debounce; `BacktestsPage.tsx:596–601` shows the repo knows the debounce pattern).
Checklist items "N+1 fetches", resource waste. Fix: debounce + abort controllers (only `DataManagementPage` and `CreateBacktestModal` use cancellation today).

**S3. Duplicated metric-shape vocabulary (smell: Duplicated Code / Data Clumps) — major (maintainability)**
The same concepts are spelled inconsistently across `api/types.ts` and consumers: `sharpe` vs `sharpe_ratio`, `net_profit` vs `total_pnl`, `max_drawdown` vs `max_drawdown_pct` (sometimes fraction, sometimes percent points — see unit guessing in `BacktestsPage.tsx:123–129`, `LiveDetail.tsx:824–838`, `LiveHub.tsx:1599–1605`), `win_rate` sometimes 0–1, sometimes 0–100. Every consumer re-implements normalization/coloring (`utils/stats.ts`, `BacktestsPage.tsx:131–173`, `MatrixGridPage.tsx:175–207`, `OptimizationsPage.tsx:9–20` vs `OptimizationResultsPage.tsx:20–37`). One shared `Metric` type + formatters module would remove ~200 duplicated lines.

**S4. Copy-pasted helpers across pages (Duplicated Code) — major (maintainability)**
Byte-for-byte or near-identical duplicates:
- `calculateRealizedTradeStats`: `LiveHub.tsx:1529–1597` ≡ `LiveDetail.tsx:731–799`.
- `asFiniteNumber`/`asFinite`: `LiveHub.tsx:1607–1618`, `LiveDetail.tsx:840–851`, `LiveViewer.tsx:1138–1145`; plus `toFiniteNumber` in `TradeChartPage.tsx:52–63`, `BacktestDetailPage.tsx:255–262`, `PortfolioDetailPage.tsx:59–66`.
- `formatUsd`/`formatSignedUsd`/`formatPercent`: `LiveHub.tsx:1477–1487`, `LiveDetail.tsx:853–869`, `LiveViewer.tsx:1147–1172`, `ComparePage.tsx:41–69`, `BacktestsPage.tsx:136–149`.
- Lightweight-charts equity+drawdown chart scaffold: `BacktestDetailPage.tsx:971–1189` ≈ `PortfolioDetailPage.tsx:769–937` (same tooltip/DD logic rewritten).
- `formatMode`/`formatSymbols`: `OptimizationsPage.tsx:22–30` ≡ `OptimizationResultsPage.tsx:39–47`.
- Two near-identical `ReactMarkdown` component maps in `StrategyDetailPage.tsx:341–398` and `441–553`.
- Table implementations triplicated: `components/DataTable.tsx`, `DataManagementPage.tsx:1983–2199` (`PanelTable`), plus bespoke tables in `LiveViewer.tsx:815–859`, `BacktestsPage.tsx:400–441`, `MatrixGridPage.tsx:676–775`.
- `TIMEFRAME_ORDER` exists in three incompatible shapes: `CreateBacktestModal.tsx:14`, `DataManagementPage.tsx:92–106`, `MatrixGridPage.tsx:125–132`.

**S5. Primitive Obsession — minor (judgement call)**
IDs, symbols, timeframes, task statuses, and metric keys are bare `string`s end-to-end (`api/types.ts:680–777`, `client.ts` throughout). Statuses are re-lowercased and compared against ad-hoc `Set`s in five places (`TaskManager.tsx:8`, `TaskHub.tsx:15–16`, `DataManagementPage.tsx:108`, `TradeChartPage.tsx:570`, `BacktestFromSignalsModal` date logic). A branded `TaskStatus`/`Symbol`/`Timeframe` type + shared predicate module would kill the repeated switches.

---

## Per-file findings

### frontend/eslint.config.js
- **minor · correctness-of-tooling:** `ecmaVersion: 2020` (`eslint.config.js:19`) is older than the ES2022 `Object.hasOwn` used in `src/lib/optimizationEvidence.ts:33` — the parser may mis-handle newer syntax; bump to `latest`.
- **minor (smell, judgement call):** README documents type-checked configs as the recommended standard but the config stops at `tseslint.configs.recommended` (`eslint.config.js:13–14`) — several findings below (unsafe `any`, floating promises) would be caught by `recommendedTypeChecked`.
- Otherwise scanned — nothing further.

### frontend/vite.config.ts
- **minor · security/maintainability:** dev proxy target hardcoded to `http://localhost:5556` (`vite.config.ts:11,15`) — not configurable via env; prod builds depend on an out-of-repo reverse proxy with no documentation pointer here.
- Scanned — nothing else (ws proxy correctly sets `ws: true`).

### frontend/src/index.css
- **minor (Duplicated Code):** hand-written `.animate-spin` + `@keyframes spin` (`index.css:56–66`) duplicate Tailwind's built-in `animate-spin` utility imported one line up; drop the custom copy or rename to avoid two sources of truth.
- **minor:** universal `*` reset (`index.css:18–22`) partially duplicates Tailwind preflight.
- Scanned — nothing else.

### frontend/src/main.tsx
- **major · security/observability:** the task brief states "Sentry wired via src/main.tsx", but there is no `@sentry/*` import, no `Sentry.init`, and no Sentry dependency in `package.json` (verified by grep over the whole frontend). `frontend/.env` still ships a live-looking DSN (`VITE_SENTRY_DSN=https://…@o4511659559944192.ingest.de.sentry.io/4511955579437136`). Either wiring was removed/regressed or never existed — production errors are currently invisible, and a stale credential-bearing `.env` sits in the tree.
- **minor:** provider nesting order means `ToastProvider`/`StrategiesProvider` render outside `BrowserRouter` — harmless now, but any future toast/router interaction (e.g. toast on navigation) won't see routing context.
- See S1 (no error boundary wrapping the root).

### frontend/src/App.tsx
- **major (see S1):** lazy routes with `Suspense` only (`App.tsx:96–122`); a rejected dynamic import leaves an unrecoverable blank pane.
- **minor · bug:** no catch-all route — navigating to an unknown path renders sidebar + empty `<main>` with no 404 feedback (`App.tsx:97–121`).
- **minor · UX inconsistency:** shell `TaskManager` is hidden on `/data*` only (`App.tsx:77`), yet `/tasks` (the Task Hub itself) still renders the floating manager card above the hub — redundant double display.
- **minor (Speculative Generality):** nineteen hand-rolled `lazy(() => import(...).then(mod => ({default: mod.X})))` wrappers repeat one shape (`App.tsx:7–73`); a small `lazyRoute(loader)` helper would collapse ~65 lines.

### frontend/src/Sidebar.tsx
- **minor (Speculative Generality / dead nav):** `/accounts` nav entry permanently `disabled: true` with "Coming soon" (`Sidebar.tsx:32,58–73`) and no route behind it — ship or delete.
- **minor · a11y:** collapse toggle button has `title` but no `aria-label`/`aria-expanded` (`Sidebar.tsx:46–52`).
- Scanned — nothing else.

### frontend/src/api/client.ts
- **minor · bug-risk:** `fetchJSON` puts `Content-Type: application/json` ahead of `...options` (`client.ts:91–95`), so a caller cannot remove/override headers for GETs or FormData uploads without trickery; spread order should be `{...defaults, ...options}` with headers merged.
- **minor · bug:** `getBacktestInfo`, `getBacktestStats`, `getBacktestTrades`, `getBacktestReplay`, `getBacktestEquity`, `getBacktestOHLCV`, `getBacktestRunStatus`, `getDataTask`, `getDataTaskLogs`, `getPortfolio`, `deletePortfolio` interpolate raw IDs into URLs without `encodeURIComponent` (`client.ts:208–225,224,239,247,248,403–404,456,458,498–499,603–606`) while sibling functions encode — inconsistent and breaks for IDs containing `/ ? # &`. (Smell: Duplicated Code hiding an inconsistency.)
- **minor (Primitive Obsession / weak typing):** `getBacktestStats` → `fetchJSON<any>` (`client.ts:211`), `getBacktestEquity` → `any` (`:239`), `getBacktestOHLCV` → `any` (`:248`), `renameBacktest` → `any` (`:250`), `deleteDataSymbol` → `any` (`:552`), `getBenchmark` → `any` (`:538`): six endpoints discard type safety at the boundary.
- **minor (Mysterious Name / Middle Man):** `getStrategy` vs `getStrategyDetail` (`client.ts:145–148`) and `getPortfolio` vs `getPortfolioDetail` (`:456–458`) are identical delegating pairs; `getBacktests` (paginated) vs `getBacktestsAll` (everything) differ far more than the names suggest.
- **minor (Duplicated Code):** the 26-line `if (params?.x != null) query.set(...)` ladders in `getBacktests` (`client.ts:156–185`) and the min/max pairs in `BacktestsQueryParams` (`types.ts:751–766`) — a generic param-mapper would halve it.
- **minor (Shotgun Surgery):** `supportsLiveStatsEndpoint` module-global cache (`client.ts:76–77,114–141`) is a second, hand-rolled caching layer next to TanStack Query; feature detection belongs in the query layer.
- **security note:** `deleteDataSymbol(path)` sends a raw server filesystem path from the client (`client.ts:551–554`, fed by `DataSymbolEntry.path`); the frontend trusts it blindly — the backend must whitelist-resolve this path (out of area, but the contract invites path traversal).
- Scanned — nothing else.

### frontend/src/api/queries.ts
- **minor · API-design footgun:** every hook spreads `...options` *last* (`queries.ts:43,56,67,81,95,110,122,137`), so a caller-provided `enabled: false` silently defeats the built-in `enabled:` guards, and defaults (`refetchInterval`, `staleTime`) can be clobbered unintentionally. Works today, but it is an inverted-precedence trap; document or pick explicitly.
- **minor (Duplicated Code):** eight hooks repeat the identical `enabled: Boolean(x) && (options?.enabled ?? true)` + 30 s polling shape (`queries.ts:71–139`) — extract `usePolledQuery(key, fn, dep, opts)`.
- Scanned — nothing else.

### frontend/src/api/types.ts
- **major (see S3):** duplicated metric vocabulary with unstable units: `BacktestSummary.win_rate` undocumented scale (`types.ts:112`), `PortfolioSummary.max_drawdown?` vs `max_drawdown_pct?` both present (`:1120–1121`), `LiveAccountStats.max_drawdown` + `max_drawdown_pct` (`:554–555`), `BacktestListItem.sharpe?` + `sharpe_ratio?` (`:695–696`), `BacktestMetadataItem.total_return` scale vs `BacktestCompareStats.total_return` scale unstated (`:712` vs `:1413`). Consumers compensate with heuristics (see S3 cites).
- **minor (Refused Bequest flavour / weak contracts):** index signatures `[key: string]: any` on `BacktestStats` (`types.ts:380`) and `Record<string, any>` on `BacktestMeta.parameters` (`:133`), `StrategyMeta.tags` (`:11`), `combined_stats.correlation_matrix` (`:1169`) disable checking exactly where payloads are least trustworthy.
- **minor (Data Clumps):** `LiveDeploymentConfig` carries both legacy `signal_source_type?/signal_source_id?/symbol_filter?` and canonical `signal_source/signal_config/settings` (`types.ts:508–513`); `DataDownloadRequest.offer_side` + legacy `price_type` (`:1310–1312`) — versioned field pairs that belong behind one adapter.
- Scanned — nothing else (structure is otherwise clean and comprehensive).

### frontend/src/lib/queryClient.ts
- Scanned — nothing found. Sensible defaults (staleTime 30 s, retry 1, gcTime 5 m).

### frontend/src/utils/stats.ts
- Scanned — nothing found. Correct clamping in `pValueMetric`; `metricValue`'s `unknown` signature is deliberate defensive parsing.

### frontend/src/lib/optimizationEvidence.ts
- **minor:** hardcoded hex palette inline (`optimizationEvidence.ts:54–56,70–72,82–84`) duplicates tone tokens used elsewhere (`ToastProvider.tsx:25–57`) — extract a shared tone map (Duplicated Code, judgement call).
- Positive: `Object.hasOwn` guard against prototype-key lookup (`:33`) is correct and tested.
- Scanned — nothing else. Well-factored, single-purpose module.

### frontend/tests/optimizationEvidence.test.ts
- **minor · maintainability:** the first test asserts *source text* of page files via regex (`optimizationEvidence.test.ts:5–18`) — brittle implementation-string testing that breaks on innocent copy edits; prefer rendering-level checks or drop to a comment.
- **minor:** importing `.ts` directly under `node --test` (`package.json` script `test:optimization-evidence`, `optimizationEvidence.test.ts:21,31,38`) requires a Node version with type stripping — pin/document the minimum Node.
- Positive: alias/degradation/unavailable-state coverage is genuinely good (`:20–85`).

### frontend/src/hooks/useWebSocket.ts
- **minor:** cleanup calls `setIsConnected(false)` / `setStatus("disconnected")` after teardown (`useWebSocket.ts:243–244`) — state updates on an unmounted component; harmless in React 18+, but dead code.
- **minor · API:** `sendJson` silently returns `false` when the socket isn't open (`useWebSocket.ts:255–262`) — callers have no way to learn why a send failed; consider surfacing via status.
- **minor:** reconnect loop has no attempt cap/jitter (`useWebSocket.ts:115–132`) — a downed backend yields a permanent 1–15 s retry hammer from every mounted stream.
- Positive: `onMessageRef` pattern avoids stale closures; socket identity checks (`socketRef.current !== activeSocket`) correctly ignore orphaned sockets; exponential backoff present. Best-written file in the area.

### frontend/src/hooks/liveStreams.ts
- **major · bug:** `data` is derived *only* from the latest envelope (`liveStreams.ts:47–57`); any server message whose `type` is neither `snapshot` nor `update` (heartbeat echo, ack, error envelope) resets `data` to `null`. Consumers like `LiveHub.tsx:371–379` then act on a transient `null` (skipping refresh) and charts flicker empty. Accumulate per-topic state instead of mirroring the last message.
- Scanned — nothing else.

### frontend/src/components/index.ts
- Scanned — nothing found (all six re-exports are consumed somewhere; barrel is tiny enough to be harmless).

### frontend/src/components/Badge.tsx
- **minor (weak typing):** `colorMap: Record<string, string>` (`Badge.tsx:6`) widens away the literal union already declared on the prop — type it `Record<BadgeProps["color"], string>`.
- Scanned — nothing else.

### frontend/src/components/LoadingSpinner.tsx
- Scanned — nothing found.

### frontend/src/components/StatCard.tsx
- **minor · bug:** `currency` format drops cents entirely and hand-formats negatives as `-$1,234` (`StatCard.tsx:21–26`); P&L figures like `$1,234.56` silently render as `$1,235` on a trading dashboard where the cent matters. Use `Intl.NumberFormat("en-US", {style:"currency"})`.
- **minor (Duplicated Code):** inline hex palette `#3fb950/#f85149/#c9d1d9` (`StatCard.tsx:38–41`) repeats across ≥8 files (S4 family).
- Scanned — nothing else.

### frontend/src/components/ToastProvider.tsx
- **minor · bug:** toast IDs are `Date.now() + random(0..999)` (`ToastProvider.tsx:63`) — collisions are possible under bursts, and a colliding id makes the timeout remove the wrong toast; use a counter ref.
- **minor:** timeouts are never cleared on unmount (`ToastProvider.tsx:65–67`) — the provider lives app-long so impact is nil, but the pattern leaks if reused.
- Scanned — nothing else.

### frontend/src/components/StrategiesProvider.tsx
- **minor · semantics:** `loading` exposes `isFetching` (`StrategiesProvider.tsx:28`) — background refetches (window focus is off, but invalidations happen) flip every consumer into "loading"; `isPending && !data` is usually what callers mean.
- **minor:** `ensureLoaded` clears `pendingLoadRef` in both `refresh`'s catch and its own `finally` (`StrategiesProvider.tsx:38–58`) — redundant double-clear, sign of merged refactor paths.
- Scanned — nothing else.

### frontend/src/components/DataTable.tsx
- **minor · bug:** array-index row keys (`DataTable.tsx:97–99`) corrupt row identity/reconciliation on sort/filter — and sorting is this table's core feature.
- **minor · bug:** comparator mixes number/string columns unsafely — `typeof av === "number" ? av - (bv as number)` (`DataTable.tsx:34–37`): a column mixing `"10"` and `10` coerces `bv` wrongly; `null==null → 0` also makes nulls compare equal to anything mid-sort.
- **minor (weak typing):** `as any` on `textAlign` (`DataTable.tsx:78,114`) and `T extends Record<string, any>` (`:19`).
- **minor · a11y:** clickable `<th>` sort headers are not buttons and have no aria-sort (`DataTable.tsx:69–93`).
- Scanned — nothing else.

### frontend/src/components/FilterBar.tsx
- **minor (Speculative Generality):** `filters`/`filterValues`/`onFilterChange` dropdown machinery (`FilterBar.tsx:19–21,148–178`) is used by exactly one page (`StrategiesPage`); everything else passes only `filterPanelContent`. Inline it or accept as deliberate API.
- Scanned — nothing else.

### frontend/src/components/MultiSelectDropdown.tsx
- **minor · UX/bug:** outside-click handler listens only to `mousedown` (`MultiSelectDropdown.tsx:34–44`) — touch taps and programmatic focus-out leave the panel open.
- **minor:** `toggleOption` rebuilds selection from `Set` insertion order (`MultiSelectDropdown.tsx:77–85`), so deselected-then-reselected items jump to the end of the visible order — surprising with long lists.
- **minor · a11y:** combobox pattern has no keyboard support (no Escape-to-close, arrow navigation, or `aria-expanded`) (`MultiSelectDropdown.tsx:87–249`).
- Scanned — nothing else.

### frontend/src/components/Modal.tsx
- **minor · a11y/correctness:** no `role="dialog"`/`aria-modal`, no focus trap, no Escape-to-close, and background scroll is not locked (`Modal.tsx:20–77`). Every destructive flow in the app (bulk delete, deduplicate) rides on this dialog.
- Scanned — nothing else.

### frontend/src/components/RenameModal.tsx
- **minor:** `onSave(trimmed)` result ignored (`RenameModal.tsx:110`); the `Promise<void>` half of the prop type is fiction — either await it and manage pending state internally, or type it sync.
- Scanned — nothing else.

### frontend/src/components/DualRangeSlider.tsx
- **minor · UX:** overlapping native range inputs rely on thumb hit-testing; when `valueMin === valueMax === bounds.min`, the max-thumb is unreachable until min moves (`DualRangeSlider.tsx:119–152`) — known dual-slider pitfall, no mitigation here.
- **minor (Duplicated Code):** `clamp` (`DualRangeSlider.tsx:16–18`) is byte-identical to `RangeFilterControl.tsx:17–19`.
- Scanned — nothing else.

### frontend/src/components/RangeFilterControl.tsx
- **minor (Speculative Generality):** dead code — `RangeFilterControl` is exported but imported nowhere in the app (verified by grep; `DualRangeSlider` covers the use case). Delete (~167 lines).
- Scanned — nothing else.

### frontend/src/components/MultiSelectDropdown.tsx / FilterBar overlap
(covered above)

### frontend/src/components/SymbolInput.tsx
- **minor · bug:** `handleChange` expands matching categories from `expandedCats` captured in the render closure (`SymbolInput.tsx:73–82`) — rapid typing can expand from a stale snapshot; functional update would be safe.
- **minor:** `symbolsQuery.error` is swallowed — a failing `/symbols` endpoint leaves an eternally empty dropdown with no signal (`SymbolInput.tsx:35–49`).
- **minor:** dropdown ignores keyboard entirely (Enter doesn't accept highlighted symbol) and `syms.slice(0,100)` cap is silent beyond the "...more" hint (`SymbolInput.tsx:189–221`).
- Scanned — nothing else.

### frontend/src/components/Sparkline.tsx
- **major · bug (conditional hook):** `drawdowns` `useMemo` sits *after* the early return `if (normalized.length < 2) return null` (`Sparkline.tsx:30` vs `:53–62`). A mount with <2 points (returns null, hooks 1–3 only) followed by a re-render with ≥2 points changes hook order → React throws "Rendered more hooks than during the previous render" and the surrounding page crashes (with S1, white screen). Realistic path: equity previews arrive asynchronously (`BacktestsPage.tsx:712–730`, `LiveHub.tsx:1502–1505`). Move all hooks above the return.
- **minor:** `Math.min(...normalized)`/`Math.max(...normalized)` spread (`Sparkline.tsx:32–33`) risks stack overflow on large arrays; previews are ≤100 today, so latent.
- **minor (dead interaction):** `interactive` tooltips can never show wherever the sparkline sits under a full-card overlay `<Link>` (`BacktestsPage.tsx:1394–1403`, `PortfoliosPage.tsx:88–97`) — the prop is passed but pointer events hit the link. Either stop passing `interactive` there or raise the SVG.
- **minor:** hover date interpolates linearly between start/end dates (`Sparkline.tsx:79–87`) — wrong for trading calendars (weekends/gaps), mislabels points by days.

### frontend/src/components/TaskManager.tsx
- **minor · perf:** unconditional 3 s polling of `/data/tasks` on every page except `/data` (`TaskManager.tsx:32–34`) — no visibilitychange pause, no backoff; combined with `TaskHub` (3 s), `LoggingPage` (5 s), `LiveDetail` (30 s), `LiveViewer` fallback (30 s), a single idle tab generates a constant request drumbeat.
- **minor:** errors are swallowed with an empty catch (`TaskManager.tsx:24–28`) — the widget silently shows stale "0 running" forever after a backend blip; at least flip a stale flag.
- Scanned — nothing else.

### frontend/src/components/LiveTerminal.tsx
- **minor · bug-risk:** `onStatusChange` is in the effect deps (`LiveTerminal.tsx:67`) — parents passing an inline callback (e.g. `ResampleModal.tsx:311`, `SmartHarvestModal.tsx:337`, `StrategyLabPage.tsx:559` with `setJobStatus` — stable — but any future inline lambda) reconnect the websocket on every parent render, truncating accumulated logs (`setLogs("")` at `:26`).
- **minor:** no reconnect/backoff — one dropped connection ends the feed permanently in "disconnected" (`LiveTerminal.tsx:50–62`), unlike `useWebSocket`.
- **minor:** log accumulation is unbounded in state (`LiveTerminal.tsx:38–41`); multi-hour downloads grow the string without cap.
- **minor (Mysterious Name):** status is parsed from log text via regex `[task:...] status=(\w+)` (`LiveTerminal.tsx:42–47`) — protocol-by-convention buried in presentation; a structured message type would be self-documenting.
- Scanned — nothing else.

### frontend/src/components/TradeTable.tsx
- **minor (Duplicated Code):** `toFiniteNumber`/`formatTimestamp`/`formatPrice`/`formatSignedUsd` family (`TradeTable.tsx:35–121`) re-implements the same helpers living in four pages (S4).
- **minor:** column defs rebuild is memoized only on `showSignalColumns` (`TradeTable.tsx:124–282`) — fine, but the giant inline formatter repetition would collapse with ag-Grid `valueFormatter` factories.
- Scanned — nothing else (ag-grid usage, virtualization and `getRowId` are correct).

### frontend/src/components/TradingViewChart.tsx
- **minor · perf:** the data effect tears down and recreates *all* indicator series on every `rangePreset` change (`TradingViewChart.tsx:505–550` — `rangePreset` in deps alongside data); preset switching should only touch `timeScale().setVisibleLogicalRange`.
- **minor:** chart lifecycle effect depends solely on `height` (`TradingViewChart.tsx:384–503`) — locale/theme changes would be ignored; acceptable, but undocumented invariant.
- **minor (Duplicated Code):** `normalizeUnixTime`/`toChartTime`/`formatPrice`/`formatPnl` (`TradingViewChart.tsx:96–179`) duplicate `TradeChartPage.tsx:113–128` and the page-level formatters (S4).
- Scanned — nothing else (marker dedupe, price-line cleanup, ResizeObserver teardown all correct).

### frontend/src/components/CreateBacktestModal.tsx
- **minor · bug:** dead effect — `useEffect(() => { if (!strategy) {...reset...} setStrategyParameters({}); setParameterTypes({}); }, [strategy])` (`CreateBacktestModal.tsx:82–91`) wipes parameter drafts on every strategy change, racing the detail-fetch effect at `:93–111`; when the detail query serves cached data for the *previous* strategy (TanStack cache), parameters briefly populate from the wrong strategy.
- **minor:** `parseFloat(capital)` unchecked (`CreateBacktestModal.tsx:225`) — `capital=""` sends `NaN` → serialized as `null` in JSON body; leverage and params get validation, capital doesn't.
- **minor (see S2):** availability check fans out one `getDataCheck` per symbol per edit without debounce (`CreateBacktestModal.tsx:135–186`).
- **minor (Duplicated Code):** `TIMEFRAME_ORDER`/`TIMEFRAME_LABELS` (`CreateBacktestModal.tsx:14–26`) third private copy of timeframe ordering (S4).
- **minor:** `inputStyle`/`labelStyle` objects rebuilt per render (`CreateBacktestModal.tsx:244–262`) — hoist like `BacktestFromSignalsModal.tsx:23–41` does.
- Scanned — nothing else.

### frontend/src/components/OptimizeModal.tsx
- **minor:** `handleStart` not wrapped in `void` at the button (`OptimizeModal.tsx:701`) — it's an async function attached directly to onClick; a synchronous throw before the try (none today) would produce an unhandled rejection; siblings use `onClick={() => void handleStart()}` (`BacktestsPage.tsx:1653`) — inconsistent convention.
- **minor (Repeated Switch):** int/float/categorical branch pairs recur in `buildRangePayload`, `normalizeChoices`, and the row renderer (`OptimizeModal.tsx:92–137,71–90,458–575`); a small strategy map per kind would centralize.
- **minor:** `gridTemplateColumns` ternary yields identical values for both branches (`OptimizeModal.tsx:473`) — leftover from an edit.
- Positive: `canSubmit` mirror-comment (`:226–235`) documents a previously-fixed enable/disable bug; validation is thorough.
- Scanned — nothing else.

### frontend/src/components/DownloadModal.tsx
- **major (see S2):** Dukascopy coverage refetch per keystroke (`DownloadModal.tsx:314–356`) — `parsedSymbols` memo splits on every comma/character; no debounce/abort.
- **minor · bug:** reset effect depends on `normalizedDefaultTimeframes` *and* its join-key (`DownloadModal.tsx:306–312`) — the array identity changes every render of the parent that passes a fresh literal (e.g. `DataManagementPage.tsx:1183–1190` passes state arrays, fine, but any inline-array caller resets the modal mid-typing).
- **minor:** `handleSubmit` catches as `err: unknown` but the success path calls `onClose()` even if the backend returned a task that instantly failed — acceptable, noted for awareness.
- **minor (Duplicated Code):** the entire "form → LiveTerminal stream → Run another/Hide terminal" chrome is copy-paste across `DownloadModal`, `ResampleModal`, `SmartHarvestModal` (compare `DownloadModal.tsx:1153–1258` ≈ `ResampleModal.tsx:270–370` ≈ `SmartHarvestModal.tsx:298–395`) — extract a `TaskStreamModal`.
- Scanned — nothing else.

### frontend/src/components/ResampleModal.tsx
- **minor:** `catch (err: any)` (`ResampleModal.tsx:96`) — the only `any`-typed catch among the modals; unify with `err: unknown`.
- **minor (see S4):** shares the copy-pasted stream chrome (cite above).
- Scanned — nothing else.

### frontend/src/components/SmartHarvestModal.tsx
- **minor (Speculative Generality):** source `<select>` offers exactly one option, `dukascopy` (`SmartHarvestModal.tsx:179–186`), while the submit path normalizes whatever string arrives (`:89,97`) — placeholder scaffolding for a choice that doesn't exist.
- **minor (see S4):** stream chrome duplication (cite above).
- Scanned — nothing else.

### frontend/src/components/BacktestFromSignalsModal.tsx
- **minor:** processors fetched imperatively per open with local state (`BacktestFromSignalsModal.tsx:62–83`) — a third copy of the "load signal processors" dance (`LiveHub.tsx:334–348`, `PortfoliosPage.tsx:290–300`, `LiveDetail.tsx:147–156`); belongs in `api/queries.ts` (Duplicated Code / Shotgun Surgery).
- **minor:** `signalProcessor` defaults to `"stockkit"` (`BacktestFromSignalsModal.tsx:53`) — a magic string that may not exist in the returned options (fallback logic at `:71–77` handles absence, but the initial select shows a phantom value while loading).
- Scanned — nothing else.

### frontend/src/pages/DataManagementPage.tsx
- **major · checklist (unhandled promise rejections):** `loadOverview` and `loadCategoryDetails` are try/`finally` with **no catch** (`DataManagementPage.tsx:386–406,408–443`); invoked via `void loadOverview()` (`:457`) and the category-name effect (`:465–469`) — a failing `/data/overview` produces an unhandled rejection, `loading` stuck true forever, and a permanently spinning page. Every other page catches; this one doesn't.
- **major · checklist (N+1):** `handleDeleteAllForSymbol` deletes entries strictly sequentially (`DataManagementPage.tsx:677–681`) — N sequential DELETE round-trips with no progress indicator; batch endpoint or `Promise.allSettled`.
- **minor:** `window.confirm`/`window.alert` for destructive ops (`DataManagementPage.tsx:649,664,671,686`) — inconsistent with the styled Modal flows elsewhere (Backtests bulk delete also confirms natively; Portfolios too; Services doesn't confirm at all).
- **minor (Duplicated Code):** `PanelTable` (`DataManagementPage.tsx:1983–2199`) is a second generic sortable/paginated table beside `components/DataTable.tsx` (S4).
- **minor:** background-task poller's `handledTaskRefreshIdsRef` grows unbounded for session lifetime (`DataManagementPage.tsx:355,513–518`) — trivial memory, but unbounded sets deserve pruning.
- **minor:** `TIMEFRAME_ORDER` mixes two naming conventions (`m5` and `4h`, `DataManagementPage.tsx:92–106`) and ranks unknown TFs to 99 — silent mis-sorting for novel timeframes.
- Scanned — nothing else (tree state machine with refs is careful).

### frontend/src/pages/BacktestsPage.tsx
- **minor (Shotgun Surgery/state smell):** module-level mutable cache `backtestsAllCache` / `backtestsAllPromise` (`BacktestsPage.tsx:114–115,274–298`) — a hand-rolled singleton cache beside TanStack Query; untestable, survives HMR oddly, and duplicates what `queryClient` already does.
- **minor · bug:** `normalizeWinRate` guesses units (`value > 1 && <= 100 → /100`, `BacktestsPage.tsx:123–129`) — a legitimate 100% stored as `1.0` is fine, but `1.5` (150%?) becomes `0.015`... the heuristic silently rewrites out-of-range data instead of surfacing it (S3 consequence).
- **minor · perf:** preview effect lists `previewById` in its own deps (`BacktestsPage.tsx:712–730`) — each batch arrival re-runs the effect; the `missingIds` guard prevents refetch but the effect still thrashes; derive missing ids via a memo instead.
- **minor:** 33-entry manual dependency array on `filterPanelContent` (`BacktestsPage.tsx:1107–1147`) — unmaintainable; select the few needed primitives.
- **minor (dead interaction, with Sparkline):** `interactive` sparklines under the absolute overlay `<Link>` (`BacktestsPage.tsx:1394–1403` vs `:1433–1441`) can never show their hover tooltip.
- **minor:** checkbox uses `onClick` + `onChange` both (`BacktestsPage.tsx:1404–1408`) — `stopPropagation` on click is doing the work; harmless but subtle.
- Scanned — nothing else (filter/sort/pager math checked; `buildPageNumbers` correct).

### frontend/src/pages/LiveHub.tsx
- **major · bug (unclickable controls):** in `LiveDeploymentCard` the absolute overlay `<Link>` covers the whole card with `zIndex: 1` (`LiveHub.tsx:114–118`), but the Pause/Stop/Delete buttons beneath have **no positioning/z-index** (`LiveHub.tsx:149–177`) — positioned elements paint above static content, so on a deployed card the three management buttons are likely unreachable (click navigates instead). The sibling pattern proves intent: `BacktestsPage.tsx:1404–1416` gives its checkbox `zIndex: 2`, `PortfoliosPage.tsx:106–119` gives its delete button `position:relative; zIndex:2`. Buttons need `position:relative; zIndex:2` (or the link narrowed).
- **major · checklist (N+1):** `refreshDeployments` fans out 3 requests per account (`stats`, `balances`, `positions`), then a 4th `getLiveTrades` fallback per failing account (`LiveHub.tsx:238–272`) — and this whole fan-out re-runs on **every WebSocket accounts message** (`:371–379`) plus a 30 s fallback poll (`:381–391`). With A accounts that's 3A–4A requests per tick; the WS payload should carry or gate the deltas.
- **minor:** `refreshDeployments`/`refreshStrategies`/etc. guard with loading flags captured in closures (`LiveHub.tsx:302–316,318–332,334–348`) — the classic stale-closure guard; two rapid invocations both see `false` and double-fire. Use refs or TanStack Query dedup.
- **minor (Mysterious Name):** `handleControl` accepts `"play" | "pause" | "stop"` (`LiveHub.tsx:431–441`) but only pause/stop are ever wired — the play branch + `controlBtnStyle("play", ...)` styling (`:1372–1384`) are unreachable (Speculative Generality).
- **minor:** broken aria-label template literal `` aria-label={`Open live account `} `` — interpolation forgotten (`LiveHub.tsx:116`).
- **minor (Duplicated Code):** `calculateRealizedTradeStats` + `asFiniteNumber` + `formatSignedUsd` duplicated verbatim with `LiveDetail.tsx` (S4).
- Scanned — nothing else (settings JSON parsing validated properly at `:503–518`).

### frontend/src/pages/BacktestDetailPage.tsx
- **minor · bug:** stats fallbacks read `stats?.winners` / `stats?.losers` (`BacktestDetailPage.tsx:225–226`) — fields that don't exist on `BacktestStats` (declared `win_rate`, `total_trades`; only reachable through the `any` index signature). If `stats` loads, `winners` is `undefined` → `typeof undefined === "number"` false → falls back to trades-derived counts, so behavior survives by luck; dead/mistyped access.
- **minor · bug-risk:** benchmark rescale divides by `normalizedBenchmark[0].equity` (`BacktestDetailPage.tsx:1053`) without a zero/finiteness guard — a benchmark series starting at 0 renders `Infinity` spikes.
- **minor · perf:** crosshair tooltip scans the whole equity array per mousemove (`BacktestDetailPage.tsx:1110–1116`) — O(n) per event on up to 4000 points; binary-search (already implemented in `TradingViewChart.tsx:189–212` — another duplication).
- **minor (see S4):** `EquityChart` is a second hand-rolled lightweight-charts wrapper duplicating `PortfolioDetailPage`'s.
- **minor:** `catch (renameIssue: any)` (`BacktestDetailPage.tsx:180`), `benchmarkData` typed `any[]` (`:84`), `(eqVal as any).value` (`:1107`).
- **minor:** trades effect races on fast page clicks — no `active` guard like other pages (`BacktestDetailPage.tsx:117–132`), so an earlier slow response can overwrite a newer page's rows.
- Scanned — nothing else.

### frontend/src/pages/TradeChartPage.tsx
- **minor · bug:** `window.history.replaceState(...)` for trade navigation (`TradeChartPage.tsx:273–279,478–483`) bypasses React Router — the router's location state desyncs (back/forward behave oddly, and any subscriber to `useLocation` won't re-render). Use `navigate(..., {replace:true})`.
- **minor · UX/global side effect:** window-level keydown hijacks ArrowLeft/Right/**Space**/Escape across the whole app while mounted (`TradeChartPage.tsx:529–566`) — Space toggles playback even when focus is on a button (button activation also fires), and Escape *navigates away from the page*. Should require the viewport to contain focus or be scoped to the fullscreen element.
- **minor:** download-status poll effect restarts on every status change and reloads the **entire replay payload** when a task completes (`TradeChartPage.tsx:568–597`) — heavy but correct; a targeted OHLCV refetch endpoint would fit better.
- **minor (Duplicated Code):** `sanitizeOHLCVBars`/`sanitizeIndicatorPoints`/`normalizeUnixTime` (`TradeChartPage.tsx:113–184`) parallel `TradingViewChart`'s normalization (S4).
- **minor:** `indicatorIsPanel` name-token heuristic list (`TradeChartPage.tsx:205–229`) — classification by substring matching (`name.includes("osc")`) will misclassify indicators like "Momentum Oscillator Overlay"; the server already sends `type`/`pane`.
- Scanned — nothing else (keyboard guard against inputs is correctly implemented at `:531–538`).

### frontend/src/pages/LiveViewer.tsx
- **minor · bug-risk:** `decodeURIComponent(rawAccountId ?? "")` (`LiveViewer.tsx:82`) — react-router already decodes params; a second decode throws `URIError` on a literal `%` in the id → with S1, white screen. Same pattern in `LiveDetail.tsx:56`.
- **minor · fragility:** `queryClient.setQueryData(["live-signal-log", accountId, 200], ...)` (`LiveViewer.tsx:155`) hardcodes the limit `200` to match `useLiveSignalLog`'s default — rename/change the default elsewhere and the WS cache silently stops feeding the UI (Shotgun Surgery via stringly-typed cache keys).
- **minor:** `headerStats.dailyPnlPct` divides by `max(1, balance - dailyPnl)` (`LiveViewer.tsx:292–293`) — for balances < $1 the percentage explodes; guard meaningful bases only.
- **minor:** exit markers plot at `signal_time` rather than execution time (`LiveViewer.tsx:622–631`) — exits appear at signal bars, visually offset from actual fills; `execution_time` is available on the row.
- **minor (Duplicated Code):** `asFinite`/`formatUsd`/`formatSignedUsd`/`formatTableTime` again (S4).
- Scanned — nothing else (chart teardown, marker/primitive cleanup correct; SL/TP price lines cleaned properly at `:750–756`).

### frontend/src/pages/TradeChartPage.tsx — see above

### frontend/src/pages/PortfoliosPage.tsx
- **minor:** `createDisabled` only checks name/rows (`PortfoliosPage.tsx:475`); per-row strategy/symbol validity surfaces only as post-submit toasts from thrown errors (`:348–349`) — enable-disable should reflect row validity.
- **minor:** `parseOptionalJson` errors lack row context (`PortfoliosPage.tsx:350–360` calls it inside `map`) — "JSON must be an object" doesn't say *which component's* Risk Config is bad.
- **minor (dead interaction, with Sparkline):** `interactive` sparkline under the overlay link (`PortfoliosPage.tsx:88–97,134`).
- **minor:** `Remove Empty` has no confirmation and no count preview, unlike Backtests' guarded variant (`PortfoliosPage.tsx:453–463` vs `BacktestsPage.tsx:850–869`) — inconsistent destructiveness.
- **minor (Duplicated Code):** duplicates/deduplicate/bulk-delete modal block mirrors `BacktestsPage.tsx:1694–1762` nearly line-for-line (S4).
- Scanned — nothing else (sort-direction toggle verified correct).

### frontend/src/pages/PortfolioDetailPage.tsx
- **minor · robustness (with S1):** contribution cells dereference required-typed fields directly — `row.return_contribution * 100`, `row.drawdown_contribution * 100`, `row.win_rate.toFixed(2)` (`PortfolioDetailPage.tsx:718–729`) — if the API omits any (they're optional in spirit; sibling fields use `?? 0`), the page throws. Defensive reads cost nothing here.
- **minor (see S4):** `PortfolioEquityChart` duplicates `BacktestDetailPage.EquityChart`.
- **minor:** trades filter effect refetches on every filter change without debounce/abort (`PortfolioDetailPage.tsx:156–176`) — mild, filters are selects.
- Scanned — nothing else.

### frontend/src/pages/LiveDetail.tsx
- **major · bug (infinite refetch):** the signal-processors effect keys on the very flag it toggles: `if (!replayModalOpen || signalProcessorsLoading) return; … setSignalProcessorsLoading(true); getSignalProcessors()…finally(setSignalProcessorsLoading(false))` with deps `[replayModalOpen, signalProcessorsLoading]` (`LiveDetail.tsx:147–156`). Opening the modal starts fetch → loading flips true→false → effect re-runs → fetch again → forever (a tight request loop for as long as the modal is open). Fetch-on-open needs an "already loaded for this open" ref or a stable boolean dep.
- **minor:** slippage-PnL model `-slippage * qty * directionFactor` (`LiveDetail.tsx:176–177`) treats negative slippage as gain symmetrically — fine — but metadata `pnl` wins even when zero (`Number.isFinite(0)`) possibly masking computed fallback; verify backend semantics.
- **minor (see S4):** `calculateRealizedTradeStats`/helpers duplicated with LiveHub (cite above).
- **minor:** 30 s poll continues in hidden tabs (`LiveDetail.tsx:137–139`).
- Scanned — nothing else.

### frontend/src/pages/MatrixGridPage.tsx
- **minor (anti-pattern):** `setPage(safePage)` called *inside* `useMemo` (`MatrixGridPage.tsx:326–331`) — a render-phase state update from a memo; works only because React retries the render, and it will warn under StrictMode double-render. Clamp at usage sites instead.
- **minor (Duplicated Code / Shotgun Surgery):** local `MatrixRow`/`StrategySummary`/`BlockedStrategy` interfaces (`MatrixGridPage.tsx:11–98`) duplicate `api/types.ts:795–925` (`MatrixGridRow`, `MatrixGridStrategySummary`, `MatrixGridBlockedStrategy`) instead of importing them — two schemas to keep in sync by hand.
- **minor:** German UI copy throughout (`MatrixGridPage.tsx:361–377,391,416,443,451,484,544,619,673,709,786,791`) while the rest of the app is English — locale consistency (judgement call, but user-facing).
- **minor:** grid view ignores the global sort/filters beyond strategy selection (`MatrixGridPage.tsx:500`) — filteredRows computed but `selectedStrategyRows` feeds the grid; symbol/timeframe/status filters silently don't apply in Grid mode.
- Scanned — nothing else.

### frontend/src/pages/StrategyDetailPage.tsx
- **minor (Primitive Obsession / weak typing):** `detail` is `any` (`StrategyDetailPage.tsx:12`), backtest cards `any` (`:590`), portfolios `any` (`:713`) — `getStrategyDetail` already returns `StrategyDetail`; the types exist and are simply unused here.
- **minor (Duplicated Code):** two ~60-line near-identical `ReactMarkdown` component maps (`StrategyDetailPage.tsx:341–398` and `:441–553`) — extract one `markdownComponents` const.
- **minor · compat:** react-markdown v10 no longer passes an `inline` prop to `code` renderers (`StrategyDetailPage.tsx:367,507` — typed `any` to suppress); with v10 the `inline` branch is dead and fenced-code styling relies on the `pre` override; verify rendering or migrate to the v9+ `pre`/`code` component contract.
- **minor:** markdown rendered from `documentation_markdown`/`notes` — safe by default (no `rehype-raw`, so no raw HTML injection; checklist XSS item satisfied — positive note, no `dangerouslySetInnerHTML` anywhere in the area).
- Scanned — nothing else.

### frontend/src/pages/LoggingPage.tsx
- **major (see S2):** every search keystroke triggers a 5000-line refetch (`LoggingPage.tsx:230–262`), stacked on the unconditional 5 s poll — the heaviest request path in the app.
- **minor · bug:** expanded-row identity includes the array index — `` rowId = `${index}:${timestampLong}:${module}` `` (`LoggingPage.tsx:305,366`) — as new lines arrive and shift indices, expansion state attaches to different rows.
- **minor:** `warned` flag inside the effect (`LoggingPage.tsx:231,247–250`) only suppresses the toast per-effect-instance; the 5 s poll re-runs `loadLogs` but `warned` persists per mount — OK, but the flag lives across file switches confusingly.
- **minor:** level/module filtering happens client-side over the fetched window while `levelParam`/search go to the server (`LoggingPage.tsx:225–228,279–298`) — split-brain filtering; deselecting INFO still costs full transfer when the server could filter.
- Scanned — nothing else (virtualizer usage with measured rows is correct).

### frontend/src/pages/StrategyLabPage.tsx
- **minor:** `catch (err: any)` (`StrategyLabPage.tsx:93`).
- **minor (Mysterious Name):** `jobStatus` doubles as both task state and UI phase ("idle"/"starting"/websocket-parsed statuses/"failed") (`StrategyLabPage.tsx:49,72,94`) — conflates transport state with task state (LiveTerminal owns the latter).
- Scanned — nothing else.

### frontend/src/pages/OptimizationResultsPage.tsx
- **minor (Duplicated Code):** `formatComparisonValue`/`formatMode`/`formatSymbols` duplicated with `OptimizationsPage.tsx` (S4); `best_params` chips markup duplicated too (`OptimizationResultsPage.tsx:373–391` ≈ `OptimizationsPage.tsx:329–347`).
- **minor · UX:** while `loadedId !== id` the page shows a full-screen loader even when displaying a *previous* optimization is possible — deliberate, but the derived `loading` (`OptimizationResultsPage.tsx:56`) means back/forward between two optimizations flashes blank each time.
- **minor:** `topTrialColumns` builds `trial_id` label via `#${row.trial_id}` render (`OptimizationResultsPage.tsx:88–92`) while `DataTable` sorts the underlying string `"#12"` lexically — trial ordering under sort is 1,10,11,2…
- Scanned — nothing else (OOS evidence banner integration is exemplary).

### frontend/src/pages/OptimizationsPage.tsx
- **minor (Repeated Switch / S3):** `target_metric === "net_profit" ? "total_pnl" : …` inline aliasing (`OptimizationsPage.tsx:199`) re-implements `resolveOptimizationMetricKey` from `lib/optimizationEvidence.ts` instead of calling it.
- **minor:** single-shot fetch with no refresh affordance (`OptimizationsPage.tsx:38–48`) — new Optuna runs require a full remount; siblings use TanStack Query.
- Scanned — nothing else.

### frontend/src/pages/Services.tsx
- **major · UX/data-loss:** `Delete` performs the destructive `deleteService` with **no confirmation** (`Services.tsx:62–70,269–271`) — every other destructive flow in the area confirms (native or modal). One misclick removes a persistent bot/scheduled job.
- **minor:** cron expression accepted as free text with no validation or human-readable preview (`Services.tsx:135–141`); "Next Run: cron-based" placeholder (`:241`) suggests the computation was planned and skipped.
- **minor:** `disabled={statusMutation.isPending}` disables *all* services' Pause/Start buttons while any one mutation is in flight (`Services.tsx:262`) — per-row pending needed.
- Scanned — nothing else.

### frontend/src/pages/TaskHub.tsx
- **minor (Duplicated Code):** active-status set + polling skeleton duplicated with `TaskManager.tsx` (`TaskHub.tsx:15–17` ≈ `TaskManager.tsx:8`), and the toast-on-transition effect (`TaskHub.tsx:103–119`) is logic the TaskManager widget would benefit from too.
- **minor:** `categoryIcon` maps `resample` and `backtest` to the same 📊 emoji (`TaskHub.tsx:46–50`) — indistinguishable at a glance (trivial).
- Scanned — nothing else (status-transition dedupe via `statusRef` is well done).

### frontend/src/pages/ComparePage.tsx
- **minor:** effect dep `ids.join(",")` (`ComparePage.tsx:105`) — works, but violates exhaustive-deps conventions the repo's ESLint enforces; memoize the ids string.
- **minor:** delta coloring marks any non-zero non-improving delta red even when the delta favors neither side meaningfully (`ComparePage.tsx:232–234`) — cosmetic.
- **minor (Duplicated Code):** `formatMetric`/`formatMetricRaw` pair (`ComparePage.tsx:41–69`) joins the signed-formatting pile (S4).
- Scanned — nothing else (equity overlay merge-map is correct).

### frontend/src/pages/StrategiesPage.tsx
- **minor (Repeated Switch):** seven-way sort `switch` (`StrategiesPage.tsx:161–180`) — a `Record<SortKey, comparator>` map matches the repo's own data-driven style elsewhere.
- **minor:** no error/empty distinction — query failure renders the same "empty grid minus cards" as zero strategies (`StrategiesPage.tsx:118–120,231–256` shows Loading then nothing; `useStrategies` error state unread).
- Scanned — nothing else.

### frontend/src/pages/LoggingPage.tsx / others already cited

### Files with nothing (further) to report
- `frontend/src/lib/queryClient.ts` — scanned, nothing found.
- `frontend/src/utils/stats.ts` — scanned, nothing found.
- `frontend/src/hooks/useWebSocket.ts` — minors only (listed).
- `frontend/src/components/LoadingSpinner.tsx` — scanned, nothing found.
- `frontend/src/components/index.ts` — scanned, nothing found.
- `frontend/src/api/types.ts`, `client.ts`, `queries.ts` — findings listed above; otherwise structurally sound.

---

## Security summary (TS/React checklist)
- **XSS:** no `dangerouslySetInnerHTML` anywhere; `react-markdown` used without `rehype-raw` (safe-by-default). ✅
- **Error boundaries:** absent (S1) — the biggest reliability gap. ❌
- **Unhandled rejections:** `DataManagementPage` (major), assorted `void fn()` sites are fine because callees catch; `TaskManager`, `MatrixGrid` catch. ⚠️
- **Stale closures:** largely avoided (refs used in hot paths); residual cases in `SymbolInput.tsx:73–82`, `LiveHub.tsx:302–348`. ⚠️
- **N+1 fetches:** `LiveHub` fan-out, `DataManagementPage` sequential deletes, unbounced input effects (S2). ❌
- **Secrets/config:** committed `.env` with a Sentry DSN for wiring that no longer exists (`frontend/.env`, `main.tsx`) — rotate or remove. ⚠️
- **Trust boundaries:** raw server filesystem paths round-tripped through the client (`deleteDataSymbol`, ResampleModal `source_path`, BacktestFromSignalsModal `filepath`) — backend must validate; frontend should treat them as opaque. ⚠️

## Maintainability hot spots (ranked)
1. `pages/LiveHub.tsx` (1,730) — data orchestration + presentation + domain math in one file.
2. `pages/DataManagementPage.tsx` (2,199) — largest file, contains its own table framework; missing error handling on its two core loaders.
3. `api/types.ts` (1,455) — central contract undermined by duplicated metric names/units (S3).
4. Helper duplication web (S4) — ~10 helper families × 3–5 copies.
5. `BacktestsPage.tsx` (1,835) — module-singleton cache + 33-dep memo array.

## Assumptions stated
- Backend behavior (path whitelisting, `win_rate`/`total_return` scales, `stats.winners`) inferred from frontend types only; findings about those are flagged as contract risks rather than proven bugs.
- The brief's claim of Sentry wiring was tested empirically (grep + dependency list) and reported as absent rather than assumed.

---
## Finding counts by file-group
Counts below aggregate every per-file bullet above (systemic findings S1–S5 counted once each, at their primary citation site).

| Severity | Count |
|---|---|
| Critical | 0 |
| Major | 17 |
| Minor | 150 |

(Counts include every per-file bullet above plus the five systemic findings S1–S4 as major and S5 as minor; total 167.)
