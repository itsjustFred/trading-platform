import { memo } from "react";

import { toPercentPoints, type PercentScale } from "../api/metricScale";

interface StatCardProps {
  label: string;
  value: string | number | null | undefined;
  subtitle?: string;
  color?: string;
  format?: "currency" | "percent" | "number" | "none";
  /** Required whenever format="percent": the scale the value arrives in. */
  percentScale?: PercentScale;
}

const NEUTRAL_COLOR = "#c9d1d9";
const POSITIVE_COLOR = "#3fb950";
const NEGATIVE_COLOR = "#f85149";

/** Auto-detect the value color when the caller did not pin one. */
function resolveStatColor(
  color: string | undefined,
  format: StatCardProps["format"],
  numVal: number,
  finite: boolean,
): string {
  if (color) return color;
  if (!finite || (format !== "currency" && format !== "percent")) return NEUTRAL_COLOR;
  if (numVal > 0) return POSITIVE_COLOR;
  if (numVal < 0) return NEGATIVE_COLOR;
  return NEUTRAL_COLOR;
}

export const StatCard = memo(function StatCard({
  label,
  value,
  subtitle,
  color,
  format = "none",
  percentScale = "points",
}: StatCardProps) {
  const numVal = typeof value === "number" ? value : parseFloat(String(value));
  const finite = !Number.isNaN(numVal);
  let formatted = String(value);

  if (format === "currency" && finite) {
    const abs = Math.abs(numVal);
    formatted =
      numVal >= 0
        ? `$${abs.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`
        : `-$${abs.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
  } else if (format === "percent" && finite) {
    const points = toPercentPoints(numVal, percentScale) ?? 0;
    formatted = `${points.toFixed(2)}%`;
  } else if (format === "number" && finite) {
    formatted = numVal.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  return (
    <div
      style={{
        background: "var(--color-surface)",
        border: "1px solid var(--color-border)",
        borderRadius: "8px",
        padding: "16px",
      }}
    >
      <div
        style={{
          fontSize: "0.75rem",
          textTransform: "uppercase",
          color: "var(--color-text-muted)",
          letterSpacing: "0.5px",
          marginBottom: "4px",
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: "1.25rem",
          fontWeight: 700,
          fontVariantNumeric: "tabular-nums",
          color: resolveStatColor(color, format, numVal, finite),
        }}
      >
        {formatted}
      </div>
      {subtitle && (
        <div
          style={{
            color: "var(--color-text-muted)",
            fontSize: "0.7rem",
            marginTop: "2px",
          }}
        >
          {subtitle}
        </div>
      )}
    </div>
  );
});
