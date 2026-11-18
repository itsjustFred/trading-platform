import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  formatMaintenanceDeletionResult,
  reconcileMaintenanceSelection,
} from "../src/lib/maintenanceDeletion.ts";
import {
  isActiveTaskStatus,
  isFailedTaskStatus,
  isTerminalTaskStatus,
} from "../src/lib/taskStatus.ts";

const read = (path: string) => readFile(new URL(path, import.meta.url), "utf8");

function deletionItem(
  id: string,
  status: "deleted" | "not_found" | "failed" | "cleanup_pending",
) {
  return { id, status, database: "records", artifact: "artifacts" };
}

test("successful maintenance deletions retain the success result", () => {
  const result = formatMaintenanceDeletionResult("backtest(s)", 2, [
    deletionItem("one", "deleted"),
    deletionItem("two", "deleted"),
  ]);

  assert.deepEqual(result, {
    message: "Deleted 2 backtest(s).",
    tone: "success",
  });
});

test("partial maintenance deletion failures are surfaced as errors", () => {
  const result = formatMaintenanceDeletionResult("portfolio(s)", 1, [
    deletionItem("one", "deleted"),
    deletionItem("two", "failed"),
  ]);

  assert.equal(result.tone, "error");
  assert.match(result.message, /Deleted 1 portfolio\(s\)/);
  assert.match(result.message, /1 failed/);
});

test("maintenance deletion notices use item statuses over a conflicting reported count", () => {
  const underreported = formatMaintenanceDeletionResult("backtest(s)", 0, [
    deletionItem("one", "deleted"),
  ]);
  const overreported = formatMaintenanceDeletionResult("backtest(s)", 2, [
    deletionItem("one", "deleted"),
    deletionItem("two", "failed"),
  ]);

  assert.deepEqual(underreported, {
    message: "Deleted 1 backtest(s).",
    tone: "success",
  });
  assert.deepEqual(overreported, {
    message: "Deleted 1 backtest(s); 1 failed.",
    tone: "error",
  });
});

test("maintenance cleanup pending is never reported as success", () => {
  const partial = formatMaintenanceDeletionResult("backtest(s)", 1, [
    deletionItem("one", "deleted"),
    deletionItem("two", "cleanup_pending"),
  ]);
  const pendingOnly = formatMaintenanceDeletionResult("portfolio(s)", 0, [
    deletionItem("one", "cleanup_pending"),
  ]);
  const journalPending = formatMaintenanceDeletionResult("backtest(s)", 1, [
    { ...deletionItem("one", "deleted"), journal_pending: true },
  ]);

  assert.equal(partial.tone, "warning");
  assert.match(partial.message, /1 pending cleanup/);
  assert.equal(pendingOnly.tone, "error");
  assert.match(pendingOnly.message, /No portfolio\(s\) were deleted/);
  assert.match(pendingOnly.message, /1 pending cleanup/);
  assert.equal(journalPending.tone, "warning");
  assert.match(journalPending.message, /1 pending cleanup/);
});

test("maintenance selection only clears resolved items", () => {
  const update = reconcileMaintenanceSelection(
    new Set(["deleted", "missing", "failed", "pending", "journal", "unreported"]),
    [
      deletionItem("deleted", "deleted"),
      deletionItem("missing", "not_found"),
      deletionItem("failed", "failed"),
      deletionItem("pending", "cleanup_pending"),
      { ...deletionItem("journal", "deleted"), journal_pending: true },
    ],
  );

  assert.deepEqual([...update.selectedIds].sort(), [
    "failed",
    "journal",
    "pending",
    "unreported",
  ]);
  assert.deepEqual([...update.retryIds].sort(), [
    "failed",
    "journal",
    "pending",
    "unreported",
  ]);
});

