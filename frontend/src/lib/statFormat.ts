/**
 * Formatting helpers shared by StatCard-style value tiles.
 *
 * Extracted from components/StatCard.tsx to keep the component itself free of
 * nested conditional expressions (Sonar S3358) and to make the number
 * formatting rules testable without a DOM.
 */

export type StatCardFormat = "currency" | "percent" | "number" | "none";

/**
 * Rendered when a metric is genuinely unavailable.
 *
 * A missing metric must never be shown as `0`, `$0` or `0.00%`: a real zero
 * return, a flat equity curve and "we could not compute this" are different
 * facts, and collapsing them misleads whoever reads the tile.
 */
export const UNAVAILABLE_STAT_TEXT = "—";

const CURRENCY_DIGITS = {
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
} as const;

function toNumber(value: string | number): number {
  return typeof value === "number" ? value : Number.parseFloat(String(value));
}

/**
 * True when a value carries no number AND no meaningful text: null, undefined,
 * an empty/whitespace string, or a non-finite number.
 *
 * A non-empty non-numeric string such as "n/a" is deliberate labelling by the
 * caller and is passed through unchanged; it is not an unavailable metric.
 */
export function isUnavailableStatValue(
  value: string | number | null | undefined,
): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "number") return !Number.isFinite(value);
  if (typeof value === "string") {
    return value.trim() === "";
  }
  return true;
}

/** Renders a stat value for the requested display format. */
export function formatStatCardValue(
  value: string | number | null | undefined,
  format: StatCardFormat,
): string {
  if (isUnavailableStatValue(value)) {
    return UNAVAILABLE_STAT_TEXT;
  }
  if (typeof value === "string" && !Number.isFinite(toNumber(value))) {
    // Deliberate non-numeric label such as "n/a": show it verbatim.
    return value;
  }
  const numVal = toNumber(value as string | number);

  if (format === "currency") {
    const magnitude = Math.abs(numVal).toLocaleString("en-US", CURRENCY_DIGITS);
    return numVal >= 0 ? `$${magnitude}` : `-$${magnitude}`;
  }
  if (format === "percent") {
    return `${numVal.toFixed(2)}%`;
  }
  if (format === "number") {
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
  value: string | number | null | undefined,
  format: StatCardFormat,
): string {
  if (color) {
    return color;
  }
  if (format !== "currency" && format !== "percent") {
    return SIGN_COLORS.neutral;
  }
  if (isUnavailableStatValue(value)) {
    return SIGN_COLORS.neutral;
  }
  const numVal = toNumber(value as string | number);
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

const PSR_TONES = {
  high: "#3fb950",
  mid: "#d29922",
  low: "#f85149",
} as const;

/** Sign/tier color for Probability of Sharpe Ratio values (0..1). */
export function psrTone(psr: number): string {
  if (psr >= 0.95) return PSR_TONES.high;
  if (psr >= 0.5) return PSR_TONES.mid;
  return PSR_TONES.low;
}

const SHARPE_TONES = {
  strong: "#3fb950",
  weak: "#d29922",
  negative: "#f85149",
} as const;

/**
 * Formats a ratio (Sharpe, Sortino, PSR) to a fixed number of decimals, or
 * returns null when the value is absent so the caller renders the shared
 * unavailable marker.
 *
 * Ratios need more decimals than StatCard's "number" format provides, so the
 * precision is applied here -- but only when a value actually exists. Coercing
 * with `|| 0` first would render a ratio that could not be computed as a genuine
 * zero, and those are very different facts: one is a measurement, the other is
 * an absence.
 */
export function formatRatioStat(
  value: number | string | null | undefined,
  decimals = 3,
): string | null {
  if (isUnavailableStatValue(value)) return null;
  return Number(value).toFixed(decimals);
}

/**
 * Tier color for a Sharpe ratio, or `undefined` when it cannot be computed.
 *
 * The backend emits `null` for a statistic it could not compute, and `undefined` is
 * how a caller says "no opinion" to `StatCard`, which then renders its neutral
 * state. Returning a tone for an absent number would attach a performance judgement
 * to a measurement that was never made - amber for a null Sharpe reads as "measured,
 * and mediocre".
 *
 * This is the only place the Sharpe tiers are decided. `PortfolioDetailPage` used to
 * inline a two-tier version of this rule while `BacktestDetailPage` called this
 * helper, so a *negative* Sharpe showed amber on the portfolio page and red on the
 * backtest page: the same metric, two screens, two answers.
 */
export function sharpeTone(
  sharpe: number | string | null | undefined,
): string | undefined {
  if (isUnavailableStatValue(sharpe)) return undefined;
  const value = Number(sharpe);
  if (value > 1) return SHARPE_TONES.strong;
  if (value > 0) return SHARPE_TONES.weak;
  return SHARPE_TONES.negative;
}


/** "+"/"-" prefix for signed USD values (no prefix at zero). */
export function signedUsdPrefix(value: number): string {
  if (value > 0) {
    return "+";
  }
  return value < 0 ? "-" : "";
}

/** Renders a signed USD amount with a fixed 2-decimal magnitude. */
export function formatSignedUsd(value: number): string {
  const sign = signedUsdPrefix(value);
  return `${sign}$${Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}
