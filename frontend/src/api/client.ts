import type {
  DataCategoryResponse,
  DataDownloadRequest,
  DataOverviewResponse,
  DataResampleRequest,
  DataTaskLogsResponse,
  DataTaskStartResponse,
  DataTaskStatusResponse,
  DukascopyGenesisResponse,
  LogContent,
  LogFile,
} from "./types";

const BASE_URL = "/api";

async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${url}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

// Strategies
export const getStrategies = () => fetchJSON<any[]>("/strategies");
export const getStrategy = (name: string) =>
  fetchJSON<any>(`/strategies/${encodeURIComponent(name)}`);
export const getStrategyDetail = (name: string) =>
  fetchJSON<any>(`/strategies/${encodeURIComponent(name)}`);

// Backtests
export const getBacktests = () => fetchJSON<any[]>("/backtests");
export const getBacktestInfo = (id: string) =>
  fetchJSON<any>(`/backtest/${id}/info`);
export const getBacktestStats = (id: string) =>
  fetchJSON<any>(`/backtest/${id}/stats`);
export const getBacktestTrades = (id: string) =>
  fetchJSON<any[]>(`/backtest/${id}/trades`);
export const getBacktestTrade = (id: string, nr: number) =>
  fetchJSON<any>(`/backtest/${id}/trade/${nr}`);
export const getBacktestEquity = (id: string) =>
  fetchJSON<any>(`/backtest/${id}/equity`);
export const getBacktestOHLCV = (id: string) =>
  fetchJSON<any>(`/backtest/${id}/ohlcv`);
export const compareBacktests = (ids: string[]) =>
  fetchJSON<any>(`/compare?ids=${ids.join(",")}`);

// Run backtest
export const runBacktest = (data: {
  strategy: string;
  engine: string;
  symbols: string[];
  timeframe: string;
  start: string;
  end: string;
  capital: number;
  risk_per_trade_pct?: number;
  parameters?: Record<string, any>;
}) =>
  fetchJSON<{ job_id: string; status: string }>("/backtests/run", {
    method: "POST",
    body: JSON.stringify(data),
  });
export const getBacktestRunStatus = (jobId: string) =>
  fetchJSON<{ status: string; result: any; error: any }>(
    `/backtests/run/${jobId}/status`,
  );

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
export const getDataTask = (taskId: string) =>
  fetchJSON<DataTaskStatusResponse>(`/data/tasks/${taskId}`);
export const getDataTaskLogs = (taskId: string) =>
  fetchJSON<DataTaskLogsResponse>(`/data/tasks/${taskId}/logs`);