test("batch maintenance pages use item-aware result notices", async () => {
  const [backtestsPage, portfoliosPage] = await Promise.all([
    read("../src/pages/BacktestsPage.tsx"),
    read("../src/pages/PortfoliosPage.tsx"),
  ]);
  const backtestHandlers = [
    ["const handleBulkDelete", "const handleCompare"],
    ["const handleDeduplicate", "const emptyBacktestsCount"],
    ["const handleRemoveEmpty", "const filterPanelContent"],
  ];
  const portfolioHandlers = [
    ["const handleBulkDelete", "const handleShowDuplicates"],
    ["const handleDeduplicate", "const handleRemoveEmpty"],
    ["const handleRemoveEmpty", "const inputStyle"],
  ];

  for (const [source, handlers] of [
    [backtestsPage, backtestHandlers],
    [portfoliosPage, portfolioHandlers],
  ] as const) {
    for (const [startMarker, endMarker] of handlers) {
      const start = source.indexOf(startMarker);
      const end = source.indexOf(endMarker, start + startMarker.length);
      assert.ok(start >= 0 && end > start, `${startMarker} must exist`);
      assert.match(
        source.slice(start, end),
        /formatMaintenanceDeletionResult\(/,
        `${startMarker} must inspect per-item deletion status`,
      );
    }
  }
  assert.doesNotMatch(portfoliosPage, /setSelectedIds\(new Set\(\)\)/);
  assert.match(portfoliosPage, /applyMaintenanceResult\(result\.items\)/);
  assert.match(portfoliosPage, /applyMaintenanceResult\(payload\.items\)/);
});

test("bulk maintenance deletes are two-phase: dry run first, delete only confirmed ids", async () => {
  // Issue #156: the server selects the items, so the user must see the exact
  // list before anything is deleted. The first handler may only ask for a dry
  // run; the delete call lives in the confirm handler and sends the ids of
  // the list that was shown.
  const [backtestsPage, portfoliosPage] = await Promise.all([
    read("../src/pages/BacktestsPage.tsx"),
    read("../src/pages/PortfoliosPage.tsx"),
  ]);
  const cases = [
    [backtestsPage, "handleDeduplicate", "deduplicateBacktests"],
    [backtestsPage, "handleRemoveEmpty", "removeEmptyBacktests"],
    [portfoliosPage, "handleDeduplicate", "deduplicatePortfolios"],
    [portfoliosPage, "handleRemoveEmpty", "removeEmptyPortfolios"],
  ] as const;
  for (const [source, handler, apiCall] of cases) {
    const start = source.indexOf(`const ${handler} = `);
    const confirmStart = source.indexOf(`const ${handler}Confirm = `);
    const cancelStart = source.indexOf(`const ${handler}Cancel = `);
    assert.ok(
      start >= 0 && confirmStart > start && cancelStart > confirmStart,
      `${handler}, ${handler}Confirm and ${handler}Cancel must exist in this order`,
    );
    const preview = source.slice(start, confirmStart);
    const confirm = source.slice(confirmStart, cancelStart);
    assert.match(preview, new RegExp(`${apiCall}\\(\\{ dry_run: true \\}\\)`));
    assert.doesNotMatch(preview, /dry_run: false/, `${handler} must not delete`);
    assert.match(
      confirm,
      new RegExp(`${apiCall}\\(\\{ dry_run: false, ids: candidateIds \\}\\)`),
    );
    assert.match(confirm, /candidate_ids/, `${handler}Confirm must send the shown list`);
  }
});

test("a skipped bulk-delete item is reported, not counted as a failure", async () => {
  const { formatMaintenanceDeletionResult } = await import(
    "../src/lib/maintenanceDeletion.ts"
  );
  const notice = formatMaintenanceDeletionResult("empty backtest(s)", 1, [
    { id: "a", status: "deleted", database: "deleted", artifact: "deleted" },
    {
      id: "b",
      status: "skipped",
      database: "not_attempted",
      artifact: "not_attempted",
      reason: "no_longer_empty",
    },
  ]);
  assert.equal(notice.tone, "warning");
  assert.match(notice.message, /Deleted 1 empty backtest\(s\)/);
  assert.match(notice.message, /1 skipped/);
  assert.doesNotMatch(notice.message, /failed/);
});

test("manual-required, partial, skipped, and cleanup statuses are terminal, not active", () => {
  for (const status of [
    "partial",
    "skipped",
    "manual_required",
    "cleanup_pending",
    "cleanup_exhausted",
  ]) {
    assert.equal(isTerminalTaskStatus(status), true, status);
    assert.equal(isActiveTaskStatus(status), false, status);
  }
});

test("terminal intervention failures are not hidden as successful tasks", () => {
  assert.equal(isFailedTaskStatus("failed"), true);
  assert.equal(isFailedTaskStatus("interrupted"), true);
  assert.equal(isFailedTaskStatus("manual_required"), true);
  assert.equal(isFailedTaskStatus("cleanup_exhausted"), true);
  assert.equal(isFailedTaskStatus("cleanup_pending"), false);
});

test("task hub and live terminal use the shared terminal classification", async () => {
  const [taskHub, liveTerminal] = await Promise.all([
    read("../src/pages/TaskHub.tsx"),
    read("../src/components/LiveTerminal.tsx"),
  ]);

  assert.match(taskHub, /isTerminalTaskStatus\(task\.status\)/);
  assert.match(taskHub, /isFailedTaskStatus\(status\)/);
  assert.match(liveTerminal, /!taskFinished/);
  assert.match(liveTerminal, /isTerminalTaskStatus\(taskStatus\)/);
});

test("data management polling and terminal labels use shared classification", async () => {
  const [dataManagement, resample, download, harvest] = await Promise.all([
    read("../src/pages/DataManagementPage.tsx"),
    read("../src/components/ResampleModal.tsx"),
    read("../src/components/DownloadModal.tsx"),
    read("../src/components/SmartHarvestModal.tsx"),
  ]);

  assert.match(dataManagement, /isTerminalTaskStatus\(nextStatus\)/);
  assert.doesNotMatch(dataManagement, /FINAL_DATA_TASK_STATUSES/);
  for (const source of [resample, download, harvest]) {
    assert.match(source, /isTerminalTaskStatus\(jobStatus\)/);
  }
});
