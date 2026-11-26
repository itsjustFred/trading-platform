import type {
  AgentConvertRequest,
  DataCategoryResponse,
  DataDownloadRequest,
  DataHarvestRequest,
  DataOverviewResponse,
  DataResampleRequest,
  DataTaskListResponse,
  DataTaskStartResponse,
  DataTaskStatusResponse,
  DataCheckResponse,
  DukascopyGenesisResponse,
  LiveAccount,
  LiveAccountStats,
  LiveBalancesResponse,
  LiveDeploymentCreateRequest,
  LiveDeploymentConfig,
  LiveDeploymentDeleteResponse,
  LiveDeploymentStatusUpdateResponse,
  LiveKillSwitchHaltResponse,
  LiveKillSwitchMode,
  LiveKillSwitchResumeResponse,
  LiveKillSwitchState,
  LiveSignalLogResponse,
  LivePositionsResponse,
  LiveQuoteResponse,
  LiveDeploymentStatus,
  LiveDashboardResponse,
  Service,
  ServiceStatus,
  ServiceType,
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
  StrategyDetail,
  StrategyMeta,
  Trade,
  BacktestInfo,
  BacktestCapmResponse,
  BulkDeleteConfirmRequest,
  BulkDeleteDryRunRequest,
  BacktestsDeduplicateConfirmResponse,
  BacktestsDeduplicateDryRunResponse,
  BacktestsDeduplicateResponse,
  BacktestsBulkDeleteResponse,
  BacktestsRemoveEmptyConfirmResponse,
  BacktestsRemoveEmptyDryRunResponse,
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
  PortfoliosDeduplicateConfirmResponse,
  PortfoliosDeduplicateDryRunResponse,
  PortfoliosDeduplicateResponse,
  PortfoliosRemoveEmptyConfirmResponse,
  PortfoliosRemoveEmptyDryRunResponse,
  PortfoliosRemoveEmptyResponse,
  BacktestsDuplicatesResponse,
  BacktestMetadataItem,
  BacktestReplayResponse,
  PortfoliosBulkDeleteResponse,
  BacktestStats,
  BenchmarkResponse,
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
  fetchJSON<BacktestStats>(`/backtest/${id}/stats`);
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
  return fetchJSON<Record<string, number>[]>(
    withQuery(`/backtest/${id}/equity`, queryString),
  );
};
export const getBacktestCapm = (id: string, benchmark?: string) => {
  const params = new URLSearchParams();
  if (benchmark) params.set("benchmark", benchmark);
  const query = params.toString();
  return fetchJSON<BacktestCapmResponse>(
    withQuery(`/backtest/${id}/capm`, query),
  );
};
export const renameBacktest = (id: string, title: string) =>
  fetchJSON<unknown>(`/backtests/${encodeURIComponent(id)}/name`, {
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
// Two-phase bulk delete: a dry run returns the exact candidate list and
// deletes nothing; the confirm call deletes only the ids it is given.
export function deduplicateBacktests(
  payload: BulkDeleteDryRunRequest,
): Promise<BacktestsDeduplicateDryRunResponse>;
export function deduplicateBacktests(
  payload: BulkDeleteConfirmRequest,
): Promise<BacktestsDeduplicateConfirmResponse>;
export function deduplicateBacktests(
  payload: BulkDeleteDryRunRequest | BulkDeleteConfirmRequest,
): Promise<BacktestsDeduplicateResponse> {
  return fetchJSON<BacktestsDeduplicateResponse>("/backtests/deduplicate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
// Two-phase bulk delete: a dry run returns the exact candidate list and
// deletes nothing; the confirm call deletes only the ids it is given.
export function removeEmptyBacktests(
  payload: BulkDeleteDryRunRequest,
): Promise<BacktestsRemoveEmptyDryRunResponse>;
export function removeEmptyBacktests(
  payload: BulkDeleteConfirmRequest,
): Promise<BacktestsRemoveEmptyConfirmResponse>;
export function removeEmptyBacktests(
  payload: BulkDeleteDryRunRequest | BulkDeleteConfirmRequest,
): Promise<BacktestsRemoveEmptyResponse> {
  return fetchJSON<BacktestsRemoveEmptyResponse>("/backtests/remove-empty", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
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
// The confirmation echoes the deployment id back. The server requires it, so
// this is not decorative: it is what proves the operator confirmed *this* row
// rather than the similar-looking one directly above it in the list.
export const updateLiveDeploymentStatus = (
  deploymentId: string,
  status: LiveDeploymentStatus,
) =>
  fetchJSON<LiveDeploymentStatusUpdateResponse>(
    `/live/deployments/${encodeURIComponent(deploymentId)}/status`,
    {
      method: "PUT",
      body: JSON.stringify({
        status,
        confirmation: { confirmed_deployment_id: deploymentId },
      }),
    },
  );
export const deleteLiveDeployment = (deploymentId: string) =>
  fetchJSON<LiveDeploymentDeleteResponse>(
    // DELETE carries no body by convention, so the confirmation travels as a
    // query parameter rather than in a JSON payload.
    `/live/deployments/${encodeURIComponent(deploymentId)}` +
      `?confirm_deployment_id=${encodeURIComponent(deploymentId)}`,
    { method: "DELETE" },
  );

// Kill switch. The state read is what the daemon enforces, so `readable:
// false` must reach the UI as its own state rather than as "nothing halted".
export const getLiveKillSwitch = () =>
  fetchJSON<LiveKillSwitchState>("/live/kill-switch");
// The modes that move money (`orders`, `flatten`) and every resume are
// confirmed by echoing the scope back, exactly like the deployment controls:
// the server refuses a bare call with 428, and the echo is what proves the
// operator confirmed *this* scope. `entries` stays unconfirmed on purpose -
// it can cost nothing and must remain one click in an incident.
export const haltLiveKillSwitch = (
  scope: string,
  mode: LiveKillSwitchMode,
  options?: { reason?: string; confirmedScope?: string },
) =>
  fetchJSON<LiveKillSwitchHaltResponse>("/live/kill-switch/halt", {
    method: "POST",
    body: JSON.stringify({
      scope,
      mode,
      reason: options?.reason ?? "",
      confirmed_scope: options?.confirmedScope ?? null,
    }),
  });
export const resumeLiveKillSwitch = (scope: string, confirmedScope: string) =>
  fetchJSON<LiveKillSwitchResumeResponse>("/live/kill-switch/resume", {
    method: "POST",
    body: JSON.stringify({ scope, confirmed_scope: confirmedScope }),
  });

// Services
export const getServices = (type?: ServiceType) => {
  const params = new URLSearchParams();
  if (type) params.set("type", type);
  const query = params.toString();
  return fetchJSON<Service[]>(withQuery(`/services`, query));
};
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
// Two-phase bulk delete: a dry run returns the exact candidate list and
// deletes nothing; the confirm call deletes only the ids it is given.
export function deduplicatePortfolios(
  payload: BulkDeleteDryRunRequest,
): Promise<PortfoliosDeduplicateDryRunResponse>;
export function deduplicatePortfolios(
  payload: BulkDeleteConfirmRequest,
): Promise<PortfoliosDeduplicateConfirmResponse>;
export function deduplicatePortfolios(
  payload: BulkDeleteDryRunRequest | BulkDeleteConfirmRequest,
): Promise<PortfoliosDeduplicateResponse> {
  return fetchJSON<PortfoliosDeduplicateResponse>("/portfolios/deduplicate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
// Two-phase bulk delete: a dry run returns the exact candidate list and
// deletes nothing; the confirm call deletes only the ids it is given.
export function removeEmptyPortfolios(
  payload: BulkDeleteDryRunRequest,
): Promise<PortfoliosRemoveEmptyDryRunResponse>;
export function removeEmptyPortfolios(
  payload: BulkDeleteConfirmRequest,
): Promise<PortfoliosRemoveEmptyConfirmResponse>;
export function removeEmptyPortfolios(
  payload: BulkDeleteDryRunRequest | BulkDeleteConfirmRequest,
): Promise<PortfoliosRemoveEmptyResponse> {
  return fetchJSON<PortfoliosRemoveEmptyResponse>("/portfolios/remove-empty", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

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
  return fetchJSON<BenchmarkResponse>(`/benchmark/${symbol}?${params}`);
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
  fetchJSON<unknown>(`/data/symbol?path=${encodeURIComponent(path)}`, {
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
