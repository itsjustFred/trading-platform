export type StrategyParameterValue = number | string | boolean | null;

export interface StrategyMeta {
  slug: string;
  name: string;
  version: string;
  file: string;
  created_at: string;
  source_url: string;
  notes: string;
  tags: Record<string, any>;
  type: string[];
  source: string;
  asset_class: string[];
  complexity: string;
  engine: string;
  parameters: Record<string, StrategyParameterValue>;
  backtest_count: number;
  best_sharpe: number | null;
  equity_preview?: number[];
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
  task_id?: string;
  job_id: string;
  status: string;
}

export interface RunBacktestStatusResponse {
  status: string;
  result: unknown;
  error: unknown;
}

export interface SignalReplayInput {
  timestamp: string;
  symbol: string;
  action: "buy" | "sell" | "long" | "short";
  entry_price: number;
  stop_loss?: number;
  take_profit?: number;
  timeframe?: string;
  source?: string;
  metadata?: Record<string, unknown>;
}

export type BacktestFromSignalsSource = "csv" | "discord-history";

export interface RunBacktestSignalsRequest {
  signals?: SignalReplayInput[];
  signal_source?: LiveSignalSource;
  signal_processor?: string;
  account_id?: string;
  start_date: string;
  end_date: string;
  timeframe?: string;
  capital?: number;
  risk_per_trade_pct?: number;
  max_position_pct?: number;
  max_positions?: number;
  auto_download?: boolean;
}

export interface RunBacktestFromSignalsRequest {
  signal_source: BacktestFromSignalsSource;
  filepath?: string;
  signal_processor?: string;
  account_id?: string;
  start_date: string;
  end_date: string;
  timeframe?: string;
  capital?: number;
  risk_per_trade_pct?: number;
  max_position_pct?: number;
  max_positions?: number;
  auto_download?: boolean;
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
  entry_time: number;
  entry_time_iso?: string;
  entry_price: number;
  exit_time: number;
  exit_time_iso?: string;
  exit_price: number;
  quantity: number;
  position_size?: number | null;
  risk_amount?: number | null;
  risk_percent?: number | null;
  exit_reason?: string | null;
  pnl: number;
  pnl_pct: number;
  duration: string;
  bars_held?: number;
  holding_bars?: number;
  exits?: TradeExitView[];
  meta?: TradeMeta;
}

export interface TradeExitView {
  time: number;
  time_iso?: string;
  price: number;
  quantity: number;
  pnl?: number;
  reason?: string;
}

export interface TradeRiskMath {
  sizing_mode?: string;
  equity_at_entry?: number | null;
  risk_target_usd?: number | null;
  entry_price?: number | null;
  sl_price?: number | null;
  stop_distance?: number | null;
  position_size?: number | null;
  required_notional?: number | null;
  final_notional?: number | null;
  max_position_notional?: number | null;
  max_leverage_notional?: number | null;
  max_position_pct?: number | null;
  max_leverage?: number | null;
  capped_by_max_position?: boolean;
  capped_by_max_leverage?: boolean;
  [key: string]: unknown;
}

export interface TradeMeta {
  risk_math?: TradeRiskMath;
  [key: string]: unknown;
}

export interface MissingOHLCVDetail {
  code: "MISSING_OHLCV";
  message: string;
  symbol: string;
  timeframe: string;
  category?: string;
  suggested_source: string;
  alternative_sources?: string[];
  download_request?: {
    source: string;
    symbols: string;
    timeframes: string;
    start?: string;
    end?: string;
  };
}

export interface DataCheckResponse {
  available: boolean;
  missing_range: {
    start: string;
    end: string;
  } | null;
}

export interface IndicatorPoint {
  time: number;
  value: number | null;
}

export interface IndicatorStyle {
  color?: string;
  lineStyle?: number;
  width?: number;
  pane?: string;
}

export interface IndicatorSeriesView {
  name: string;
  type: string;
  data: IndicatorPoint[];
  style?: IndicatorStyle;
  color?: string;
  lineStyle?: number;
  pane?: string;
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
  timeframe: {
    start: string;
    end: string;
  };
  data_timeframe: string;
  timeframes: string[];
  initial_capital: number;
  created_at: string;
  trade_count: number;
  parameters: Record<string, StrategyParameterValue>;
  has_equity_curve: boolean;
  has_indicators: boolean;
}

