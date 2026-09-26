import type { ReactElement } from "react";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";

const STATUS_ICON_COLORS = {
  ok: "#86efac",
  failed: "#fda4af",
  running: "#67e8f9",
} as const;

/** Shared task-status glyph used by the data-task modals. */
export function renderTaskStatusIcon(
  status: string | null | undefined,
  size = 18,
): ReactElement {
  if (status === "completed") {
    return <CheckCircle2 size={size} color={STATUS_ICON_COLORS.ok} />;
  }
  if (status === "failed") {
    return <XCircle size={size} color={STATUS_ICON_COLORS.failed} />;
  }
  return (
    <Loader2
      size={size}
      className="animate-spin"
      color={STATUS_ICON_COLORS.running}
    />
  );
}
