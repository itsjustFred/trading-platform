# Frontend Area Review — Reviewer r1 (panel-ox-alpha-2026-08-24)

- **Scope**: all 61 tracked code files under `frontend/` (pages, components, api, hooks, lib, utils, tests, config). Every file was read in full; every file is accounted for below.
- **Method**: blind and independent — only `frontend/**` sources were read; nothing under `reports/` was consulted. Read-only: no files were edited.
- **Stack observed**: React 19 + Vite 7 + TypeScript, TanStack Query v5, react-router v7, lightweight-charts, recharts, ag-grid community, Tailwind v4 (`@theme` in `index.css`) mixed with heavy inline styles, node:test for the single unit-test file.
- **Severity scale**: `critical` = core functionality broken / data-loss / security hole. `major` = incorrect data shown or sent, crash risk in production paths, request floods, or high-cost structural debt. `minor` = robustness, a11y, consistency, small smells.

---

## 0. Executive summary

The frontend is feature-rich and mostly idiomatic modern React, but it has four systemic problems that account for most of the risk:

1. **No error boundary anywhere and no Sentry** — despite the task brief stating "Sentry wired via src/main.tsx", there is **no Sentry SDK import in the entire tracked frontend** (only unused `VITE_SENTRY_DSN` / `VITE_SENTRY_ENVIRONMENT` declarations in `src/vite-env.d.ts`, which is not even part of my file list). Several pages contain unguarded property chains that throw on malformed/legacy API payloads; with no boundary, any of them whitescreens the SPA silently.
2. **Massive copy-paste of numeric/date/chart helpers** — `asFiniteNumber`/`toFiniteNumber`/`metricValue` exist in 5 files, `formatUsd`/`formatSignedUsd`/`formatPercent` in 4, `resolveChartLocale` in 3, the lightweight-charts bootstrap block (createChart + dark layout + ResizeObserver + fitContent) is re-implemented in 4 pages, and — worst — the **realized-P&L FIFO accounting engine is duplicated verbatim (~70 lines of subtle financial logic) in `LiveHub.tsx` and `LiveDetail.tsx`**, already having drifted (`toWinRatePercent` signatures differ).
3. **Unguarded fetch cascades** — `LiveHub` refires a 3N+3-request fan-out on *every* WebSocket message; `DownloadModal` and `CreateBacktestModal` fire provider/data-check requests on *every keystroke*; `LoggingPage`, `TaskManager`, `TaskHub`, `LiveDetail` poll unconditionally regardless of tab visibility or error state.
4. **One critical interaction bug**: on the Live Hub, an absolutely-positioned overlay `<Link>` paints above the PAUSE/STOP/DELETE buttons of every deployment card, so those live-trading controls are **unclickable** — and no other screen offers pause/stop.

Metric-unit conventions (fraction vs percent vs ×100) are untyped and inconsistent across ~8 screens (Primitive Obsession), which is the root cause of several display bugs noted below.

---

## 1. Per-file findings

Conventions: `(a)` bugs/correctness, `(b)` security, `(c)` Fowler baseline smells, `(d)` maintainability. Line numbers refer to current working tree.

---

### frontend/src/App.tsx

- **(a) [major] No error boundary in the entire tree.** `App.tsx:96` wraps routes in `<Suspense>` only. A render-time exception in any page (several identified below, e.g. `OptimizationResultsPage.tsx:250`, `PortfolioDetailPage.tsx:729`) unmounts the whole React tree with no recovery UI and no reporting. This is the TS-checklist item "missing error boundaries" and it applies to the root of the app.
- **(a) [minor]** Lazy-chunk load failure (deploy hash mismatch after redeploy, offline nav) rejects the lazy import; without a boundary this also whitescreens with only a console error (`App.tsx:7-73`).
- **(c) [minor] Speculative Generality**: the shell-level `TaskManager` slot (`App.tsx:83-95`) is rendered on every page except `/data`; it is a permanent 3-second poller bolted into the layout (see TaskManager below) — presentation concern embedded at routing level.
- **(d)** Route table is clean; lazy-wrapper boilerplate is verbose but conventional. Hot spot: this file is the only place that knows about every page module, which is fine, but adding a page requires touching `App.tsx` + `Sidebar.tsx` (Shotgun Surgery, mild).

### frontend/src/Sidebar.tsx

- **(c) [minor] Speculative Generality**: permanently disabled `/accounts` nav entry (`Sidebar.tsx:32`) with no route behind it — dead navigation surface shipped to users ("Coming soon").
- **(a)** NavLink active-state matching uses prefix semantics of react-router (`isActive` on `/live` also matches nothing problematic here since routes are flat) — no issue found.
- **(d)** Scanned; otherwise nothing found.

### frontend/src/api/client.ts