export interface OptimizationParameterRange {
  type: "int" | "float" | "categorical";
  min?: number;
  max?: number;
  step?: number;
  choices?: Array<number | string | boolean>;
}

export interface OptimizationRunRequest {
  base_backtest_id: string;
  target_metric: "sharpe" | "net_profit" | "calmar" | "cagr" | "win_rate";
  n_trials: number;
  train_split_pct: number;
  parameter_ranges: Record<string, OptimizationParameterRange>;
  optimization_mode?:
    | "parameter"
    | "risk_parity_universe"
    | "risk_parity_covariance";
  symbol_universes?: string[][];
}

export interface OptimizationRunResponse {
  task_id?: string;
  job_id: string;
  status: string;
}

export interface OptimizationPeriod {
  start: string;
  end: string;
}

export interface OptimizationTrial {
  number: number;
  state: string;
  value: number | null;
  params: Record<string, StrategyParameterValue>;
  effective_parameters: Record<string, StrategyParameterValue>;
  symbols?: string[];
  trade_count: number;
  penalty_applied: boolean;
  train_stats: OptimizationTrainStat;
}

export type OptimizationTrainStat = Record<
  string,
  number | string | boolean | null
>;

export interface OptimizationSummary {
  id: string;
  filename: string;
  created_at: string;
  base_backtest_id: string;
  target_metric: string;
  n_trials: number;
  train_split_pct: number;
  best_value: number | null;
  best_params: Record<string, StrategyParameterValue>;
  effective_best_parameters: Record<string, StrategyParameterValue>;
  symbols?: string[];
  best_symbols?: string[];
  optimization_mode?: string;
  symbol_universes?: string[][];
  train_period: OptimizationPeriod;
  test_period: OptimizationPeriod;
  train_stats: OptimizationTrainStat;
  test_stats: Record<string, number | string | boolean | null>;
}

