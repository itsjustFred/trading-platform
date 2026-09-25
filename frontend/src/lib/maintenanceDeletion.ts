import type { MaintenanceDeleteItem } from "../api/types";

export interface MaintenanceDeletionNotice {
  message: string;
  tone: "success" | "warning" | "error";
}

function normalizedCount(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.trunc(value)) : 0;
}

export function formatMaintenanceDeletionResult(
  entity: string,
  reportedSuccessful: number,
  items: readonly MaintenanceDeleteItem[],
): MaintenanceDeletionNotice {
  let successful = normalizedCount(reportedSuccessful);
  let failed = 0;
  let cleanupPending = 0;
  let notFound = 0;

  for (const item of items) {
    if (item.status === "failed") failed += 1;
    else if (item.status === "not_found") notFound += 1;
    else if (item.status === "cleanup_pending") cleanupPending += 1;
    else if (item.status !== "deleted") failed += 1;

    if (item.journal_pending && item.status !== "cleanup_pending") {
      cleanupPending += 1;
    }
  }

  if (items.length > 0) {
    successful = items.filter((item) => item.status === "deleted").length;
  }

  const issues: string[] = [];
  if (failed > 0) issues.push(`${failed} failed`);
  if (cleanupPending > 0) issues.push(`${cleanupPending} pending cleanup`);
  if (notFound > 0) issues.push(`${notFound} not found`);

  if (issues.length === 0) {
    if (successful === 0) {
      return {
        message: `No ${entity} were deleted.`,
        tone: "error",
      };
    }
    return {
      message: `Deleted ${successful} ${entity}.`,
      tone: "success",
    };
  }

  const outcome = successful > 0 ? `Deleted ${successful} ${entity}; ` : `No ${entity} were deleted; `;
  return {
    message: `${outcome}${issues.join("; ")}.`,
    tone: failed > 0 || successful === 0 ? "error" : "warning",
  };
}
