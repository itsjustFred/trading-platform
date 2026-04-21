Repository-wide audit — findings and test results
================================================

Scope and summary
- I performed a repository-wide static scan for risky patterns (unguarded navigator access, Date parsing, divisions by small denominators, optional numeric usage, SQL/DB edge-cases) and reviewed all files changed in the PR plus direct call-sites.
- I ran the test suite inside the project's virtualenv to verify runtime correctness after the changes.

Test run
- Command executed: source .venv/bin/activate && pytest -q --disable-warnings --maxfail=1
- Result: 303 passed, 70 skipped, 11 warnings (total runtime ~51s)

High-priority issues (must fix)
--------------------------------

1) Database preview loader drops valid rows when timestamp is falsy
- Description: Rows with valid but falsy `time` values (e.g., numeric 0) get dropped because the code converts `time` to string and rejects falsy strings.
- PR Git Diff Pointer:
```diff
-            backtest_id = str(mapping.get("backtest_id") or "").strip()
+            timestamp = str(mapping.get("time") or "").strip()
...
-        if not backtest_id or backtest_id not in previews:
+        if not backtest_id or backtest_id not in previews or not timestamp:
             continue
```
- Evidence: The code now rejects rows when `timestamp` is falsy; previously rows were appended regardless of `time` content and the UI expects preview points from DB query.
- How to Fix: Validate `time` numerically (e.g., attempt int/float conversion and use numeric checks) and only skip rows when parse fails, not when value equals 0.

2) SQL ordering differs for limit==1 vs bucketed path (inconsistent preview selection)
- Description: The query branch for `normalized_limit <= 1` uses ORDER BY time DESC while the >1 branch buckets with ORDER BY time ASC and picks bucket representatives; these differing ordering semantics can return inconsistent canonical points.
- PR Git Diff Pointer:
```diff
+    if normalized_limit <= 1:
+        ranked_query = text(
+            f"""
+            WITH ranked AS (
+                SELECT
+                    backtest_id,
+                    time,
+                    equity,
+                    ROW_NUMBER() OVER (
+                        PARTITION BY backtest_id
+                        ORDER BY time DESC, id DESC
+                    ) AS row_number
+                FROM equity_points
+                WHERE backtest_id IN ({", ".join(placeholders)})
+            )
+            SELECT backtest_id, time, equity
+            FROM ranked
+            WHERE row_number = 1
+            ORDER BY backtest_id ASC, time ASC
+            """
+        )
```
- Evidence: The two branches use opposite time ordering and different selection logic (single row vs bucketed selection).
- How to Fix: Decide canonical semantics for single-sample preview (earliest or latest) and apply identical ordering in both branches so limit==1 yields the same representative as bucketed sampling would.

3) viewer._compute_portfolio_contributions: metric_weight usage and numeric instability
- Description: `metric_weight` is computed using `max(weight, 1e-6)` and later used as denominator; tiny denominators amplify noise and may lead to unstable aggregates.
- PR Git Diff Pointer:
```diff
-        metric_weight = max(weight, 0.000001)
+        bucket["metric_weight"] = _safe_float(bucket.get("metric_weight"), 0.0) + metric_weight
...
-        cagr = _safe_float(bucket.get("cagr_sum"), 0.0) / metric_weight
```
- Evidence: The code sums metric-weighted metrics and divides by the summed `metric_weight` without a robust check for tiny denominators, and also uses float('nan') fallbacks earlier for component contributions.
- How to Fix: Use explicit guards before dividing (if metric_weight < threshold treat metric as not computable or return fallback), avoid adding tiny epsilons that hide logic errors, and normalise/validate component weights upstream.

4) New test uses SimpleNamespace without explicit import in diff
- Description: The added pytest code creates `SimpleNamespace(...)` but the diff doesn't show importing it, which raises NameError if not present elsewhere.
- PR Git Diff Pointer:
```diff
    monkeypatch.setattr(
        viewer_app_module,
        "_load_result",
        lambda backtest_id: (SimpleNamespace(stats={}, trades=[]) if backtest_id == "fallback-backtest" else None),
    )
```
- Evidence: Python requires `from types import SimpleNamespace` or similar; missing import will fail tests in environments where `SimpleNamespace` is not already in scope.
- How to Fix: Add `from types import SimpleNamespace` to the test file or use a plain dict/namespace stub.

