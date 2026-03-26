import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronDown, ChevronUp, GitCompare, Plus, RefreshCw, Trash2, X } from "lucide-react";
import {
  deduplicateBacktests,
  deleteBacktestsBulk,
  getBacktests,
  getBacktestDuplicates,
  getStrategies,
  getSymbols,
} from "../api/client";
import type {
  BacktestListItem,
  BacktestsQueryParams,
  BacktestsSortField,
} from "../api/types";
import { useToast } from "../components/ToastProvider";
import { CreateBacktestModal } from "../components/CreateBacktestModal";
import { RangeFilterControl, type RangeValue } from "../components/RangeFilterControl";
import { MultiSelectDropdown } from "../components/MultiSelectDropdown";

type SortOrder = "asc" | "desc" | null;

const DEFAULT_TIMEFRAMES = ["tick", "1s", "1m", "5m", "15m", "30m", "1h", "4h", "1d"];

const NUMERIC_FONT = '"SFMono-Regular", "Cascadia Code", "Roboto Mono", ui-monospace, monospace';

const DEFAULT_RANGES = {
  sharpe: { min: -2, max: 5 },
  psr: { min: 0, max: 1 },
  pValue: { min: 0, max: 1 },
  cagr: { min: -100, max: 300 },
  maxDrawdown: { min: 0, max: 100 },
};

type RangeKey = keyof typeof DEFAULT_RANGES;

function formatPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function formatNum(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

function parseIsoToDate(value: string | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

function sortParamFor(column: BacktestsSortField, order: SortOrder): BacktestsQueryParams {
  if (!order) {
    return {};
  }
  return { sort: column, order };
}

function normalizeSharpe(backtest: BacktestListItem): number | null {
  const value = backtest.sharpe_ratio ?? backtest.sharpe;
  return value == null || !Number.isFinite(value) ? null : Number(value);
}

function normalizePsr(backtest: BacktestListItem): number | null {
  const value = backtest.psr;
  return value == null || !Number.isFinite(value) ? null : Number(value);
}

function normalizePValue(backtest: BacktestListItem): number | null {
  const value = backtest.p_value;
  return value == null || !Number.isFinite(value) ? null : Number(value);
}

function normalizeCagr(backtest: BacktestListItem): number | null {
  const value = backtest.cagr;
  return value == null || !Number.isFinite(value) ? null : Number(value);
}

function normalizeMaxDrawdown(backtest: BacktestListItem): number | null {
  const value = backtest.max_drawdown_pct;
  return value == null || !Number.isFinite(value) ? null : Math.abs(Number(value));
}

function percentageColor(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "var(--color-text-muted)";
  return value >= 0 ? "#3fb950" : "#f85149";
}

function sharpeColor(value: number | null): string {
  if (value == null) return "var(--color-text-muted)";
  if (value >= 1) return "#3fb950";
  if (value >= 0) return "#d4b263";
  return "#f85149";
}

function psrColor(value: number | null): string {
  if (value == null) return "var(--color-text-muted)";
  if (value >= 0.95) return "#3fb950";
  if (value >= 0.5) return "#d4b263";
  return "#f85149";
}

function pValueColor(value: number | null): string {
  if (value == null) return "var(--color-text-muted)";
  return value <= 0.05 ? "#3fb950" : "#f85149";
}

function toErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  if (typeof error === "string" && error.trim()) {
    return error;
  }
  return fallback;
}

interface ScreenerFilters {
  strategies: string[];
  symbols: string[];
  symbolSearch: string;
  timeframes: string[];
  sharpe: RangeValue;
  psr: RangeValue;
  pValue: RangeValue;
  cagr: RangeValue;
  maxDrawdown: RangeValue;
}

function cloneInitialFilters(): ScreenerFilters {
  return {
    strategies: [],
    symbols: [],
    symbolSearch: "",
    timeframes: [],
    sharpe: { ...DEFAULT_RANGES.sharpe },
    psr: { ...DEFAULT_RANGES.psr },
    pValue: { ...DEFAULT_RANGES.pValue },
    cagr: { ...DEFAULT_RANGES.cagr },
    maxDrawdown: { ...DEFAULT_RANGES.maxDrawdown },
  };
}

function isRangeActive<K extends RangeKey>(key: K, range: RangeValue): boolean {
  const baseline = DEFAULT_RANGES[key];
  return range.min !== baseline.min || range.max !== baseline.max;
}

export function BacktestsPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [backtests, setBacktests] = useState<BacktestListItem[]>([]);
  const [totalBacktests, setTotalBacktests] = useState(0);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(25);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deduplicating, setDeduplicating] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [allStrategies, setAllStrategies] = useState<string[]>([]);
  const [allSymbols, setAllSymbols] = useState<string[]>([]);
  const [filters, setFilters] = useState<ScreenerFilters>(() => cloneInitialFilters());
  const [sortColumn, setSortColumn] = useState<BacktestsSortField>("created_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const [debouncedFilters, setDebouncedFilters] = useState<ScreenerFilters>(() => cloneInitialFilters());

  useEffect(() => {
    const handle = window.setTimeout(() => {
      setDebouncedFilters(filters);
      setPage(1);
    }, 300);
    return () => window.clearTimeout(handle);
  }, [filters]);

  useEffect(() => {
    let active = true;
    Promise.allSettled([getStrategies(), getSymbols()]).then((results) => {
      if (!active) return;

      const strategiesResult = results[0];
      if (strategiesResult.status === "fulfilled") {
        const names = strategiesResult.value
          .flatMap((entry) => [entry.name, entry.slug])
          .filter((value): value is string => Boolean(value?.trim()));
        setAllStrategies([...new Set(names)].sort((a, b) => a.localeCompare(b)));
      }

      const symbolsResult = results[1];
      if (symbolsResult.status === "fulfilled") {
        const symbols = [
          ...(symbolsResult.value.symbols ?? []),
          ...Object.values(symbolsResult.value.categories ?? {}).flatMap((entries) => entries),
        ].filter((value): value is string => Boolean(value?.trim()));
        setAllSymbols([...new Set(symbols)].sort((a, b) => a.localeCompare(b)));
      }
    });

    return () => {
      active = false;
    };
  }, []);

  const buildQueryParams = useMemo<BacktestsQueryParams>(() => {
    const params: BacktestsQueryParams = {
      page,
      per_page: perPage,
      ...sortParamFor(sortColumn, sortOrder),
    };

    if (isRangeActive("sharpe", debouncedFilters.sharpe)) {
      params.sharpe_min = debouncedFilters.sharpe.min;
      params.sharpe_max = debouncedFilters.sharpe.max;
    }
    if (isRangeActive("psr", debouncedFilters.psr)) {
      params.psr_min = debouncedFilters.psr.min;
      params.psr_max = debouncedFilters.psr.max;
    }
    if (isRangeActive("pValue", debouncedFilters.pValue)) {
      params.p_value_min = debouncedFilters.pValue.min;
      params.p_value_max = debouncedFilters.pValue.max;
    }
    if (isRangeActive("cagr", debouncedFilters.cagr)) {
      params.cagr_min = debouncedFilters.cagr.min;
      params.cagr_max = debouncedFilters.cagr.max;
    }
    if (isRangeActive("maxDrawdown", debouncedFilters.maxDrawdown)) {
      params.max_drawdown_min = debouncedFilters.maxDrawdown.min;
      params.max_drawdown_max = debouncedFilters.maxDrawdown.max;
    }
    if (debouncedFilters.strategies.length > 0) {
      params.strategy = debouncedFilters.strategies.join(",");
    }
    if (debouncedFilters.symbols.length > 0) {
      params.symbol = debouncedFilters.symbols.join(",");
    }
    if (debouncedFilters.timeframes.length > 0) {
      params.timeframe = debouncedFilters.timeframes.join(",");
    }
    if (debouncedFilters.symbolSearch.trim()) {
      params.search = debouncedFilters.symbolSearch.trim();
    }
    return params;
  }, [debouncedFilters, page, perPage, sortColumn, sortOrder]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    getBacktests(buildQueryParams)
      .then((payload) => {
        if (!active) return;
        setBacktests(payload.items ?? []);
        setTotalBacktests(payload.total ?? 0);
        setSelected((current) => {
          if (current.size === 0) return current;
          const validIds = new Set((payload.items ?? []).map((item) => item.id));
          const next = new Set<string>();
          current.forEach((id) => {
            if (validIds.has(id)) next.add(id);
          });
          return next;
        });
      })
      .catch((error: Error) => {
        if (!active) return;
        showToast(error.message || "Failed to load backtests.", "error");
      })
      .finally(() => {
        if (!active) return;
        setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [buildQueryParams, showToast]);

  const clientFiltered = useMemo(() => {
    const symbolNeedles = debouncedFilters.symbols.map((symbol) => symbol.toUpperCase());
    const quickSearch = debouncedFilters.symbolSearch.trim().toUpperCase();

    return backtests.filter((item) => {
      if (
        debouncedFilters.strategies.length > 0 &&
        !debouncedFilters.strategies.includes(item.strategy)
      ) {
        return false;
      }

      if (debouncedFilters.timeframes.length > 0) {
        const tf = item.timeframes?.[0] ?? "";
        if (!debouncedFilters.timeframes.includes(tf)) {
          return false;
        }
      }

      if (symbolNeedles.length > 0) {
        const symbols = (item.symbols ?? []).map((symbol) => symbol.toUpperCase());
        if (!symbolNeedles.every((needle) => symbols.includes(needle))) {
          return false;
        }
      }

      if (quickSearch) {
        const haystack = `${item.strategy} ${(item.symbols ?? []).join(" ")} ${item.id}`.toUpperCase();
        if (!haystack.includes(quickSearch)) {
          return false;
        }
      }

      const maxDrawdown = normalizeMaxDrawdown(item);
      if (isRangeActive("maxDrawdown", debouncedFilters.maxDrawdown) && maxDrawdown != null) {
        if (
          maxDrawdown < debouncedFilters.maxDrawdown.min ||
          maxDrawdown > debouncedFilters.maxDrawdown.max
        ) {
          return false;
        }
      }
      return true;
    });
  }, [backtests, debouncedFilters]);

  const strategyOptions = useMemo(() => {
    const fromRows = backtests.map((item) => item.strategy).filter(Boolean);
    return [...new Set([...allStrategies, ...fromRows])]
      .sort((a, b) => a.localeCompare(b))
      .map((value) => ({ label: value, value }));
  }, [allStrategies, backtests]);

  const symbolOptions = useMemo(() => {
    const fromRows = backtests.flatMap((item) => item.symbols ?? []).filter(Boolean);
    return [...new Set([...allSymbols, ...fromRows])]
      .sort((a, b) => a.localeCompare(b))
      .map((value) => ({ label: value, value }));
  }, [allSymbols, backtests]);

  const timeframeOptions = useMemo(() => {
    const fromData = new Set(backtests.flatMap((item) => item.timeframes ?? []).filter(Boolean));
    DEFAULT_TIMEFRAMES.forEach((timeframe) => fromData.add(timeframe));
    return [...fromData].map((value) => ({ label: value, value }));
  }, [backtests]);

  const totalPages = Math.max(1, Math.ceil(totalBacktests / perPage));

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  const allVisibleSelected =
    clientFiltered.length > 0 && clientFiltered.every((item) => selected.has(item.id));

  const toggleSelect = (id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const toggleSelectAllVisible = () => {
    setSelected((current) => {
      if (allVisibleSelected) {
        const next = new Set(current);
        clientFiltered.forEach((item) => next.delete(item.id));
        return next;
      }
      const next = new Set(current);
      clientFiltered.forEach((item) => next.add(item.id));
      return next;
    });
  };

  const cycleSort = (column: BacktestsSortField) => {
    if (sortColumn !== column) {
      setSortColumn(column);
      setSortOrder("asc");
      return;
    }
    if (sortOrder === "asc") {
      setSortOrder("desc");
      return;
    }
    if (sortOrder === "desc") {
      setSortOrder(null);
      return;
    }
    setSortOrder("asc");
  };

  const resetFilters = () => {
    const next = cloneInitialFilters();
    setFilters(next);
    setDebouncedFilters(next);
    setPage(1);
    setSortColumn("created_at");
    setSortOrder("desc");
    setSelected(new Set());
  };

  const selectedIds = useMemo(() => [...selected], [selected]);

  const handleBulkDelete = async () => {
    if (selectedIds.length === 0) return;
    const confirmed = window.confirm(`Delete ${selectedIds.length} selected backtests?`);
    if (!confirmed) return;

    try {
      const result = await deleteBacktestsBulk(selectedIds);
      showToast(`Deleted ${result.deleted} backtest(s).`, "success");
      setSelected(new Set());
      const refreshed = await getBacktests(buildQueryParams);
      setBacktests(refreshed.items ?? []);
      setTotalBacktests(refreshed.total ?? 0);
    } catch (error: unknown) {
      showToast(toErrorMessage(error, "Bulk delete failed."), "error");
    }
  };

  const handleDeduplicate = async () => {
    setDeduplicating(true);
    try {
      const duplicates = await getBacktestDuplicates();
      if (!duplicates.total_groups) {
        showToast("No duplicate backtests found.", "info");
        return;
      }

      const confirmed = window.confirm(
        `Found ${duplicates.total_groups} duplicate groups. Remove older duplicates and keep newest runs?`,
      );
      if (!confirmed) return;

      const result = await deduplicateBacktests();
      showToast(
        `Removed ${result.deleted_ids.length} duplicate backtests across ${result.groups_affected} groups.`,
        "success",
      );
      setSelected(new Set());
      const refreshed = await getBacktests(buildQueryParams);
      setBacktests(refreshed.items ?? []);
      setTotalBacktests(refreshed.total ?? 0);
    } catch (error: unknown) {
      showToast(toErrorMessage(error, "Failed to remove duplicates."), "error");
    } finally {
      setDeduplicating(false);
    }
  };

  const thStyle: CSSProperties = {
    position: "sticky",
    top: 0,
    zIndex: 2,
    background: "#141b2b",
    color: "var(--color-text-muted)",
    fontSize: "0.72rem",
    fontWeight: 600,
    letterSpacing: "0.04em",
    textTransform: "uppercase",
    padding: "10px 8px",
    borderBottom: "1px solid rgba(148, 163, 184, 0.26)",
    whiteSpace: "nowrap",
  };

  const tdStyle: CSSProperties = {
    padding: "8px 8px",
    borderBottom: "1px solid rgba(148, 163, 184, 0.14)",
    fontSize: "0.75rem",
    color: "var(--color-text)",
    whiteSpace: "nowrap",
  };

  return (
    <div style={{ padding: "20px 22px 80px", background: "#131722", minHeight: "100%" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "12px",
          flexWrap: "wrap",
          marginBottom: "14px",
        }}
      >
        <div>
          <h1 style={{ fontSize: "1.35rem", fontWeight: 700 }}>Backtests Screener</h1>
          <div style={{ color: "var(--color-text-muted)", fontSize: "0.76rem" }}>
            TradingView-style scanner with live API filters, sorting and bulk actions
          </div>
        </div>

        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
          <button
            type="button"
            onClick={() => setShowCreateModal(true)}
            style={{
              borderRadius: "9px",
              border: "1px solid rgba(88, 166, 255, 0.5)",
              background: "rgba(88, 166, 255, 0.16)",
              color: "#cfe3ff",
              fontSize: "0.76rem",
              padding: "8px 12px",
              display: "flex",
              alignItems: "center",
              gap: "6px",
              cursor: "pointer",
            }}
          >
            <Plus size={13} />
            New Backtest
          </button>
          <button
            type="button"
            onClick={() => void handleDeduplicate()}
            disabled={deduplicating}
            style={{
              borderRadius: "9px",
              border: "1px solid rgba(248, 81, 73, 0.3)",
              background: "rgba(248, 81, 73, 0.12)",
              color: "#f87171",
              fontSize: "0.76rem",
              padding: "8px 12px",
              display: "flex",
              alignItems: "center",
              gap: "6px",
              cursor: deduplicating ? "not-allowed" : "pointer",
            }}
          >
            <RefreshCw size={13} className={deduplicating ? "animate-spin" : ""} />
            {deduplicating ? "Removing..." : "Remove Duplicates"}
          </button>
        </div>
      </div>

      <section
        style={{
          borderRadius: "14px",
          border: "1px solid rgba(148, 163, 184, 0.2)",
          background: "rgba(19, 23, 34, 0.98)",
          padding: "12px",
          marginBottom: "12px",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: "10px",
            flexWrap: "wrap",
            alignItems: "center",
            marginBottom: "10px",
          }}
        >
          <MultiSelectDropdown
            label="Strategy"
            options={strategyOptions}
            selected={filters.strategies}
            onChange={(next) => setFilters((current) => ({ ...current, strategies: next }))}
            searchable
            searchPlaceholder="Search strategy..."
          />

          <MultiSelectDropdown
            label="Timeframe"
            options={timeframeOptions}
            selected={filters.timeframes}
            onChange={(next) => setFilters((current) => ({ ...current, timeframes: next }))}
          />

          <MultiSelectDropdown
            label="Symbol"
            options={symbolOptions}
            selected={filters.symbols}
            onChange={(next) => setFilters((current) => ({ ...current, symbols: next }))}
            searchable
            searchPlaceholder="Search symbol..."
          />

          <div style={{ minWidth: "220px", flex: "1 1 220px", maxWidth: "360px" }}>
            <input
              type="text"
              value={filters.symbolSearch}
              onChange={(event) =>
                setFilters((current) => ({ ...current, symbolSearch: event.target.value }))
              }
              placeholder="Quick search: strategy, symbol, id..."
              style={{
                width: "100%",
                border: "1px solid rgba(143, 154, 184, 0.26)",
                borderRadius: "10px",
                background: "rgba(19, 23, 34, 0.88)",
                color: "var(--color-text)",
                fontSize: "0.76rem",
                padding: "9px 10px",
              }}
            />
          </div>

          <button
            type="button"
            onClick={resetFilters}
            style={{
              border: "1px solid rgba(143, 154, 184, 0.28)",
              borderRadius: "10px",
              background: "rgba(19, 23, 34, 0.88)",
              color: "var(--color-text-muted)",
              padding: "9px 12px",
              fontSize: "0.75rem",
              cursor: "pointer",
            }}
          >
            Reset Filters
          </button>
        </div>

        {filters.strategies.length > 0 || filters.timeframes.length > 0 || filters.symbols.length > 0 ? (
          <div style={{ display: "flex", gap: "7px", flexWrap: "wrap", marginBottom: "10px" }}>
            {filters.strategies.map((strategy) => (
              <FilterChip
                key={`strategy-${strategy}`}
                label={strategy}
                onRemove={() =>
                  setFilters((current) => ({
                    ...current,
                    strategies: current.strategies.filter((entry) => entry !== strategy),
                  }))
                }
              />
            ))}
            {filters.timeframes.map((timeframe) => (
              <FilterChip
                key={`timeframe-${timeframe}`}
                label={timeframe}
                onRemove={() =>
                  setFilters((current) => ({
                    ...current,
                    timeframes: current.timeframes.filter((entry) => entry !== timeframe),
                  }))
                }
              />
            ))}
            {filters.symbols.map((symbol) => (
              <FilterChip
                key={`symbol-${symbol}`}
                label={symbol}
                onRemove={() =>
                  setFilters((current) => ({
                    ...current,
                    symbols: current.symbols.filter((entry) => entry !== symbol),
                  }))
                }
              />
            ))}
          </div>
        ) : null}

        <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
          <RangeFilterControl
            label="Sharpe Ratio"
            value={filters.sharpe}
            bounds={DEFAULT_RANGES.sharpe}
            step={0.1}
            onChange={(next) => setFilters((current) => ({ ...current, sharpe: next }))}
          />
          <RangeFilterControl
            label="PSR"
            value={filters.psr}
            bounds={DEFAULT_RANGES.psr}
            step={0.01}
            onChange={(next) => setFilters((current) => ({ ...current, psr: next }))}
          />
          <RangeFilterControl
            label="p-Value"
            value={filters.pValue}
            bounds={DEFAULT_RANGES.pValue}
            step={0.001}
            onChange={(next) => setFilters((current) => ({ ...current, pValue: next }))}
          />
          <RangeFilterControl
            label="CAGR"
            value={filters.cagr}
            bounds={DEFAULT_RANGES.cagr}
            step={1}
            numberSuffix="%"
            onChange={(next) => setFilters((current) => ({ ...current, cagr: next }))}
          />
          <RangeFilterControl
            label="Max Drawdown"
            value={filters.maxDrawdown}
            bounds={DEFAULT_RANGES.maxDrawdown}
            step={1}
            numberSuffix="%"
            onChange={(next) => setFilters((current) => ({ ...current, maxDrawdown: next }))}
          />
        </div>
      </section>

      <section
        style={{
          borderRadius: "14px",
          border: "1px solid rgba(148, 163, 184, 0.2)",
          background: "rgba(19, 23, 34, 0.96)",
          overflow: "hidden",
        }}
      >
        <div style={{ overflowX: "auto", maxHeight: "calc(100vh - 330px)", overflowY: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "1220px" }}>
            <thead>
              <tr>
                <th style={{ ...thStyle, width: "42px" }}>
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleSelectAllVisible}
                    style={{ cursor: "pointer", accentColor: "#58a6ff" }}
                  />
                </th>
                <HeaderCell
                  label="Strategy"
                  active={sortColumn === "strategy_name"}
                  order={sortColumn === "strategy_name" ? sortOrder : null}
                  onClick={() => cycleSort("strategy_name")}
                />
                <th style={thStyle}>Symbol</th>
                <th style={thStyle}>Timeframe</th>
                <HeaderCell
                  label="Total Return"
                  active={sortColumn === "total_return"}
                  order={sortColumn === "total_return" ? sortOrder : null}
                  onClick={() => cycleSort("total_return")}
                />
                <HeaderCell
                  label="CAGR"
                  active={sortColumn === "cagr"}
                  order={sortColumn === "cagr" ? sortOrder : null}
                  onClick={() => cycleSort("cagr")}
                />
                <HeaderCell
                  label="Sharpe"
                  active={sortColumn === "sharpe_ratio"}
                  order={sortColumn === "sharpe_ratio" ? sortOrder : null}
                  onClick={() => cycleSort("sharpe_ratio")}
                />
                <HeaderCell
                  label="PSR"
                  active={sortColumn === "psr"}
                  order={sortColumn === "psr" ? sortOrder : null}
                  onClick={() => cycleSort("psr")}
                />
                <HeaderCell
                  label="p-Value"
                  active={sortColumn === "p_value"}
                  order={sortColumn === "p_value" ? sortOrder : null}
                  onClick={() => cycleSort("p_value")}
                />
                <th style={thStyle}>Max Drawdown</th>
                <HeaderCell
                  label="Trades"
                  active={sortColumn === "trade_count"}
                  order={sortColumn === "trade_count" ? sortOrder : null}
                  onClick={() => cycleSort("trade_count")}
                />
                <th style={thStyle}>Win Rate</th>
                <HeaderCell
                  label="Created"
                  active={sortColumn === "created_at"}
                  order={sortColumn === "created_at" ? sortOrder : null}
                  onClick={() => cycleSort("created_at")}
                />
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td
                    colSpan={13}
                    style={{
                      ...tdStyle,
                      color: "var(--color-text-muted)",
                      textAlign: "center",
                      padding: "26px 0",
                    }}
                  >
                    Loading backtests...
                  </td>
                </tr>
              ) : clientFiltered.length === 0 ? (
                <tr>
                  <td
                    colSpan={13}
                    style={{
                      ...tdStyle,
                      color: "var(--color-text-muted)",
                      textAlign: "center",
                      padding: "28px 0",
                    }}
                  >
                    No backtests found for current filter selection.
                  </td>
                </tr>
              ) : (
                clientFiltered.map((item) => {
                  const sharpe = normalizeSharpe(item);
                  const psr = normalizePsr(item);
                  const pValue = normalizePValue(item);
                  const cagr = normalizeCagr(item);
                  const maxDrawdown = normalizeMaxDrawdown(item);
                  const selectedRow = selected.has(item.id);

                  return (
                    <tr
                      key={item.id}
                      onClick={() => navigate(`/backtests/${item.id}`)}
                      style={{
                        background: selectedRow ? "rgba(88, 166, 255, 0.1)" : "transparent",
                        cursor: "pointer",
                      }}
                      onMouseEnter={(event) => {
                        if (!selectedRow) {
                          event.currentTarget.style.background = "rgba(88, 166, 255, 0.07)";
                        }
                      }}
                      onMouseLeave={(event) => {
                        if (!selectedRow) {
                          event.currentTarget.style.background = "transparent";
                        }
                      }}
                    >
                      <td
                        style={tdStyle}
                        onClick={(event) => {
                          event.stopPropagation();
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={selectedRow}
                          onChange={() => toggleSelect(item.id)}
                          style={{ cursor: "pointer", accentColor: "#58a6ff" }}
                        />
                      </td>
                      <td style={tdStyle}>
                        <div style={{ display: "grid", gap: "3px" }}>
                          <span style={{ fontWeight: 600 }}>{item.strategy}</span>
                          <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                            <StatBadge label="PSR" value={psr == null ? "—" : `${(psr * 100).toFixed(0)}%`} color={psrColor(psr)} />
                            <StatBadge label="p-Value" value={pValue == null ? "—" : pValue.toFixed(3)} color={pValueColor(pValue)} />
                          </div>
                        </div>
                      </td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT }}>{(item.symbols ?? []).join(", ") || "—"}</td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT }}>{item.timeframes?.[0] ?? "—"}</td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT, color: percentageColor(item.net_profit_pct) }}>
                        {formatPct(item.net_profit_pct)}
                      </td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT, color: percentageColor(cagr) }}>{formatPct(cagr)}</td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT, color: sharpeColor(sharpe) }}>{formatNum(sharpe, 3)}</td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT, color: psrColor(psr) }}>
                        {psr == null ? "—" : `${(psr * 100).toFixed(1)}%`}
                      </td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT, color: pValueColor(pValue) }}>
                        {pValue == null ? "—" : pValue.toFixed(4)}
                      </td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT, color: "#f87171" }}>
                        {maxDrawdown == null ? "—" : `${maxDrawdown.toFixed(2)}%`}
                      </td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT }}>{item.trade_count}</td>
                      <td style={{ ...tdStyle, fontFamily: NUMERIC_FONT }}>
                        {item.win_rate == null ? "—" : `${(item.win_rate * 100).toFixed(1)}%`}
                      </td>
                      <td style={tdStyle}>{parseIsoToDate(item.created_at)}</td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      <div
        style={{
          marginTop: "10px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "10px",
          flexWrap: "wrap",
        }}
      >
        <span style={{ color: "var(--color-text-muted)", fontSize: "0.76rem" }}>
          Page {page} of {totalPages} • {totalBacktests} total
        </span>

        <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
          <label style={{ fontSize: "0.74rem", color: "var(--color-text-muted)" }}>
            Rows per page{" "}
            <select
              value={perPage}
              onChange={(event) => {
                setPerPage(Number(event.target.value));
                setPage(1);
              }}
              style={{
                marginInlineStart: "4px",
                borderRadius: "8px",
                border: "1px solid rgba(148, 163, 184, 0.25)",
                background: "#121826",
                color: "var(--color-text)",
                padding: "6px 8px",
                fontSize: "0.74rem",
              }}
            >
              <option value={25}>25</option>
              <option value={50}>50</option>
              <option value={100}>100</option>
            </select>
          </label>

          <PagerButton onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={page <= 1}>
            {"<"}
          </PagerButton>
          {buildPageNumbers(page, totalPages).map((entry, index) =>
            typeof entry === "number" ? (
              <PagerButton
                key={`page-${entry}`}
                onClick={() => setPage(entry)}
                active={entry === page}
              >
                {entry}
              </PagerButton>
            ) : (
              <span key={`ellipsis-${index}`} style={{ color: "var(--color-text-muted)" }}>
                ...
              </span>
            ),
          )}
          <PagerButton
            onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
            disabled={page >= totalPages}
          >
            {">"}
          </PagerButton>
        </div>
      </div>

      {selectedIds.length > 0 ? (
        <div
          style={{
            position: "fixed",
            insetInline: "auto 18px",
            bottom: "14px",
            zIndex: 50,
            display: "flex",
            gap: "10px",
            alignItems: "center",
            borderRadius: "12px",
            border: "1px solid rgba(148, 163, 184, 0.28)",
            background: "rgba(11, 16, 27, 0.96)",
            backdropFilter: "blur(6px)",
            padding: "10px 12px",
            boxShadow: "0 18px 34px rgba(0, 0, 0, 0.4)",
          }}
        >
          <span style={{ color: "var(--color-text)", fontSize: "0.78rem" }}>
            {selectedIds.length} selected
          </span>

          <button
            type="button"
            onClick={() => navigate(`/backtests/compare?ids=${selectedIds.join(",")}`)}
            disabled={selectedIds.length < 2}
            style={{
              borderRadius: "8px",
              border: "1px solid rgba(88, 166, 255, 0.35)",
              background: "rgba(88, 166, 255, 0.14)",
              color: "#a7c8ff",
              padding: "7px 10px",
              fontSize: "0.74rem",
              display: "flex",
              alignItems: "center",
              gap: "6px",
              cursor: selectedIds.length < 2 ? "not-allowed" : "pointer",
              opacity: selectedIds.length < 2 ? 0.5 : 1,
            }}
          >
            <GitCompare size={13} />
            Compare Selected
          </button>

          <button
            type="button"
            onClick={() => void handleBulkDelete()}
            style={{
              borderRadius: "8px",
              border: "1px solid rgba(248, 81, 73, 0.4)",
              background: "rgba(248, 81, 73, 0.15)",
              color: "#fca5a5",
              padding: "7px 10px",
              fontSize: "0.74rem",
              display: "flex",
              alignItems: "center",
              gap: "6px",
              cursor: "pointer",
            }}
          >
            <Trash2 size={13} />
            Delete Selected ({selectedIds.length})
          </button>
        </div>
      ) : null}

      <CreateBacktestModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onTaskStarted={() => setShowCreateModal(false)}
      />
    </div>
  );
}

