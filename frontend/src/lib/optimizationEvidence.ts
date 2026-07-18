export type OptimizationOosEvidenceState = "available" | "degraded" | "unavailable";

export interface OptimizationOosEvidence {
  state: OptimizationOosEvidenceState;
  metricKey: string;
  oosValue: number | null;
  label: string;
  message: string;
  tone: string;
  background: string;
  border: string;
}

const TARGET_METRIC_ALIASES: Record<string, string> = {
  sharpe: "sharpe_ratio",
  sharpe_ratio: "sharpe_ratio",
  sortino: "sortino_ratio",
  sortino_ratio: "sortino_ratio",
  calmar: "calmar_ratio",
  calmar_ratio: "calmar_ratio",
  net_profit: "total_pnl",
  total_pnl: "total_pnl",
  cagr: "cagr",
  win_rate: "win_rate",
  profit_factor: "profit_factor",
  psr: "psr",
  p_value: "p_value",
  total_return: "total_return",
};

export function resolveOptimizationMetricKey(targetMetric: string): string {
  const normalized = String(targetMetric || "").trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(TARGET_METRIC_ALIASES, normalized)
    ? TARGET_METRIC_ALIASES[normalized]
    : normalized;
}

export function classifyOptimizationOosEvidence(
  targetMetric: string,
  inSampleValue: unknown,
  testStats: Record<string, unknown> | null | undefined,
): OptimizationOosEvidence {
  const metricKey = resolveOptimizationMetricKey(targetMetric);
  const rawOosValue = testStats?.[metricKey];
  const oosValue = typeof rawOosValue === "number" && Number.isFinite(rawOosValue) ? rawOosValue : null;

  if (oosValue === null) {
    return {
      state: "unavailable",
      metricKey,
      oosValue: null,
      label: "OOS evidence unavailable",
      message: `No finite out-of-sample value is available for ${metricKey}. This run is only identified as an in-sample selected candidate.`,
      tone: "#fbbf24",
      background: "rgba(120, 53, 15, 0.18)",
      border: "rgba(251, 191, 36, 0.28)",
    };
  }

  const finiteInSampleValue =
    typeof inSampleValue === "number" && Number.isFinite(inSampleValue) ? inSampleValue : null;
  const lowerIsFavorable = metricKey === "p_value";
  if (!lowerIsFavorable && finiteInSampleValue !== null && finiteInSampleValue > 0 && oosValue <= 0) {
    return {
      state: "degraded",
      metricKey,
      oosValue,
      label: "OOS sign degradation",
      message: `The positive in-sample selected score becomes non-positive OOS for ${metricKey}. Treat this candidate as degraded, not OOS validated.`,
      tone: "#fda4af",
      background: "rgba(127, 29, 29, 0.2)",
      border: "rgba(251, 113, 133, 0.3)",
    };
  }

  return {
    state: "available",
    metricKey,
    oosValue,
    label: "OOS evidence available",
    message: `A finite out-of-sample value is available for ${metricKey}. This is evidence for review, not an automatic promotion decision.`,
    tone: "#86efac",
    background: "rgba(20, 83, 45, 0.2)",
    border: "rgba(74, 222, 128, 0.26)",
  };
}
