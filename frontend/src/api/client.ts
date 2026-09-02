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
  LiveDashboardResponse,
  Service,
  ServiceStatus,
  ServiceType,
  VerificationEvidenceCreateRequest,
  VerificationEvidenceRecord,
  VerificationEvidenceSummary,
  LiveTradesResponse,
  SignalProcessorOption,
  LogContent,
  LogFile,
  OptimizationDetail,
  OptimizationRunRequest,
  OptimizationRunResponse,
  OptimizationSummary,
  RunBacktestRequest,
  RunBacktestFromSignalsRequest,
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
  BacktestsRemoveEmptyResponse,
  BacktestsCompareResponse,
  BacktestMatrixSummary,
  MatrixGridOverview,
  PortfolioComponent,
  PortfolioContribution,
  PortfolioDetail,
  PortfolioSummary,
  PortfoliosQueryParams,
  PortfoliosDuplicatesResponse,
  PortfoliosDeduplicateResponse,
  PortfoliosRemoveEmptyResponse,
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
    const messageFromDetail = describeErrorDetail(detail);
    const message = messageFromDetail || err?.error || res.statusText;
    throw new ApiError(message, res.status, detail);
  }
  return res.json();
}

async function supportsLiveStatsEndpoint(): Promise<boolean> {
  if (liveStatsSupported !== null) {
    return liveStatsSupported;
  }
  liveStatsSupportPromise ??= (async () => {
    try {
      const response = await fetch("/openapi.json");
      if (!response.ok) {
        return true;
      }
      const payload = (await response.json()) as {
        paths?: Record<string, unknown>;
      };
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
/** Renders an unknown error-detail value as a human-readable string. */
function describeErrorDetail(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (typeof detail === "object" && detail !== null && "message" in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") {
      return message;
    }
    if (message != null) {
      return JSON.stringify(message);
    }
  }
  return "";
}

/** Appends a query string to a path only when one is present. */
function withQuery(path: string, query: string): string {
  return query ? `${path}?${query}` : path;
}

const BACKTESTS_QUERY_PARAMS: ReadonlyArray<{
  key: keyof BacktestsQueryParams;
  name: string;
  kind: "string" | "number";
}> = [
  { key: "page", name: "page", kind: "number" },
  { key: "per_page", name: "per_page", kind: "number" },
  { key: "sort", name: "sort", kind: "string" },
  { key: "order", name: "order", kind: "string" },
  { key: "name", name: "name", kind: "string" },
  { key: "strategy", name: "strategy", kind: "string" },
  { key: "symbol", name: "symbol", kind: "string" },
  { key: "timeframe", name: "timeframe", kind: "string" },
  { key: "search", name: "search", kind: "string" },
  { key: "total_return_min", name: "total_return_min", kind: "number" },
  { key: "total_return_max", name: "total_return_max", kind: "number" },
  { key: "sharpe_min", name: "sharpe_min", kind: "number" },
  { key: "sharpe_max", name: "sharpe_max", kind: "number" },
  { key: "psr_min", name: "psr_min", kind: "number" },
  { key: "psr_max", name: "psr_max", kind: "number" },
  { key: "p_value_min", name: "p_value_min", kind: "number" },
  { key: "p_value_max", name: "p_value_max", kind: "number" },
  { key: "cagr_min", name: "cagr_min", kind: "number" },
  { key: "cagr_max", name: "cagr_max", kind: "number" },
  { key: "win_rate_min", name: "win_rate_min", kind: "number" },
  { key: "win_rate_max", name: "win_rate_max", kind: "number" },
  { key: "drawdown_min", name: "drawdown_min", kind: "number" },
  { key: "drawdown_max", name: "drawdown_max", kind: "number" },
  { key: "max_drawdown_min", name: "max_drawdown_min", kind: "number" },
  { key: "max_drawdown_max", name: "max_drawdown_max", kind: "number" },
];

export const getBacktests = (params?: BacktestsQueryParams) => {
  const query = new URLSearchParams();
  for (const { key, name } of BACKTESTS_QUERY_PARAMS) {
    const value = params?.[key];
    if (value == null || value === "") {
      continue;
    }
    query.set(name, String(value));
  }
  return fetchJSON<BacktestsListResponse>(
    withQuery(`/backtests`, query.toString()),
  );
};
export const getBacktestsAll = (
  name?: string,
  options?: { includeEquityPreview?: boolean; previewLimit?: number },
) => {
  const query = new URLSearchParams();
  if (options?.includeEquityPreview) {
    query.set("include_equity_preview", "true");
    query.set(
      "preview_limit",
      String(Math.max(1, Math.min(100, options.previewLimit ?? 48))),
    );
  }
  if (name?.trim()) query.set("name", name.trim());
  return fetchJSON<BacktestMetadataItem[]>(
    `/backtests/all?${query.toString()}`,
  );
};
export const getBacktestEquityPreview = (id: string, limit = 80) =>
  fetchJSON<number[]>(
    `/backtests/${encodeURIComponent(id)}/equity-preview?limit=${Math.max(1, Math.min(100, limit))}`,
  );
export const getBacktestEquityPreviews = (ids: string[], limit = 48) => {
  const query = new URLSearchParams();
  query.set("ids", ids.join(","));
  query.set("limit", String(Math.max(1, Math.min(100, limit))));
  return fetchJSON<Record<string, number[]>>(
    `/backtests/equity-preview/batch?${query.toString()}`,
  );
};
export const getBacktestInfo = (id: string) =>
  fetchJSON<BacktestInfo>(`/backtest/${id}/info`);
export const getBacktestStats = (id: string) =>
  fetchJSON<any>(`/backtest/${id}/stats`);
export const getBacktestTrades = (
  id: string,
  params?: { offset?: number; limit?: number },
) => {
  const query = new URLSearchParams();
  if (params?.offset != null)
    query.set("offset", String(Math.max(0, Math.trunc(params.offset))));
  if (params?.limit != null)
    query.set(
      "limit",
      String(Math.max(1, Math.min(100, Math.trunc(params.limit)))),
    );
  const queryString = query.toString();
  return fetchJSON<Trade[]>(withQuery(`/backtest/${id}/trades`, queryString));
};
export const getBacktestTrade = (id: string, nr: number) =>
  fetchJSON<TradeChartResponse>(`/backtest/${id}/trade/${nr}`);
export const getBacktestReplay = (id: string) =>
  fetchJSON<BacktestReplayResponse>(`/backtest/${id}/replay`);
export const getBacktestEquity = (
  id: string,
  params?: { max_points?: number; sampling?: "none" | "minmax" },
) => {
  const query = new URLSearchParams();
  if (params?.max_points != null) {
    const capped = Math.max(0, Math.min(20000, Math.trunc(params.max_points)));
    query.set("max_points", String(capped));
  }
  if (params?.sampling) {
    query.set("sampling", params.sampling);
  }
  const queryString = query.toString();
  return fetchJSON<any>(withQuery(`/backtest/${id}/equity`, queryString));
};
export const getBacktestCapm = (id: string, benchmark?: string) => {
  const params = new URLSearchParams();
  if (benchmark) params.set("benchmark", benchmark);
  const query = params.toString();
  return fetchJSON<BacktestCapmResponse>(
    withQuery(`/backtest/${id}/capm`, query),
  );
};
export const getBacktestOHLCV = (id: string) =>
  fetchJSON<any>(`/backtest/${id}/ohlcv`);
export const renameBacktest = (id: string, title: string) =>
  fetchJSON<any>(`/backtests/${encodeURIComponent(id)}/name`, {
    method: "PUT",
    body: JSON.stringify({ name: title }),
  });
export const compareBacktests = (ids: string[]) =>
  fetchJSON<BacktestsCompareResponse>(
    `/backtests/compare?ids=${ids.join(",")}`,
  );
export const getBacktestDuplicates = () =>
  fetchJSON<BacktestsDuplicatesResponse>("/backtests/duplicates");
export const getBacktestMatrix = () =>
  fetchJSON<BacktestMatrixSummary>("/backtests/matrix/summary");
export const getBacktestMatrixMatrixCsvUrl = () =>
  "/api/backtests/matrix/matrix.csv";
export const getBacktestMatrixDailyEquityCsvUrl = () =>
  "/api/backtests/matrix/daily-equity.csv";
export const getMatrixGrid = (strategyId = "all", tradesLimit = 3) =>
  fetchJSON<MatrixGridOverview>(
    `/grid/${encodeURIComponent(strategyId)}?trades_limit=${Math.max(0, Math.min(20, Math.trunc(tradesLimit)))}`,
  );
export const deduplicateBacktests = () =>
  fetchJSON<BacktestsDeduplicateResponse>("/backtests/deduplicate", {
    method: "POST",
  });
export const removeEmptyBacktests = () =>
  fetchJSON<BacktestsRemoveEmptyResponse>("/backtests/remove-empty", {
    method: "POST",
  });
export const deleteBacktest = (id: string) =>
  fetchJSON<{ id: string; status: string }>(
    `/backtests/${encodeURIComponent(id)}`,
    {
      method: "DELETE",
    },
  );
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
  return fetchJSON<LiveAccountStats>(
    `/live/stats/${encodeURIComponent(accountId)}`,
  );
};
export const getLiveBalances = (accountId: string) =>
  fetchJSON<LiveBalancesResponse>(
    `/live/balances/${encodeURIComponent(accountId)}`,
  );
export const getLiveTrades = (accountId: string) =>
  fetchJSON<LiveTradesResponse>(
    `/live/trades/${encodeURIComponent(accountId)}`,
  );
export const getLiveDashboard = () =>
  fetchJSON<LiveDashboardResponse>("/live/dashboard");
export const getLiveQuote = (symbol: string) =>
  fetchJSON<LiveQuoteResponse>(`/live/quotes/${encodeURIComponent(symbol)}`);
export const getLivePositions = (accountId: string) =>
  fetchJSON<LivePositionsResponse>(
    `/live/positions/${encodeURIComponent(accountId)}`,
  );
export const getLiveSignalLog = (accountId: string, limit = 200) => {
  const params = new URLSearchParams();
  if (Number.isFinite(limit)) {
    params.set("limit", String(Math.max(1, Math.min(1000, Math.trunc(limit)))));
  }
  const query = params.toString();
  return fetchJSON<LiveSignalLogResponse>(
    withQuery(`/live/signals/${encodeURIComponent(accountId)}`, query),
  );
};
export const getSignalProcessors = () =>
  fetchJSON<SignalProcessorOption[]>("/signal-processors");
export const createLiveDeployment = (payload: LiveDeploymentCreateRequest) =>
  fetchJSON<LiveDeploymentConfig>("/live/deployments", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const updateLiveDeploymentStatus = (
  deploymentId: string,
  status: LiveDeploymentStatus,
) =>
  fetchJSON<LiveDeploymentConfig>(
    `/live/deployments/${encodeURIComponent(deploymentId)}/status`,
    {
      method: "PUT",
      body: JSON.stringify({ status }),
    },
  );
export const deleteLiveDeployment = (deploymentId: string) =>
  fetchJSON<{ status: string; id: string }>(
    `/live/deployments/${encodeURIComponent(deploymentId)}`,
    { method: "DELETE" },
  );

// Services
export const getServices = (type?: ServiceType) => {
  const params = new URLSearchParams();
  if (type) params.set("type", type);
  const query = params.toString();
  return fetchJSON<Service[]>(withQuery(`/services`, query));
};
export const getService = (serviceId: string) =>
  fetchJSON<Service>(`/services/${encodeURIComponent(serviceId)}`);
export const createService = (payload: {
  name: string;
  type: ServiceType;
  status?: ServiceStatus;
  config?: Record<string, unknown>;
}) =>
  fetchJSON<Service>("/services", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const updateServiceStatus = (serviceId: string, status: ServiceStatus) =>
  fetchJSON<Service>(`/services/${encodeURIComponent(serviceId)}/status`, {
    method: "PUT",
    body: JSON.stringify({ status }),
  });
export const deleteService = (serviceId: string) =>
  fetchJSON<{ status: string; id: string }>(
    `/services/${encodeURIComponent(serviceId)}`,
    {
      method: "DELETE",
    },
  );
export const getServiceLogs = (
  serviceId: string,
  lines?: number,
  level?: string,
  search?: string,
) => {
  const params = new URLSearchParams();
  if (lines) params.set("lines", String(lines));
  if (level) params.set("level", level);
  if (search) params.set("search", search);
  const query = params.toString();
  return fetchJSON<LogContent>(
    withQuery(`/services/${encodeURIComponent(serviceId)}/logs`, query),
  );
};

// Verification evidence
export const getVerificationEvidence = (params?: {
  component?: string;
  category?: string;
  status?: "success" | "failed" | "partial" | "skipped";
  limit?: number;
}) => {
  const query = new URLSearchParams();
  if (params?.component) query.set("component", params.component);
  if (params?.category) query.set("category", params.category);
  if (params?.status) query.set("status", params.status);
  if (params?.limit != null) query.set("limit", String(params.limit));
  const queryString = query.toString();
  return fetchJSON<VerificationEvidenceRecord[]>(
    withQuery(`/verification/evidence`, queryString),
  );
};

export const getVerificationEvidenceSummary = (params?: {
  component?: string;
  category?: string;
  limit?: number;
}) => {
  const query = new URLSearchParams();
  if (params?.component) query.set("component", params.component);
  if (params?.category) query.set("category", params.category);
  if (params?.limit != null) query.set("limit", String(params.limit));
  const queryString = query.toString();
  return fetchJSON<VerificationEvidenceSummary[]>(
    withQuery(`/verification/evidence/summary`, queryString),
  );
};

export const createVerificationEvidence = (
  payload: VerificationEvidenceCreateRequest,
) =>
  fetchJSON<VerificationEvidenceRecord>("/verification/evidence", {
    method: "POST",
    body: JSON.stringify(payload),
  });

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
export const runBacktestFromSignals = (data: RunBacktestFromSignalsRequest) =>
  fetchJSON<RunBacktestResponse>("/backtests/run-from-signals", {
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
  return fetchJSON<PortfolioSummary[]>(withQuery(`/portfolios`, queryString));
};
export const getPortfolio = (id: string) =>
  fetchJSON<PortfolioDetail>(`/portfolios/${id}`);
export const getPortfolioDetail = (id: string) =>
  fetchJSON<PortfolioDetail>(`/portfolios/${id}`);
export const renamePortfolio = (id: string, name: string) =>
  fetchJSON<{ id: string; name: string; status: string }>(
    `/portfolios/${encodeURIComponent(id)}/name`,
    {
      method: "PUT",
      body: JSON.stringify({ name }),
    },
  );
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
    withQuery(`/portfolios/${encodeURIComponent(id)}/trades`, query),
  );
};
export const createPortfolio = (data: {
  name: string;
  account_capital: number;
  components: Array<
    Pick<
      PortfolioComponent,
      | "strategy_name"
      | "symbol"
      | "timeframe"
      | "weight"
      | "risk_config"
      | "settings"
    > & { backtest_id?: string | null }
  >;
}) =>
  fetchJSON<PortfolioDetail>("/portfolios", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const deletePortfolio = (id: string) =>
  fetchJSON<{ status: string; id: string }>(`/portfolios/${id}`, {
    method: "DELETE",
  });
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
export const removeEmptyPortfolios = () =>
  fetchJSON<PortfoliosRemoveEmptyResponse>("/portfolios/remove-empty", {
    method: "POST",
  });
export const comparePortfolios = (ids: string[]) =>
  fetchJSON<{ portfolios: PortfolioDetail[] }>(
    `/portfolios/compare?ids=${ids.join(",")}`,
  );

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
    withQuery(`/data/dukascopy/genesis`, query),
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
export const listDataTasks = () =>
  fetchJSON<DataTaskListResponse>("/data/tasks");
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
    withQuery(`/data/cache/invalidate`, query),
    { method: "POST" },
  );
};
