import type {
  AgentConvertRequest,
  DataCategoryResponse,
  DataDownloadRequest,
  DataHarvestRequest,
  DataOverviewResponse,
  DataResampleRequest,
  DataTaskDeleteResponse,
  DataTaskListResponse,
  DataTaskLogsResponse,
  DataTaskStartResponse,
  DataTaskStatusResponse,
  DataCacheInvalidationResponse,
  DukascopyGenesisResponse,
  LiveAccount,
  LiveAccountStats,
  LiveBalancesResponse,
  LiveDeploymentCreateRequest,
  LiveDeploymentConfig,
  LiveDeploymentStatus,
  LiveTradesResponse,
  LogContent,
  LogFile,
  OptimizationDetail,
  OptimizationRunRequest,
  OptimizationRunResponse,
  OptimizationSummary,
  RunBacktestRequest,
  RunBacktestResponse,
  RunBacktestStatusResponse,
  StrategyDetail,
  StrategyMeta,
  Trade,
  TradeChartResponse,
  BacktestInfo,
  BacktestCapmResponse,
  BacktestReplayResponse,
} from "./types";

const BASE_URL = "/api";
const LIVE_STATS_PATH_FRAGMENT = "/live/stats/";

let liveStatsSupported: boolean | null = null;
let liveStatsSupportPromise: Promise<boolean> | null = null;

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${url}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    const detail = err?.detail ?? err;
    const message =
      (typeof detail === "object" && detail && "message" in detail
        ? String((detail as { message?: unknown }).message ?? "")
        : "") ||
      err?.error ||
      res.statusText;
    throw new ApiError(message, res.status, detail);
  }
  return res.json();
}

async function supportsLiveStatsEndpoint(): Promise<boolean> {
  if (liveStatsSupported !== null) {
    return liveStatsSupported;
  }
  if (!liveStatsSupportPromise) {
    liveStatsSupportPromise = (async () => {
      try {
        const response = await fetch("/openapi.json");
        if (!response.ok) {
          return true;
        }
        const payload = (await response.json()) as { paths?: Record<string, unknown> };
        const paths = Object.keys(payload?.paths ?? {});
        return paths.some((path) => path.includes(LIVE_STATS_PATH_FRAGMENT));
      } catch {
        return true;
      }
    })()
      .then((supported) => {
        liveStatsSupported = supported;
        return supported;
      })
      .finally(() => {
        liveStatsSupportPromise = null;
      });
  }
  return liveStatsSupportPromise;
}

// Strategies
export const getStrategies = () => fetchJSON<StrategyMeta[]>("/strategies");
export const getStrategy = (name: string) =>
  fetchJSON<StrategyDetail>(`/strategies/${encodeURIComponent(name)}`);
export const getStrategyDetail = (name: string) =>
  fetchJSON<StrategyDetail>(`/strategies/${encodeURIComponent(name)}`);
export const renameStrategy = (name: string, displayName: string) =>
  fetchJSON<StrategyDetail>(`/strategies/${encodeURIComponent(name)}`, {
    method: "PATCH",
    body: JSON.stringify({ name: displayName }),
  });

// Backtests
export const getBacktests = () => fetchJSON<any[]>("/backtests");
export const getBacktestInfo = (id: string) =>
  fetchJSON<BacktestInfo>(`/backtest/${id}/info`);
export const getBacktestStats = (id: string) =>
  fetchJSON<any>(`/backtest/${id}/stats`);
export const getBacktestTrades = (id: string) =>
  fetchJSON<Trade[]>(`/backtest/${id}/trades`);
export const getBacktestTrade = (id: string, nr: number) =>
  fetchJSON<TradeChartResponse>(`/backtest/${id}/trade/${nr}`);
export const getBacktestReplay = (id: string) =>
  fetchJSON<BacktestReplayResponse>(`/backtest/${id}/replay`);
export const getBacktestEquity = (id: string) =>
  fetchJSON<any>(`/backtest/${id}/equity`);
export const getBacktestCapm = (id: string, benchmark?: string) => {
  const params = new URLSearchParams();
  if (benchmark) params.set("benchmark", benchmark);
  const query = params.toString();
  return fetchJSON<BacktestCapmResponse>(`/backtest/${id}/capm${query ? `?${query}` : ""}`);
};
export const getBacktestOHLCV = (id: string) =>
  fetchJSON<any>(`/backtest/${id}/ohlcv`);
