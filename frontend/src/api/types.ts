export type StrategyParameterValue = number | string | boolean | null;

export interface StrategyMeta {
  slug: string;
  name: string;
  version: string;
  file: string;
  created_at: string;
  tags: Record<string, any>;
  type: string[];
  source: string;
  asset_class: string[];
  complexity: string;
  engine: string;
  parameters: Record<string, StrategyParameterValue>;
  backtest_count: number;
  best_sharpe: number | null;
}

export interface StrategyDetail extends StrategyMeta {
  link: string;
  documentation_markdown: string;
  backtests: BacktestSummary[];
  portfolios: PortfolioRef[];
}

export interface RunBacktestRequest {
  strategy: string;
  engine: string;
  symbols: string[];
  timeframe: string;
  start: string;
  end: string;
  capital: number;
  risk_per_trade_pct?: number;
  max_leverage?: number;
  modeling_resolution?: string;
  parameters?: Record<string, number | string>;
}

export interface RunBacktestResponse {
  job_id: string;
  status: string;
}

export interface RunBacktestStatusResponse {
  status: string;
  result: unknown;
  error: unknown;
}

export interface BacktestSummary {
  id: string;
  title: string;
  notes: string;
  strategy_slug: string;
  engine: string;
  symbols: string[];
  timeframes: string[];
  backtest_start: string;
  backtest_end: string;
  trade_count: number;
  total_pnl: number;
  win_rate: number;
  created_at: string;
}

export interface BacktestMeta {
  id: string;
  created_at: string;
  engine: string;
  strategies: string[];
  backtest_start: string;
  backtest_end: string;
  duration_days: number;
  total_bars: number;
  initial_capital: number;
  currency: string;
  data: {
    timeframes: string[];
    symbols: string[];
    universe: string;
    source: string;
  };
  parameters: Record<string, any>;
  tags: string[];
  notes: string;
}

export interface Trade {
  nr: number;
  symbol: string;
  direction: string;
  entry_time: string;
  entry_price: number;
  exit_time: string;
  exit_price: number;
  quantity: number;
  pnl: number;
  pnl_pct: number;
  duration: string;
  bars_held: number;
  meta?: Record<string, unknown>;
}

export interface BacktestInfo {
  id: string;
  title: string;
  notes: string;
  strategy: string;
  strategy_slug: string;
  strategy_display_name: string;
  strategy_file: string;
  engine: string;
  symbols: string[];
  timeframes: string[];
  trade_count: number;
  start: string;
  end: string;
}

export interface BacktestStats {
  net_profit: number;
  net_profit_pct: number;
  cagr: number;
  sharpe: number;
  sortino: number;
  max_drawdown_pct: number;
  max_drawdown_duration: string;
  win_rate: number;
  profit_factor: number;
  total_trades: number;
  avg_trade_pnl: number;
  avg_win: number;
  avg_loss: number;
  best_trade: number;
  worst_trade: number;
  avg_bars_held: number;
  calmar: number;
  expectancy: number;
  psr?: number;
  p_value?: number;
  [key: string]: any;
}

export interface EquityPoint {
  time: number;
  equity: number;
}

export interface BacktestListItem {
  id: string;
  title?: string;
  notes?: string;
  strategy: string;
  strategy_slug?: string;
  strategy_display_name?: string;
  strategy_file?: string;
  engine: string;
  symbols: string[];
  timeframes: string[];
  trade_count: number;
  start: string;
  end: string;
  created_at?: string;
}

export interface TradeDetail extends Trade {
  ohlcv: OHLCVBar[];
}

export interface OHLCVBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface PortfolioRef {
  id: string;
  name: string;
}

export interface PortfolioSummary {
  id: string;
  name: string;
  account_capital: number;
  strategy_count: number;
  sharpe: number;
  cagr: number;
  max_drawdown: number;
  net_profit: number;
  psr?: number;
  p_value?: number;
  created_at: string;
}

export interface PortfolioDetail {
  id: string;
  name: string;
  created_at: string;
  account_capital: number;
  strategies: PortfolioStrategy[];
  combined_stats: {
    sharpe: number;
    cagr: number;
    max_drawdown: number;
    net_profit: number;
    psr?: number;
    p_value?: number;
    correlation_matrix: Record<string, any>;
  };
  combined_equity: { time: string; equity: number }[];
  strategy_contributions: {
    strategy_name: string;
    pnl_contribution: number;
    pnl_contribution_pct: number;
  }[];
  trades: PortfolioTrade[];
}

export interface PortfolioStrategy {
  backtest_id: string;
  strategy_name: string;
  risk_per_trade_pct: number;
  max_positions: number;
}

export interface PortfolioTrade {
  time: string;
  pnl: number;
  strategy: string;
  symbol: string;
  direction: string;
}

export interface LogFile {
  name: string;
  size_bytes: number;
  modified: string | null;
}

export interface LogContent {
  name: string;
  lines: string[];
  total_lines: number;
}

export interface DukascopyGenesisResponse {
  source: string;
  genesis: Record<string, Record<string, string>>;
  lower_bound: string;
}

export interface DataCategorySummary {
  name: string;
  symbol_count: number;
  file_count: number;
  total_size_mb: number;
  start?: string | null;
  end?: string | null;
}

export interface DataSymbolEntry {
  symbol: string;
  timeframe: string;
  path: string;
  size_mb: number;
  start?: string | null;
  end?: string | null;
  source: string;
  repo?: boolean;
  last_updated?: string | null;
  validation?: {
    status?: string;
  };
  error?: string | null;
  warning_message?: string | null;
  validation_message?: string | null;
}

export interface DataOverviewResponse {
  categories: DataCategorySummary[];
}

export interface DataCategoryResponse {
  symbols: DataSymbolEntry[];
}

export interface DataDownloadRequest {
  source: string;
  symbols: string;
  timeframes: string;
  start?: string;
  end?: string;
  price_type?: "bid" | "ask";
  merge_tick?: boolean;
  merge_s1?: boolean;
  batch_size?: number;
  pause_ms?: number;
}

export interface DataResampleRequest {
  source_path: string;
  target_timeframes: string;
}

export interface DataTaskProgress {
  completed: number;
  failed: number;
  total: number;
  percent: number;
  current: string;
}

export interface DataTaskStatusResponse {
  id?: string;
  name?: string;
  description?: string;
  kind?: string;
  source?: string;
  status: string;
  created_at?: string;
  updated_at?: string;
  log?: string;
  progress?: DataTaskProgress;
  can_pause?: boolean;
  can_resume?: boolean;
  can_cancel?: boolean;
}

export interface DataTaskListResponse {
  tasks: DataTaskStatusResponse[];
}

export interface DataTaskLogsResponse {
  logs: string;
  status: string;
}

export interface DataTaskStartResponse {
  task_id: string;
  job_id?: string;
  status: string;
}

export interface DataTaskDeleteResponse {
  task_id: string;
  status: string;
}

export interface AgentConvertRequest {
  code: string;
  strategy_name?: string;
}

export interface CompareResult {
  ids: string[];
  strategies: string[];
  stats: BacktestStats[];
  equity_curves: EquityPoint[][];
  combined_equity: EquityPoint[];
  correlation: number;
}
