# Graph Report - trading-platform  (2026-04-22)

## Corpus Check
- 232 files · ~701,343 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2988 nodes · 9372 edges · 63 communities detected
- Extraction: 50% EXTRACTED · 50% INFERRED · 0% AMBIGUOUS · INFERRED: 4670 edges (avg confidence: 0.61)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 95|Community 95]]
- [[_COMMUNITY_Community 96|Community 96]]
- [[_COMMUNITY_Community 97|Community 97]]
- [[_COMMUNITY_Community 102|Community 102]]
- [[_COMMUNITY_Community 103|Community 103]]

## God Nodes (most connected - your core abstractions)
1. `BacktestResult` - 281 edges
2. `EquityPoint` - 233 edges
3. `Trade` - 225 edges
4. `DukascopyNativeDownloader` - 165 edges
5. `StrategyEntry` - 158 edges
6. `JForexDownloader` - 126 edges
7. `OptimizationConfig` - 122 edges
8. `DownloadCancelled` - 122 edges
9. `PortfolioStats` - 120 edges
10. `BacktestConfig` - 100 edges

## Surprising Connections (you probably didn't know these)
- `_ThreadBoundTaskLogHandler` --calls--> `test_thread_bound_task_log_handler_emits_without_shadowing_handle()`  [INFERRED]
  viewer/core.py → tests/test_viewer_features.py
- `_download_missing_ohlcv_data()` --calls--> `test_download_missing_ohlcv_data_falls_back_to_yfinance_when_alpaca_credentials_missing()`  [INFERRED]
  viewer/main.py → tests/test_viewer_features.py
- `Wait until the next candle close for the given timeframe.` --uses--> `TradeSignal`  [INFERRED]
  live_runner/signal_sources/execution_timing.py → risk/models.py
- `Configuration passed to any engine adapter.` --uses--> `BacktestResult`  [INFERRED]
  runner/engines/base.py → shared/models.py
- `Merge STRATEGY_META.parameters defaults with explicit overrides.` --uses--> `BacktestResult`  [INFERRED]
  runner/engines/base.py → shared/models.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.01
