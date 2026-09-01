/**
 * Formatting helpers shared by StatCard-style value tiles.
 *
 * Extracted from components/StatCard.tsx to keep the component itself free of
 * nested conditional expressions (Sonar S3358) and to make the number
 * formatting rules testable without a DOM.
 */

export type StatCardFormat = "currency" | "percent" | "number" | "none";

const CURRENCY_DIGITS = {
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
} as const;

function toNumber(value: string | number): number {
  return typeof value === "number" ? value : Number.parseFloat(String(value));
}

/** Renders a stat value for the requested display format, falling back to `String(value)`. */
export function formatStatCardValue(
  value: string | number,
  format: StatCardFormat,
): string {
  const numVal = toNumber(value);

  if (format === "currency" && Number.isFinite(numVal)) {
    const magnitude = Math.abs(numVal).toLocaleString("en-US", CURRENCY_DIGITS);
    return numVal >= 0 ? `$${magnitude}` : `-$${magnitude}`;
  }
  if (format === "percent" && Number.isFinite(numVal)) {
    return `${numVal.toFixed(2)}%`;
  }
  if (format === "number" && Number.isFinite(numVal)) {
    return numVal.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  return String(value);
}

const SIGN_COLORS = {
  positive: "#3fb950",
  negative: "#f85149",
  neutral: "#c9d1d9",
} as const;

/** Auto-detects a sign-based color for numeric currency/percent values. */
export function resolveStatCardColor(
  color: string | undefined,
  value: string | number,
  format: StatCardFormat,
): string {
  if (color) {
    return color;
  }
  if (format !== "currency" && format !== "percent") {
    return SIGN_COLORS.neutral;
  }
  const numVal = toNumber(value);
  if (!Number.isFinite(numVal)) {
    return SIGN_COLORS.neutral;
  }
  if (numVal > 0) {
    return SIGN_COLORS.positive;
  }
  if (numVal < 0) {
    return SIGN_COLORS.negative;
  }
  return SIGN_COLORS.neutral;
}