function HeaderCell({
  label,
  active,
  order,
  onClick,
}: {
  label: string;
  active: boolean;
  order: SortOrder;
  onClick: () => void;
}) {
  return (
    <th
      onClick={onClick}
      style={{
        position: "sticky",
        top: 0,
        zIndex: 2,
        background: "#141b2b",
        color: active ? "#d8e4ff" : "var(--color-text-muted)",
        fontSize: "0.72rem",
        fontWeight: 600,
        letterSpacing: "0.04em",
        textTransform: "uppercase",
        padding: "10px 8px",
        borderBottom: "1px solid rgba(148, 163, 184, 0.26)",
        whiteSpace: "nowrap",
        cursor: "pointer",
      }}
    >
      <span style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
        {label}
        {order === "asc" ? <ChevronUp size={12} /> : null}
        {order === "desc" ? <ChevronDown size={12} /> : null}
      </span>
    </th>
  );
}

function PagerButton({
  children,
  onClick,
  disabled = false,
  active = false,
}: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        borderRadius: "8px",
        border: active
          ? "1px solid rgba(88, 166, 255, 0.6)"
          : "1px solid rgba(148, 163, 184, 0.24)",
        background: active ? "rgba(88, 166, 255, 0.2)" : "rgba(17, 24, 39, 0.9)",
        color: active ? "#dbe7ff" : "var(--color-text)",
        minWidth: "30px",
        height: "30px",
        padding: "0 8px",
        fontSize: "0.74rem",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.45 : 1,
      }}
    >
      {children}
    </button>
  );
}