- **(a) [minor] `fetchJSON` assumes every response has a JSON body** (`client.ts:111`). `deleteDataSymbol`, `deleteService`, `cancelDataTask` etc. will throw `SyntaxError: Unexpected end of JSON input` if the backend answers `204 No Content` or an empty body — the thrown error is not an `ApiError`, so callers' `err.message` shows a parser message instead of the outcome. Fragile contract.
- **(a) [minor]** `Content-Type: application/json` is forced onto **all** requests including bodyless GETs and POSTs (`client.ts:92-95`); harmless today but precludes future multipart/form endpoints without editing the shared client.
- **(a) [minor]** `getServiceLogs(... lines)` / `getLogContent(... lines)` use truthiness `if (lines)` (`client.ts:369`, `client.ts:527`) — requesting `lines=0` is silently coerced to "server default". Should be `!= null` like the neighboring params.
- **(a) [minor]** Module-level mutable singletons `liveStatsSupported` / `liveStatsSupportPromise` (`client.ts:76-77`) cache the OpenAPI capability probe **forever per page-load**: if the backend gains `/live/stats/*` after first probe (rolling deploy), the UI stays in fallback mode until reload; conversely the probe result is shared across all components (hidden global state — Fowler's global-data smell).
- **(b) [minor]** `compareBacktests`/`comparePortfolios` interpolate ids without `encodeURIComponent` (`client.ts:255`, `client.ts:516`) while every other path param is encoded. Ids are server-generated today; the inconsistency invites a future injection/parse bug.
- **(c) [minor] Middle Man / Duplicated Code**: `getStrategy` vs `getStrategyDetail` (`client.ts:145-148`) and `getPortfolio` vs `getPortfolioDetail` (`client.ts:456-458`) are identical delegates; pick one name.
- **(c) [minor] Speculative Generality**: `getBacktestsAll(name, options)` — the `options.includeEquityPreview`/`previewLimit` branch (`client.ts:186-197`) is never used by any caller (BacktestsPage always calls with `name` only).
- **(d)** The 26-line repeated `query.set(...)` ladders (`getBacktests`, `getBacktestsAll`, `getLogs`…) could collapse to an object-to-URLSearchParams helper; currently every new query param is a Shotgun-Surgery edit in this file.

### frontend/src/api/queries.ts

- **(a) [minor]** In `useStrategyDetail`/`useLiveQuotes`/etc., the caller's `options` object is spread **after** the computed `enabled` guard (`queries.ts:54-57` and siblings): a caller who passes `enabled: true` while `name` is empty overrides the `Boolean(name)` guard and fires a request with an empty key segment. Latent; current callers pass constants so it doesn't fire.
- **(a) [minor] Magic-number coupling**: `useLiveSignalLog(accountId, 200)` default must stay byte-identical to the cache key injected by the WebSocket in `LiveViewer.tsx:155` (`["live-signal-log", accountId, 200]`). Changing either silently breaks stream→cache hydration. Should be an exported constant.
- **(c) [minor] Repeated configuration**: the `refetchInterval: 30_000, staleTime: 30_000` pair is copy-pasted across five hooks; a `LIVE_POLL` constant (or shared options factory) would remove the drift risk.
- **(d)** Otherwise scanned; nothing found. These hooks are the right abstraction and several pages ignore them in favor of hand-rolled `useEffect` fetching (see per-page notes) — the codebase pays for both patterns.

### frontend/src/api/types.ts

- **(c) [minor] Primitive Obsession / weakened contracts**: `tags: Record<string, any>` (`types.ts:11`), `parameters: Record<string, any>` (`types.ts:133`), `BacktestStats[..., any]` index signature (`types.ts:380`), `correlation_matrix: Record<string, any>` (`types.ts:1169`). The index signature on `BacktestStats` means typo'd stat keys (`stats.sharpe_ration`) typecheck.
- **(c) [minor] Duplicated Code / Data Clumps**: four parallel representations of "one backtest" exist — `BacktestSummary` (100), `BacktestMeta` (116), `BacktestInfo` (242), `BacktestListItem` (680), `BacktestMetadataItem` (707) — with overlapping-but-different metric names (`max_drawdown_pct` vs `max_drawdown`, `net_profit_pct` vs `total_return`). This type-level drift is the direct cause of the fraction-vs-percent bugs flagged on BacktestsPage / PortfolioDetailPage / ComparePage.
- **(c) [minor] Mysterious Name / duplication**: `BacktestsQueryParams` carries both `drawdown_min/max` **and** `max_drawdown_min/max` (`types.ts:763-766`), and `getBacktests` serializes both (`client.ts:179-182`). Which one does the backend honor?
- **(d)** File is otherwise a competent hand-written API surface; consider generating from OpenAPI (the frontend already probes `/openapi.json` in `client.ts`, so the schema exists).

### frontend/src/components/BacktestFromSignalsModal.tsx

- **(a) [minor] Mount-frozen date range**: `const range = useMemo(() => defaultDateRange(), [])` (`:49`) computes "last 30 days" once per mount; reopening the modal days later (SPA long-lived) resets dates to the stale mount-time window. Recompute on `open` instead (the effect at `:62-83` already re-seeds from it).
- **(b) [minor]** Free-text **server filesystem path** input for CSV signals (`filepath`, `:141-146`, placeholder `/home/ubuntu/data/signals/...`). The client can't validate, but this hands arbitrary path strings to a backend runner; ensure the backend confines it (out of frontend scope, flagged for the panel).
- **(a) [minor]** `signalProcessor` defaults to `"stockkit"` (`:53`) and the open-effect only overrides it if the processors list contains it (`:71-77`); if the fetch fails, the form silently submits a possibly-nonexistent processor id.
- **(d)** Scanned; otherwise consistent with sibling modals.

### frontend/src/components/Badge.tsx

- Scanned — **nothing found**. Small, correct, relies on theme tokens defined in `index.css`.

### frontend/src/components/CreateBacktestModal.tsx

- **(a) [minor] N+1 fetch burst per keystroke**: the availability effect (`:135-186`) runs `Promise.all(symbolList.map(getDataCheck))` on every change of `symbolList`/`timeframe`/`start`/`end` with **no debounce**. Typing "EURUSD" char-by-char issues up to 6×N checks; paste of 20 symbols fires 20 concurrent checks per edit. Add 300–500 ms debounce (the codebase already has the pattern in `BacktestsPage.tsx:596-601`).
- **(a) [minor] Dead constants / mismatched option list**: `TIMEFRAME_ORDER` includes `tick, s1, 1w` (`:14`) but the Timeframe `<select>` offers only `1m..1d` (`:332-339`), so `getModelingTimeframes` has unreachable branches and the canonical-order comment ("matches shared/constants.py") describes a list the UI can't produce.
- **(a) [minor]** `capital: parseFloat(capital)` (`:225`) sends `NaN`→`null` when the field is cleared (no validation, unlike leverage below it); `start > end` is never validated.
- **(a) [minor]** Parameter harvesting drops non-number/non-string defaults: booleans and `null` parameter values in `detail.parameters` are skipped (`:100-108`), so strategies with boolean switches lose those inputs silently in the form.
- **(c) [minor] Duplicated Code**: engine auto-detect, parameter-type mapping, and the `inputStyle`/`labelStyle` objects are near-copies of `PortfoliosPage`/`BacktestFromSignalsModal` style constants — a shared `formStyles.ts` would remove ~5 copies app-wide.
- **(d)** Effect chain (`preselectedStrategy` → `strategy` → detail query → parameters) is order-dependent and resets user-edited parameters whenever the detail query refetches (`:93-111` overwrite on every `data` identity change, e.g. window refocus is disabled so mainly on remount) — acceptable but brittle.

### frontend/src/components/DataTable.tsx

- **(a) [minor]** Rows keyed by array index (`key={i}`, `:99`): after a sort, React reuses row DOM across different data; harmless today (rows are stateless) but an anti-pattern that bites the moment rows gain internal state or animations.
- **(a) [minor]** Sort comparator returns `0` whenever either value is `null/undefined` (`:33`), making nulls order-dependence arbitrary under `Array.sort` stability — acceptable, but the visual "sorted" arrow claims an ordering it doesn't guarantee.
- **(c) [minor]** `as any` casts on `textAlign` (`:78`, `:114`) defeat the union type that exists two lines up.
- **(d)** Generic table is fine for the 10-row trial leaderboard it serves.

### frontend/src/components/DownloadModal.tsx

- **(a) [major] Coverage request storm**: the Dukascopy genesis-coverage effect (`:314-356`) fires `getDukascopyGenesis(parsedSymbols, selectedTimeframes)` on **every keystroke** in the symbols field (deps include `parsedSymbols`/`requestedTimeframes`, both derived from the raw input string). This endpoint proxies an external provider (Dukascopy) — each keystroke costs a backend round-trip and potentially an upstream call. Needs debounce + abort of in-flight (`cancelled` flag only prevents state writes, not the fetch).
- **(a) [minor] No double-submit guard**: `handleSubmit` (`:425-485`) has no `running` state; the footer button (`:1132-1149`) is never disabled — a double-click queues two download tasks. (Sibling modals share this; see Resample/SmartHarvest.)
- **(a) [minor] Status via log scraping**: completion detection depends on `LiveTerminal` regex `[task:<id>] status=(\w+)` over streamed log text (`:42` in LiveTerminal; consumed at `:358-366` here). If the backend log format changes, `onTaskFinished` never fires and the modal shows "running" forever. A structured WS status frame would decouple this.
- **(c) [minor] Duplicated Code**: the form-reset effect (`:284-312`), terminal-header/footer chrome, and `resetStream` are structurally identical to `ResampleModal` and `SmartHarvestModal` — a third of each modal is shared boilerplate that lives in three places.
- **(b)** Nothing found (no injection sinks; datalist values are static).
- **(d)** 1313 lines for one modal — the largest component in the area. The coverage-table section (`:914-1085`) is a self-contained feature that deserves extraction.

### frontend/src/components/DualRangeSlider.tsx

- **(a) [minor]** Two full-width stacked `<input type=range>`s: when `valueMin === valueMax` both thumbs sit at the same pixel; the bottom (max) thumb occludes the top (min) one, and the only way to move min inward is dragging max away first. Known dual-slider tradeoff; worth `pointer-events` zoning.
- **(c)** Clean, small, pure. `clamp`/`normalizeStep` are duplicated in spirit with `RangeFilterControl.clamp` (which is dead code — see below).
- **(d)** Scanned; otherwise nothing found.

### frontend/src/components/FilterBar.tsx

- Scanned — **nothing found.** Simple controlled component; the `filterPanelContent` slot is what makes BacktestsPage's dependency-list monster necessary (flagged there).

### frontend/src/components/LiveTerminal.tsx

- **(a) [major] Hand-rolled second WebSocket implementation with no reconnect**: opens a raw `WebSocket` (`:31`) with zero retry/backoff/heartbeat, while `hooks/useWebSocket.ts` in the same repo implements reconnect + exponential backoff + ping/pong. Any transient drop (laptop sleep, proxy hiccup) permanently kills the task feed mid-download with status stuck at "disconnected"; the user must close/reopen the modal. Should delegate to the existing hook (Duplicated Code + missing resilience).
- **(a) [minor] Unbounded log buffer**: every WS message is appended to a single state string forever (`:40`); a multi-hour download accumulates megabytes of string reallocations per message (O(n) copy each). Cap or ring-buffer.
- **(a) [minor]** Cleanup closes the socket but leaves `onclose` attached (`:55-62`, `:64-66`): after unmount, the close event still invokes `setStatus`/parent `onStatusChange` — benign in React 18+ (no-op warning gone) but it fires a parent `setState` for an unmounted subtree path.
- **(c) [minor] Mysterious Name**: status is parsed out of free text (`:42`) — the "protocol" is log folklore; named status frames would fix DownloadModal's fragility too.
- **(d)** Tailwind classes (`font-mono text-green-400 bg-gray-900 …`, `:160`) mixed into an otherwise inline-style file — two styling systems in one component.

### frontend/src/components/LoadingSpinner.tsx

- Scanned — **nothing found.**

### frontend/src/components/Modal.tsx

- **(a) [minor] Accessibility/UX gaps**: no `Escape`-to-close, no focus trap, no initial-focus management, no body-scroll lock (`:20-77`). Long modals (Optimize, Download) leave page scroll live behind the overlay. Several parents pass guarded `onClose` (e.g. BacktestsPage blocks close while deduplicating) — the guard belongs in the Modal as an `onCloseRequest` veto, not re-implemented per caller.
- **(a) [minor]** Backdrop click closes instantly even while a submit request is in flight (no `busy` concept); combined with the missing double-submit guards in the data modals this lets users abandon a starting task unknowingly.
- **(c)** Fine as a presentational middle layer; no Middle Man abuse.

### frontend/src/components/MultiSelectDropdown.tsx

- **(a) [minor]** Outside-click handling listens to `mousedown` only (`:34-44`): on touch devices tapping outside doesn't close the panel (no mousedown synthesized on iOS Safari in some flows). Also no `Escape`/focus-return.
- **(a) [minor]** `selectedPreview` shows only options that exist in `normalizedOptions` (`:69-75`): if a previously-selected value is no longer offered (filter set changed), the count shows `(3)` but the preview omits the ghost values — mildly confusing.
- **(d)** Scanned; otherwise clean and reusable.

### frontend/src/components/OptimizeModal.tsx

- **(a) [minor] Unit-confusing label**: "Train / Test Split %" with accepted range `0.1–0.95` and default `0.7` (`:172`, `:652-661`). A user entering `70` (the natural reading of "%") gets the error "must be between 0.1 and 0.95". Rename to "Train split (fraction)" or divide by 100.
- **(a) [minor] Draft-wipe edge case**: the reset effect depends on `parameters` (`:176-202`); `BacktestDetailPage` passes `info?.parameters || {}` — a **new** `{}` identity on every render while `info` is null. Opening the modal in that state re-runs the effect each parent render, wiping user-typed ranges mid-edit. Guard by `open` transition or stabilize the prop.
- **(c) [minor] Duplicated Code / dead branch**: `gridTemplateColumns: isCategorical ? "240px minmax(0, 1fr)" : "240px minmax(0, 1fr)"` (`:473`) — both arms identical.
- **(c)** Positive note: the `canSubmit` comment block (`:225-235`) documents a past bug fix; good practice.
- **(d)** Validation is submit-time-only (parse errors appear after clicking Start); acceptable, but the configured-count badge (`:399`) counting universes as "Active parameters" is slightly misleading labeling.

### frontend/src/components/RangeFilterControl.tsx

- **(c) [major → Speculative Generality / dead code]** The exported `RangeFilterControl` is referenced **nowhere** in the repo (verified by grep across `frontend/src`). 167 lines of dead, maintained UI. Delete it or wire it into the filter panels it was evidently built for (it duplicates `DualRangeSlider` responsibilities with number inputs).
- **(c) [minor]** Its `clamp` is a third copy of the same helper in `DualRangeSlider.tsx:16`.
- *(No (a)/(b) findings — unreachable code.)*

### frontend/src/components/RenameModal.tsx

- **(a) [minor]** `helperText` doubles as the error channel: `BacktestDetailPage.tsx:907-910` and `PortfolioDetailPage.tsx:645` pass `renameError` into it, so API failures render in muted guidance-gray, indistinguishable from help text, and the modal stays open with no error affordance.
- **(d)** Scanned; otherwise clean. `autoFocus` on the input is appropriate here.

### frontend/src/components/ResampleModal.tsx

- **(a) [minor]** No double-submit guard on "Start resample" (`:249-266`); same as DownloadModal.
- **(b) [minor]** `sourcePath` is a free-text **absolute server path** (`:178-183`); same trust-the-backend caveat as BacktestFromSignalsModal.
- **(c) [minor]** `catch (err: any)` (`:96`) — repo convention elsewhere is `unknown` + narrowing.
- **(d)** Third copy of the modal chrome/terminal/resetStream scaffolding (see DownloadModal (c)).

### frontend/src/components/SmartHarvestModal.tsx

- **(a) [minor] Escaped-entity artifact in UI copy**: `` Auto-tuning output is streamed from `task_&lt;job_id&gt;.log`. `` (`:333`) renders the literal characters `task_&lt;job_id&gt;.log` because HTML entities are not decoded in JSX text. Visible bug in shipped copy.
- **(a) [minor]** No double-submit guard (`:277-294`).
- **(c) [minor] Speculative Generality**: Source `<select>` offers exactly one option, `dukascopy` (`:179-185`) — a dropdown for a constant.
- **(d)** Fourth copy of the modal scaffolding.

### frontend/src/components/Sparkline.tsx

- **(a) [major] Rules-of-Hooks violation — latent crash**: the early return `if (normalized.length < 2) return null;` (`:30`) precedes the `useMemo` computing `drawdowns` (`:53-62`). A mounted Sparkline whose `data` transitions across the 2-point boundary (empty→populated or populated→empty) changes the hook count between renders → React throws "Rendered fewer/more hooks than expected" and the card tree crashes. Today every call site happens to pre-guard (`BacktestsPage.tsx:1433`, `LiveHub.tsx:1264`, `PortfoliosPage.tsx:132`), so it is latent — but the component's own contract is broken, `eslint-plugin-react-hooks` (enabled in `eslint.config.js:15`) should be flagging it, and the next caller will not know to guard. Move all hooks above the return.
- **(a) [minor]** Hover tooltip left-clamp uses a hard-coded 192px tooltip width (`:145`); long localized date + values can overflow the left edge on narrow cards.
- **(c) [minor] Inconsistent i18n**: hover date formatted `de-DE` and fallback label `Punkt ${n}` (`:86-88`) inside an otherwise English UI (same de-DE convention appears in BacktestsPage `formatPeriodDate` and all of MatrixGridPage) — the app has three date-locale conventions with no documented standard.
- **(d)** Nice, tight SVG component otherwise.

### frontend/src/components/StatCard.tsx

- **(a) [minor] Currency rounding**: `format="currency"` renders `maximumFractionDigits: 0` (`:23-26`) — Net Profit of `$12,345.67` displays `$12,346`. On a trading dashboard, silently rounding money is wrong; callers compensate nowhere.
- **(a) [minor] Percent unit ambiguity**: `format="percent"` blindly appends `%` to whatever number arrives. Callers disagree about units: `BacktestDetailPage.tsx:597` passes `stats.win_rate` raw (assumes already-percent) while `PortfolioDetailPage.tsx:463` passes `(stats.win_rate||0)*100` (assumes fraction) — one of these pages is displaying win rates off by 100× depending on backend shape, and nothing in the type system catches it. Root cause is the Primitive Obsession noted in §0.
- **(c) [minor]** Auto-color heuristic treats exactly `0` as neutral gray even for `percent` metrics where 0 is meaningful.
- **(d)** Scanned; otherwise clean `memo` component.

### frontend/src/components/StrategiesProvider.tsx

- **(a) [minor]** `loading = strategiesQuery.isFetching` (`:28`) is true during *background* refetches, not just cold loads — any consumer rendering a spinner off it will flicker on refetch. (`StrategiesPage` correctly uses `isLoading` instead.)
- **(d)** Positive: `pendingLoadRef` correctly dedupes concurrent `ensureLoaded` promises (`:45-61`). Scanned; otherwise nothing found.

### frontend/src/components/SymbolInput.tsx

- **(a) [minor]** Category list capped at `slice(0, 100)` with an "...and N more" footnote (`:189-220`) and **no way to reach the remaining symbols** — for large universes the autocompleter silently hides valid picks unless the user narrows by query first.
- **(a) [minor]** Dropdown has no keyboard support: no arrow-key navigation, no Enter-to-select, no Escape-to-close; mouse-only operation (`:149-225`).
- **(c) [minor]** Local `CATEGORY_LABELS` emoji map duplicates taxonomy knowledge that the `/symbols` response could carry.
- **(d)** Scanned; otherwise tidy.

### frontend/src/components/TaskManager.tsx

- **(a) [minor] Always-on 3 s polling**: mounted globally by `App.tsx` on every page; polls `listDataTasks` every 3 s even when idle and when the tab is hidden (no `document.visibilitychange` gating, no backoff, silent `catch {}` at `:24-28` hides persistent failures). Multiply by TaskHub's own 3 s poll, LoggingPage's 5 s poll, and the live pages' 30 s polls — the app maintains many independent timers with no coordination.
- **(c) [minor]** Silent catch means the "Task Hub" chip can show a stale count indefinitely during an outage with zero indication.
- **(d)** Component itself is fine; the placement (global chrome) is the issue.

### frontend/src/components/ToastProvider.tsx

- **(a) [minor] No cap on stacked toasts**: the polling layers above can emit error toasts in a burst (e.g., TaskHub's status-change detector during an outage); nothing limits the stack or coalesces duplicates (`:62-68`, `:87-112`).
- **(a) [minor]** Toast id `Date.now() + random(1000)` (`:63`) can collide for two toasts in the same millisecond → React key collision warning and dropped toast. A module counter is safer.
- **(c) [minor]** Toasts are not dismissible (pointer-events enabled but no click handler).
- **(d)** Scanned; provider/context wiring is textbook.

### frontend/src/components/TradeTable.tsx

- **(a) [minor]** Fixed `height: "560px"` (`:287`) regardless of context — on the LiveViewer right column (inside a 3/8-width grid section) the ag-grid viewport is far taller than the surrounding panel; on short laptop screens it forces page scroll inside pages that already scroll.
- **(a) [minor]** `pnlPct` coloring (`cellStyle: pnlCellStyle`, `:270-278`) reuses the raw-value color rule for a column whose unit depends on the caller (BacktestDetail passes backend `pnl_pct`, LiveViewer passes `null`) — same unit-ambiguity family as StatCard.
- **(c) [minor] Fifth copy** of `toFiniteNumber` (`:35-46`) and another private `formatPrice`/`formatPnl`/`formatPercent` family.
- **(d)** ag-grid usage (community modules, legacy theme, `getRowId`) is correct and the column defs are readable.

### frontend/src/components/TradingViewChart.tsx

- **(a) [minor] Full teardown on `height` change**: the chart-lifecycle effect is keyed only on `[height]` (`:384-503`); `TradeChartPage` changes `chartHeight` when toggling fullscreen or when separate-pane indicators appear (540↔640↔fullscreen px), destroying and recreating the chart — losing the user's zoom/scroll position each time. Data/indicator/marker updates correctly avoid recreation (good separation at `:505-664`); height should follow via `resize`.
- **(a) [minor]** `handleScreenshot` calls `chart.takeScreenshot(true, false)` (`:377`); the API takes no arguments — harmless today, misleading signature.
- **(c) [minor]** `resolveChartLocale` third copy (`:181-187`); `normalizeUnixTime`/`toChartTime` duplicate `TradeChartPage`'s (`:96-119` vs `TradeChartPage.tsx:113-128`).
- **(d)** Best-structured chart wrapper in the repo: normalized memos, marker-details map, focused-range binary search (`findNearestBarIndex`) — this is the pattern the four page-local charts should be refactored to reuse.

### frontend/src/components/index.ts

- Barrel exports six components; `RangeFilterControl` (dead) is *not* exported here while live components like `ToastProvider` aren't either — barrel is partially decorative since most imports are deep. Scanned; minor inconsistency only.

### frontend/eslint.config.js

- **(d) [minor — process gap, not a code bug]** `reactHooks.configs.flat.recommended` is enabled (`:15`), which includes `rules-of-hooks` — the violation in `Sparkline.tsx:30/53` and the `ids.join(",")` dep-array dodge in `ComparePage.tsx:105` indicate lint is either not run in CI or its warnings are not enforced. The config itself is fine.
- Scanned; nothing else found.

### frontend/vite.config.ts

- Scanned — **nothing found.** Dev proxy (`/api`→`:5556`, `/ws` ws-upgrade) matches the client's BASE_URL and `buildWebSocketUrl` assumptions. Hardcoded localhost target is acceptable for a dev-only config.

### frontend/tests/optimizationEvidence.test.ts

- **(a) [minor — environment assumption]** Imports `.tsx/.ts` sources directly under `node:test` (`:21`, `:32`, `:39`) — requires Node ≥22.6 with type-stripping (or a loader); nothing pins that, so CI on older Node fails opaquely.
- **(c) [minor — brittle by design]** The first test asserts page **source text** via regex (`:13-17`). As a drift-guard for a past regression it's pragmatic, but renaming a heading breaks the build with a test failure that doesn't describe user-visible behavior. Acceptable tradeoff; documenting it would help.
- Positive: the alias/`__proto__` safety tests (`:30-35`) pin down the prototype-pollution hardening in `optimizationEvidence.ts`.

### frontend/src/hooks/liveStreams.ts

- Scanned — **nothing found.** Thin, correct envelope filtering (`snapshot|update`) over `useWebSocket`; URL building matches vite ws proxy. Only note: envelope contract (`type/topic/data`) is implicit with the backend — a shared types entry would help.

### frontend/src/hooks/useWebSocket.ts

- **(a) [minor]** Heartbeat `socket.send(JSON.stringify({type:"ping"...}))` (`:166`) can throw `InvalidStateError` if the readyState flips between the OPEN check and the send; the interval callback has no try/catch, so an unlucky race throws an uncaught error each 15 s until close settles.
- **(a) [minor] Unconditional reconnect**: `scheduleReconnect` retries forever with capped backoff for **any** close, including server-initiated permanent rejections (auth revoked, unknown topic). A give-up policy or respect for close codes (e.g. 4000/1008) would avoid a polite DoS loop against a rejecting server.
- **(c)** Otherwise this is a genuinely good hook (refs for handlers, stale-socket guards, disposed flags) — the puzzle is why `LiveTerminal` doesn't use it.

### frontend/src/index.css

- **(c) [minor] Duplication**: `.animate-spin` keyframes (`:56-66`) re-implement Tailwind's built-in `animate-spin` utility (Tailwind v4 is imported at `:1`); the lucide `className="animate-spin"` usages work through either — one owner should win.
- **(a) [minor]** The universal `* { margin:0; padding:0 }` reset (`:18-22`) stacks with Tailwind preflight; double resets make inherited spacing bugs harder to reason about. Cosmetic.
- Scanned; theme tokens are consumed consistently by Badge/Tailwind-class components.

### frontend/src/lib/optimizationEvidence.ts

- Scanned — **nothing found.** Prototype-safe alias lookup (`Object.hasOwn`), finite-number discipline, and clear evidence states; directly covered by its test file. This is the model small module in the repo.

### frontend/src/lib/queryClient.ts

- Scanned — **nothing found.** Sensible defaults (`retry: 1`, `refetchOnWindowFocus: false` — the latter quietly saves the live pages from refetch storms).

### frontend/src/main.tsx

- **(a) [major] Sentry is declared but not wired** (contradicting the review brief): no `@sentry/*` import exists anywhere under `frontend/src` — only `VITE_SENTRY_DSN`/`VITE_SENTRY_ENVIRONMENT` ambient types in `vite-env.d.ts`. Combined with the missing error boundary (App.tsx above), production exceptions are completely unobserved. Either initialize Sentry here (per the brief's intent) or remove the dead env contract.
- **(a) [minor]** Provider order is fine (QueryClient → Toast → Strategies → Router); `StrictMode` double-invokes effects in dev, which the WebSocket/polling effects all survive (cleanups are correct) — worth remembering when debugging dev-only double connects.
- **(d)** Scanned; otherwise minimal and correct.

### frontend/src/pages/BacktestDetailPage.tsx

- **(a) [major] Page-scoped statistics presented as whole-backtest**: `BacktestStats` has no `winners`/`losers` fields, so `typeof stats?.winners === "number"` is always false and `tradeStats.wins/losses` fall back to filtering **only the currently loaded 100-trade page** (`:225-226`); likewise `bestTrade`/`worstTrade` (`:238-249`), `avgPnl` (page mean when stats absent, `:227-232`), and `longTrades`/`shortTrades` (`:250-251`, always page-only). These render in a "Trade Statistics" panel (`:802-811`) with no qualification — for any backtest with >100 trades, Win/Loss and Long/Short are wrong numbers stated as fact.
- **(a) [minor] Chart rebuilt on benchmark change**: `EquityChart`'s effect removes and recreates the entire chart instance whenever `data`/`benchmark`/`benchmarkSymbol` change (`:991-999`, deps `:1142`) — selecting a benchmark resets zoom/tooltip state. Series-level updates (as TradingViewChart does) would preserve state.
- **(a) [minor]** Fabricated fallback `(info?.initial_capital || 100000)` (`:432`) invents $100k when the field is 0/absent — displayed as fact.
- **(c) [minor] Duplicated Code**: local `toNumberOrNull` (`:255-262`) is the sixth copy of the finite-number coercion helper; `EquityChart` is the fourth copy of the chart bootstrap block (create options `:1009-1028`, ResizeObserver `:1132-1135`, drawdown histogram `:1074-1094` — the dd-histogram block also appears verbatim in `PortfolioDetailPage.tsx:834-852`).
- **(a) [minor]** Trades effect races are unguarded: rapid pagination clicks can resolve out of order (`:117-132` has no `active`/ignore flag, unlike sibling pages) — last-resolved wins, which is usually but not necessarily last-clicked.
- **(b)** Nothing found (ReactMarkdown not used here; no injection sinks).
- **(d)** 1293 lines mixing data orchestration, two chart components, and table mapping — extract `EquityChart`/`CapmScatterPlot` (they're generic) and the trade-row mapper.

### frontend/src/pages/BacktestsPage.tsx

- **(a) [major] Module-level mutable cache**: `backtestsAllCache` / `backtestsAllPromise` (`:114-115`) are file globals that survive unmount and are shared by every consumer of the module. Consequences: (1) with a name filter active, `loadAllMetadata` passes `force=false` (`:604`) so the list is served from cache even after backtests were created/deleted elsewhere (e.g. via Strategy Lab or CreateBacktestModal from StrategyDetailPage) — stale data with no indication; (2) the promise singleton means an in-flight filtered fetch can be answered from a stale cache mid-flight; (3) it's untestable global state. TanStack Query is already in the stack and would express this in three lines.
- **(a)/(d) [major] 41-entry hand-maintained `useMemo` dependency list**: `filterPanelContent` (`:871-1147`) enumerates every bound/filter primitive (`:1107-1147`). One forgotten identifier = stale slider UI that silently disagrees with state — this is the classic exhaustive-deps trap at its worst and the clearest maintainability hot spot in the area. Extract `FilterPanel` as a component and let React prop-diff do the work.
- **(a) [minor]** `normalizeWinRate` heuristically divides any value in `(1, 100]` by 100 (`:123-129`) — a legitimate stored win-rate of e.g. `55` (already-percent, as `StatCard` elsewhere assumes!) is reinterpreted as `0.55`; the two conventions collide inside one app.
- **(a) [minor]** Preview effect depends on `[currentPageItems, previewById]` (`:712-730`): each batch arrival re-runs it; the `missingIds` guard converges it, but a preview payload missing an id (backend caps?) would re-request it every render — add a attempted-set.
- **(c) [minor] Duplicated Code**: `metricValue`/percentage/number formatters (`:131-149`) duplicate `utils/stats.ts` and five other files; `Th`/`Td`/`MatrixMetric` are page-private table primitives paralleling `DataTable`.
- **(c) [minor]** Imperative hover styling via `onMouseEnter/onMouseLeave` mutating `event.currentTarget.style` (`:1379-1392`) — fights React reconciliation; a CSS class does this for free.
- **(a) [minor]** `window.confirm` for bulk delete (`:797`) while duplicates-removal uses a bespoke Modal — two confirmation paradigms on one screen.
- **(d)** 1835 lines: matrix panel, card grid, filter panel, pager, duplicates modal in one file. `BacktestMatrixPanel` (`:312-444`) is independently useful and self-contained.

### frontend/src/pages/ComparePage.tsx

- **(a) [minor]** Effect deps `[ids.join(",")]` (`:105`) — works, but defeats exhaustive-deps and re-runs on any rejoin-equal string; fine, just lint-hostile.
- **(a) [minor]** Missing stats keys silently coerce to 0 (`Number(stats[key] || 0)`, `:223-224`): comparing a backtest lacking `psr` shows `0.000` vs the other side's real value and colors the delta red — fabricated comparison data rather than "N/A".
- **(a) [minor]** `delta === 0` colored neutral but signed formatter renders `+0.00`/`-0.00` inconsistently for negative-zero.
- **(c) [minor]** `METRICS` table duplicates the metric-threshold/color knowledge that lives in BacktestsPage/MatrixGridPage (third copy of "sharpe higher-better, psr ≥0.95 good" conventions).
- **(d)** Scanned; otherwise compact and correct.

### frontend/src/pages/DataManagementPage.tsx

- **(a) [major] Failures render as an empty success state**: `loadOverview`/`loadCategoryDetails` use `try { … } finally { … }` with **no catch** (`:408-443`, `:369-406`) and their `void`-invoked call sites (`:457`, `:467`) let rejections escape as unhandledrejection. On first-load failure the page shows "Categories 0 / 0 nodes" with no error message — indistinguishable from an empty catalog, on the screen whose job is deleting data files. There is no error state anywhere in 2199 lines.
- **(c) [major — maintainability] Dual state+ref mirror pattern**: `categoryEntriesRef`/`loadingCategoriesRef`/`selectionRef` shadow their states (`:352-367`), and the refs are **mutated inside the setState updater callbacks** (`:381-403`) to make them synchronous for the guard clauses. It works, but it's a hand-rolled concurrency layer that every future editor must understand; the guards it enables ("don't refetch a loaded category") are exactly what TanStack Query gives for free, and this page is the only place using the pattern.
- **(a) [minor] Sequential N-delete**: `handleDeleteAllForSymbol` awaits `deleteDataSymbol` per file serially (`:678-680`); a failure mid-loop leaves the symbol half-deleted with a generic alert and no indication of which files survived.
- **(a) [minor]** `PanelTable`'s sort fallback indexes rows as `Record<string, unknown>` by `sortKey` (`:2015-2020`) — fine, but sortable columns lacking `sortValue` silently string-compare numbers (none currently).
- **(c) [minor]** `window.confirm`/`alert` quartet (`:649`, `:664`, `:670`, `:686`) — third confirmation paradigm in the app.
- **(d)** Well-decomposed internally (HeaderBlock/SectionPanel/MetricTile/PanelTable are good primitives trapped in a page file); the tree/table models (`SymbolGroup`, `CategoryInventoryRow`) are clean.

### frontend/src/pages/LiveDetail.tsx

- **(a) [major] Infinite refetch loop while the replay modal is open**: the signal-processors effect (`:147-156`) guards on `signalProcessorsLoading` and its own dep list is `[replayModalOpen, signalProcessorsLoading]`. Cycle: modal opens → loading=false → fetch starts, loading=true → finally sets loading=false → effect re-runs (dep changed) → modal still open, loading=false → fetch again → **forever**, hammering `/signal-processors` every round-trip for as long as the modal is open. (LiveHub solves the same need imperatively on open — `LiveHub.tsx:582`.) Fix: fetch on open transition only, or drop the loading flag from deps.
- **(c) [major — Duplicated Code, divergence-prone]** `calculateRealizedTradeStats` (`:731-799`) is a **verbatim copy** of `LiveHub.tsx:1529-1597` — ~70 lines of FIFO position accounting (average-price blending, partial closes, flip handling) that constitutes financial-grade logic existing twice. The copies have already drifted at the edges (`toWinRatePercent(value)` here takes a fallback, `LiveHub`'s doesn't; `maxDrawdownFromBalances` exists only here). One more edit applied to one copy produces two different "Total PnL" numbers on two screens for the same account. Extract to `utils/liveStats.ts` immediately.
- **(c) [minor]** Fourth/fifth copies: `asFiniteNumber` (`:840-851`), `formatUsd`/`formatSignedUsd` (`:853-866`), `formatPercent` (`:868-870`), chart bootstrap (`:521-600`).
- **(a) [minor]** Double-decode: `decodeURIComponent(rawAccountId ?? "")` (`:56`) — `useParams` already decodes; an account id containing a literal `%` sequence corrupts. Same pattern in `LiveViewer.tsx:82`.
- **(a) [minor]** 30 s poll continues while the tab is hidden; `tradeRows` unmemoized recomputed per render (`:170-198`).
- **(b)** Nothing found.
- **(d)** The replay-modal form duplicates `LiveHub`'s replay form almost field-for-field (dates/processor/timeframe/auto-download) — a shared `ReplayFormModal` would collapse ~200 lines across the two files.

### frontend/src/pages/LiveHub.tsx

- **(a) [CRITICAL — deployment controls unclickable]** Every `LiveDeploymentCard` renders a full-card overlay `<Link … style={{position:"absolute", inset:0, zIndex:1}}>` (`:114-118`). The PAUSE / STOP / DELETE buttons (`:149-177`) are **non-positioned** siblings, so the positioned, z-indexed link paints above them and intercepts every click/press: on the Live Hub it is impossible to pause, stop, or delete a deployment — clicking those buttons silently navigates to the account page instead. This is the **only** surface offering these controls (`LiveDetail` has none), so a runaway live strategy cannot be stopped from the UI. `PortfoliosPage` demonstrates the known fix in the same repo: give the interactive child `position:relative; zIndex:2` (`PortfoliosPage.tsx:106-119`). Rated critical because core live-trading operations are inoperable, with no in-app workaround.
- **(a) [major] Request cascade per WebSocket message**: the stream effect (`:371-379`) calls `refreshDeployments(true)` on **every** `accountsStream.data` emission. `refreshDeployments` fans out to `getLiveAccounts` + `getLiveDeployments` + fire-and-forget `getLiveDashboard` + **3 requests per account** (`getLiveStats`/`getLiveBalances`/`getLivePositions`, `:238-244`) — i.e. `3N+3` HTTP calls per WS tick. With a chatty stream (snapshots every few seconds, N accounts) this saturates the backend and starves the very endpoints the UI needs. Debounce/throttle, or apply the WS payloads directly to state (the code already does this for `accounts` at `:377`).
- **(a) [minor] Truncated aria-label**: ``aria-label={`Open live account `}`` (`:116`) interpolates nothing — the template literal is empty; screen readers announce "Open live account" for every card. Clearly unintended code (the account name was meant to be interpolated).
- **(a) [minor] Silent dashboard degradation to zeros**: `getLiveDashboard()` failures are swallowed to `null` (`:235-237`) and the telemetry tiles then display `$0` total equity / `0%` allocation (`:1100-1126`) — fabricated zeros on a money screen rather than an error/placeholder.
- **(c) [minor]** `handleControl`'s `statusByAction` map includes a `"play"` branch (`:437-441`) that no UI path issues — dead arm of the map.
- **(c) [major→grouped]** Origin of the duplicated realized-PnL engine (see LiveDetail) — counted once there; noted here as the twin.
- **(b)** Nothing found beyond the general same-origin posture.
- **(d)** 1730 lines; the account-card JSX block (`:1150-1365`) and the style-function tail (`:1372-1729`) dominate. The `settings_json` textarea is validated nicely (`:503-518`) — good.

### frontend/src/pages/LiveViewer.tsx

- **(a) [minor]** Exit markers plotted at `trade.signalTimeUnix` (the **entry** signal time of the closing fill's originating signal) rather than the execution time (`:620-632`, data from `:965`) — EXIT arrows land on/near the entry bar of that execution instead of precisely on the exit fill; with fast fills it's near-correct, with slow fills it misplaces the marker by the fill lag the app itself measures elsewhere.
- **(a) [minor]** Double-decode of `accountId` (`:82`) as in LiveDetail.
- **(a) [minor] Cache-key magic coupling**: stream hydration writes `["live-signal-log", accountId, 200]` (`:155`) — must track `useLiveSignalLog`'s default limit (see queries.ts).
- **(a) [minor]** `positionsFallbackPolling`/`staleTime: Infinity` choreography (`:190-218`) is clever but means a stream that connects *after* mount leaves staleTime pinned at Infinity until reconnect toggles `isConnected`; a stream flap mid-session flips query options identity each time (harmless churn).
- **(c) [minor]** Fifth copy set of `asFinite`/formatters (`:1138-1179`); third copy of `resolveChartLocale` (`:73-77`); SL/TP price lines loop draws up to 10 overlapping dashed pairs (`:750-790`) — latest-only or dedupe would read better.
- **(b)** Nothing found.
- **(d)** Good streaming architecture overall (WS→query-cache bridge with polling fallback); the file is long mostly due to the chart + table + style tail.

### frontend/src/pages/LoggingPage.tsx

- **(a) [minor] Position-based expansion IDs**: `rowId = ${index}:${timestamp}:${module}` (`:305`) — after any filter/level/search change, index N denotes a different entry, so expanded-detail state silently attaches to the wrong rows. Key by content hash of the raw line instead.
- **(a) [minor] Level heuristic misclassifies**: fallback `normalizeLevel(levelMatch?.[1] ?? line)` (`:114`) passes the **whole line** when no token matched, so any message containing "error"/"debug" as a substring ("0 errors", "debugging disabled") is bucketed as ERROR/DEBUG and colored/filtered accordingly.
- **(a) [minor]** Search box feeds the server param per keystroke with no debounce (`:230-262` deps include `search`) alongside the unconditional 5 s poll of up to 5000 lines (`LOG_FETCH_LIMIT`, `LOG_REFRESH_MS`) regardless of tab visibility — the heaviest endpoint in the app is polled hardest.
- **(c) [minor]** `POLLING_PATHS`/`POLLING_PREFIXES` (`:16-17`) hard-code route knowledge that duplicates the client's own API surface — route renames require synchronized edits here (Shotgun Surgery).
- **(d)** The virtualizer setup is correct (`measureElement` + dynamic estimates) and the JSON detail expander is a genuinely good log UX.

### frontend/src/pages/MatrixGridPage.tsx

- **(a) [minor] setState during render inside useMemo**: `pagedRows` calls `setPage(safePage)` from within a `useMemo` (`:326-331`). React's sanctioned "adjust state during render" pattern must occur in the render body of the same component with a guarded setter; doing it inside `useMemo` is an undocumented side effect that happens to work. Compute `safePage` for slicing and correct `page` in an effect.
- **(a) [minor] Reset can't clear strategy filter**: `resetFilters` sets `selectedStrategy: ""` (`:349`), but the auto-select effect (`:283-287`) immediately re-picks `strategies[0]` — "Reset" never returns to the all-strategies view.
- **(c) [minor] Types duplicated from `api/types.ts`**: local `MatrixRow`/`StrategySummary`/`MatrixGridOverview` (`:11-98`) restate `MatrixGridRow`/`MatrixGridStrategySummary`/`MatrixGridOverview` with small differences — drift guaranteed.
- **(c) [minor] i18n split-brain**: this page is fully German ("Strategien fertig", "Lade Matrix-Ergebnisse…", "Zurück/Weiter") while the rest of the app is English — no localization convention is documented anywhere.
- **(d)** Grid/table views are cleanly separated (`StrategyGrid`, `MatrixTableView`); `downloadJson` anchor-without-append is fine on modern browsers.

### frontend/src/pages/OptimizationResultsPage.tsx

- **(a) [major] Unguarded deep access → whitescreen risk**: `optimization.test_period.start.slice(0, 10)` (`:250`) assumes `test_period.start` exists. Older/legacy optimization records (or any backend shape drift) throw `TypeError` here; with no error boundary the entire app blanks on this route. Optional-chain with a fallback is a one-line fix.
- **(a) [minor]** Same exposure, lower stakes: `optimization.created_at` → `new Date(...).toLocaleString` (`:220`) renders "Invalid Date" rather than crashing; `train_split_pct * 100` assumes fraction while the modal collects fractions too — consistent here, unlike StatCard's world.
- **(c) [minor]** `formatComparisonValue`/`formatMode`/`formatSymbols` (`:20-47`) are near-copies of `OptimizationsPage.tsx:9-30` — the two optimization pages should share a formatter module (they already share `optimizationEvidence.ts`, proving the pattern).
- **(d)** The loading gate (`loadedId !== id`, `:52-56`) elegantly avoids flash-of-empty; nice pattern worth reusing.

### frontend/src/pages/OptimizationsPage.tsx

- **(a) [minor]** Archive grid renders every optimization card with no pagination/windowing (`:190-368`); hundreds of sweeps = hundreds of gradient-bordered DOM subtrees. Fine at current scale, flagged for growth.
- **(c) [minor]** Formatter duplication (see OptimizationResultsPage above); OOS verdict banner styling duplicated between the two pages (`:311-326` vs results `:273-288`).
- **(d)** Scanned; otherwise clean, and the honest "In-sample selected score" labeling (enforced by the test file) is exemplary product thinking.

### frontend/src/pages/PortfolioDetailPage.tsx

- **(a) [major] Crash on optional field**: `ContributionTable` renders `row.win_rate.toFixed(2)` (`:729`) but `PortfolioContribution.win_rate` is typed optional (`api/types.ts:1230`). A contribution row without `win_rate` throws during render → no error boundary → blank page. Sibling cells in the same table carefully null-check (`psr`, `p_value`, `max_drawdown`, `cagr` all do) — this one was missed.
- **(a) [minor] Redundant fetch on load**: initial `Promise.all` fetches `getPortfolioContributions(id, "component")` (`:116`) although `detail.component_contributions` already arrived in the detail payload; the groupBy effect then refetches. One wasted request per visit, and a window where the two sources can disagree.
- **(a) [minor]** `correlation_matrix` is delivered in `combined_stats` and never rendered anywhere — dead payload (either surface it or drop it from the type contract).
- **(c) [minor]** `PortfolioEquityChart` is the fifth chart-bootstrap copy including the verbatim drawdown-histogram block (`:834-852` ≈ `BacktestDetailPage.tsx:1074-1094`); `toFiniteNumber`/`resolveChartLocale`/`filterUnique` are more private copies.
- **(a) [minor]** Win-Rate StatCard passes `(stats.win_rate||0)*100` (`:463`) while Max-DD passes `((stats.max_drawdown||0)*100)` and Total Return `*100` — but Sharpe/CAGR cells in the contribution table multiply by 100 while the top StatCards also do: internally consistent here, inconsistent with BacktestDetailPage's conventions (see StatCard finding).
- **(d)** GroupBy tabs + sortable contribution table is a strong feature; the JSX indentation glitch at `:400-412` suggests a refactor scar.

### frontend/src/pages/PortfoliosPage.tsx

- **(a) [minor] No debounce on server-side name filter**: `loadPortfolios` re-fires per keystroke via effect-on-callback-identity (`:263-287`) — same pattern fixed with a debounce in BacktestsPage but not here.
- **(a) [minor] Unconfirmed destructive action**: `handleRemoveEmpty` (`:453-463`) deletes all empty portfolios on a single click with **no confirmation** — BacktestsPage's equivalent confirms with a count (`:856`). One accidental click on "Remove Empty" is unrecoverable.
- **(a) [minor]** Metric pills hide when a value is exactly `0` (`portfolio.sharpe !== 0 && …`, `:147-177`): a genuine 0.00 Sharpe is indistinguishable from a missing metric.
- **(c) [minor]** `componentId()` UUID helper (`:29-34`) is a local reimplementation of what `crypto.randomUUID` guards already provide — fine, but it's also a third "generate an id" approach in the app.
- **(c) [minor]** `inputStyle` defined inside the component body (`:465-473`) — recreated per render, defeating nothing but inconsistent with the module-level constants used in sibling files.
- **(d)** The overlay-link + zIndex-2 delete button here (`:88-119`) is the reference implementation the LiveHub cards should copy.

### frontend/src/pages/Services.tsx

- **(a) [minor]** Delete has **no confirmation** (`:62-70`) — one click removes a (possibly live) Discord bot service; every other destructive flow in the app confirms.
- **(a) [minor]** "Next Run: cron-based" (`:241`) presents a placeholder string as data — the backend knows the schedule; showing pseudo-information trains users to ignore the field.
- **(a) [minor]** Cron expression is submitted unvalidated (`:72-103`); a typo'd schedule silently registers a job that never fires.
- **(a) [minor]** `statusMutation.isPending` disables the Pause/Start button of **every** service during any one mutation (`:261-264`) — coarse but functional.
- **(d)** Scanned; this is the only page consistently using the `queries.ts` hooks for both queries and mutations — the pattern the other pages should adopt.

### frontend/src/pages/StrategiesPage.tsx

- **(a) [minor] Mislabeled sort options**: "Trade Count (High to Low)/(Low to High)" sort by `backtest_count` (`:173-176` vs labels at `:223-224`) — label/value mismatch.
- **(a) [minor]** Query error is unhandled: `strategiesQuery.data ?? []` with only an `isLoading` gate — a failed `/strategies` renders an empty, error-free grid (`:118-120`, `:231-256`).
- **(c) [minor]** `filters: Record<string, string>` stringly-typed filter bag loses key safety that `FilterConfig.key` provides.
- **(d)** Scanned; otherwise the cleanest list page (memoized card, derived facet options, shared FilterBar).

### frontend/src/pages/StrategyDetailPage.tsx

- **(b) Verified — no XSS sink**: documentation/notes render through `ReactMarkdown` **without** `rehype-raw`/`dangerouslySetInnerHTML` (`:340-399`, `:441-556`); raw HTML in strategy docs is escaped by default. Custom link components enforce `target="_blank" rel="noreferrer"` — good.
- **(a) [minor]** `detail` is typed `any` (`:12`) and every downstream access (`backtests.map((backtest: any)`, `:590`) forfeits the rich `StrategyDetail` type that exists in `api/types.ts`.
- **(a) [minor]** Load failure is indistinguishable from not-found (`:27-29` catch → `setDetail(null)` → "Strategy not found"): a 500 and a bad slug render identically.
- **(d)** The two large `ReactMarkdown components={{…}}` maps (`:341-398`, `:442-553`) are ~85% identical — extract one shared renderer.

### frontend/src/pages/StrategyLabPage.tsx

- **(a) [minor]** `catch (err: any)` (`:93`); error text is surfaced raw from the agent endpoint.
- **(a) [minor]** Free-text `customModel` for the copilot provider (`:416-431`) is forwarded verbatim as a model id — server-side validation burden; a client allow-list would fail fast.
- **(c) [minor]** One-click triggers an LLM conversion pipeline (token cost) with no confirmation or cost hint — minor product consideration.
- **(d)** Scanned; layout-heavy but coherent, and correctly delegates streaming to `LiveTerminal` (inheriting its no-reconnect weakness).

### frontend/src/pages/TaskHub.tsx

- **(a) [minor]** Status-transition toasts diff against `statusRef` populated from the previous poll (`:103-119`): a task that goes queued→failed→completed **between two 3 s polls** yields a single "completed" toast — the failure is never announced. Acceptable at this cadence, worth knowing.
- **(a) [minor]** Unconditional 3 s poll (like TaskManager) with silent catch; `runAction` catch typed `any` (`:161`).
- **(c) [minor]** `categoryIcon` maps both backtest and resample to 📊 (`:45-50`) — ambiguous iconography.
- **(d)** Completed-task list truncates at 100 with no indicator that more exist (`COMPLETED_TASK_LIMIT`, `:18`, `:128-135`).

### frontend/src/pages/TradeChartPage.tsx

- **(a) [minor] Router desync via `history.replaceState`**: `goToTradeNumber` and `loadReplay` mutate the URL with raw `history.replaceState` (`:273-279`, `:478-482`) instead of `navigate(..., {replace:true})`. React Router's location state diverges from the address bar: back/forward behave oddly around traded steps, and any router subscriber (analytics, future guards) misses the change.
- **(a) [minor] Space-key double-toggle risk**: the global keydown handler toggles play on Space (`:550-553`) without excluding focused buttons — pressing Space with a transport button focused can both activate the button and toggle playback, netting a no-op or double-advance depending on browser keyup semantics. Exclude `BUTTON`/`role=button` targets like the typing check excludes inputs.
- **(a) [minor]** Fullscreen chart height is computed once per render from `window.innerHeight` (`:434-439`); entering fullscreen via OS shortcut (no React event) leaves stale height until some state changes. Listen to `resize`.
- **(a) [minor]** Escape is bound to "stop playing → exit fullscreen → **navigate away from the page**" (`:555-562`): users pressing Escape to exit a modal-ish state land on the backtest page unexpectedly.
- **(c) [minor]** `sanitizeOHLCVBars`/`sanitizeIndicatorPoints`/`normalizeUnixTime` (`:113-184`) duplicate TradingViewChart's normalization — the page sanitizes, then the chart re-sanitizes (defense in depth or duplicated responsibility?).
- **(d)** Keyboard-driven trade stepping with autoplay is a standout feature; the missing-OHLCV recovery flow (`triggerMissingDataDownload` + task polling, `:568-626`) is thoughtful. Poll interval recreated on each status change is fine.

### frontend/src/utils/stats.ts

- **(a) [minor]** `pValueMetric` clamps negatives to `0`, so a corrupt `-1` p-value renders as `0.000` in green ("significant") via `formatPValue`/`pValueColor` (`:14-20`) — anomaly-laundering rather than surfacing. Consider rendering "N/A" outside `[0,1]`.
- **(c) [minor]** This 20-line module exists precisely to be the shared home for metric coercion, yet `BacktestsPage`, `LiveHub`, `LiveDetail`, `LiveViewer`, `PortfolioDetailPage`, `TradeTable`, `TradingViewChart` each define their own — the consolidation has one customer so far.
- Scanned; otherwise correct.

---

## 2. Cross-cutting smell ledger (Fowler ch.3)

| Smell | Where | Remedy |
|---|---|---|
| **Duplicated Code** | Realized-PnL FIFO engine ×2 (LiveHub:1529 / LiveDetail:731); finite-number coercion ×6; USD/percent formatters ×4-5; `resolveChartLocale` ×3; chart-bootstrap block ×5 (BacktestDetail, PortfolioDetail, LiveDetail, LiveViewer, TradingViewChart); drawdown-histogram block ×2 verbatim; modal scaffolding ×3 (Download/Resample/SmartHarvest); optimization formatters ×2 | `utils/format.ts`, `utils/liveStats.ts`, `components/charts/EquityChart`, shared `TaskStreamModal` |
| **Speculative Generality** | `RangeFilterControl` (dead file); `getBacktestsAll` options; Sidebar `/accounts`; SmartHarvest single-option select; `handleControl` play-arm; OptimizeModal identical ternary; `drawdown_min/max` vs `max_drawdown_min/max` | Delete / consolidate |
| **Primitive Obsession** | Metric units (fraction vs % vs ×100) untyped across StatCard callers, BacktestsPage, PortfolioDetailPage, ComparePage, OptimizationResults; money as bare numbers; win_rate dual-convention heuristics | Branded types or a `Metric` value object carrying unit |
| **Data Clumps** | `NumericBoundsState`/`NumericFiltersState` identical twins (BacktestsPage:40-58); start/end date pairs through 8 files; `{min,max}` redefined in 3 components | One `Range` + `MetricFilter` type |
| **Repeated Switches** | Threshold-color ladders (`sharpe>=1 ? green : >=0 ? amber : red`, psr ≥0.95…) re-implemented in BacktestsPage, MatrixGridPage, PortfolioDetailPage, PortfoliosPage, BacktestDetailPage | Shared `metricTone(metric, value)` map |
| **Shotgun Surgery** | Adding one metric touches: types.ts, BacktestsPage (bounds+filters+sort+slider), ComparePage METRICS, MatrixGridPage, PortfolioDetailPage columns, StatCard call sites | Central metric registry |
| **Middle Man** | `getStrategy`/`getStrategyDetail`, `getPortfolio`/`getPortfolioDetail` | Collapse |
| **Global Data** | `backtestsAllCache/Promise` (BacktestsPage), `liveStatsSupported` (client.ts) | Move into query cache |
| **Refused Bequest** | None — no inheritance in the area | — |

## 3. Fixed checklist coverage (TS/React)

- **Unhandled promise rejections**: DataManagementPage `try/finally` without catch (major); client.ts 204-parse risk; LiveTerminal post-unmount setState; heartbeat `send` race. Everything else audited — `void`-prefixed call sites generally have catches.
- **Stale closures**: largely avoided via refs (DataManagementPage's mirror pattern is the cost); no live stale-closure bug found beyond the LiveDetail effect-loop (inverse problem).
- **Missing error boundaries**: none exist anywhere (major, App/main).
- **XSS / `dangerouslySetInnerHTML`**: zero occurrences in the area (all 61 files read); Markdown path is escape-by-default. **Verified clean.**
- **N+1 fetches**: LiveHub per-WS-message fan-out (major); DownloadModal/CreateBacktestModal per-keystroke bursts; DataManagement delete-all loop; PortfolioDetail redundant contributions fetch; TaskManager+TaskHub+LoggingPage unconditional polling.

## 4. Maintainability hot spots (ranked)

1. `BacktestsPage.tsx` — 1835 lines + the 41-dep `filterPanelContent` useMemo.
2. `LiveHub.tsx` / `LiveDetail.tsx` — duplicated financial engine + 1700/870-line files.
3. `DataManagementPage.tsx` — 2199 lines with the state/ref mirror pattern.
4. Chart bootstrap duplication (5 sites) — one `useDarkChart` hook would delete ~300 lines.
5. Metric-unit conventions — untyped and mutually contradictory; the highest-leverage type-level fix in the area.

## 5. Files with no findings

Fully scanned with nothing to report: `components/Badge.tsx`, `components/FilterBar.tsx`, `components/LoadingSpinner.tsx`, `hooks/liveStreams.ts`, `lib/optimizationEvidence.ts`, `lib/queryClient.ts`, `eslint.config.js`, `vite.config.ts`. (All other 53 files produced at least one finding above.)

---

## Finding counts by severity

Counts are per tagged finding bullet in §1 (severity-tagged items only; untagged "scanned / nothing found" and observational notes are excluded). The LiveHub `major→grouped` entry is a cross-reference to the LiveDetail duplicated-engine finding and is not double-counted.

| Severity | Count |
|---|---|
| Critical | 1 |
| Major | 16 |
| Minor | 147 |
| **Total** | **164** |

Severity distribution is dominated by `minor` because the area's systemic issues (duplication, unit ambiguity, polling hygiene) were itemized per file rather than collapsed into one bullet each; the highest-impact individual findings are the critical LiveHub click-blocking overlay and the majors on error boundaries/Sentry absence, page-scoped trade statistics, module-level backtest cache, the LiveDetail refetch loop, the LiveHub request cascade, render-crash risks in OptimizationResultsPage/PortfolioDetailPage, the Sparkline hooks violation, and the DownloadModal coverage-request storm.