export const renameBacktest = (id: string, title: string) =>
  fetchJSON<any>(`/backtests/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ name: title }),
  });
export const compareBacktests = (ids: string[]) =>
  fetchJSON<any>(`/compare?ids=${ids.join(",")}`);

// Live Hub
export const getLiveDeployments = () =>
  fetchJSON<LiveDeploymentConfig[]>("/live/deployments");
export const getLiveAccounts = () => fetchJSON<LiveAccount[]>("/live/accounts");
export const getLiveStats = async (accountId: string) => {
  const supported = await supportsLiveStatsEndpoint();
  if (!supported) {
    throw new ApiError("Live stats endpoint not available", 404, {
      endpoint: "/api/live/stats/{account_id}",
    });
  }
  return fetchJSON<LiveAccountStats>(`/live/stats/${encodeURIComponent(accountId)}`);
};
export const getLiveBalances = (accountId: string) =>
  fetchJSON<LiveBalancesResponse>(`/live/balances/${encodeURIComponent(accountId)}`);
export const getLiveTrades = (accountId: string) =>
  fetchJSON<LiveTradesResponse>(`/live/trades/${encodeURIComponent(accountId)}`);
export const createLiveDeployment = (payload: LiveDeploymentCreateRequest) =>
  fetchJSON<LiveDeploymentConfig>("/live/deployments", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const updateLiveDeploymentStatus = (deploymentId: string, status: LiveDeploymentStatus) =>
  fetchJSON<LiveDeploymentConfig>(`/live/deployments/${encodeURIComponent(deploymentId)}/status`, {
    method: "PUT",
    body: JSON.stringify({ status }),
  });
export const deleteLiveDeployment = (deploymentId: string) =>
  fetchJSON<{ status: string; id: string }>(
    `/live/deployments/${encodeURIComponent(deploymentId)}`,
    { method: "DELETE" },
  );

// Run backtest
export const runBacktest = (data: RunBacktestRequest) =>
  fetchJSON<RunBacktestResponse>("/backtests/run", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const getBacktestRunStatus = (jobId: string) =>
  fetchJSON<RunBacktestStatusResponse>(`/backtests/run/${jobId}/status`);

// Optimizations
export const runOptimization = (data: OptimizationRunRequest) =>
  fetchJSON<OptimizationRunResponse>("/optimizations/run", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const getOptimizations = () =>
  fetchJSON<OptimizationSummary[]>("/optimizations");
export const getOptimization = (id: string) =>
  fetchJSON<OptimizationDetail>(`/optimizations/${encodeURIComponent(id)}`);

// Portfolios
export const getPortfolios = () => fetchJSON<any[]>("/portfolios");
export const getPortfolio = (id: string) => fetchJSON<any>(`/portfolios/${id}`);
export const getPortfolioDetail = (id: string) =>
  fetchJSON<any>(`/portfolios/${id}`);
export const createPortfolio = (data: {
  name: string;
  account_capital: number;
  strategies: {
    name: string;
    risk_per_trade_pct: number;
    max_positions: number;
  }[];
}) =>
  fetchJSON<any>("/portfolios", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const deletePortfolio = (id: string) =>
  fetchJSON<any>(`/portfolios/${id}`, { method: "DELETE" });
export const comparePortfolios = (ids: string[]) =>
  fetchJSON<any>(`/portfolios/compare?ids=${ids.join(",")}`);

// Logs
export const getLogs = () => fetchJSON<LogFile[]>("/logs");
export const getLogContent = (
  name: string,
  lines?: number,
  level?: string,
  search?: string,
) => {
  const params = new URLSearchParams();
  if (lines) params.set("lines", String(lines));
  if (level) params.set("level", level);
  if (search) params.set("search", search);
  return fetchJSON<LogContent>(`/logs/${encodeURIComponent(name)}?${params}`);
};

// Benchmark
export const getBenchmark = (symbol: string, start?: string, end?: string) => {
  const params = new URLSearchParams();
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  return fetchJSON<any>(`/benchmark/${symbol}?${params}`);
};

export const getSymbols = () =>
  fetchJSON<{ symbols: string[]; categories: Record<string, string[]> }>(
    "/symbols",
  );

// Data Management
export const getDataOverview = () =>
  fetchJSON<DataOverviewResponse>("/data/overview");
export const getDataCategory = (name: string) =>
  fetchJSON<DataCategoryResponse>(`/data/category/${encodeURIComponent(name)}`);
export const deleteDataSymbol = (path: string) =>
  fetchJSON<any>(`/data/symbol?path=${encodeURIComponent(path)}`, {
    method: "DELETE",
  });
export const getDukascopyGenesis = (
  symbols?: string[],
  timeframes?: string[],
) => {
  const params = new URLSearchParams();
  if (symbols?.length) params.set("symbols", symbols.join(","));
  if (timeframes?.length) params.set("timeframes", timeframes.join(","));
  const query = params.toString();
  return fetchJSON<DukascopyGenesisResponse>(
    `/data/dukascopy/genesis${query ? `?${query}` : ""}`,
  );
};
export const downloadData = (data: DataDownloadRequest) =>
  fetchJSON<DataTaskStartResponse>("/data/download", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const resampleData = (data: DataResampleRequest) =>
  fetchJSON<DataTaskStartResponse>("/data/resample", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const harvestData = (data: DataHarvestRequest) =>
  fetchJSON<DataTaskStartResponse>("/data/harvest", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const startAgentConvert = (data: AgentConvertRequest) =>
  fetchJSON<DataTaskStartResponse>("/agent/convert", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const listDataTasks = () => fetchJSON<DataTaskListResponse>("/data/tasks");
export const getDataTask = (taskId: string) =>
  fetchJSON<DataTaskStatusResponse>(`/data/tasks/${taskId}`);
export const getDataTaskLogs = (taskId: string) =>
  fetchJSON<DataTaskLogsResponse>(`/data/tasks/${taskId}/logs`);
export const pauseDataTask = (taskId: string) =>
  fetchJSON<DataTaskStatusResponse>(`/data/tasks/${taskId}/pause`, {
    method: "POST",
  });
export const resumeDataTask = (taskId: string) =>
  fetchJSON<DataTaskStatusResponse>(`/data/tasks/${taskId}/resume`, {
    method: "POST",
  });
export const cancelDataTask = (taskId: string) =>
  fetchJSON<DataTaskStatusResponse>(`/data/tasks/${taskId}/cancel`, {
    method: "POST",
  });
export const deleteDataTask = (taskId: string) =>
  fetchJSON<DataTaskDeleteResponse>(`/data/tasks/${taskId}`, {
    method: "DELETE",
  });
export const invalidateDataCache = (
  reason = "manual",
  includeValidation?: boolean,
) => {
  const params = new URLSearchParams();
  if (reason) params.set("reason", reason);
  if (typeof includeValidation === "boolean") {
    params.set("include_validation", includeValidation ? "true" : "false");
  }
  const query = params.toString();
  return fetchJSON<DataCacheInvalidationResponse>(
    `/data/cache/invalidate${query ? `?${query}` : ""}`,
    { method: "POST" },
  );
};