Medium-priority issues (should fix)
------------------------------------

5) getBacktestsAll switched default to include equity preview (performance regression)
- Description: `getBacktestsAll` sets `include_equity_preview` from "false" to "true", increasing response sizes by including arrays of equity preview points by default.
- PR Git Diff Pointer:
```diff
-  query.set("include_equity_preview", "false");
+  query.set("include_equity_preview", "true");
```
- Evidence: Listing endpoints will now include previews unless callers opt out; UI previously avoided heavy payloads by default.
- How to Fix: Make inclusion opt-in via function parameter or revert default to "false" and only request previews when the page needs them.

6) Sparkline: Date.parse on unvalidated strings and hover default X=0 inconsistent rendering
- Description: Sparkline uses `Date.parse(startDate || "")` and `Date.parse(endDate || "")` then interpolates hover date; hoverX defaults to 0 when no hover index leading to possible vertical line/tooltip misplacement.
- PR Git Diff Pointer:
```diff
+  const parsedStart = Date.parse(startDate || "");
+  const parsedEnd = Date.parse(endDate || "");
+  const hasDateRange = Number.isFinite(parsedStart) && Number.isFinite(parsedEnd) && parsedEnd >= parsedStart;
...
+  const hoverIdx = hoverIndex == null ? null : Math.max(0, Math.min(normalized.length - 1, hoverIndex));
+  const hoverX = hoverIdx == null ? 0 : (hoverIdx / (normalized.length - 1)) * width;
+  const hoverValue = hoverIdx == null ? null : normalized[hoverIdx];
```
- Evidence: Invalid or non-ISO dates result in NaN; hoverX fallback 0 may cause drawing at left edge without a value.
- How to Fix: Require validated date strings or numeric timestamps; only compute hoverX/hoverValue when hoverIdx != null (do not default hoverX to 0), and unify rendering null-checks.

7) navigator.language access scattered and not uniformly guarded
- Description: Multiple modules access `navigator.language` with inconsistent guards; use of `navigator` should be mediated via `globalThis?.navigator?.language` to avoid SSR/bundler issues.
- Evidence: `frontend/src/components/TradingViewChart.tsx`, `frontend/src/pages/LiveViewer.tsx`, `frontend/src/pages/BacktestDetailPage.tsx`, `frontend/src/pages/PortfolioDetailPage.tsx`, etc. contain various `navigator` checks.
- How to Fix: Replace ad-hoc checks with a single helper like `function resolveChartLocale(): string { return (globalThis as any)?.navigator?.language || 'en-US'; }` and reuse it.

8) selectedSymbol normalization inconsistent (UI highlighting/case-sensitivity)
- Description: selected symbol is set from various sources and sometimes compared raw; inconsistent casing causes mismatches, especially when comparing position symbol vs selectedSymbol.
- PR Git Diff Pointer:
```diff
-  const symbols = uniqueSymbols(deployments, fills, positions, signals);
+  const symbols = useMemo(
+    () => uniqueSymbols(deployments, fills, positions, signals),
+    [deployments, fills, positions, signals],
+  );
...
-                  onClick={() => setSelectedSymbol(position.symbol)}
-                  style={positionCardStyle(position, selectedSymbol)}
+                  onClick={() => setSelectedSymbol(position.symbol)}
+                  style={positionCardStyle(position, effectiveSymbol)}
```
- Evidence: positionCardStyle does `position.symbol.toUpperCase() === selectedSymbol.toUpperCase()`; passing `effectiveSymbol` or raw values without normalizing can produce mismatches.
- How to Fix: Store and pass selectedSymbol normalized (trimmed + uppercased) and normalize all incoming symbol sources.

Low-priority issues and suggestions
------------------------------------

9) Optional numeric fields (cagr, p_value, psr) rendered without consistent Number.isFinite guards
- Description: New `cagr` type was added; UI uses `row.cagr` in formatting; missing numeric guard may show NaN or incorrect percent formatting.
- Evidence:
```diff
+  cagr?: number;
...
+                <td style={{ ...tdStyle, color: Number(row.cagr || 0) >= 0 ? "#3fb950" : "#f85149" }}>
+                {row.cagr != null ? `${(Number(row.cagr) * 100).toFixed(2)}%` : "—"}
+              </td>
```
- How to Fix: Use `Number.isFinite(row.cagr)` checks before arithmetic and show fallback '—' when not finite.

