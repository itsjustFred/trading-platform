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

export const StatCard = memo(function StatCard({
  label,
  value,
  subtitle,
  color,
  format = "none",
  percentScale = "points",
}: StatCardProps) {
  const numVal = typeof value === "number" ? value : parseFloat(String(value));
  const finite = !isNaN(numVal);
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

  // Auto-detect color from value
  const resolvedColor =
    color ||
    (finite && (format === "currency" || format === "percent")
      ? numVal > 0
        ? "#3fb950"
        : numVal < 0
          ? "#f85149"
          : "#c9d1d9"
      : "#c9d1d9");

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
          color: resolvedColor,
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