Nodes (394): _align_series(), compare_backtests(), compute_combined_equity(), compute_combined_stats(), compute_drawdown_pct(), compute_returns(), pearson_correlation(), Strategy comparison logic — ported from custom_lean_integration/compare_strategi (+386 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (228): Short engine identifier, e.g. 'vectorbt', 'lean'., Execute a backtest and return the result in universal format.          Args:, compare_scenario(), equity_to_returns_series(), fmt(), make_dummy_trade(), make_equity_curve_objs(), print_results() (+220 more)

### Community 2 - "Community 2"
Cohesion: 0.01
Nodes (164): handleDelete(), handleRename(), normalizeEquityPoints(), handleStart(), boundsToFilters(), buildNumericBounds(), buildPageNumbers(), cloneFilters() (+156 more)

### Community 3 - "Community 3"
Cohesion: 0.02
Nodes (169): _assemble_tick_dataframe(), main(), _normalize_supported_timeframe(), _parse_csv_list(), Alpaca historical stock bars downloader.  Canonical output path:   {output_dir}/, _to_utc_timestamp(), Downloader, DownloadResult (+161 more)

### Community 4 - "Community 4"
Cohesion: 0.02
Nodes (127): BacktestConfig, EngineAdapter, Base classes for data downloaders.  All downloaders implement the Downloader ABC, Optional validation. Returns list of error messages (empty = valid).         Def, Configuration passed to any engine adapter., Abstract base class for backtest engine adapters., cmd_convert(), cmd_engines() (+119 more)

### Community 5 - "Community 5"
Cohesion: 0.07
Nodes (124): AlpacaLiveAdapter, _parse_bool_env(), Live adapter for Alpaca trading + trade update streaming., Initialize REST and websocket clients, and verify account access., Return open positions with pricing and unrealized PnL., Resolve Alpaca credentials from env (default or ACC{N} blocks)., resolve_alpaca_credentials(), _to_datetime() (+116 more)

### Community 6 - "Community 6"
Cohesion: 0.03
Nodes (147): agent_convert(), _available_log_files(), _backtest_info_payload(), cancel_data_task(), _coerce_finite_float(), _collect_dukascopy_genesis(), _collect_log_entries(), compare_portfolios() (+139 more)

### Community 7 - "Community 7"
Cohesion: 0.03
Nodes (81): Initialize source connections/resources., Close source connections/resources., Yield parsed trade signals from this source., BaseLiveAdapter, _coerce_bool_series(), Order, OrderType, Position (+73 more)

### Community 8 - "Community 8"
Cohesion: 0.03
Nodes (87): ABC, BaseSignalSource, BaseSignalSource, _accounts_env_mtime(), _adapter_is_ready(), _apply_signal_processor(), _block_ctrader_account(), _block_discord_service() (+79 more)

### Community 9 - "Community 9"
Cohesion: 0.05
Nodes (73): index(), _apply_risk_sizing(), _build_drawings(), _build_trade_context(), _coerce_series(), _compile_helper_namespace(), _enrich_trades_with_context(), _infer_asset_class() (+65 more)

### Community 10 - "Community 10"
Cohesion: 0.05
Nodes (58): build_catalog(), _canonical_group_path(), _catalog_timestamp_to_epoch(), _coerce_nonnegative_int(), _collect_text_values(), _detect_storage(), _extract_symbol_timeframe_and_segment(), _extract_timeframe_and_segment() (+50 more)

### Community 11 - "Community 11"
Cohesion: 0.03
Nodes (23): _allow_small_backtest_boundary_gap(), test_agent_convert_endpoint_rejects_copilot_cli_provider(), test_agent_convert_endpoint_rejects_unknown_provider(), test_backtest_boundary_gap_tolerance_allows_small_leading_gap(), test_backtest_boundary_gap_tolerance_rejects_large_gap(), test_backtest_run_from_signals_csv_date_only_end_is_inclusive(), test_backtest_run_from_signals_csv_forwards_to_replay(), test_backtest_run_registers_task_manager_progress() (+15 more)

### Community 12 - "Community 12"
Cohesion: 0.07
Nodes (34): _iter_json_backtests(), _load_json_payload(), main(), _parse_result_payload(), _recalculate_json_backtests(), _save_json_payload(), _annualize_total_return(), _attach_inferred_freq() (+26 more)

### Community 13 - "Community 13"
Cohesion: 0.05
Nodes (24): _as_float(), _as_int(), BacktestExpectation, BacktestResultValidator, _parse_time(), AICodeValidator, ValidationResult, test_ai_generated_strategy_average_score() (+16 more)

### Community 14 - "Community 14"
Cohesion: 0.08
Nodes (42): Merge STRATEGY_META.parameters defaults with explicit overrides., resolve_strategy_parameters(), _strategy_entries(), _safe_strategy_entries(), _is_finer(), _load_intrabar_data(), _load_ohlcv(), _load_strategy_module() (+34 more)

### Community 15 - "Community 15"
Cohesion: 0.08
Nodes (45): _assemble_strategy_module(), BacktestTarget, _build_backtest_command(), _build_strategy_artifacts(), _entry_rank(), _extract_engine(), _extract_markdown_response(), _extract_markdown_title() (+37 more)

### Community 16 - "Community 16"
Cohesion: 0.08
Nodes (30): _apply_metadata(), CopilotCLIProvider, _extract_content(), _apply_metadata(), _compose_prompt(), CopilotSDKProvider, _extract_content(), _extract_model_ids() (+22 more)

### Community 17 - "Community 17"
Cohesion: 0.11
Nodes (20): CTraderApiError, CTraderAuthTerminalError, CTraderLiveAdapter, _ensure_asyncio_twisted_reactor(), _is_already_logged_in_error(), _is_terminal_auth_error(), _normalize_error_code(), _parse_bool_env() (+12 more)

### Community 18 - "Community 18"
Cohesion: 0.1
Nodes (40): AdaptiveConfig, _batch_all_successful(), _build_parser(), _configure_logger(), _download_month(), _ensure_job_state(), _fetch_url(), FetchOutcome (+32 more)

### Community 19 - "Community 19"
Cohesion: 0.09
Nodes (40): _build_overview_metadata(), _cache_key(), _collect_sources(), get_cache_stats(), get_catalog(), get_catalog_ohlcv_entries(), get_overview_metadata(), invalidate_after_download() (+32 more)

### Community 20 - "Community 20"
Cohesion: 0.11
Nodes (33): _alpaca_paper_from_endpoint(), _ctrader_account_name(), _ctrader_extract_account_label(), _ctrader_extract_broker_name(), _ctrader_extract_login(), _ctrader_host_matches_environment(), _ctrader_message_name_sources(), discover_alpaca_accounts() (+25 more)

### Community 21 - "Community 21"
Cohesion: 0.09
Nodes (20): main(), Resample Tool — Generate higher timeframes from lower ones.  Given M1 (or any so, Resample all parquet files in a directory tree.      Finds all {from_tf}.parquet, Rebuild the catalog after resampling., Resample OHLCV DataFrame to a higher timeframe.      Args:         df: DataFrame, Resample a single parquet file to multiple timeframes.      Args:         source, resample_directory(), resample_file() (+12 more)

### Community 22 - "Community 22"
Cohesion: 0.18
Nodes (18): _convert_ohlcv(), _looks_like_timestamp(), main(), migrate_dukascopy_xauusd(), migrate_lean_equities_daily(), migrate_lean_equities_h1(), migrate_lean_equities_m15(), migrate_stockkit_5m() (+10 more)

### Community 23 - "Community 23"
Cohesion: 0.18
Nodes (15): _layer_pytest_args(), LayerResult, main(), _print_summary(), _run_layer(), _make_fake_probe(), Tests for data_tools.dukascopy_discovery., Return a probe_single_day drop-in that answers from a local genesis dict. (+7 more)

### Community 24 - "Community 24"
Cohesion: 0.16
Nodes (6): buildRiskMathNarrative(), formatPrice(), formatUsd(), sanitizeIndicatorPoints(), sanitizeOHLCVBars(), toFiniteNumber()

### Community 25 - "Community 25"
Cohesion: 0.23
Nodes (11): export_data(), _export_table_csv(), _list_tables(), main(), List tables in a dolt repo using 'dolt sql -r csv'., Export a dolt table to CSV using 'dolt table export'., Return the canonical OHLCV output path and migrate legacy flat exports., run_cmd() (+3 more)

### Community 26 - "Community 26"
Cohesion: 0.27
Nodes (11): _detect_separator(), _find_column(), _is_intraday_timestamp(), load_signals_from_csv(), _normalize_header(), _parse_action(), _parse_decimal(), _parse_timestamp() (+3 more)

### Community 27 - "Community 27"
Cohesion: 0.26
Nodes (6): loadLogs(), normalizeLevel(), parseLogLine(), parsePlainLine(), parseTimestamp(), tryParseJson()

### Community 28 - "Community 28"
Cohesion: 0.2
Nodes (3): formatMarkerTime(), normalizeUnixTime(), toChartTime()

### Community 29 - "Community 29"
Cohesion: 0.17
Nodes (2): useStrategies(), StrategiesProvider()

### Community 30 - "Community 30"
Cohesion: 0.23
Nodes (7): get_logger(), JsonFormatter, Centralized logging module for trading-platform services.  Provides structured J, Formats log records as structured JSON., Create or return a logger for the given service.      Each service gets:     - A, test_json_formatter_includes_required_fields_and_defaults(), test_json_formatter_uses_required_timestamp_format()

### Community 31 - "Community 31"
Cohesion: 0.25
Nodes (9): align_return_series(), calculate_capm(), _normalize_return_series(), CAPM helper functions for strategy-vs-benchmark regression analysis., Align strategy and benchmark return series on common timestamps., Compute CAPM regression metrics from aligned return series., test_align_return_series_uses_intersection(), test_calculate_capm_linear_relationship() (+1 more)

### Community 32 - "Community 32"
Cohesion: 0.29
Nodes (6): useLiveAccountsStream(), useLiveExecutionsStream(), useLivePositionsStream(), useLiveSignalsStream(), useLiveStream(), useWebSocket()

### Community 33 - "Community 33"
Cohesion: 0.31
Nodes (8): _calc_atr(), _calc_supertrend(), _coerce_bool(), generate_signals(), Project-usable core port of the Pine Script strategy "Custom Swings + Supertrend, Generate Long/Short signals for the Custom Swings + Supertrend strategy.      Pa, True Range → Wilder's ATR (equivalent to ta.atr in Pine)., Supertrend — mirrors Pine Script logic exactly.      Returns:         trend  : +

### Community 34 - "Community 34"
Cohesion: 0.54
Nodes (7): _candidate_ohlcv_paths(), _covers_requested_period(), _has_ohlcv_data(), _parse_date_bound(), _path_time_coverage(), skip_when_required_data_or_dependency_missing(), _to_utc_datetime()

### Community 35 - "Community 35"
Cohesion: 0.38
Nodes (3): clamp(), updateMax(), updateMin()

### Community 36 - "Community 36"
Cohesion: 0.6
Nodes (5): _canonical_target(), consolidate(), main(), _normalize_frame(), _normalize_timeframe()

### Community 37 - "Community 37"
Cohesion: 0.6
Nodes (4): _calc_dema(), _calc_supertrend(), generate_signals(), Strategy: Supertrend + DEMA (Pine Script Conversion)  Combines Supertrend indica

### Community 38 - "Community 38"
Cohesion: 0.6
Nodes (4): _calc_supertrend_osc(), _calc_wavetrend(), generate_signals(), Strategy: SuperOSC + VuManChu WaveTrend (Pine Script Conversion)  Combines a Sup

### Community 39 - "Community 39"
Cohesion: 0.6
Nodes (4): _calc_rsi(), generate_signals(), _load_vix_data(), Strategy: Post VIX Dip Buy  Buys SPY on panic dips: when VIX spikes significantl

### Community 40 - "Community 40"
Cohesion: 0.6
Nodes (3): formatPValue(), pValueColor(), pValueMetric()

### Community 41 - "Community 41"
Cohesion: 0.6
Nodes (3): clamp(), updateMax(), updateMin()

### Community 42 - "Community 42"
Cohesion: 0.5
Nodes (3): parse_live_deployments_config(), test_parse_live_deployments_warns_for_missing_account(), test_parse_live_deployments_yaml_valid()

### Community 43 - "Community 43"
Cohesion: 0.7
Nodes (4): _is_missing_browser_error(), _playwright_browser_launchable(), pytest_runtest_setup(), _requested_browser_name()

### Community 46 - "Community 46"
Cohesion: 0.67
Nodes (1): @pyne Opening Range Breakout Strategy (PyneCore). Converted from NinjaTrader 8 (

### Community 47 - "Community 47"
Cohesion: 0.67
Nodes (1): Strategy: Trend Following (Multi-Timeframe Momentum)  CTA-style trend following

### Community 48 - "Community 48"
Cohesion: 0.67
Nodes (2): generate_signals(), Generate signals for EMA Crossover strategy.     Entry: EMA(9) crosses above EMA

### Community 49 - "Community 49"
Cohesion: 0.67
Nodes (1): Strategy: Cross-Asset Momentum (Time-Series)  Time-series momentum applied acros

### Community 50 - "Community 50"
Cohesion: 0.67
Nodes (1): Strategy: Risk Parity (Simplified)  Equal-risk allocation across multiple asset

### Community 51 - "Community 51"
Cohesion: 0.67
Nodes (1): Strategy: Sector Rotation (Relative Momentum)  Rotates allocation among US secto

### Community 52 - "Community 52"
Cohesion: 0.67
Nodes (1): Statistical Arbitrage Pairs Trading Strategy  Simplified pairs trading: Z-score

### Community 53 - "Community 53"
Cohesion: 0.67
Nodes (1): Strategy: Intraday Volatility Breakout  Simple breakout strategy: if price moves

### Community 54 - "Community 54"
Cohesion: 0.67
Nodes (1): @pyne SMA Crossover — PyneCore-Format Strategie. Einfache Trend-Following Strate

### Community 55 - "Community 55"
Cohesion: 0.67
Nodes (1): Intraday Channel Breakout Strategy  N-Bar High/Low channel breakout with 200 EMA

### Community 56 - "Community 56"
Cohesion: 0.67
Nodes (2): generate_signals(), Generate signals for EMA Crossover strategy.     Entry: Fast EMA crosses above S

### Community 57 - "Community 57"
Cohesion: 0.67
Nodes (1): Strategy: Short-Term Reversal  Exploits weekly mean reversion: stocks that dropp

### Community 58 - "Community 58"
Cohesion: 0.67
Nodes (1): Strategy: Mean Reversion (RSI + Bollinger Bands)  Mean reversion strategy using

### Community 59 - "Community 59"
Cohesion: 0.67
Nodes (1): Strategy: Gold Momentum (Multi-Timeframe)  Multi-timeframe momentum strategy for

### Community 95 - "Community 95"
Cohesion: 1.0
Nodes (1): Short identifier, e.g. 'histdata', 'dolthub', 'huggingface'.

### Community 96 - "Community 96"
Cohesion: 1.0
Nodes (1): List datasets available for download.          Returns list of dicts with at lea

### Community 97 - "Community 97"
Cohesion: 1.0
Nodes (1): Download OHLCV data for one symbol/timeframe and save as Parquet.          Args:

### Community 102 - "Community 102"
Cohesion: 1.0
Nodes (1): Time of the final exit.

### Community 103 - "Community 103"
Cohesion: 1.0
Nodes (1): Generate filename: {strategy}_{engine}_{YYYYMMDD-HHmmss}.json

## Knowledge Gaps
- **145 isolated node(s):** `Strategy comparison logic — ported from custom_lean_integration/compare_strategi`, `Period-over-period returns from an equity series.`, `Drawdown percentage series from equity values.`, `Pearson correlation coefficient.`, `Align two [time, value] series to common timestamps.` (+140 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 29`** (12 nodes): `queries.ts`, `StrategiesProvider.tsx`, `useLivePositions()`, `useLiveQuotes()`, `useLiveSignalLog()`, `useServiceLogs()`, `useServices()`, `useStrategies()`, `useStrategyDetail()`, `useSymbols()`, `useUpdateServiceStatus()`, `StrategiesProvider()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (3 nodes): `main()`, `@pyne Opening Range Breakout Strategy (PyneCore). Converted from NinjaTrader 8 (`, `pynecore_orb.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (3 nodes): `trend_following.py`, `generate_signals()`, `Strategy: Trend Following (Multi-Timeframe Momentum)  CTA-style trend following`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (3 nodes): `generate_signals()`, `Generate signals for EMA Crossover strategy.     Entry: EMA(9) crosses above EMA`, `ema_crossoiver_conversion_test.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (3 nodes): `generate_signals()`, `Strategy: Cross-Asset Momentum (Time-Series)  Time-series momentum applied acros`, `cross_asset_momentum.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (3 nodes): `generate_signals()`, `Strategy: Risk Parity (Simplified)  Equal-risk allocation across multiple asset`, `risk_parity.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (3 nodes): `generate_signals()`, `Strategy: Sector Rotation (Relative Momentum)  Rotates allocation among US secto`, `sector_rotation.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (3 nodes): `generate_signals()`, `Statistical Arbitrage Pairs Trading Strategy  Simplified pairs trading: Z-score`, `stat_arb_pairs.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 53`** (3 nodes): `volatility_breakout.py`, `generate_signals()`, `Strategy: Intraday Volatility Breakout  Simple breakout strategy: if price moves`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (3 nodes): `main()`, `@pyne SMA Crossover — PyneCore-Format Strategie. Einfache Trend-Following Strate`, `pynecore_sma_crossover.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (3 nodes): `generate_signals()`, `Intraday Channel Breakout Strategy  N-Bar High/Low channel breakout with 200 EMA`, `channel_breakout.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (3 nodes): `generate_signals()`, `Generate signals for EMA Crossover strategy.     Entry: Fast EMA crosses above S`, `lab_generated_strategy.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (3 nodes): `generate_signals()`, `Strategy: Short-Term Reversal  Exploits weekly mean reversion: stocks that dropp`, `short_term_reversal.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (3 nodes): `generate_signals()`, `Strategy: Mean Reversion (RSI + Bollinger Bands)  Mean reversion strategy using`, `mean_reversion_rsi_bb.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 59`** (3 nodes): `generate_signals()`, `Strategy: Gold Momentum (Multi-Timeframe)  Multi-timeframe momentum strategy for`, `gold_momentum.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 95`** (1 nodes): `Short identifier, e.g. 'histdata', 'dolthub', 'huggingface'.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 96`** (1 nodes): `List datasets available for download.          Returns list of dicts with at lea`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 97`** (1 nodes): `Download OHLCV data for one symbol/timeframe and save as Parquet.          Args:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 102`** (1 nodes): `Time of the final exit.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 103`** (1 nodes): `Generate filename: {strategy}_{engine}_{YYYYMMDD-HHmmss}.json`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `sort()` connect `Community 0` to `Community 1`, `Community 2`, `Community 6`, `Community 7`, `Community 9`, `Community 14`, `Community 24`, `Community 26`?**
  _High betweenness centrality (0.100) - this node is a cross-community bridge._
- **Why does `BacktestResult` connect `Community 1` to `Community 0`, `Community 4`, `Community 5`, `Community 7`, `Community 9`, `Community 12`, `Community 14`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Why does `load()` connect `Community 2` to `Community 0`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Are the 559 inferred relationships involving `str` (e.g. with `consolidate()` and `_run()`) actually correct?**
  _`str` has 559 INFERRED edges - model-reasoned connections that need verification._
- **Are the 276 inferred relationships involving `BacktestResult` (e.g. with `_RequestPlaceholder` and `_NoopRouteApp`) actually correct?**
  _`BacktestResult` has 276 INFERRED edges - model-reasoned connections that need verification._
- **Are the 231 inferred relationships involving `EquityPoint` (e.g. with `_RequestPlaceholder` and `_NoopRouteApp`) actually correct?**
  _`EquityPoint` has 231 INFERRED edges - model-reasoned connections that need verification._
- **Are the 222 inferred relationships involving `Trade` (e.g. with `_RequestPlaceholder` and `_NoopRouteApp`) actually correct?**
  _`Trade` has 222 INFERRED edges - model-reasoned connections that need verification._