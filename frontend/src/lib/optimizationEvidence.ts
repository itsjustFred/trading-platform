export type OptimizationOosEvidenceState = "available" | "decayed" | "degraded" | "unavailable";
export type OptimizationOosStatus = "available" | "insufficient_overlap" | "no_oos_requested" | "unavailable";

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
const SUPPORTED_METRIC_KEYS = new Set(Object.values(TARGET_METRIC_ALIASES));
const VALID_OOS_STATUSES = new Set<OptimizationOosStatus>([
  "available",
  "insufficient_overlap",
  "no_oos_requested",
  "unavailable",
]);

function metricValueOutsideRange(metricKey: string, value: number): boolean {
  if (["p_value", "psr", "win_rate"].includes(metricKey)) {
    return value < 0 || value > 1;
  }
  if (metricKey === "profit_factor") return value < 0;
  return false;
}

function unavailableEvidence(metricKey: string, message: string): OptimizationOosEvidence {
  return {
    state: "unavailable",
    metricKey,
    oosValue: null,
    label: "OOS evidence unavailable",
    message,
    tone: "#fbbf24",
    background: "rgba(120, 53, 15, 0.18)",
    border: "rgba(251, 191, 36, 0.28)",
  };
}

export function resolveOptimizationMetricKey(targetMetric: string): string {
  const normalized = String(targetMetric || "").trim().toLowerCase();
  return Object.hasOwn(TARGET_METRIC_ALIASES, normalized)
    ? TARGET_METRIC_ALIASES[normalized]
    : normalized;
}

export function classifyOptimizationOosEvidence(
  targetMetric: string,
  inSampleValue: unknown,
  testStats: Record<string, unknown> | null | undefined,
  oosStatus: OptimizationOosStatus | string = "unavailable",
): OptimizationOosEvidence {
  const metricKey = resolveOptimizationMetricKey(targetMetric);
  if (!SUPPORTED_METRIC_KEYS.has(metricKey)) {
    return unavailableEvidence(
      metricKey,
      `The target metric '${metricKey}' is unsupported, so OOS evidence cannot be validated.`,
    );
  }

  const normalizedStatus =
    typeof oosStatus === "string" ? oosStatus.trim().toLowerCase() : "";
  if (!VALID_OOS_STATUSES.has(normalizedStatus as OptimizationOosStatus)) {
    return unavailableEvidence(
      metricKey,
      `The OOS status '${String(oosStatus)}' is invalid, so OOS evidence cannot be validated.`,
    );
  }
  if (normalizedStatus !== "available") {
    const reason =
      normalizedStatus === "no_oos_requested"
        ? "No out-of-sample window was requested for this optimization."
        : normalizedStatus === "unavailable"
          ? "The OOS status is unavailable, so OOS evidence cannot be validated."
          : "The out-of-sample evaluation did not have sufficient overlap.";
    return unavailableEvidence(metricKey, reason);
  }

  const rawOosValue =
    testStats && Object.hasOwn(testStats, metricKey)
      ? testStats[metricKey]
      : undefined;
  const oosValue = typeof rawOosValue === "number" && Number.isFinite(rawOosValue) ? rawOosValue : null;
  if (oosValue === null) {
    return unavailableEvidence(
      metricKey,
      `No finite out-of-sample value is available for ${metricKey}. This run is only identified as an in-sample selected candidate.`,
    );
  }

  const finiteInSampleValue =
    typeof inSampleValue === "number" && Number.isFinite(inSampleValue) ? inSampleValue : null;
  if (finiteInSampleValue === null) {
    return unavailableEvidence(
      metricKey,
      `The in-sample selected score for ${metricKey} is not finite, so OOS evidence cannot be validated.`,
    );
  }
  if (
    metricValueOutsideRange(metricKey, finiteInSampleValue) ||
    metricValueOutsideRange(metricKey, oosValue)
  ) {
    return unavailableEvidence(
      metricKey,
      `The in-sample and OOS values for ${metricKey} are outside the valid range; received ${finiteInSampleValue} and ${oosValue}.`,
    );
  }
  if (finiteInSampleValue === 0) {
    return unavailableEvidence(
      metricKey,
      `The in-sample selected score for ${metricKey} is zero, so a relative OOS comparison is not meaningful.`,
    );
  }

  const lowerIsFavorable = metricKey === "p_value";
  if (lowerIsFavorable) {
    if (finiteInSampleValue <= 0.05 && oosValue > 0.05) {
      return {
        state: "degraded",
        metricKey,
        oosValue,
        label: "OOS significance lost",
        message: `The in-sample significant p_value (${finiteInSampleValue}) becomes insignificant OOS (${oosValue}) for ${metricKey}. Treat this candidate as degraded, not OOS validated.`,
        tone: "#fda4af",
        background: "rgba(127, 29, 29, 0.2)",
        border: "rgba(251, 113, 133, 0.3)",
      };
    }
    if (oosValue > 2 * finiteInSampleValue) {
      return {
        state: "decayed",
        metricKey,
        oosValue,
        label: "OOS significance decay",
        message: `The p_value moved from ${finiteInSampleValue} in-sample to ${oosValue} OOS for ${metricKey}. Treat this candidate as decayed, not OOS validated.`,
        tone: "#fcd34d",
        background: "rgba(120, 53, 15, 0.2)",
        border: "rgba(251, 191, 36, 0.3)",
      };
    }
  } else {
    if ((finiteInSampleValue > 0 && oosValue <= 0) || (finiteInSampleValue < 0 && oosValue >= 0)) {
      const signMessage = finiteInSampleValue > 0
        ? `The positive in-sample selected score (${finiteInSampleValue}) becomes non-positive OOS (${oosValue}) for ${metricKey}. Treat this candidate as degraded, not OOS validated.`
        : `The negative in-sample selected score (${finiteInSampleValue}) becomes non-negative OOS (${oosValue}) for ${metricKey}. Treat this candidate as degraded, not OOS validated.`;
      return {
        state: "degraded",
        metricKey,
        oosValue,
        label: "OOS sign degradation",
        message: signMessage,
        tone: "#fda4af",
        background: "rgba(127, 29, 29, 0.2)",
        border: "rgba(251, 113, 133, 0.3)",
      };
    }
    if (oosValue < 0.5 * finiteInSampleValue) {
      return {
        state: "decayed",
        metricKey,
        oosValue,
        label: "OOS decay",
        message: `The out-of-sample value (${oosValue}) is less than 50% of the in-sample selected score (${finiteInSampleValue}) for ${metricKey}. Treat this candidate as decayed, not OOS validated.`,
        tone: "#fcd34d",
        background: "rgba(120, 53, 15, 0.2)",
        border: "rgba(251, 191, 36, 0.3)",
      };
    }
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
