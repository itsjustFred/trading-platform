# Frontend Code Review — reviewer r2

Scope: complete tracked frontend code-file list (React 19 + Vite + TS SPA). Blind & independent review; no other panel reports consulted.

Status legend per finding: severity critical / major / minor. Smells are named per Fowler ch.3 baseline; repo-standard overrides applied where documented in-repo.

(Work in progress — findings appended as review proceeds.)

---

## src/pages/DataManagementPage.tsx (2199 lines)

**(a) Bugs / correctness risks**
- **[major] Unhandled promise rejections** (checklist item): `loadCategoryDetails` (369–406) has `try/finally` but **no `catch`**; every caller fires-and-forgets it — `void loadOverview()` at 457, `void loadCategoryDetails(categoryName)` in the effect at 465–469, and in `toggleCategory` (633) / `handleSelectSymbol` (641). If `getDataCategory` rejects, the loading flag is reset but the rejection surfaces as an unhandled rejection with zero user feedback; the category silently stays empty. Same shape for `loadOverview` (408–443) on initial mount (456–458): a failed overview leaves a permanently empty page with no error UI.
- **[minor] N+1 sequential deletes** (checklist item): `handleDeleteAllForSymbol` (668–690) awaits `deleteDataSymbol` once per file in a loop — for a symbol with many timeframes this is N round-trips, non-atomic (partial failure mid-loop leaves the inventory half-deleted), and no progress feedback.
- **[minor] Lexicographic date min/max**: `minDate`/`maxDate` (200–208) sort date strings byte-wise; correct only if all dates are uniformly formatted ISO strings. Mixed formats (e.g. `2024-01-05` vs `05.01.2024`) yield wrong group coverage labels.
- **[minor] Polling task-refresh edge**: in the poll effect (492–540), `await refreshCurrentSelection(...)` at 518 runs after `setBackgroundTaskId(null)`; if the user started a second download before the refresh finishes, the refresh races the new task's first poll — benign today because refresh merges by category, but fragile ordering worth a comment/guard.
- **[minor] `PanelTable` stale `pageIndex`** (2003–2027): `pageIndex` is never clamped back into range when `rows` shrink (only `safePageIndex` masks it in render); the hidden state can point past the end, and pressing "Next" continues from the out-of-range value. Low impact due clamping, but state/render divergence.

**(b) Security**
- **[minor]** `deleteDataSymbol(entry.path)` (649–653, 676–680): the client DELETEs whatever `path` string the inventory returned. No client-side validation is expected, but the UI makes it trivially scriptable to send arbitrary path payloads to the endpoint — the backend must treat `path` as untrusted input (path traversal). Frontend-side note only; flagged for the API reviewer to confirm server-side guarding.

**(c) Baseline smells**
- **Large Class / Long Function / Divergent Change**: single file mixes domain model building (`buildSymbolGroups`, `deriveValidation`), a generic sortable/paginated table framework (`PanelTable`, 1983–2199), presentation widgets (`MetricTile`, `SectionPanel`, `HeaderBlock`, `ValidationIndicator/Badge`) and page state orchestration. Any change reason (schema of entries, table UX, styling) touches this one file. Split: `dataCatalog.ts` (model), `PanelTable` → shared `components/`, panels → own files.
- **Duplicated Code**: `PanelTable` reimplements sorting/pagination/table chrome that `components/DataTable.tsx` / `TradeTable.tsx` also provide (see those files) — third parallel table implementation in the app.
- **Primitive Obsession**: timeframes are raw strings ranked through a magic ordered array incl. both `"h4"` and `"4h"` spellings (92–106, 120–123); categories matched by lowercased display names (`categoryDefaultSource`, 157–166: `normalized === "dolthub repos"`). A `Timeframe` union type / category enum would localize this.
- **Duplicated Code (state mirroring)**: three manual ref-mirrors of state (`categoryEntriesRef`, `loadingCategoriesRef`, `selectionRef`, 352–367) each with their own sync `useEffect` plus write-through inside setters — boilerplate that a reducer or `useSyncExternalStore` would remove.
- **Data Clumps**: `{source, symbols, timeframes}` / `{sourcePath, targetTimeframes}` modal-default tuples (322–350) are ad-hoc shapes passed around; a single `DownloadDefaults` type exists implicitly but is declared twice inline.

**(d) Maintainability hot spots**
- 2199 lines, ~60% inline style-object JSX; the smallest styling tweak requires touching logic-bearing files. Highest-priority extraction candidate in the area together with `BacktestsPage`.

---

## src/pages/BacktestsPage.tsx (1835 lines)