export interface OptimizationDetail {
  id: string;
  file: string;
  created_at: string;
  engine: string;
  base_backtest_id: string;
  strategy: {
    identifier: string;
    slug: string;
    path: string;
  };
  symbols: string[];
  best_symbols?: string[];
  timeframe: string;
  target_metric: string;
  n_trials: number;
  train_split_pct: number;
  optimization_mode?: string;
  parameter_ranges: Record<string, OptimizationParameterRange>;
  symbol_universes?: string[][];
  base_parameters: Record<string, StrategyParameterValue>;
  best_params: Record<string, StrategyParameterValue>;
  effective_best_parameters: Record<string, StrategyParameterValue>;
  best_value: number | null;
  train_period: OptimizationPeriod;
  test_period: OptimizationPeriod;
  train_stats: OptimizationTrainStat;
  test_stats: Record<string, number | string | boolean | null>;
  top_trials: OptimizationTrial[];
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

export interface CapmScatterPoint {
  x: number;
  y: number;
}

export interface CapmRegressionLine {
  slope: number;
  intercept: number;
}

export interface BacktestCapmResponse {
  alpha: number;
  beta: number;
  r_squared: number;
  p_value: number;
  scatter_data: CapmScatterPoint[];
  regression_line: CapmRegressionLine;
  benchmark_symbol: string;
}

export interface EquityPoint {
  time: number;
  equity: number;
}

export interface LiveAccount {
  id: string;
  name: string;
  broker: "alpaca" | "ctrader";
  balance: number;
  is_demo: boolean;
  broker_login?: string;
  login?: string;
  ctid?: string;
}

export type LiveDeploymentStatus = "ACTIVE" | "PAUSED" | "STOPPED";
export type LiveSignalSource = "internal" | "discord";
export type ServiceType = "discord_bot" | "scheduled_job";
export type ServiceStatus = "running" | "paused" | "failed" | "stopped";
export type VerificationEvidenceStatus =
  | "success"
  | "failed"
  | "partial"
  | "skipped";

export interface Service {
  id: string;
  name: string;
  type: ServiceType;
  status: ServiceStatus;
  config: Record<string, unknown>;
  last_activity?: string | null;
  created_at: string;
}

export interface VerificationEvidenceRecord {
  id: string;
  run_id: string;
  component: string;
  flow_name: string;
  category: string;
  status: VerificationEvidenceStatus;
  started_at: string;
  finished_at: string;
  duration_seconds?: number | null;
  git_sha: string;
  environment: string;
  mode: string;
  metadata: Record<string, unknown>;
  error_summary?: string | null;
  evidence_ref?: string | null;
  manual_validation_required: boolean;
  source: string;
  created_at: string;
  updated_at: string;
}

export interface VerificationEvidenceSummary {
  component: string;
  flow_name: string;
  category: string;
  run_count: number;
  success_count: number;
  failed_count: number;
  partial_count: number;
  skipped_count: number;
  last_status: VerificationEvidenceStatus;
  last_run_at: string;
  last_success_at: string;
  last_error: string;
  latest_git_sha: string;
  latest_environment: string;
  latest_mode: string;
  manual_validation_required: boolean;
}

export interface VerificationEvidenceCreateRequest {
  component: string;
  flow_name: string;
  category: string;
  status: VerificationEvidenceStatus;
  started_at?: string;
  finished_at?: string;
  duration_seconds?: number;
  git_sha?: string;
  environment?: string;
  mode?: string;
  metadata?: Record<string, unknown>;
  error_summary?: string;
  evidence_ref?: string;
  manual_validation_required?: boolean;
  source?: string;
  run_id?: string;
}

export interface SignalProcessorOption {
  id: string;
  label: string;
  description: string;
}

export interface LiveDeploymentConfig {
  id: string;
  strategy_name: string;
  account_id: string;
  symbols: string[];
  timeframe: string;
  status: LiveDeploymentStatus;
  signal_source_type?: LiveSignalSource;
  signal_source_id?: string | null;
  symbol_filter?: string[];
  signal_source: LiveSignalSource;
  signal_config: Record<string, unknown>;
  settings: Record<string, unknown>;
}

export interface LiveDeploymentCreateRequest {
  strategy_name?: string;
  account_id: string;
  symbols: string[];
  timeframe: string;
  signal_source_type?: LiveSignalSource;
  signal_source_id?: string;
  symbol_filter?: string[];
  signal_source?: LiveSignalSource;
  signal_config?: Record<string, unknown>;
  settings?: Record<string, unknown>;
}

export interface LiveBalanceRecord {
  id: string;
  account_id: string;
  timestamp: string;
  time: number;
  balance: number;
  equity: number;
}

export interface LiveEquitySeriesPoint {
  time: number;
  equity: number;
  balance?: number;
  value?: number;
}

export interface LiveAccountStats {
  account_id: string;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  total_pnl: number;
  current_balance: number;
  current_equity: number;
  max_drawdown: number;
  max_drawdown_pct: number;
}

export interface LiveTradeSlippage {
  signal_id: string;
  execution_id: string;
  deployment_id: string;
  account_id: string;
  symbol: string;
  action: string;
  theoretical_price: number;
  actual_price: number;
  slippage: number;
  slippage_bps: number;
  filled_qty: number;
  commission: number;
  position_size?: number | null;
  risk_amount?: number | null;
  risk_percent?: number | null;
  exit_reason?: string | null;
  signal_timestamp: string;
  signal_time: number;
  execution_timestamp: string;
  execution_time: number;
  broker_order_id: string;
  signal_metadata: Record<string, unknown>;
}

export interface LiveBalancesResponse {
  account_id: string;
  series: LiveBalanceRecord[];
}

export interface LiveTradesResponse {
  account_id: string;
  deployment_ids: string[];
  trades: LiveTradeSlippage[];
}

export interface LiveDashboardAccount extends LiveAccount {
  current_equity: number;
  current_balance: number;
  total_trades: number;
  total_pnl: number;
}

export interface LiveDashboardStrategy extends LiveDeploymentConfig {
  capital_allocation: number;
  account_equity: number;
}

export interface LiveDashboardResponse {
  generated_at: string;
  accounts: LiveDashboardAccount[];
  active_strategies: LiveDashboardStrategy[];
  capital: {
    total_equity: number;
    active_account_count: number;
    active_strategy_count: number;
  };
  recent_fills: LiveTradeSlippage[];
}

export interface LiveQuoteBar {
  symbol: string;
  timestamp: string;
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type LiveQuote = LiveQuoteBar;

export interface LiveQuoteResponse {
  symbol: string;
  latest: LiveQuoteBar | null;
  bars: LiveQuoteBar[];
}

export interface LivePosition {
  symbol: string;
  qty: number;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
}

export interface LivePositionsResponse {
  account_id: string;
  positions: LivePosition[];
}

export type LiveSignalStatus =
  | "Pending"
  | "Waiting"
  | "Executed"
  | "Filled"
  | "Failed";

export interface LiveSignalLog {
  signal_id: string;
  deployment_id: string;
  symbol: string;
  action: string;
  status: LiveSignalStatus;
  source: string;
  signal_timestamp: string;
  signal_time: number;
  execution_timestamp: string;
  execution_time: number;
  theoretical_price: number;
  actual_price: number | null;
  filled_qty: number | null;
  commission: number | null;
  broker_order_id: string | null;
  signal_metadata: Record<string, unknown>;
  raw_message: string | null;
  waiting_for_candle_close: boolean;
  waiting_countdown_seconds: number | null;
}

export interface LiveSignalLogResponse {
  account_id: string;
  deployment_ids: string[];
  signals: LiveSignalLog[];
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
  sharpe?: number | null;
  sharpe_ratio?: number | null;
  cagr?: number | null;
  max_drawdown_pct?: number | null;
  net_profit?: number | null;
  net_profit_pct?: number | null;
  psr?: number | null;
  p_value?: number | null;
  win_rate?: number | null;
  equity_mini?: number[];
}

export interface BacktestMetadataItem {
  id: string;
  strategy_name: string;
  symbols: string[];
  timeframe: string;
  total_return: number;
  sharpe_ratio: number;
  psr: number;
  p_value: number;
  cagr: number;
  max_drawdown: number;
  trade_count: number;
  win_rate: number | null;
  start_date: string;
  end_date: string;
  created_at: string;
  tags?: string[];
  equity_preview?: number[];
}

export type BacktestsSortField =
  | "created_at"
  | "strategy_name"
  | "symbol"
  | "timeframe"
  | "trade_count"
  | "total_return"
  | "sharpe_ratio"
  | "cagr"
  | "psr"
  | "p_value"
  | "max_drawdown"
  | "win_rate";

export interface BacktestsQueryParams {
  page?: number;
  per_page?: number;
  sort?: BacktestsSortField;
  order?: "asc" | "desc";
  name?: string;
  strategy?: string;
  symbol?: string;
  timeframe?: string;
  search?: string;
  total_return_min?: number;
  total_return_max?: number;
  sharpe_min?: number;
  sharpe_max?: number;
  psr_min?: number;
  psr_max?: number;
  p_value_min?: number;
  p_value_max?: number;
  cagr_min?: number;
  cagr_max?: number;
  win_rate_min?: number;
  win_rate_max?: number;
  drawdown_min?: number;
  drawdown_max?: number;
  max_drawdown_min?: number;
  max_drawdown_max?: number;
}

export interface BacktestsListResponse {
  items: BacktestListItem[];
  page: number;
  per_page: number;
  total: number;
  pages: number;
  sort: string;
  order: string;
}

export interface BacktestMatrixStrategyOverview {
  strategy: string;
  strategy_name?: string;
  ok_cells: number;
  failed_cells: number;
  skipped_existing_cells: number;
  total_trades: number;
  total_pnl: number;
  avg_sharpe?: number | null;
  best_sharpe?: number | null;
  best_net_profit_pct?: number | null;
  best_symbol?: string | null;
  best_timeframe?: string | null;
  best_run_id?: string | null;
}

export interface BacktestMatrixRun {
  strategy: string;
  strategy_name?: string;
  symbol: string;
  timeframe: string;
  run_id?: string;
  status: string;
  total_trades?: number;
  total_pnl?: number;
  net_profit_pct?: number;
  sharpe?: number | null;
  cagr?: number | null;
  max_drawdown_pct?: number | null;
  win_rate?: number | null;
  daily_return_count?: number;
  output?: string;
  error?: string;
}

export interface BacktestMatrixSummary {
  started_at?: string;
  completed_at?: string;
  strategies: string[];
  strategy_names?: string[];
  symbols: string[];
  timeframes: string[];
  start: string;
  end: string;
  engine: string;
  capital: number;
  requested_cells: number;
  ok_cells: number;
  failed_cells: number;
  skipped_existing_cells: number;
  daily_equity_rows: number;
  matrix_csv?: string;
  daily_equity_csv?: string;
  strategy_overview?: BacktestMatrixStrategyOverview[];
  runs?: BacktestMatrixRun[];
}

export interface MatrixGridRow {
  strategy: string;
  status: string;
  symbol: string;
  timeframe: string;
  run_id: string;
  backtest_id?: string | null;
  backtest_url?: string | null;
  trades_url?: string | null;
  trade_data_available?: boolean | null;
  trades?: Trade[];
  trade_sample_count?: number;
  output?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_minutes?: number | null;
  total_trades: number;
  total_pnl?: number | null;
  net_profit_pct?: number | null;
  sharpe?: number | null;
  cagr?: number | null;
  max_drawdown_pct?: number | null;
  win_rate?: number | null;
  psr?: number | null;
  daily_return_count?: number | null;
  returncode?: number | null;
  matrix_csv?: string | null;
}

export interface MatrixGridBlockedStrategy {
  strategy: string;
  status: string;
  ok?: number | null;
  failed?: number | null;
  requested?: number | null;
  trades?: number | null;
  blocked_reason?: string | null;
  updated_at?: string | null;
}

export interface MatrixGridStrategySummary {
  strategy: string;
  status: string;
  ok: number;
  failed: number;
  skipped_existing: number;
  requested: number;
  trades: number;
  count: number;
  total_trades: number;
  total_pnl?: number | null;
  avg_sharpe?: number | null;
  best_sharpe?: number | null;
  best_sharpe_combo?: { symbol: string; timeframe: string } | null;
  avg_cagr?: number | null;
  best_cagr?: number | null;
  best_cagr_combo?: { symbol: string; timeframe: string } | null;
  avg_psr?: number | null;
  best_psr?: number | null;
  best_psr_combo?: { symbol: string; timeframe: string } | null;
  avg_max_drawdown_pct?: number | null;
  worst_max_drawdown_pct?: number | null;
  worst_drawdown_combo?: { symbol: string; timeframe: string } | null;
  avg_net_profit_pct?: number | null;
  best_net_profit_pct?: number | null;
  best_profit_combo?: { symbol: string; timeframe: string } | null;
  completed_at?: string | null;
  updated_at?: string | null;
  matrix_csv?: string | null;
}

export interface MatrixGridOverview {
  generated_at: string;
  source: string;
  api_source?: string;
  selected_strategy?: string | null;
  report_started_at?: string | null;
  report_updated_at?: string | null;
  counts: {
    strategies_done: number;
    strategies_blocked: number;
    rows: number;
    ok_rows: number;
    failed_rows: number;
    symbols: string[];
  };
  strategy_summaries: MatrixGridStrategySummary[];
  blocked_strategies: MatrixGridBlockedStrategy[];
  rows: MatrixGridRow[];
}

export interface BacktestsBulkDeleteResponse {
  deleted: number;
}

export interface BacktestsRemoveEmptyResponse {
  removed: number;
}

export interface PortfoliosBulkDeleteResponse {
  deleted: number;
}

export interface BacktestDuplicateGroup {
  fingerprint: string;
  strategy_name: string;
  symbols: string[];
  timeframe: string;
  start_date: string;
  end_date: string;
  engine: string;
  backtests: Array<{
    id: string;
    created_at: string;
  }>;
}

export interface BacktestsDuplicatesResponse {
  groups: BacktestDuplicateGroup[];
  total_groups: number;
  total_backtests: number;
}

export interface BacktestsDeduplicateResponse {
  deleted_ids: string[];
  kept_ids: string[];
  groups_affected: number;
}

export interface PortfolioDuplicateGroup {
  fingerprint: string;
  name: string;
  account_capital: number;
  component_count: number;
  portfolios: Array<{
    id: string;
    name: string;
    created_at: string;
  }>;
}

export interface PortfoliosDuplicatesResponse {
  groups: PortfolioDuplicateGroup[];
  total_groups: number;
  total_portfolios: number;
}

export interface PortfoliosDeduplicateResponse {
  deleted_ids: string[];
  kept_ids: string[];
  groups_affected: number;
}

export interface PortfoliosRemoveEmptyResponse {
  removed: number;
  remaining: number;
  removed_ids: string[];
}

export interface TradeDetail extends Trade {
  ohlcv: OHLCVBar[];
}

export interface ChartMarkerDrawing {
  type: "marker";
  time: number;
  position?: string;
  shape?: string;
  color?: string;
  text?: string;
  symbol?: string;
}

export interface ChartLineDrawing {
  type: "line";
  start_time: number;
  end_time: number;
  start_value?: number;
  end_value?: number;
  val1?: number;
  val2?: number;
  color?: string;
  symbol?: string;
  title?: string;
  lineWidth?: number;
  lineStyle?: number | "solid" | "dashed" | "dotted";
}

export interface ChartLineWithBreaksDrawing {
  type: "line_with_breaks";
  data: Array<{
    time: number;
    value: number | null;
    color?: string;
  }>;
  color?: string;
  symbol?: string;
  title?: string;
  lineWidth?: number;
  lineStyle?: number | "solid" | "dashed" | "dotted";
  start_time?: number;
  end_time?: number;
}

export interface ChartBoxDrawing {
  type: "box";
  start_time: number;
  end_time: number;
  low: number;
  high: number;
  color?: string;
  border_color?: string;
  symbol?: string;
}

export type TradeDrawing =
  | ChartMarkerDrawing
  | ChartLineDrawing
  | ChartLineWithBreaksDrawing
  | ChartBoxDrawing;

export interface TradeChartResponse {
  trade: Trade;
  ohlcv: OHLCVBar[];
  symbol: string;
  timeframe: string;
  indicators: IndicatorSeriesView[];
  drawings: TradeDrawing[];
  total_trades: number;
}

export interface BacktestReplayResponse {
  trades: Trade[];
  ohlcv_by_symbol: Record<string, OHLCVBar[]>;
  timeframe: string;
  symbols: string[];
  primary_symbol: string;
  indicators: IndicatorSeriesView[];
  drawings: TradeDrawing[];
  missing_ohlcv: Record<string, MissingOHLCVDetail>;
  total_trades: number;
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

export interface PortfolioComponent {
  component_id: string;
  strategy_name: string;
  symbol: string;
  timeframe: string;
  weight: number;
  risk_config: Record<string, unknown>;
  settings: Record<string, unknown>;
  backtest_id?: string | null;
  return_contribution?: number;
  return_contribution_pct?: number;
  drawdown_contribution?: number;
  drawdown_contribution_pct?: number;
  trade_count?: number;
  win_rate?: number;
  wins?: number;
}

export interface PortfolioSummary {
  id: string;
  name: string;
  account_capital: number;
  strategy_count: number;
  sharpe: number;
  sharpe_ratio?: number;
  total_return?: number;
  cagr: number;
  max_drawdown?: number;
  max_drawdown_pct?: number;
  win_rate?: number;
  trade_count?: number;
  net_profit: number;
  net_profit_pct?: number;
  psr?: number;
  p_value?: number;
  equity_mini?: number[];
  created_at: string;
}

export type PortfoliosSortField =
  | "created_at"
  | "total_return"
  | "sharpe_ratio"
  | "psr"
  | "p_value"
  | "cagr"
  | "max_drawdown"
  | "win_rate"
  | "trade_count";

export interface PortfoliosQueryParams {
  name?: string;
  sort?: PortfoliosSortField;
  order?: "asc" | "desc";
}

export interface PortfolioDetail {
  id: string;
  name: string;
  created_at: string;
  account_capital: number;
  components: PortfolioComponent[];
  strategies: PortfolioStrategy[];
  combined_stats: {
    sharpe: number;
    sortino?: number;
    cagr: number;
    total_return?: number;
    max_drawdown_duration?: number;
    win_rate?: number;
    profit_factor?: number;
    calmar?: number;
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
  component_contributions: PortfolioContribution[];
  trades: PortfolioTrade[];
}

export interface PortfolioStrategy {
  backtest_id: string;
  strategy_name: string;
  risk_per_trade_pct: number;
  max_positions: number;
}

export interface PortfolioTrade {
  id?: string;
  nr?: number;
  component_id?: string;
  time: string;
  date?: string;
  entry_time?: number;
  entry_time_iso?: string;
  exit_time?: number;
  exit_time_iso?: string;
  entry_price?: number;
  exit_price?: number;
  quantity?: number;
  position_size?: number;
  size?: number;
  pnl_pct?: number;
  exit_reason?: string;
  duration?: string;
  timeframe?: string;
  strategy_name?: string;
  strategy_label?: string;
  backtest_id?: string;
  pnl: number;
  strategy: string;
  symbol: string;
  direction: string;
}

export interface PortfolioContribution {
  group: "component" | "strategy" | "symbol" | "timeframe";
  group_value: string;
  component_id?: string;
  component_ids: string[];
  strategy_name: string;
  symbol: string;
  timeframe: string;
  weight: number;
  return_contribution: number;
  return_contribution_pct: number;
  drawdown_contribution: number;
  drawdown_contribution_pct: number;
  trade_count: number;
  win_rate: number;
  wins: number;
  sharpe_ratio?: number;
  psr?: number;
  p_value?: number;
  max_drawdown?: number;
  cagr?: number;
  pnl_total: number;
  pnl_contribution: number;
  pnl_contribution_pct: number;
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
  coverage: Record<
    string,
    Record<
      string,
      {
        available: boolean;
        start: string | null;
      }
    >
  >;
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
  // New preferred field describing which side(s) to download from the provider
  offer_side?: "bid" | "ask" | "both";
  // Legacy compatibility: keep price_type but allow 'both' for consistency
  price_type?: "bid" | "ask" | "both";
  merge_tick?: boolean;
  merge_s1?: boolean;
  batch_size?: number;
  pause_ms?: number;
}

export interface DataResampleRequest {
  source_path: string;
  target_timeframes: string;
}

export interface DataHarvestRequest {
  source: string;
  symbol: string;
  asset_class?: string;
  start?: string;
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
  category?: "backtest" | "optimization" | "download" | "resample";
  source?: string;
  status: string;
  created_at?: string;
  updated_at?: string;
  started_at?: string;
  finished_at?: string;
  completed_at?: string;
  progress_percent?: number | null;
  progress_message?: string;
  elapsed_seconds?: number | null;
  eta_seconds?: number | null;
  duration_seconds?: number | null;
  error?: string | null;
  error_message?: string | null;
  provider?: string | null;
  model?: string | null;
  log?: string;
  progress?: DataTaskProgress;
  can_pause?: boolean;
  can_resume?: boolean;
  can_cancel?: boolean;
  can_restart?: boolean;
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

export interface DataCacheInvalidationResponse {
  status: string;
  removed_entries: number;
  reason: string;
  include_validation: boolean | null;
  cache_stats: Record<string, unknown>;
}

export type AgentModelId =
  | "moonshotai/kimi-k2.5"
  | "meta/llama-3.3-70b-instruct"
  | "deepseek-ai/deepseek-v3.2"
  | "qwen/qwen2.5-coder-32b-instruct";

export type AgentProviderId = "nim" | "copilot_sdk";

export interface AgentConvertRequest {
  code: string;
  strategy_name?: string;
  provider?: AgentProviderId;
  model?: string;
}

export interface BacktestCompareStats {
  total_return: number;
  net_profit: number;
  sharpe_ratio: number;
  psr: number;
  p_value: number;
  cagr: number;
  max_drawdown: number;
  win_rate: number;
  trade_count: number;
}

export interface BacktestCompareSide {
  id: string;
  name: string;
  strategy_name: string;
  engine: string;
  symbols: string[];
  timeframe: string;
  stats: BacktestCompareStats;
  equity_curve: EquityPoint[];
  trade_distribution: {
    total_trades: number;
    winning_trades: number;
    losing_trades: number;
  };
}

export interface BacktestsCompareResponse {
  ids: string[];
  backtest1: BacktestCompareSide;
  backtest2: BacktestCompareSide;
  comparison: {
    delta_return: number;
    delta_net_profit: number;
    delta_sharpe: number;
    delta_psr: number;
    delta_p_value: number;
    delta_cagr: number;
    delta_max_drawdown: number;
    delta_win_rate: number;
    delta_trade_count: number;
  };
}
