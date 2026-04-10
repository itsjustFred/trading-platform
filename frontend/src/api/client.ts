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
  DataCheckResponse,
  DukascopyGenesisResponse,
  LiveAccount,
  LiveAccountStats,
  LiveBalancesResponse,
  LiveDeploymentCreateRequest,
  LiveDeploymentConfig,
  LiveSignalLogResponse,
  LivePositionsResponse,
  LiveQuoteResponse,
  LiveDeploymentStatus,
  LiveTradesResponse,
  LogContent,
  LogFile,
  OptimizationDetail,
  OptimizationRunRequest,
  OptimizationRunResponse,
  OptimizationSummary,
  RunBacktestRequest,
  RunBacktestSignalsRequest,
  RunBacktestResponse,
  RunBacktestStatusResponse,
  StrategyDetail,
  StrategyMeta,
  Trade,
  TradeChartResponse,
  BacktestInfo,
  BacktestCapmResponse,
  BacktestsDeduplicateResponse,
  BacktestsBulkDeleteResponse,
  BacktestsCompareResponse,
  PortfolioComponent,
  PortfolioContribution,
  PortfolioDetail,
  PortfolioSummary,
  PortfoliosQueryParams,
  PortfoliosDuplicatesResponse,
  PortfoliosDeduplicateResponse,
  BacktestsDuplicatesResponse,
  BacktestMetadataItem,
  BacktestsListResponse,
  BacktestsQueryParams,
  BacktestReplayResponse,
  PortfoliosBulkDeleteResponse,
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
export const getBacktests = (params?: BacktestsQueryParams) => {
  const query = new URLSearchParams();
  if (params?.page != null) query.set("page", String(params.page));
  if (params?.per_page != null) query.set("per_page", String(params.per_page));
  if (params?.sort) query.set("sort", params.sort);
  if (params?.order) query.set("order", params.order);
  if (params?.name) query.set("name", params.name);
  if (params?.strategy) query.set("strategy", params.strategy);
  if (params?.symbol) query.set("symbol", params.symbol);
  if (params?.timeframe) query.set("timeframe", params.timeframe);
  if (params?.search) query.set("search", params.search);
  if (params?.total_return_min != null) query.set("total_return_min", String(params.total_return_min));
  if (params?.total_return_max != null) query.set("total_return_max", String(params.total_return_max));
  if (params?.sharpe_min != null) query.set("sharpe_min", String(params.sharpe_min));
  if (params?.sharpe_max != null) query.set("sharpe_max", String(params.sharpe_max));
  if (params?.psr_min != null) query.set("psr_min", String(params.psr_min));
  if (params?.psr_max != null) query.set("psr_max", String(params.psr_max));
  if (params?.p_value_min != null) query.set("p_value_min", String(params.p_value_min));
  if (params?.p_value_max != null) query.set("p_value_max", String(params.p_value_max));
  if (params?.cagr_min != null) query.set("cagr_min", String(params.cagr_min));
  if (params?.cagr_max != null) query.set("cagr_max", String(params.cagr_max));
  if (params?.win_rate_min != null) query.set("win_rate_min", String(params.win_rate_min));
  if (params?.win_rate_max != null) query.set("win_rate_max", String(params.win_rate_max));
  if (params?.drawdown_min != null) query.set("drawdown_min", String(params.drawdown_min));
  if (params?.drawdown_max != null) query.set("drawdown_max", String(params.drawdown_max));
  if (params?.max_drawdown_min != null) query.set("max_drawdown_min", String(params.max_drawdown_min));
  if (params?.max_drawdown_max != null) query.set("max_drawdown_max", String(params.max_drawdown_max));
  const queryString = query.toString();
  return fetchJSON<BacktestsListResponse>(`/backtests${queryString ? `?${queryString}` : ""}`);
};
export const getBacktestsAll = (name?: string) => {
  const query = new URLSearchParams();
  query.set("include_equity_preview", "true");
  query.set("preview_limit", "50");
  if (name && name.trim()) query.set("name", name.trim());
  return fetchJSON<BacktestMetadataItem[]>(`/backtests/all?${query.toString()}`);
};
export const getBacktestEquityPreview = (id: string, limit = 80) =>
  fetchJSON<number[]>(
    `/backtests/${encodeURIComponent(id)}/equity-preview?limit=${Math.max(50, Math.min(100, limit))}`,
  );
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
  fetchJSON<any>(`/backtests/${encodeURIComponent(id)}/name`, {
    method: "PUT",
    body: JSON.stringify({ name: title }),
  });
export const compareBacktests = (ids: string[]) =>
  fetchJSON<BacktestsCompareResponse>(`/backtests/compare?ids=${ids.join(",")}`);
export const getBacktestDuplicates = () =>
  fetchJSON<BacktestsDuplicatesResponse>("/backtests/duplicates");