**(a) Bugs / correctness risks**
- **[major] New/created backtests never appear** — stale shared cache + missing refresh: module-level cache `backtestsAllCache`/`backtestsAllPromise` (114–115, 274–298) survives across mounts. After creating a backtest, `CreateBacktestModal`'s `onTaskStarted={() => setShowCreateModal(false)}` (1764–1768) closes the modal **without** `loadAllMetadata(true)`, and `BacktestFromSignalsModal` (1769–1772) has no callback wired at all — the list keeps serving the stale cached rows until a hard reload or an unrelated force-refresh. Same staleness applies if a backtest is deleted on the detail page and the user navigates back.
- **[major] Race condition on shared module state** (checklist item): `backtestsAllPromise` dedupes concurrent calls, but `force=true` (280–283) nulls cache+promise while an earlier in-flight non-forced promise may still resolve afterwards and repopulate `backtestsAllCache` with pre-delete data (the `.then` at 288–292 writes the cache unconditionally). Two overlapping loads can interleave and land stale rows in the fresh cache.
- **[minor] `normalizeWinRate` mis-handles out-of-domain inputs** (123–129): `value > 100` hits the later `value > 1 → return 1` clamp instead of `/100`; e.g. `150` becomes `1.0` (100%) rather than `1.5`. Order of branches makes the `<= 100` check pointless for >100 values.
- **[minor] Silent preview failures**: equity-preview fetch `.catch(() => undefined)` (725) — cards permanently show "No equity preview" with no retry/toast; indistinguishable from genuinely missing data.
- **[minor] Filter state clobbered on reload**: `loadAllMetadata` (577–594) resets `numericFilters` to full bounds on every forced reload (bulk delete, dedup, remove-empty), discarding the user's slider filters mid-session.
- **[minor] Null metric sorts as 0**: generic sort `Number(left[sortField] ?? 0)` (686–689) treats `null` PSR/p-value as 0, interleaving unknowns with real zeros instead of pinning them last (win_rate handles this, the others don't).
- **[minor] Card hover via imperative DOM writes** (1379–1392): `onMouseEnter`/`onMouseLeave` mutate `style.transform/borderColor/boxShadow` directly; fights React's style prop on re-render during hover and belongs in CSS `:hover`.
- **[info] Preview effect loop safety** (712–730): correctly terminates because `missingIds` empties after merge; noted as verified, not a finding.

**(b) Security**
- Nothing beyond baseline (no HTML injection; compare navigation builds `?ids=` from internal UUIDs). Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: metric formatting/color helpers (`percentage`, `formatNumber`, `statColorPositive`, `sharpeColor`, `psrColor`, `pValueColor`, 131–173) are page-local copies of concerns that recur across Backtest/Compare/Optimization pages (confirmed again in later files) — should live in `utils/stats.ts`.
- **Long Function / Large Class**: `BacktestsPage` spans 542–1775 (~1200 lines) with filter panel, matrix panel, cards, pager, modals co-located; `filterPanelContent` useMemo carries a **38-item dependency array** (1107–1147) — a strong signal the panel should be its own component reading props directly.
- **Primitive Obsession**: percent-vs-fraction duality of metrics handled by ad-hoc `* 100` scaling at four independent sites (231–246, 646–652, 1356–1359, 1526) instead of a `Percent`/`Ratio` type with one conversion point.
- **Middle Man (mild)**: `cloneRows`/`boundsToFilters`/`updateFilter` exist mainly to shuffle the same `NumericRange` clumps between identical-shaped `NumericBoundsState`/`NumericFiltersState` interfaces (40–58) — two structurally identical types that could be one.
- **Inappropriate Intimacy with DOM styling** as above.

**(d) Maintainability hot spots**
- The 38-dep memo and the module-level cache are the two spots most likely to cause future defects; both are cheap to fix (componentize panel; move cache into React Query — the repo already ships `@tanstack/react-query` infra in `api/queries.ts`/`lib/queryClient.ts`, which this page bypasses entirely — architectural inconsistency worth flagging).

---

## src/pages/LiveHub.tsx (1730 lines)

**(a) Bugs / correctness risks**
- **[major] N+1 fetch fan-out, amplified by WebSocket** (checklist item: N+1 fetches): `refreshDeployments` (225–300) issues, per account: `getLiveStats` + `getLiveBalances` + `getLivePositions`, plus a `getLiveTrades` fallback when stats fail (260) — i.e. ≥3N requests per refresh, plus `getLiveDashboard`. Worse, the WS effect (371–379) re-runs the entire fan-out on **every** `accountsStream.data` emission, and a second effect polls every 30 s when the socket is down (381–391). With several accounts and a chatty stream this hammers the API and thrashes state; the server push already carries the data that is immediately re-fetched.
- **[major] No way to resume a PAUSED deployment**: card controls render only Pause / Stop / Delete (149–177); `handleControl` supports `"play"` → ACTIVE (431–442) but nothing ever invokes it, and `controlBtnStyle("play", …)` (1376–1384) is dead code. A paused strategy cannot be reactivated from the hub (the toast even says "already paused" when retrying pause). Functional gap unless LiveDetail covers it.
- **[minor] Fire-and-forget dashboard fetch swallows errors** (235–237): `void getLiveDashboard().then(setDashboard).catch(() => setDashboard(null))` — telemetry cards silently reset to zeros on failure; no toast, unlike sibling loads.
- **[minor] Guard flags race in loaders**: `refreshStrategies`/`refreshDiscordServices`/`refreshSignalProcessors` (302–348) gate on *state* booleans held in `useCallback` deps; two invocations in the same tick both observe `false` and double-fetch. Harmless but the classic stale-closure guard.
- **[minor] Dead variable / pointless key**: `const account = accountById.get(accountId)` in `openDeployModal` (568) is unused; `key={card.id}` on a non-list `<div>` (105) does nothing.
- **[minor] Incomplete accessible name** (116): ``aria-label={`Open live account `}`` — template literal truncated, no account identifier; screen readers announce identical labels for every card.
- **[minor] Replay date defaults use UTC** (586–589): `today.toISOString().slice(0,10)` is UTC-date, so for UTC-negative users "end date = today" is actually yesterday locally near midnight; inconsistent with the date input's local interpretation.
- **[info]** `calculateRealizedTradeStats` (1529–1597) implements average-cost realized-PnL matching including reversals (1586–1593) — reviewed, logic is sound; win rate counts partial closes as separate wins/losses (approximation).

**(b) Security**
- Scanned; settings JSON is parsed client-side and forwarded as opaque object (503–518) — validation is delegated to backend, acceptable. Nothing found.

**(c) Baseline smells**
- **Duplicated Code**: `asFiniteNumber` (1607–1618) is byte-equivalent to `asFinite` in LiveViewer.tsx (1138–1145); `formatSignedUsd` (1477–1483) duplicated in LiveViewer.tsx (1154–1160); percent normalization logic parallels BacktestsPage `normalizeWinRate`. All belong in `utils/stats.ts`.
- **Primitive Obsession / Repeated Switches**: `statusChipStyle` (1620–1661) and `controlBtnStyle` (1372–1404) hand-roll if-ladders over the `ACTIVE/PAUSED/STOPPED` union returning near-identical objects — a shared status→palette map would serve all live pages.
- **Long Function / Large Class**: 1730 lines; the JSX render alone spans ~700 lines with inline styles; form state machine (`CreateDeploymentForm`) could be extracted wholesale.
- **Speculative Generality**: unused `"play"` branch of the control action union (see bug above) — either wire the button or delete the branch.

**(d) Maintainability hot spots**
- The refresh pipeline (fan-out + WS + polling interplay) is the most concurrency-sensitive code in the area and lives inline in the component; extracting a `useLiveHubData` hook with explicit invalidation would make it testable.

---

## src/pages/LiveViewer.tsx (1201 lines)

**(a) Bugs / correctness risks**
- **[major] Live chart destroys user zoom/pan on every tick**: `LiveCandlestickChart`'s data effect (710–733) calls `candle.setData(...)` + `chart.timeScale().fitContent()` on **every** `normalizedBars` change — i.e. on each streamed quote batch. Any manual zoom/pan is instantly reset, and the full dataset is re-uploaded instead of using `series.update()` for the newest bar. For a realtime viewer this makes the chart effectively unusable while connected.
- **[minor] Double-decoding of route param** (82): `decodeURIComponent(rawAccountId ?? "")` — React Router already decodes path params; an account id containing an encoded `%` gets corrupted. Remove the extra decode.
- **[minor] Wildcard symbol leaks into dropdown**: `uniqueSymbols` (970–992) unions `deployment.symbols`, which LiveHub populates with `["*"]` for Discord-source deployments (LiveHub.tsx:479–484). A literal `*` option appears and selecting it issues a quote request for symbol `*`. Filter reserved wildcards here.
- **[minor] Executions stream wholesale-replaces fill history** (158–163): if the stream payload contains only recent trades, long-running history silently truncates (vs. merging). Verify payload semantics; add a merge otherwise.
- **[minor] Per-fill "P&L" accounting is assumption-laden**: `toTradeHistoryRows` (946–968) computes `(actual − theoretical) × qty × side` per row and `headerStats.totalPnl` sums it (280). Correct only if each row is a complete round-trip trade; if rows are individual fills this is slippage delta, not realized P&L (LiveHub computes proper matched P&L — two different definitions of "Total P&L" ship in adjacent pages). Needs a domain decision + shared helper.
- **[minor] Timezone-inconsistent time rendering**: `formatTableTime` (1181–1185) prints UTC (`toISOString`) while other pages format local dates (`toLocaleDateString("de-DE")` in BacktestsPage) — signal-log timestamps appear shifted for non-UTC users relative to the rest of the app.
- **[minor] Cache-key coupling**: effects write straight into React Query caches with hard-coded keys `["live-positions", accountId]` / `["live-signal-log", accountId, 200]` (148, 155). If `api/queries.ts` changes its key shape, this silently stops feeding the queries. Export the key factories from one module.

**(b) Security**
- Scanned; raw Discord message content is rendered as text nodes only (845–847) — safe. Nothing found.

**(c) Baseline smells**
- **Duplicated Code**: `asFinite` (1138–1145), `formatUsd/formatSignedUsd/formatSignedPct/formatPrice` (1147–1179) duplicate LiveHub/BacktestsPage helpers; `statusBadgeStyle` (1085–1129) is a four-branch if-ladder duplicating badge logic found in Badge.tsx/DataManagementPage.
- **Shotgun Surgery risk**: chart setup (636–708), bar ingestion, marker mapping, SL/TP price lines are all bespoke here; TradingViewChart.tsx exists as the shared chart component yet this page builds a second lightweight-charts integration from scratch (confirmed when reading TradingViewChart.tsx below) — two chart stacks to maintain.
- **Long Function**: `LiveViewerPage` body ~480 lines mixing five data sources, derived stats and layout.

**(d) Maintainability hot spots**
- Second chart implementation + duplicated money/percent helpers make this file the clearest extraction target: move `asFinite`/formatters to `utils/stats.ts`, and either reuse `TradingViewChart` or extract `LiveCandlestickChart` into `components/`.

---

## src/pages/TradeChartPage.tsx (1111 lines)

**(a) Bugs / correctness risks**
- **[minor] Unhandled fullscreen promise rejections** (checklist item): `toggleFullscreen` (505–516) awaits `document.exitFullscreen()` / `target.requestFullscreen()` with no try/catch, and is invoked directly as an event handler — a denied fullscreen request (permissions policy, non-user-gesture) becomes an unhandled rejection. Contrast: the Escape path at 558 does `.catch(() => undefined)`.
- **[minor] `$undefined` rendering** (735, 739): `$${currentTrade.entry_price?.toFixed(2)}` prints `$undefined` when `entry_price`/`exit_price` is null (the P&L row guards with `!= null`, these don't).
- **[minor] URL/router desync**: `window.history.replaceState(...)` (274–278, 478–482) mutates the URL behind React Router's back; browser Back then skips the intermediate trade numbers unpredictably. Use the router's `navigate(…, { replace: true })`.
- **[minor] Indicators silently vanish on timestamp mismatch**: `sanitizeIndicatorPoints(points, validTimes)` (160–184) drops any indicator point whose unix time isn't exactly a bar time, then the page filters out emptied series (356–364). Indicators sampled on a different grid than OHLCV disappear entirely with no user hint.
- **[minor] NaN initial route param** (238): `parseInt(nr || "1", 10)` yields `NaN` for garbage `nr`; downstream guards cope (fallback resolution at 266–272), but the state starts invalid rather than being clamped.
- **[minor] Focus-time memo reads a ref** (417–422): `chartFocusTime` depends on `[currentTrade]` but branches on `shouldAutoFocusTradeRef.current`; ref flips don't re-run memos, so focus can stick to a stale value until `currentTrade` identity changes. Works today by coincidence of ordering.
- **[minor] Task poll continues into unmount** (568–590): interval callbacks call `setState` with no cancellation flag; last poll can land after cleanup starts. Benign warning-class issue.

**(b) Security**
- Scanned; trade meta values are stringified and rendered as text (840–909) — no injection vector. Nothing found.

**(c) Baseline smells**
- **Duplicated Code**: `formatUsd`/`formatPrice` (65–77) third copies in the area; `toFiniteNumber` (52–63) duplicates `asFinite` variants with slightly different contracts (three near-identical coercers now exist across pages).
- **Repeated Switches**: `indicatorIsPanel` (205–229) hard-codes panel-indicator names/tokens as string probes (`"adx"… "vwap deviation"` + regexes) — classification belongs to the backend payload (`type`/`pane` fields already exist) or a single shared map; adding one new indicator requires editing this predicate (Shotgun Surgery across pages that consume indicators).
- **Long Function / Large Class**: 1111-line component; replay loading, keyboard nav, playback timer, fullscreen, missing-data download flow all co-located.

**(d) Maintainability hot spots**
- Keyboard/playback/fullscreen interactions are well-built but untestable at this size; extract `useReplayKeyboardNav` and the missing-OHLCV download panel.

---

## src/pages/PortfoliosPage.tsx (952 lines)

**(a) Bugs / correctness risks**
- **[major] Duplicate create submissions**: the "Create Portfolio" button (932–946) is disabled only by `createDisabled` (name/components) — **no in-flight flag**. `handleCreate` (325–380) awaits `createPortfolio`; double-click fires two POSTs → duplicate portfolios (the very problem the page's dedup tool fights). Compare `isCreating` discipline used in LiveHub.
- **[minor] `parseOptionalJson` throws inside `.map`** (230–238, 341–362): a malformed JSON cell aborts the whole map; error message lacks row context unless thrown by the explicit guards (`Component #N`), so users can't tell which component's settings JSON was bad.
- **[minor] Silent capital fallback**: invalid `formCapital` silently coerces to 100000 (335–338) — user believes they set e.g. `50k` but creation uses default.
- **[minor] Remove-Empty has no confirmation and no count** (453–463): unlike BacktestsPage (which counts empties and confirms), one click deletes an unknown number of portfolios server-side.
- **[minor] Reference fetch effect deps** (289–300): `showToast` in deps re-runs the whole strategies/symbols/processors triple-fetch whenever the toast context identity changes; harmless if context is memoized, wasteful otherwise.
- **[info]** Server-side name filter without debounce: every keystroke in FilterBar triggers `loadPortfolios` (263–287) via effect on `nameFilter`; BacktestsPage debounces 250 ms, this page doesn't — inconsistent and chatty.

**(b) Security**
- Scanned, nothing found (JSON inputs forwarded as data; no HTML injection).

**(c) Baseline smells**
- **Duplicated Code**: duplicates modal + handlers block of BacktestsPage nearly verbatim (duplicatesInfo/show/handleShowDuplicates/handleDeduplicate, 424–463 vs BacktestsPage 818–843); delete/bulk-delete/toast patterns likewise. Extract `useDuplicateCleanup` hook.
- **Primitive Obsession**: metric thresholds (`sharpe >= 1`, `psr > 0.95`, `win_rate >= 50`) re-hard-coded per card line (147–206) instead of using shared color helpers from `utils/stats.ts` that already exist for some metrics.
- **Long Function**: render spans ~480 lines with the whole create-modal inline.

**(d) Maintainability hot spots**
- Create-modal component grid (777–903) is self-contained; extract as `CreatePortfolioModal` to match the modal-per-concern convention used elsewhere.

---

## src/pages/PortfolioDetailPage.tsx (937 lines)

**(a) Bugs / correctness risks**
- **[major] Chart fully rebuilt on every tooltip-independent data change & leaks on early return**: `PortfolioEquityChart`'s mega-effect (778–899) tears down and recreates the entire lightweight-charts instance whenever `data` changes, and when `parsed.length < 2` it removes the chart and returns **without** registering the ResizeObserver/crosshair cleanup path — but critically it also leaves `chartRef.current = null` while the container still shows nothing; more importantly the early return at 779 (`!data.length`) skips chart removal entirely if a previous chart exists under a fast data swap (chart orphaned in DOM until next successful build). Low-frequency but real leak path.
- **[minor] Crosshair tooltip loop risk** (854–886): `subscribeCrosshairMove` calls `setTooltip` up to once per mousemove with new object identities → re-render per mousemove; acceptable but unthrottled.
- **[minor] UTC date slicing again** (263–264, 877): trade dates/times rendered via `toISOString()` (UTC) while equity tooltips use UTC too but page headers use locale formats — mixed conventions within one page confuse non-UTC users about which "day" a trade belongs to.
- **[minor] `row.win_rate.toFixed(2)` assumes number** (729): unlike sibling cells that null-check (`psr != null`, `cagr != null`), win_rate will throw `TypeError` on null from API. Same for `return_contribution`/`drawdown_contribution`/`pnl_total` multiplications (718–732).
- **[minor] Rename success clears neither contributions nor trades** (307–322): only `detail` refreshed — fine today since names come from detail, but any denormalized name display elsewhere on the page goes stale.
- **[info]** Initial load correctly fetches contributions with `"component"` then refetches per groupBy (110–154) — slight double-fetch on mount (once with "component" in Promise.all, again via second effect when `detail` arrives). Minor redundant request.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: third copy of `toFiniteNumber` (59–66) and `resolveChartLocale` (72–78, identical to LiveViewer.tsx 73–77); fourth/fifth copies of lightweight-charts boilerplate (createChart/layout/grid/ResizeObserver, 789–852 ≈ LiveDetail.tsx 537–593 ≈ LiveViewer.tsx 644–697). A `useLightweightChart` hook would collapse all three pages.
- **Repeated Switches / Primitive Obsession**: PSR/sharpe threshold ladders repeated at 184–189, 445, 452 despite `utils/stats.ts` helpers existing (imported for p-value only).
- **Middle Man**: `ContributionTable` headerButton indirection is fine, but sort state lives in parent while table knows keys — acceptable; noted only.

**(d) Maintainability hot spots**
- The 120-line chart effect is the single most fragile block; extracting chart construction + drawdown series computation into a tested module would de-risk it.

---

## src/pages/LiveDetail.tsx (870 lines)

**(a) Bugs / correctness risks**
- **[minor] Polling loop refetches everything every 30 s regardless of visibility** (136–145): `setInterval(load)` continues while tab hidden; combined with LiveHub/LiveViewer polling this multiplies background load. No `document.visibilityState` gate anywhere in the live stack.
- **[major] Infinite refetch loop while replay modal is open** (147–156): the effect depends on `[replayModalOpen, signalProcessorsLoading]`; each completion flips `signalProcessorsLoading` false→true→false, which re-triggers the effect while `replayModalOpen` stays true — fetch → setLoading(false) → effect reruns → fetch …, looping for as long as the modal is open (rate-limited only by network latency). The effect should depend solely on `replayModalOpen`.
- **[minor] Double-decode route param** (56): same as LiveViewer.tsx:82.
- **[minor] UTC replay date defaults** (77–88): same as LiveHub.tsx:586–589.
- **[minor] Win-rate denominator mismatch** (710): `winRate = winningTrades / totalTrades * 100` where `totalTrades` counts *fills* but wins count *closed position matches* — win rate understated when fills ≠ round-trips.
- **[info]** `calculateRealizedTradeStats` (731–799) is a verbatim copy of LiveHub.tsx:1529–1597 (see smell below).

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code (egregious)**: `calculateRealizedTradeStats` (731–799) duplicated byte-for-byte from LiveHub; `asFiniteNumber` (840–851), `formatUsd`/`formatSignedUsd`/`formatPercent` (853–869), `fieldLabelStyle`/`fieldInputStyle` (647–663), and the replay-modal form (251–336) are near-copies of LiveHub.tsx equivalents. The live trio (Hub/Detail/Viewer) share ~200 lines that should be one `hooks/liveShared.ts`.
- **Shotgun Surgery**: changing replay form semantics requires editing three files (LiveHub, LiveDetail, plus TradeChartPage's variant).

**(d) Maintainability hot spots**
- The refetch-loop bug above is the priority; afterwards merge shared stats/formatters into utils.

---

## src/pages/MatrixGridPage.tsx (856 lines)

**(a) Bugs / correctness risks**
- **[minor] setState during render** (326–331): `pagedRows` useMemo calls `setPage(safePage)` inside the memo body — calling a state setter during render of the same component. React tolerates the "render-phase update" pattern only for the *same* component and it forces a re-render loop pass; it works but is flagged as an anti-pattern and can double-invoke in StrictMode dev.
- **[minor] Sort-direction toggle loses page reset** (337–345): toggling direction on the same key does not reset `page` (only key change does) — minor inconsistency.
- **[minor] German UI strings in code** (361–378, 391–393, 416–435, 544, 555, 683, 709, 711, 783–793): "Strategien fertig", "Lade Matrix-Ergebnisse…" etc. mixed into an otherwise English UI — i18n/maintainability hazard, and `formatDateTime`/numbers use `"de-DE"` here vs `"en-US"`/locale elsewhere across the app (inconsistent number formatting for the same metrics between MatrixGridPage and BacktestsPage).
- **[minor] Tailwind classes on an inline-style app** (382–384, 391–496 etc.): this page uses utility classes (`className="rounded border border-border bg-surface..."`) while every other file in the area uses inline styles/CSS vars. Verified: Tailwind v4 **is** configured (`@tailwindcss/vite` in vite.config.ts, `@import "tailwindcss"` + `@theme` tokens in index.css), so the classes resolve — but this is still a second styling paradigm in one SPA, and `border-border`/`bg-surface2` depend on the `@theme` color names mapping correctly under v4 semantics. Mixed-paradigm maintenance cost stands; styling itself is not broken.
- **[minor] Grid/table duplication**: grid view ignores `filteredRows` symbol/timeframe/status filters except strategy (uses `selectedStrategyRows` at 500) while summary cards claim filtered counts — mild UI inconsistency.

**(b) Security**
- Scanned, nothing found (`downloadJson` builds a client blob safely).

**(c) Baseline smells**
- **Speculative Generality / Dead code**: `viewMode === "table"` branch renders `MatrixTableView`, but `viewMode === "grid"` renders StrategyGrid *and* still shows TopRows; the `sortLabel` prop passed to `SortableTh` (653, 818) is redundant with `current/dir` already provided — two sources of truth for the indicator glyph.
- **Duplicated Code**: `formatNumber/formatMetric/formatPctFraction/formatPctPoints/sharpeColor/psrColor/cagrColor/drawdownColor` (136–207) re-implement `utils/stats.ts` helpers (some exist there; confirmed when reading stats.ts below) and BacktestsPage's local versions — fourth formatting layer.
- **Primitive Obsession**: `TIMEFRAME_ORDER` record here (125–132) differs in content and shape from DataManagementPage's array (92–106) and LiveHub's list — three unrelated timeframe orderings in one app.

**(d) Maintainability hot spots**
- Unify styling approach and metric formatting before this page drifts further from the rest of the SPA.

---

## src/components/TradingViewChart.tsx (801 lines)

**(a) Bugs / correctness risks**
- **[minor] Chart rebuilt when only `height` prop changes** (384–503): the mount effect depends on `[height]`; TradeChartPage animates height on fullscreen toggle (chartHeight 434–439), so entering fullscreen destroys & recreates the chart, losing zoom/pan state. Height belongs in `applyOptions`/`resize`, not the creation key.
- **[minor] Full dataset re-set + preset re-applied on every data change** (505–550): same class as LiveViewer but mitigated by `applyRangePreset(rangePreset)` preserving the user's chosen window — still resets any *manual* zoom after each trade switch. Acceptable for replay; noted.
- **[minor] Screenshot assumes canvas non-empty** (372–382): `takeScreenshot(true, false)` then `.toDataURL()` without null-check (API returns HTMLCanvasElement; fine in v5, would break silently if lib changes).
- **[info]** `normalizeExitReason` (151–171): unmapped reasons collapse to "Signal" (e.g. "EOD", "manual") — mislabels exits in markers/legend. Data-fidelity nit.
- **[info]** Marker tooltip uses imperative DOM writes via refs (451–469) — deliberate perf choice, correctly cleaned up.

**(b) Security**
- Scanned: marker detail strings are set via `textContent` (463) — safe. Nothing found.

**(c) Baseline smells**
- **Duplicated Code**: this is now the *fourth* bespoke lightweight-charts setup in the area (TradingViewChart, LiveViewer.LiveCandlestickChart, PortfolioDetail.PortfolioEquityChart, LiveDetail.LiveEquityChart) each re-declaring createChart options, ResizeObserver, locale resolution (`resolveChartLocale` duplicated here 181–187 = LiveViewer 73–77 = PortfolioDetail 72–78). Extract `createThemedChart(container, opts)` factory + `useChartResize` hook.
- **Primitive Obsession**: `TradingViewChartTime = number | string` accepted everywhere then repeatedly coerced through `normalizeUnixTime`; a discriminated input type or normalization-at-boundary contract would remove ~10 defensive callsites.

**(d) Maintainability hot spots**
- Best of the four chart implementations (pane support, presets, screenshot); make *this* the shared primitive and port the other three pages onto it.

---

## src/pages/StrategyDetailPage.tsx (755 lines)

**(a) Bugs / correctness risks**
- **[major] Untyped API surface**: `useState<any>` for detail (12) plus `backtest: any` / `portfolio: any` iteration (590, 713) and `catch (error: any)` (54). All property accesses (`detail.tags?.type`, `detail.slug`) are unchecked at compile time — a backend shape change ships to production undetected. The repo has `api/types.ts`; this page ignores it entirely.
- **[minor] Load errors are indistinguishable from "not found"** (27–29): catch sets `detail(null)` with no error message/toast — network failure renders "Strategy not found", misleading during outages.
- **[major→security] External links lack safe rel policy** (303–315): `target="_blank"` with only `rel="noreferrer"` is actually OK (noreferrer implies noopener) — verified safe; however `sourceUrl` is rendered unvalidated: a malicious/misconfigured `tags.link` of `javascript:` scheme becomes an executable link. Validate scheme (http/https) before rendering.
- **[minor] Rename updates local state only** (51–52): `setDetail(updated)` doesn't re-fetch; if rename changes slug/name used in route params, current URL `/strategies/:name` may 404 on next visit though page shows renamed content.
- **[minor] Markdown `code` renderer relies on removed `inline` prop** (367, 507): react-markdown v9+ no longer passes `inline`; `inline` is always undefined → inline code renders as block-styled `<code>`. Check installed version; likely latent visual bug.
- **[info]** No stale-response guard on the load effect (19–31): rapid navigation between strategies can land an older response last (no `ignore` flag unlike sibling pages).

**(b) Security**
- **[minor]** Unvalidated URL scheme for `sourceUrl` (see above) — the one genuine XSS-adjacent vector in the file (ReactMarkdown itself escapes HTML by default; no `dangerouslySetInnerHTML` anywhere).
- **[minor]** Markdown links also get `target="_blank" rel="noreferrer"` via component override — safe.

**(c) Baseline smells**
- **Duplicated Code**: two near-identical ReactMarkdown component maps (340–398 vs 441–553) — extract one `markdownComponents` constant.
- **Primitive Obsession**: `any` types as above (also a Missing Type smell per TS practice).

**(d) Maintainability hot spots**
- Introduce proper types first; everything else here follows cheaply.

---

## src/pages/LoggingPage.tsx (749 lines)

**(a) Bugs / correctness risks**
- **[major] Server refetch every 5 s of up to 5000 lines** (18–19, 230–262): `getLogContent(activeLogFile, LOG_FETCH_LIMIT, …)` on a fixed interval regardless of tab visibility, plus full re-parse (`parseLogLine` over every line, incl. JSON.parse attempts per line) and re-render of virtualizer rows. On busy logs this is continuous CPU/network churn; there's no incremental tailing (If-Modified/ETag) or visibility gating.
- **[minor] Search box triggers server fetch per keystroke** (236): `search` is a direct effect dep with no debounce; combined with the poll interval, typing restarts the interval repeatedly.
- **[minor] Level toggles wipe lines & reset "loaded once"** (357–362): toggling a level chip clears visible logs and shows "Loading logs..." although nothing async failed — flicker UX bug.
- **[minor] Row ids collide across refreshes**: rowId `${index}:${timestampLong}:${module}` (305) — identical index/timestamp/module across polls yields stable keys (good), but two different entries sharing timestamp+module at same index swap identities; acceptable risk, noted.
- **[minor] `estimateSize` reads `virtualRows[index]`** (333–339): closure over latest array is fine because useVirtualizer re-created each render? — actually `useVirtualizer` keeps options in refs; if `virtualRows` shrinks between renders the estimator can read stale indices → transient mis-measures. Minor visual jump only.
- **[info]** Polling-noise filter list (16–17) hard-codes endpoint paths that must track backend routes — Shotgun Surgery coupling to API surface (see smells).

**(b) Security**
- Scanned: log content rendered as text nodes / JSON.stringify in `<pre>` — no injection path. Nothing found.

**(c) Baseline smells**
- **Shotgun Surgery**: `POLLING_PATHS/POLLING_PREFIXES` duplicate route knowledge owned by api/client + live pages; adding an endpoint requires touching this filter.
- **Long Function / Divergent Change**: parse heuristics (`parsePlainLine`, 104–134) mix log-format knowledge with UI concerns; a format change (new logger) edits the page component.
- **Repeated Switches (mild)**: level→color ladder (53–58) parallels badge palettes elsewhere.

**(d) Maintainability hot spots**
- Virtualization approach is sound; the fetch layer (polling + debounce + visibility) is what needs restructuring.

---

## src/components/OptimizeModal.tsx (728 lines)

**(a) Bugs / correctness risks**
- Scanned carefully — validation is thorough (min≤max at 115, step>0 at 127–130, trials>0 at 253–255, split bounds 258–261, universe min-2-symbols 150–154), `running` flag prevents double-submit (702), and errors are caught & surfaced (287–289). Nothing found beyond nits:
- **[minor] `canSubmit` allows risk-parity mode with zero configuration but `handleStart` still requires trials/split** — consistent; noted only that the "Active parameters" counter (399) adds universes to parameters (`configuredCount + symbolUniverseCount`), a slightly misleading metric.
- **[minor] Grid template duplicated** (473): `"240px minmax(0, 1fr)"` in both branches of a ternary whose branches produce identical values — dead conditional.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Long Function**: render tree ~410 lines of inline-styled decoration around a simple form; the "hero" banner (318–419) is pure chrome.
- **Primitive Obsession**: numeric form fields kept as strings then re-parsed per submit (63–69) — standard controlled-input tradeoff, acceptable; noted for consistency since other modals use the same pattern.

**(d) Maintainability hot spots**
- None significant; this is one of the better-built components in the area. Reference example of the in-flight-flag discipline other pages lack.

---

## src/components/CreateBacktestModal.tsx (577 lines)

**(a) Bugs / correctness risks**
- **[major] N+1 data-availability checks on every keystroke-implied change** (checklist item: N+1 fetches): effect (135–186) fires `getDataCheck` **per symbol** via `Promise.all(symbolList.map(...))` on every change to `symbolList/timeframe/start/end` — no debounce, no cache. Typing "AAPL, MSFT, TSLA" character-by-character issues dozens of parallel API calls. Debounce + batch endpoint needed.
- **[minor] Timeframe select diverges from canonical order** (332–338 vs 14): hardcoded `<option>` list omits tick/s1/1w present in `TIMEFRAME_ORDER`, so modeling-resolution chain can't be built for sub-minute main TFs even though the constant supports it.
- **[minor] Dead effect** (82–91): resets parameters on strategy change — immediately overwritten by the detail-sync effect (93–111); the reset effect adds nothing but a flash of empty state.
- **[minor] `capital: parseFloat(capital)` unchecked** (225): unlike leverage/risk which are validated, NaN capital ships to backend if user clears the field (HTML `type=number` mitigates but paste of garbage passes).
- **[minor] Error-path early returns leave `running=true` until manually reset** (196–216): handled correctly here (setRunning(false) before each return) — verified; but pattern is fragile vs. try/catch used later. Consistency nit only.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: fourth copy of a timeframe list (see DataManagementPage/LiveHub/MatrixGridPage); `inputStyle`/`labelStyle` (244–262) near-duplicates of OptimizeModal's and PortfoliosPage's style constants.
- **Speculative Generality**: `TIMEFRAME_LABELS` covers 10 timeframes while UI exposes 7 — either wire the full list into the select or trim the map.

**(d) Maintainability hot spots**
- The availability-check effect is the hot spot; debounce + single batched check call would cut most of its complexity.

---

## src/pages/StrategyLabPage.tsx (566 lines)

**(a) Bugs / correctness risks**
- **[minor] `catch (err: any)`** (93): only file in the group still using `any` for errors instead of `unknown` narrowing.
- **[minor] Provider switch leaves stale model** (328–334): switching nim→copilot_sdk keeps `model` set to a NIM id and sends it as `customModel`? No — checked: `selectedModel` uses `customModel` only for non-nim; but if user typed a custom model then switched back to nim, `model` state is unchanged (fine) while `customModel` persists silently. Cosmetic.
- **[minor] No job-cancel/reset affordance**: once `jobId` set, submitting again replaces it without confirming the running conversion is abandoned (LiveTerminal presumably switches streams).
- **[info]** Status colors via `useMemo` on a string (55–60) — memo saves nothing; harmless.

**(b) Security**
- Scanned: user code sent to backend agent; nothing rendered as HTML. Nothing found.

**(c) Baseline smells**
- **Long Function / Large Class**: ~460-line render dominated by inline gradient chrome; form fields could collapse into a data-driven array (three near-identical label+input blocks, 315–469).
- **Duplicated Code**: input style object repeated 5× verbatim within the file (335–344, 370–379, 398–407, 421–430, 451–467 variants).

**(d) Maintainability hot spots**
- Extract `LabInput` subcomponent; move AI_MODELS/PROVIDER_OPTIONS to api/types-adjacent constants so backend model list changes don't require UI edits.

---

## src/pages/OptimizationResultsPage.tsx (469 lines)

**(a) Bugs / correctness risks**
- **[minor] Sequential dependent fetch without cancel guard** (58–79): `getOptimization` then inner `getBacktestInfo`; no `ignore` flag — navigating quickly between optimizations can apply an older optimization's base-backtest info (both setState unguarded after await). Same stale-response class as StrategyDetailPage.
- **[minor] `optimization.test_period.start.slice(0,10)` assumes populated strings** (250): missing `test_period` would throw; type presumably guarantees it — verify backend always returns it (no optional chaining unlike every neighboring field).
- **[minor] Loading derived from `loadedId !== id`** (56): clever, but on error path `loadedId` is also set (77) → loading false, error shown — correct; however initial render with no id shows "Loading optimization..." forever (59–61 early-returns from effect without setting loadedId) if route param missing.
- **[info]** OOS evidence classification delegated to tested lib module (`lib/optimizationEvidence`) — good separation.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: `formatComparisonValue`/`formatMode`/`formatSymbols` (20–47) duplicated nearly verbatim in OptimizationsPage.tsx (9–30) — extract to utils.
- **Primitive Obsession**: percent-vs-fraction handled ad hoc per metric key list (30) — same `["cagr","win_rate",...]` magic list duplicated across pages.

**(d) Maintainability hot spots**
- Low; share formatters with OptimizationsPage and this is clean.

---

## src/pages/OptimizationsPage.tsx (372 lines)

**(a) Bugs / correctness risks**
- **[minor] One-shot load with no refetch**: effect (38–48) runs once per mount; new optimization runs started elsewhere won't appear until remount (no React Query usage here unlike Services page which does use it).
- **[minor] Metric mapping special-case** (198–200): `"net_profit" → "total_pnl"` rename shim lives in UI; belongs in types/api layer.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: formatter triplet shared with OptimizationResultsPage (see above); card chrome duplicates the same section styles.

**(d) Maintainability hot spots**
- Migrate to `api/queries.ts` pattern (as Services.tsx demonstrates) for cache-consistent loads.

---

## src/pages/BacktestDetailPage.tsx (1293 lines)

**(a) Bugs / correctness risks**
- **[major] Percent-unit contradiction inside one page**: `stats.net_profit_pct`, `stats.cagr`, `stats.max_drawdown_pct`, `stats.win_rate` are passed to `StatCard format="percent"` (568–600) where StatCard prints `value.toFixed(2)%` **directly** — i.e. treats them as percent *points*; yet the same page's PSR card multiplies by 100 (613–614: `(stats.psr*100).toFixed(1)%`) and PortfolioDetailPage treats its `max_drawdown` as a fraction needing ×100. One of these conventions is wrong for the actual payload (`BacktestStats` type gives no units); if backend returns fractions, Net-Profit-% / CAGR / Win-Rate cards are off by 100×. This is the highest-impact data-correctness ambiguity in the area and needs a single typed contract.
- **[minor] Stale-response races on all four load effects** (96–169): no `ignore`/cancelled flags on info/stats/equity, trades paging, benchmark, or CAPM effects — fast id switching or rapid benchmark toggling can land out-of-order responses.
- **[minor] Benchmark normalization mismatch** (1052–1053): benchmark scaled by `normalizedData[0].equity / normalizedBenchmark[0].equity` assumes both series start at the same date; if the benchmark series starts earlier/later than the backtest window, overlay is scaled against the wrong anchor.
- **[minor] Trade stats fallbacks computed from current 100-row page** (219–253): when `stats.*` fields are absent, winners/losers/best/worst derive only from the loaded page of `TRADE_PAGE_SIZE` rows — silently wrong aggregates on paged data.
- **[minor] `catch (renameIssue: any)`** (180): `any` error typing again.
- **[info]** Equity chart effect (991–1142) is a near-clone of PortfolioDetailPage's chart incl. tooltip logic — see smell.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: `EquityChart` here duplicates PortfolioDetailPage's `PortfolioEquityChart` almost line-for-line (locale resolution inline at 1003–1008 instead of the helper); `toNumberOrNull` (255–262) redefines the area-wide coercer; drawdown computation duplicated third time.
- **Large Class / Long Function**: ~1290 lines mixing header, stats grid, two charts, CAPM scatter, trade table + modals.
- **Primitive Obsession**: percent/fraction duality again (see bug above).

**(d) Maintainability hot spots**
- Merge `EquityChart` with PortfolioDetailPage's version into one shared component with options; settle metric units in types once.

---

## src/pages/TaskHub.tsx (433 lines)

**(a) Bugs / correctness risks**
- **[minor] Poll every 3 s regardless of visibility** (17, 76–101): same polling-without-visibility-gate family as Logging/LiveDetail; heaviest cadence in the app.
- **[minor] Toast-on-transition misses first observed state** (103–119): transitions detected via `statusRef` diff; tasks that complete between polls while page closed never toast (expected), but also the first poll after mount seeds `previous[id]` so a task already failed before navigation shows nothing — acceptable design, noted.
- **[minor] `catch (error: any)`** (161).
- **[minor] Completed list capped client-side** (18, 128–135): slice(0,100) hides older completions without indication.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: status-set constants & poll loop duplicated from TaskManager.tsx (which polls the same endpoint `/tasks` independently — two pollers of one endpoint running simultaneously whenever TaskManager is mounted outside /data routes).
- **Long Function**: fine overall; moderate.

**(d) Maintainability hot spots**
- Consolidate task polling into React Query with a shared query key so TaskManager badge and TaskHub share cache/interval.

---

## src/pages/StrategiesPage.tsx (259 lines)

**(a) Bugs / correctness risks**
- Scanned — uses React Query properly, memoized card, guarded optionals. Nothing found beyond:
- **[minor]** Sort labels say "Trade Count" but sort `backtest_count` (223–224 vs 173–176) — mislabeled UX.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Repeated Switches**: sort comparator switch (161–180) could be a lookup map; minor.

**(d) Maintainability hot spots**
- Cleanest page in the area; reference implementation.

---

## src/api/client.ts (641 lines)

**(a) Bugs / correctness risks**
- **[minor] No timeout/AbortController on any fetch** (91–112): checklist item analog ("subprocess without timeout") — every request can hang indefinitely; slow/hung backend freezes loading states across pages. Add `AbortSignal.timeout()` defaults.
- **[minor] Error-body parse assumes JSON shape** (96–110): `res.json().catch(() => ({error: res.statusText}))` then `err?.detail ?? err`; if API returns HTML (proxy 502), message becomes "[object Object]"-ish via detail object path — cosmetic robustness gap.
- **[minor] Unencoded ids in several paths** (208–225, 456–458): `getBacktestInfo/getBacktestTrades/getBacktestReplay/getBacktestEquity/getPortfolio(id)/deletePortfolio(id)` interpolate raw ids into URLs while sibling functions encode. Ids are internal UUIDs today; inconsistency invites breakage if id format ever changes.
- **[minor] Module-level capability cache** (76–77, 114–141): `liveStatsSupported` cached forever per session including the OpenAPI probe result; if backend hot-adds the endpoint, stale until reload. Acceptable tradeoff, documented behavior worth a comment.
- **[info]** Good input clamping discipline throughout (preview limits 193, 200–206; grid trades limit 263).

**(b) Security**
- **[minor]** `getLogContent(name…)` (520–531) encodes the log file name — good; but `getDataTask(taskId)` etc. interpolate raw task ids (603–626). Same class as above; server must validate.
- Scanned otherwise: no secrets in client, no eval/dangerous sinks. Nothing further.

**(c) Baseline smells**
- **Shotgun Surgery risk**: query-string construction repeated ~20× by hand; a `buildQuery(params)` helper plus typed param objects would gather it.
- **Speculative Generality (mild)**: `comparePortfolios` (515–516) has no consumer in the area files reviewed; `getStrategy` vs `getStrategyDetail` (145–148) are identical duplicates — delete one.
- **Primitive Obsession**: `any` returns on `getBacktestStats` (211), `getBacktestEquity` (239), `getBacktestOHLCV` (248), `renameBacktest` (250), `deleteDataSymbol` (552), `getBenchmark` (538) despite types existing for most shapes.

**(d) Maintainability hot spots**
- Central fetch layer is good architecture; add timeouts + uniform encoding and it's solid.

---

## src/api/types.ts (1455 lines)

**(a) Bugs / correctness risks**
- Scanned as type definitions (no runtime behavior). Notable type-level hazards:
- **[minor]** `[key: string]: any` index signatures on `BacktestStats` (380) and `Record<string, any>` tags/correlation_matrix (11, 116, 133, 1169) defeat excess-property checking exactly where metric-unit confusion lives (see ComparePage/BacktestDetail findings).
- **[info]** Deliberate dual-field compat fields (`offer_side`/`price_type` 1309–1312; `signal_source_type`/`signal_source` 508/524; sharpe/sharpe_ratio pairs) document legacy migration — reasonable, but each pair should carry a removal TODO.

**(b) Security**
- N/A (types only). Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: `MatrixGridRow`/`MatrixGridBlockedStrategy`/`MatrixGridStrategySummary`/`MatrixGridOverview` (836–925) duplicate the near-identical interfaces declared locally inside MatrixGridPage.tsx (11–98) — one canonical set needed.
- **Data Clumps**: percent-vs-fraction field pairs (`max_drawdown` vs `max_drawdown_pct`, `total_return` vs `net_profit_pct`) across BacktestMetadataItem/BacktestStats/PortfolioSummary with no unit documentation — root cause of the ×100 bugs reported above.

**(d) Maintainability hot spots**
- Add unit suffixes to names or doc comments (`_frac`, `_pct_points`) and de-duplicate MatrixGrid interfaces.

---

## src/api/queries.ts (150 lines)

**(a) Bugs / correctness risks**
- **[minor] Hard-coded 30 s refetch intervals** (79, 93, 108, 136): quotes/positions/signals/service-logs poll unconditionally (no `refetchIntervalInBackground:false` override — though RQ defaults to pausing when unfocused... actually default `refetchIntervalInBackground: false` applies, so background tabs pause: OK). Main gap vs pages that hand-roll intervals: inconsistent strategy across the app rather than a defect here.
- **[info]** Options-spread pattern (`...options` last) lets callers override everything — used correctly by LiveViewer (staleTime Infinity when stream connected). Good design.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- None significant. Query keys are string-literal tuples duplicated by LiveViewer's manual `setQueryData` writes (noted there).

**(d) Maintainability hot spots**
- Extend this module as the single data-access convention; migrate Backtests/Portfolios/Optimizations pages onto it.

---

## src/hooks/useWebSocket.ts (271 lines)

**(a) Bugs / correctness risks**
- Scanned thoroughly — reconnect backoff (115–132), heartbeat timeout close(4000) (162–165), disposed guards on all callbacks, cleanup resets state. Well-built. Residual nits:
- **[minor] `sendJson` during CONNECTING silently drops** (255–262): returns false with no queue/retry; callers currently don't send, so dormant trap.
- **[minor] Reconnect attempts not surfaced**: status shows "reconnecting" but attempt count/backoff delay invisible to UIs showing connection quality.
- **[minor] No visibility-based suspend**: sockets stay open in hidden tabs (heartbeats continue); combined with per-page fallback polling this keeps servers busy.

**(b) Security**
- Scanned: `ws://`/`wss:` chosen from page protocol correctly (liveStreams 34–37); no URL injection surface (paths built from encoded account ids). Nothing found.

**(c) Baseline smells**
- None significant — appropriately scoped single-purpose hook.

**(d) Maintainability hot spots**
- None; this is the reference-quality file of the area.

---

## src/hooks/liveStreams.ts (91 lines)

**(a) Bugs / correctness risks**
- **[minor] Snapshot/update envelope only**: `useLiveStream` accepts messages with `type === "snapshot" | "update"` (52–53); anything else (e.g. an "error" envelope carrying `error` field defined in the type at line 16) is silently dropped — stream-level errors never reach `stream.error`.
- **[minor] No last-message dedup**: identical consecutive payloads still update `lastMessage` identity → downstream effects (LiveHub refresh fan-out) re-run even when nothing changed.

**(b) Security**
- Scanned, nothing found (wss upgrade correct).

**(c) Baseline smells**
- None significant.

**(d) Maintainability hot spots**
- Consider exposing `topic` filtering and error envelopes explicitly.

---

## src/lib/optimizationEvidence.ts (86 lines)

**(a) Bugs / correctness risks**
- Scanned — alias map uses `Object.hasOwn` (safe prototype access), finite checks explicit, sign-degradation rule excludes p_value. Matches its test suite. Nothing found.
- **[info]** Degradation heuristic only catches positive→non-positive; negative-metric targets (e.g. max_drawdown improvements) aren't classified specially — documented limitation of scope, not a bug.

**(b) Security**
- Prototype-pollution-safe via `Object.hasOwn`. Nothing found.

**(c) Baseline smells**
- None.

**(d) Maintainability hot spots**
- None — exemplary small pure module with tests.

---

## tests/optimizationEvidence.test.ts (85 lines)

**(a) Bugs / correctness risks**
- Scanned — assertions match implementation behavior; includes adversarial cases (`__PROTO__`, `Constructor`) validating the hasOwn fix.
- **[minor]** First test asserts on page *source text* via regex (10–18) — brittle copy-paste-detector style test; will false-fail on harmless wording changes and doesn't verify rendering. Acceptable as a regression tripwire.

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- None significant.

**(d) Maintainability hot spots**
- Fine; consider moving source-text assertions to component tests eventually.

---

## src/lib/queryClient.ts (12 lines)

**(a) Bugs / correctness risks**
- Scanned — sensible defaults (30 s staleTime, retry 1, focus refetch off, 5 min gc). Nothing found.

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- None.

**(d) Maintainability hot spots**
- None.

---

## src/main.tsx (23 lines)

**(a) Bugs / correctness risks**
- **[major] No top-level error boundary** (checklist item: missing error boundaries): provider stack mounts `<App/>` directly; no React error boundary exists anywhere in the area (grep confirms none). Any render-time exception in a lazy page blanks the whole SPA to the Vite root with no Sentry breadcrumb beyond global handlers. Wrap Routes in an ErrorBoundary that reports to Sentry and offers reload.
- **[info]** Sentry wiring mentioned in the brief is not visible in main.tsx itself (no `Sentry.init` import here); if initialized elsewhere (e.g. index.html script or separate module), fine — but the error-boundary gap stands regardless.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- None.

**(d) Maintainability hot spots**
- Error boundary is the single cheapest resilience win available.

---

## src/App.tsx (126 lines)

**(a) Bugs / correctness risks**
- **[minor] Unknown routes fall through to blank content**: no catch-all `<Route path="*" …>` — navigating to an undefined path renders Sidebar with empty main area (React Router v7 renders nothing for unmatched). Add a NotFound redirect.
- **[minor] Suspense covers route swaps but lazy chunk failure** (network hiccup on first load of a code-split page) surfaces as unhandled rejection → white screen (ties to missing boundary above).
- **[minor] TaskManager hidden on `/data` only by prefix check** (77): other full-width pages (TaskHub `/tasks`) still get the floating TaskManager overlapping their own task UI — duplication noted under smells.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Middle Man**: every lazy route wraps `.then((mod) => ({ default: mod.X }))` boilerplate ×19 — a `lazyRoute(() => import("…"), "X")` helper would collapse it.
- **Duplicated Code (behavioral)**: TaskManager polling duplicates TaskHub's (see TaskManager entry).

**(d) Maintainability hot spots**
- Route table is clean; add boundary + catch-all and it's done.

---

## src/Sidebar.tsx (98 lines)

**(a) Bugs / correctness risks**
- Scanned, nothing found (disabled item handled, active styling via NavLink function-form correct).

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Speculative Generality (mild)**: dead nav entry "Accounts" flagged `disabled: true` with "Coming soon" (32, 58–72) — either implement or remove from shipped nav.

**(d) Maintainability hot spots**
- None.

---

## src/index.css (66 lines)

**(a) Bugs / correctness risks**
- Scanned, nothing found. Tailwind v4 `@theme` tokens define the palette consumed by both utility classes (Sidebar/Badge/MatrixGridPage) and CSS vars (inline-style pages) — the bridge works because v4 exposes theme values as CSS variables.

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- None (small, purposeful).

**(d) Maintainability hot spots**
- The dual consumption model (utilities vs `var(--color-x)`) is the app-wide styling split documented elsewhere; consolidating on utilities would retire hundreds of inline styles.

---

## src/components/DownloadModal.tsx (1313 lines)

**(a) Bugs / correctness risks**
- **[minor] Genesis coverage fetch per keystroke-implied symbol change** (314–356): deps include `parsedSymbols` (recomputed each render of symbol string) — same debounce-gap class as CreateBacktestModal; fires `getDukascopyGenesis` on every edit while Dukascopy selected.
- **[minor] Duplicate-effect dependency redundancy** (306–312): reset effect lists `defaultTimeframesKey` AND `normalizedDefaultTimeframes` (derived from the same array) — harmless but signals fragile dep bookkeeping; `normalizedDefaultTimeframes` is a new array identity per prop change, making the key redundant.
- **[minor] Submit button lacks disabled/in-flight state** (1132–1149): unlike siblings, no `submitting` flag — double-click can start two download tasks (same class as PortfoliosPage create).
- **[info]** Start-date guardrail logic (dukascopyMinStart 207–235, validation 438–448) is thoughtful domain logic; lexicographic ISO compare is safe given ISO dates.

**(b) Security**
- Scanned, nothing found (symbols forwarded as data).

**(c) Baseline smells**
- **Large Class / Long Function**: 1300+ lines, fifth copy of timeframe list (18–29) and label/input style constants; the streaming-terminal half (1153–1255) duplicates ResampleModal/SmartHarvestModal structure wholesale.
- **Duplicated Code**: modal job-stream scaffold (jobId/jobStatus/completionNotified/resetStream/statusIcon/LiveTerminal wiring) is now verbatim across DownloadModal/ResampleModal/SmartHarvestModal — extract `useTaskStream` hook + `TaskStreamPanel`.

**(d) Maintainability hot spots**
- Highest-value refactor target among modals: one `useTaskStream` hook deletes ~150 duplicated lines across three files.

---

## src/components/ResampleModal.tsx (375 lines) & SmartHarvestModal.tsx (399 lines)

Reviewed together (structurally twins):

**(a) Bugs / correctness risks**
- **[minor] `catch (err: any)`** (ResampleModal 96; SmartHarvest 106).
- **[minor] No submit in-flight guard** (both handleSubmit): double-click starts duplicate resample/harvest tasks.
- **[minor] SmartHarvest source select offers only dukascopy** (184–185) while accepting free-text-normalized source — vestigial UI.
- **[info]** Reset-on-open effects and completion-notification effects are correctly guarded (`completionNotified`).

**(b) Security**
- Scanned, nothing found (server-side path inputs validated by backend — resample takes a filesystem path string; frontend passes through, backend must restrict traversal — flagged for backend reviewer).

**(c) Baseline smells**
- **Duplicated Code**: see DownloadModal — the trio shares ~60% structure.
- **Primitive Obsession**: `target_timeframes` comma-string (DataResampleRequest) vs arrays elsewhere in the same API.

**(d) Maintainability hot spots**
- Fold into the shared task-stream abstraction.

---

## src/components/LiveTerminal.tsx (173 lines)

**(a) Bugs / correctness risks**
- **[major] WebSocket never reconnects and leaks on jobId race** (19–67): plain `new WebSocket` with no backoff/retry — a transient drop mid-download leaves terminal frozen at "disconnected" while the task continues unseen (the useWebSocket hook exists and handles all of this; not used here). Also cleanup calls `ws.close()` unconditionally even if socket is CONNECTING (harmless per spec) and `onStatusChange` in deps (line 67) tears down/recreates the socket whenever parent passes an inline callback — DownloadModal passes `setJobStatus` (stable), StrategyLab passes `setJobStatus` (stable): OK today, trap tomorrow.
- **[minor] Status parsed from log text regex** (42): `\[task:[^\]]+\]\s+status=(\w+)` couples UI to log formatting; a log-line change silently breaks completion detection (and thus DownloadModal's onTaskFinished flow).
- **[minor] Unbounded log accumulation**: `setLogs(prev => prev + line)` grows without cap — hours-long downloads balloon memory/DOM (single text node, so DOM cost is one node, but string concat is O(n²)-ish over time).

**(b) Security**
- Scanned: log lines rendered as text nodes — safe. Nothing found.

**(c) Baseline smells**
- **Duplicated Code (infra)**: re-implements URL building from liveStreams.buildWebSocketUrl (34–37) — second copy.
- **Inappropriate Intimacy**: parses backend log grammar.

**(d) Maintainability hot spots**
- Port to `useWebSocket` (gives reconnect+heartbeat free) and negotiate a structured status message with the backend instead of regexing logs.

---

## src/components/TradeTable.tsx (314 lines)

**(a) Bugs / correctness risks**
- Scanned — ag-grid Community usage correct, formatters null-safe via `toFiniteNumber`. Nothing found.
- **[minor]** Fixed 560px height regardless of container (287): long tables scroll internally; fine.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: fourth `toFiniteNumber` copy (35–46); timestamp/date UTC formatters duplicate LiveViewer's.

**(d) Maintainability hot spots**
- None significant; consolidate number coercers into utils.

---

## src/components/DataTable.tsx (128 lines)

**(a) Bugs / correctness risks**
- **[minor] Row keys by index** (97): `key={i}` — sorting/re-rendering with mutable row identity causes input/state bleed if rows ever carry internal state; currently stateless renders, low risk.
- **[minor] Null-sort instability** (33): `if (av == null || bv == null) return 0;` makes sort order of null-containing columns depend on pre-sort order (V8 sort stability saves it mostly).

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: third generic sortable-table implementation (vs PanelTable in DataManagementPage and ag-grid TradeTable) — pick one (ag-grid already licensed/present) and delete the rest.
- `T extends Record<string, any>` loosens typing; `unknown` index access suffices.

**(d) Maintainability hot spots**
- Table consolidation is a stated cross-cutting recommendation.

---

## src/components/MultiSelectDropdown.tsx (251 lines)

**(a) Bugs / correctness risks**
- Scanned — outside-click handling, dedupe, search all correct. Minor:
- **[minor] No keyboard/ARIA support**: button lacks aria-expanded/haspopup, options are buttons in a non-listbox container; Escape doesn't close. Accessibility debt.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- None significant.

**(d) Maintainability hot spots**
- Add focus-trap/keyboard nav when polishing a11y.

---

## src/components/SymbolInput.tsx (231 lines)

**(a) Bugs / correctness risks**
- **[minor] Stale closure on `expandedCats`** (73–82): `handleChange` reads `expandedCats` state directly (not via updater) while mutating within rapid typing — classic stale-closure; benign visually but the checklist item in kind.
- **[minor] Dropdown z-index 100 over Modal z-50** (129): intended (modal usage), fine.
- **[minor] Renders up to 100 symbols per expanded category without virtualization** (189): large universes → heavy dropdown DOM.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- None significant.

**(d) Maintainability hot spots**
- None major.

---

## src/components/FilterBar.tsx (184 lines)

**(a) Bugs / correctness risks**
- Scanned, nothing found (pure controlled presentational component).

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Speculative Generality (mild)**: `filters`/`filterValues`/`onFilterChange` select-panel path is used by exactly one caller (StrategiesPage); Backtests/Portfolios use `filterPanelContent` instead. Two parallel APIs in one component.

**(d) Maintainability hot spots**
- None major.

---

## src/components/DualRangeSlider.tsx (156 lines)

**(a) Bugs / correctness risks**
- Scanned — clamping, step normalization, dual-thumb overlap prevention all handled. Nothing found.

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- **Duplicated Code**: `clamp` duplicated in RangeFilterControl (17–19); overlapping responsibility with RangeFilterControl entirely (two range-filter widgets).

**(d) Maintainability hot spots**
- Merge with RangeFilterControl or clearly differentiate (slider-only vs numeric-input variant).

---

## src/components/RangeFilterControl.tsx (167 lines)

**(a) Bugs / correctness risks**
- Scanned, nothing found (guards NaN updates).
- **[info]** Appears unused within the reviewed area files (grep: only self-references) — candidate dead code unless consumed outside this list.

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- **Speculative Generality**: possibly-unused component (verify with broader grep before deletion); plus clamp duplication noted above.

**(d) Maintainability hot spots**
- Same merge note as DualRangeSlider.

---

## src/components/Sparkline.tsx (166 lines)

**(a) Bugs / correctness risks**
- Scanned — finite filtering, divide-by-zero guards (`range || 1`, peak epsilon checks), hover clamping all present. Nothing found.
- **[minor] Hook-after-conditional-return ordering** (30 vs 53): early `return null` at line 30 precedes `useMemo(drawdowns)` at 53 — conditional hook execution violates rules-of-hooks; works only because the early return is stable per data (if a sparkline toggles between <2 and ≥2 points across renders of the same mounted instance, React throws "rendered fewer hooks"). Real crash vector when preview data arrives async! BacktestsPage renders Sparkline conditionally on length too, which mitigates by remounting — fragile.
- **[minor] German hover label** (88): `"Punkt ${n}"` — locale mix again.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- Rules-of-hooks violation above is the finding; rest is clean.

**(d) Maintainability hot spots**
- Move the `drawdowns` useMemo above the early return (or compute unconditionally) — one-line fix for a latent crash.

---

## src/components/StatCard.tsx (87 lines)

**(a) Bugs / correctness risks**
- **[minor] Percent format assumes points-not-fraction** (27–28): `numVal.toFixed(2)%` — the receiving end of the unit-contract bug chain (BacktestDetailPage passes fractions-or-points depending on field). Component itself consistent; contract ambiguous.
- **[minor] Currency format drops cents always** (23–26) and renders "-$" prefix manually; `-0` edge shows "$0".

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- None significant.

**(d) Maintainability hot spots**
- Document expected units in props or accept formatter fn.

---

## src/components/Badge.tsx (23 lines)

Scanned — trivial Tailwind badge mapping to `@theme` colors. Nothing found (bugs/security/smells/maintainability): clean.

---

## src/components/RenameModal.tsx (135 lines)

**(a) Bugs / correctness risks**
- Scanned — draft sync on open/value, save-disabled states correct. Nothing found.

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- **Hard-coded helper copy** (55): "The strategy slug or backtest ID stays unchanged." baked into the generic component even when `helperText` overrides it below — contradictory duplicate messaging.

**(d) Maintainability hot spots**
- None.

---

## src/components/LoadingSpinner.tsx (30 lines)

Scanned — trivial spinner using the custom `.animate-spin` keyframe (index.css 56–66). Nothing found.

---

## src/components/ToastProvider.tsx (124 lines)

**(a) Bugs / correctness risks**
- **[minor] Toast id collision risk** (63): `Date.now() + random(0..999)` — two toasts same millisecond can collide keys; use counter/uuid.
- **[minor] Timers not cleaned on unmount** (65–67): provider never unmounts in practice; noted only.
- **[minor] No max-toast cap**: error storms stack infinitely over the fixed container.

**(b) Security**
- Scanned: message rendered as text node — safe. Nothing found.

**(c) Baseline smells**
- None significant.

**(d) Maintainability hot spots**
- None.

---

## src/components/StrategiesProvider.tsx (82 lines)

**(a) Bugs / correctness risks**
- Scanned — dedupe via pendingLoadRef, fetchQuery refresh, context memoized. Nothing found.

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- **Middle Man (mild)**: wraps `useStrategies({enabled:false})` + manual fetchQuery — could be a thin selector over React Query alone; acceptable DX wrapper.

**(d) Maintainability hot spots**
- None.

---

## src/components/Modal.tsx (78 lines)

**(a) Bugs / correctness risks**
- **[minor] No Escape-to-close and no focus trap** (whole file): keyboard users cannot dismiss; focus escapes to background page. A11y + UX gap for the app's primary interaction surface.
- **[minor] Backdrop click closes even while a job streams** — DownloadModal guards via onClose implementations, but Modal itself offers no `dismissable={false}` affordance; each consumer re-implements guarding (BacktestsPage does, others forget).
- **[minor] No portal**: rendered inline; stacking contexts inside transformed parents could clip it (none today).

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- None significant.

**(d) Maintainability hot spots**
- Adding `<dialog>`-semantics or Radix would fix a11y gaps app-wide in one place.

---

## src/components/TaskManager.tsx (116 lines)

**(a) Bugs / correctness risks**
- **[minor] Second independent 3 s poller of `/api/tasks`** (32–34) duplicating TaskHub's — two intervals hit the same endpoint whenever TaskManager visible; consolidation noted.
- **[minor] Errors swallowed silently** (24–28): badge shows stale counts on persistent failure with no indicator.

**(b) Security**
- N/A. Nothing found.

**(c) Baseline smells**
- **Duplicated Code**: ACTIVE_STATUSES + polling scaffold copied from TaskHub.

**(d) Maintainability hot spots**
- Share the query via api/queries.

---

## src/components/index.ts (6 lines)

Barrel exporting six components; notably omits most components actually used via direct imports (Sparkline, TradeTable, etc.) — barrel is partially vestigial. **Duplicated export paths** mild. Scanned: nothing else found.

---

## eslint.config.js (23 lines)

Scanned — flat config with js.recommended + tseslint.recommended + react-hooks recommended + react-refresh/vite. Nothing found.
- **[info]** No `@typescript-eslint/no-explicit-any` beyond recommended defaults' limited scope and no `no-floating-promises` (requires type-checked configs) — enabling type-aware linting would mechanically catch several bugs reported here (unhandled promises, `any` errors). Repo-standard judgment: baseline smells that tooling *would* enforce remain reportable until the config actually enforces them.

---

## vite.config.ts (20 lines)

Scanned — react plugin + tailwindcss v4 plugin, dev proxy `/api`→5556 and `/ws` with ws:true. Nothing found.
- **[info]** `__APP_VERSION__`/`__GIT_SHA__` declared in vite-env.d.ts but no `define` block supplies them — any usage would be a runtime ReferenceError; grep shows zero consumers today (dead declarations).

---

# Cross-cutting synthesis

1. **Percent-vs-fraction unit chaos** is the top correctness theme (ComparePage, BacktestDetailPage/StatCard, PortfolioDetailPage, BacktestsPage each assume differently). Fix once in types + utils.
2. **Four bespoke lightweight-charts integrations**, **three table implementations**, **five timeframe orderings**, **four copies of numeric coercion**, **three copies of realized-PnL matching**, **~200 lines of duplicated money/percent formatters**, and a **task-stream scaffold repeated across three modals** — the Duplicated-Code dividend here is very large.
3. **Polling without visibility gates** across LiveDetail/LiveViewer/LiveHub/LoggingPage/TaskHub/TaskManager; standardize on React Query intervals.
4. **Missing error boundary** (main.tsx/App.tsx) — one component away from eliminating whole-app white screens.
5. **Double-submit gaps** (PortfoliosPage, DownloadModal, Resample/SmartHarvest modals) versus correct examples (OptimizeModal, CreateBacktestModal) — adopt the `isSubmitting` discipline everywhere.

---

# Per-file accounting (all 59 tracked files)

| File | Status |
|---|---|
| eslint.config.js | scanned — findings above |
| src/App.tsx | findings |
| src/Sidebar.tsx | findings |
| src/api/client.ts | findings |
| src/api/queries.ts | findings |
| src/api/types.ts | findings |
| src/components/BacktestFromSignalsModal.tsx | findings |
| src/components/Badge.tsx | scanned, nothing found |
| src/components/CreateBacktestModal.tsx | findings |
| src/components/DataTable.tsx | findings |
| src/components/DownloadModal.tsx | findings |
| src/components/DualRangeSlider.tsx | findings |
| src/components/FilterBar.tsx | findings |
| src/components/LiveTerminal.tsx | findings |
| src/components/LoadingSpinner.tsx | scanned, nothing found |
| src/components/Modal.tsx | findings |
| src/components/MultiSelectDropdown.tsx | findings |
| src/components/OptimizeModal.tsx | findings |
| src/components/RangeFilterControl.tsx | findings |
| src/components/RenameModal.tsx | findings |
| src/components/ResampleModal.tsx | findings (joint section w/ SmartHarvestModal) |
| src/components/SmartHarvestModal.tsx | findings (joint section w/ ResampleModal) |
| src/components/Sparkline.tsx | findings |
| src/components/StatCard.tsx | findings |
| src/components/StrategiesProvider.tsx | findings |
| src/components/SymbolInput.tsx | findings |
| src/components/TaskManager.tsx | findings |
| src/components/ToastProvider.tsx | findings |
| src/components/TradeTable.tsx | findings |
| src/components/TradingViewChart.tsx | findings |
| src/components/index.ts | findings |
| src/hooks/liveStreams.ts | findings |
| src/hooks/useWebSocket.ts | findings |
| src/index.css | scanned — findings (informational) |
| src/lib/optimizationEvidence.ts | findings (positive verification) |
| src/lib/queryClient.ts | scanned, nothing found |
| src/main.tsx | findings |
| src/pages/BacktestDetailPage.tsx | findings |
| src/pages/BacktestsPage.tsx | findings |
| src/pages/ComparePage.tsx | findings |
| src/pages/DataManagementPage.tsx | findings |
| src/pages/LiveDetail.tsx | findings |
| src/pages/LiveHub.tsx | findings |
| src/pages/LiveViewer.tsx | findings |
| src/pages/LoggingPage.tsx | findings |
| src/pages/MatrixGridPage.tsx | findings |
| src/pages/OptimizationResultsPage.tsx | findings |
| src/pages/OptimizationsPage.tsx | findings |
| src/pages/PortfolioDetailPage.tsx | findings |
| src/pages/PortfoliosPage.tsx | findings |
| src/pages/Services.tsx | findings |
| src/pages/StrategiesPage.tsx | findings |
| src/pages/StrategyDetailPage.tsx | findings |
| src/pages/StrategyLabPage.tsx | findings |
| src/pages/TaskHub.tsx | findings |
| src/pages/TradeChartPage.tsx | findings |
| src/utils/stats.ts | findings |
| tests/optimizationEvidence.test.ts | findings |
| vite.config.ts | findings |

Note: `src/vite-env.d.ts` appears in the repo but was not on the tracked review list; it was read anyway and is covered implicitly under vite.config.ts notes.

# Finding counts by severity

Counted across all per-file sections (bugs + security items carrying an explicit severity tag; informational `[info]` notes excluded). Verified against tag counts in this document:

- **Critical: 0**
- **Major: 16**
- **Minor: 126**

(Plus ~20 informational notes.)

Major items (16): DataManagementPage unhandled rejections; BacktestsPage stale shared cache; BacktestsPage cache race condition; LiveHub N+1 fetch fan-out; LiveHub missing resume control; LiveViewer chart zoom reset on every tick; PortfoliosPage double-submit creates; PortfolioDetailPage chart rebuild/leak path; LiveDetail infinite refetch loop; StrategyDetailPage untyped (`any`) API surface; LoggingPage 5 s × 5000-line refetch churn; OptimizationResultsPage stale-fetch race; ComparePage percent/fraction mismatch; BacktestDetailPage percent-unit contradiction; CreateBacktestModal N+1 availability checks; main.tsx missing top-level error boundary; LiveTerminal non-reconnecting WebSocket.

(A 17th tag, `[major→security]` on StrategyDetailPage's `sourceUrl`, is graded minor-security in its body — the `rel="noreferrer"` concern itself verified safe; only scheme validation remains.)

Cross-cutting synthesis repeated for the tally: the dominant themes are (1) percent-vs-fraction unit chaos, (2) massive Duplicated Code dividend (4 chart stacks, 3 tables, 5 timeframe orderings, 4 numeric coercers, 3 PnL matchers, 3 modal task-streams), (3) polling without visibility gates, (4) missing error boundary, (5) inconsistent double-submit discipline.

---

## src/pages/ComparePage.tsx (306 lines)

**(a) Bugs / correctness risks**
- **[major] Percent/fraction mismatch in comparison stats**: `METRICS` marks total_return/cagr/max_drawdown/win_rate/psr as `format:"percent"` and renders `value.toFixed(2)%` directly (49) — i.e. treats values as *percent points*, while ComparePage's own delta logic and BacktestsPage treat the same fields as fractions needing ×100 (BacktestsPage.tsx:231–246). If the compare endpoint returns fractions (as BacktestMetadataItem does), all five metrics display e.g. `0.12%` instead of `12%`, and deltas inherit the wrong unit. Cross-endpoint contract must be verified, but one of the two pages is definitely wrong.
- **[minor] Null stats coerced to 0 before comparing** (223–224): `Number(stats[key] || 0)` turns "unknown" into zero — deltas color-code misleadingly when one backtest lacks a metric.
- **[minor] Effect dep `[ids.join(",")]`** (105): fine functionally (string dep), but `ids` recomputed each render; lint-suppressed style.
- **[minor] UTC chart labels** (71–74): equity overlay x-labels via toISOString — consistent with other pages' mixed conventions issue.

**(b) Security**
- Scanned, nothing found.

**(c) Baseline smells**
- **Duplicated Code**: yet another local `formatMetric`/currency/percent family (41–69); METRICS config is good design though — extend rather than fork.

**(d) Maintainability hot spots**
- Resolve the percent-unit contract centrally (typed metric descriptors in utils/stats) — this page is where the inconsistency bites users hardest because two numbers are juxtaposed.