10) Sparkline normalization filter uses Number.isFinite on raw value (strings filtered out)
- Description: Normalization filters values via `Number.isFinite(value)` which excludes numeric strings, then maps `Number(value)` — inconsistent acceptance.
- PR Git Diff Pointer:
```diff
+    () => (Array.isArray(data) ? data.filter((value) => Number.isFinite(value)).map((value) => Number(value)) : []),
```
- How to Fix: Decide whether numeric strings are valid; if so, use `const n = Number(value); if (Number.isFinite(n)) keep n`.

11) Table colSpan and header count updated (ensure tests/snapshots updated)
- Description: Added CAGR column increased table columns from 10 to 11 and adjusted colSpan: ensure tests and layout assumptions are updated.
- PR Git Diff Pointer:
```diff
-              <td colSpan={10} style={{ ...tdStyle, color: "var(--color-text-muted)" }}>
+              <td colSpan={11} style={{ ...tdStyle, color: "var(--color-text-muted)" }}>
                 No contribution rows match current filters.
               </td>
```
- How to Fix: Update any unit/snapshot tests or CSS that assumed previous column counts.

12) StrategiesPage removed Sparkline previews (UI change review)
- Description: Sparkline import and rendering in strategy cards were removed; verify this UX change is intentional.
- PR Git Diff Pointer:
```diff
-import { Sparkline } from "../components/Sparkline";
...
-      {Array.isArray(strategy.equity_preview) && strategy.equity_preview.length > 1 ? (
-        <div style={{ marginTop: "8px" }}>
-          <Sparkline data={strategy.equity_preview} width={280} height={46} />
-        </div>
-      ) : null}
```
- How to Fix: If removal is unintentional, re-enable preview rendering selectively; if intentional, remove unused imports and update tests.

Repository-wide pattern sweep notes
-----------------------------------
- navigator usage: found across many frontend files and should be unified with `globalThis?.navigator`.
- Date.parse / new Date: used widely; many call-sites already validate parse results with Number.isFinite, but a number of locations use `Date.parse(x) || 0` which silently collapses invalid dates to epoch 0 — consider explicit error handling.
- Division by metric weights: viewer/main.py centralizes metric-weight denominator logic; add clear guards and document minimal denominator semantics.
- Tests: suite passed fully (303 passed, 70 skipped) indicating current PR changes do not break existing tests, but the new test additions must be checked for missing imports across environments.

Action plan and recommended concrete changes
--------------------------------------------
1. Database
   - Change time validation to numeric parsing: try cast to int/float and check with math.isfinite; do not drop zero timestamps.
   - Harmonize SQL ordering for limit==1 and >1 branch; add unit tests covering single and multiple points.

2. viewer/main.py
   - Add guard: if metric_weight < 1e-6 return safe defaults instead of dividing; use explicit skip when weights are zero/negative.
   - Remove or document epsilon usage and ensure no NaN propagation.

3. Frontend
   - Implement a shared helper for chart locale: `resolveChartLocale()` should use `globalThis?.navigator?.language` and be used across components.
   - Update Sparkline: require valid date inputs, compute hover only when hover index defined, and unify hover rendering checks.
   - Normalize selectedSymbol globally; ensure event handlers set uppercased symbol and all comparisons use normalized values.
   - Revert getBacktestsAll default `include_equity_preview` to false or add parameter to opt-in to previews.

4. Tests & CI
   - Add `from types import SimpleNamespace` to the new test file or replace SimpleNamespace with plain dict for compatibility.
   - Add unit tests for new DB preview code (zero timestamps, tiny datasets, very large datasets).
   - Re-run test suite on CI after making above fixes.

Final status
------------
- The test suite currently passes: 303 passed, 70 skipped, 11 warnings.
- The most urgent fixes are in shared/database.py (timestamp truthiness & SQL ordering) and viewer aggregation denominators (metric_weight), followed by frontend robustness around dates and global navigator access.
- Apply fixes and re-run the test suite; add focused unit tests for DB preview and contributions aggregation.

This report completes the repository-wide static audit and test execution.