export const deduplicateBacktests = () =>
  fetchJSON<BacktestsDeduplicateResponse>("/backtests/deduplicate", {
    method: "POST",
  });
export const deleteBacktest = (id: string) =>
  fetchJSON<{ id: string; status: string }>(`/backtests/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
export const deleteBacktestsBulk = (ids: string[]) =>
  fetchJSON<BacktestsBulkDeleteResponse>("/backtests/delete-bulk", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });

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
export const getLiveQuote = (symbol: string) =>
  fetchJSON<LiveQuoteResponse>(`/live/quotes/${encodeURIComponent(symbol)}`);
export const getLivePositions = (accountId: string) =>
  fetchJSON<LivePositionsResponse>(`/live/positions/${encodeURIComponent(accountId)}`);
export const getLiveSignalLog = (accountId: string, limit = 200) => {
  const params = new URLSearchParams();
  if (Number.isFinite(limit)) {
    params.set("limit", String(Math.max(1, Math.min(1000, Math.trunc(limit)))));
  }
  const query = params.toString();
  return fetchJSON<LiveSignalLogResponse>(
    `/live/signals/${encodeURIComponent(accountId)}${query ? `?${query}` : ""}`,
  );
};
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
export const runBacktestSignals = (data: RunBacktestSignalsRequest) =>
  fetchJSON<RunBacktestResponse>("/backtests/run-signals", {
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
export const getPortfolios = (params?: PortfoliosQueryParams) => {
  const query = new URLSearchParams();
  if (params?.name) query.set("name", params.name);
  if (params?.sort) query.set("sort", params.sort);
  if (params?.order) query.set("order", params.order);
  const queryString = query.toString();
  return fetchJSON<PortfolioSummary[]>(`/portfolios${queryString ? `?${queryString}` : ""}`);
};
export const getPortfolio = (id: string) => fetchJSON<PortfolioDetail>(`/portfolios/${id}`);
export const getPortfolioDetail = (id: string) =>
  fetchJSON<PortfolioDetail>(`/portfolios/${id}`);
export const renamePortfolio = (id: string, name: string) =>
  fetchJSON<{ id: string; name: string; status: string }>(`/portfolios/${encodeURIComponent(id)}/name`, {
    method: "PUT",
    body: JSON.stringify({ name }),
  });
export const getPortfolioContributions = (
  id: string,
  groupBy: "component" | "strategy" | "symbol" | "timeframe" = "component",
) =>
  fetchJSON<PortfolioContribution[]>(
    `/portfolios/${encodeURIComponent(id)}/contributions?group_by=${encodeURIComponent(groupBy)}`,
  );
export const getPortfolioTrades = (
  id: string,
  filters?: { strategy?: string; symbol?: string; timeframe?: string },
) => {
  const params = new URLSearchParams();
  if (filters?.strategy) params.set("strategy", filters.strategy);
  if (filters?.symbol) params.set("symbol", filters.symbol);
  if (filters?.timeframe) params.set("timeframe", filters.timeframe);
  const query = params.toString();
  return fetchJSON<PortfolioDetail["trades"]>(
    `/portfolios/${encodeURIComponent(id)}/trades${query ? `?${query}` : ""}`,
  );
};
export const createPortfolio = (data: {
  name: string;
  account_capital: number;
  components: Array<
    Pick<
      PortfolioComponent,
      "strategy_name" | "symbol" | "timeframe" | "weight" | "risk_config" | "settings"
    > & { backtest_id?: string | null }
  >;
}) =>
  fetchJSON<PortfolioDetail>("/portfolios", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const deletePortfolio = (id: string) =>
  fetchJSON<{ status: string; id: string }>(`/portfolios/${id}`, { method: "DELETE" });
export const deletePortfoliosBulk = (ids: string[]) =>
  fetchJSON<PortfoliosBulkDeleteResponse>("/portfolios/delete-bulk", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
export const getPortfolioDuplicates = () =>
  fetchJSON<PortfoliosDuplicatesResponse>("/portfolios/duplicates");
export const deduplicatePortfolios = () =>
  fetchJSON<PortfoliosDeduplicateResponse>("/portfolios/deduplicate", {
    method: "POST",
  });
export const comparePortfolios = (ids: string[]) =>
  fetchJSON<{ portfolios: PortfolioDetail[] }>(`/portfolios/compare?ids=${ids.join(",")}`);

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
export const getDataCheck = (
  symbol: string,
  timeframe: string,
  start: string,
  end: string,
) => {
  const params = new URLSearchParams({
    symbol,
    timeframe,
    start,
    end,
  });
  return fetchJSON<DataCheckResponse>(`/data/check?${params.toString()}`);
};
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
export const listTasks = () => fetchJSON<DataTaskListResponse>("/tasks");
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
export const restartDataTask = (taskId: string) =>
  fetchJSON<DataTaskStatusResponse>(`/data/tasks/${taskId}/restart`, {
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