function buildPageNumbers(currentPage: number, totalPages: number): Array<number | "..."> {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }
  const pages = new Set<number>([1, totalPages, currentPage, currentPage - 1, currentPage + 1]);
  const validPages = [...pages].filter((page) => page >= 1 && page <= totalPages).sort((a, b) => a - b);
  const entries: Array<number | "..."> = [];
  for (let i = 0; i < validPages.length; i += 1) {
    const page = validPages[i];
    const previous = validPages[i - 1];
    if (i > 0 && page - previous > 1) {
      entries.push("...");
    }
    entries.push(page);
  }
  return entries;
}

function StatBadge({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "4px",
        borderRadius: "999px",
        border: "1px solid rgba(148, 163, 184, 0.24)",
        background: "rgba(14, 20, 30, 0.9)",
        padding: "2px 7px",
        fontFamily: NUMERIC_FONT,
        fontSize: "0.66rem",
      }}
    >
      <span style={{ color: "var(--color-text-muted)" }}>{label}</span>
      <span style={{ color, fontWeight: 700 }}>{value}</span>
    </span>
  );
}

function FilterChip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span
      style={{
        borderRadius: "999px",
        border: "1px solid rgba(96, 165, 250, 0.4)",
        background: "rgba(37, 99, 235, 0.18)",
        color: "#dbeafe",
        padding: "4px 8px",
        fontSize: "0.7rem",
        display: "inline-flex",
        alignItems: "center",
        gap: "6px",
      }}
    >
      {label}
      <button
        type="button"
        onClick={onRemove}
        style={{
          border: "none",
          background: "transparent",
          color: "#bfdbfe",
          cursor: "pointer",
          display: "inline-flex",
          alignItems: "center",
          padding: 0,
        }}
      >
        <X size={12} />
      </button>
    </span>
  );
}
