import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowUpDown, Plus, Scale, Trash2 } from "lucide-react";

import {
  deduplicateBacktests,
  deleteBacktestsBulk,
  getBacktestDuplicates,
  getBacktestsAll,
} from "../api/client";
import type { BacktestMetadataItem, BacktestsDuplicatesResponse } from "../api/types";
import { CreateBacktestModal } from "../components/CreateBacktestModal";
import { DualRangeSlider } from "../components/DualRangeSlider";
import { FilterBar } from "../components/FilterBar";
import { Modal } from "../components/Modal";
import { MultiSelectDropdown } from "../components/MultiSelectDropdown";
import { Sparkline } from "../components/Sparkline";
import { useToast } from "../components/ToastProvider";

type SortField =
  | "created_at"
  | "total_return"
  | "sharpe_ratio"
  | "psr"
  | "p_value"
  | "cagr"
  | "max_drawdown"
  | "win_rate"
  | "trade_count";
type SortOrder = "asc" | "desc";

type NumericRange = { min: number; max: number };

interface NumericBoundsState {
  totalReturn: NumericRange;
  sharpe: NumericRange;
  psr: NumericRange;
  pValue: NumericRange;
  cagr: NumericRange;
  maxDrawdown: NumericRange;
  winRate: NumericRange;
}

interface NumericFiltersState {
  totalReturn: NumericRange;
  sharpe: NumericRange;
  psr: NumericRange;
  pValue: NumericRange;
  cagr: NumericRange;
  maxDrawdown: NumericRange;
  winRate: NumericRange;
}

interface BacktestCardModel extends BacktestMetadataItem {
  equity_preview: number[];
}

const DEFAULT_PER_PAGE = 24;
const NUMERIC_FONT = '"SFMono-Regular", "Cascadia Code", "Roboto Mono", ui-monospace, monospace';

const BACKTEST_SORT_OPTIONS: Array<{ value: SortField; label: string }> = [
  { value: "created_at", label: "Created Date" },
  { value: "total_return", label: "Total Return" },
  { value: "sharpe_ratio", label: "Sharpe (higher = better)" },
  { value: "psr", label: "PSR (higher = better)" },
  { value: "p_value", label: "p-Value (lower = better)" },
  { value: "cagr", label: "CAGR (higher = better)" },
  { value: "max_drawdown", label: "Max DD (lower = better)" },
  { value: "win_rate", label: "Win Rate (higher = better)" },
  { value: "trade_count", label: "Trade Count" },
];

const FILTER_PANEL_GRID_STYLE: React.CSSProperties = {
  display: "grid",
  gap: "10px",
};

const FILTER_PANEL_CONTROLS_STYLE: React.CSSProperties = {
  display: "grid",
  gap: "10px",
  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
};

const SELECT_ALL_LABEL_STYLE: React.CSSProperties = {
  display: "inline-flex",
  gap: "6px",
  alignItems: "center",
  fontSize: "0.76rem",
  color: "var(--color-text-muted)",
};

const BULK_BAR_STYLE: React.CSSProperties = {
  position: "fixed",
  insetInlineStart: "50%",
  transform: "translateX(-50%)",
  insetBlockEnd: "16px",
  zIndex: 60,
  borderRadius: "999px",
  border: "1px solid rgba(90, 105, 138, 0.34)",
  background: "rgba(11, 18, 31, 0.95)",
  boxShadow: "0 18px 40px rgba(0,0,0,0.45)",
  padding: "8px 10px",
  display: "inline-flex",
  gap: "8px",
  alignItems: "center",
};

let backtestsAllCache: BacktestCardModel[] | null = null;
let backtestsAllPromise: Promise<BacktestCardModel[]> | null = null;

function toErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  return fallback;
}

function normalizeWinRate(value: number | null | undefined): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  if (value > 1 && value <= 100) return value / 100;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

function metricValue(value: number | null | undefined, fallback = 0): number {
  if (value == null || !Number.isFinite(value)) return fallback;
  return value;
}

function percentage(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "N/A";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function formatNumber(value: number | null | undefined, digits = 3): string {
  if (value == null || !Number.isFinite(value)) return "N/A";
  return value.toFixed(digits);
}

function formatPValue(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "N/A";
  return value.toFixed(4);
}

function statColorPositive(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--color-text-muted)";
  return value >= 0 ? "#3fb950" : "#f87171";
}

function sharpeColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--color-text-muted)";
  if (value >= 1) return "#3fb950";
  if (value >= 0) return "#fbbf24";
  return "#f87171";
}

function psrColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--color-text-muted)";
  if (value >= 0.95) return "#3fb950";
  if (value >= 0.5) return "#fbbf24";
  return "#f87171";
}

function pValueColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--color-text-muted)";
  return value <= 0.05 ? "#3fb950" : "#f87171";
}

function formatPeriodDate(value: string | null | undefined): string {
  const text = String(value || "").trim();
  if (!text) return "N/A";
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) {
    return text;
  }
  return parsed.toLocaleDateString("de-DE");
}

function buildPageNumbers(currentPage: number, totalPages: number): Array<number | "..."> {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, index) => index + 1);
  const values = new Set<number>([1, totalPages, currentPage - 1, currentPage, currentPage + 1]);
  const pages = [...values].filter((entry) => entry >= 1 && entry <= totalPages).sort((a, b) => a - b);
  const output: Array<number | "..."> = [];
  pages.forEach((value, index) => {
    const previous = pages[index - 1];
    if (index > 0 && value - previous > 1) {
      output.push("...");
    }
    output.push(value);
  });
  return output;
}

function cloneRows(rows: BacktestCardModel[]): BacktestCardModel[] {
  return rows.map((row) => ({
    ...row,
    symbols: Array.isArray(row.symbols) ? [...row.symbols] : [],
    tags: Array.isArray(row.tags) ? [...row.tags] : [],
    equity_preview: Array.isArray(row.equity_preview) ? [...row.equity_preview] : [],
  }));
}

function normalizeBacktestRow(row: BacktestMetadataItem & { equity_preview?: number[] }): BacktestCardModel {
  return {
    ...row,
    symbols: Array.isArray(row.symbols) ? row.symbols : [],
    tags: Array.isArray(row.tags) ? row.tags : [],
    equity_preview: Array.isArray(row.equity_preview)
      ? row.equity_preview.filter((value) => Number.isFinite(value)).map((value) => Number(value))
      : [],
  };
}

function toBounds(values: number[], fallback: NumericRange): NumericRange {
  const valid = values.filter((value) => Number.isFinite(value));
  if (valid.length === 0) return fallback;
  return {
    min: Math.min(...valid),
    max: Math.max(...valid),
  };
}

function buildNumericBounds(items: BacktestCardModel[]): NumericBoundsState {
  return {
    totalReturn: toBounds(items.map((item) => metricValue(item.total_return) * 100), { min: -50, max: 50 }),
    sharpe: toBounds(items.map((item) => metricValue(item.sharpe_ratio)), { min: -2, max: 3 }),
    psr: toBounds(items.map((item) => metricValue(item.psr)), { min: 0, max: 1 }),
    pValue: toBounds(items.map((item) => metricValue(item.p_value, 1)), { min: 0, max: 1 }),
    cagr: toBounds(items.map((item) => metricValue(item.cagr) * 100), { min: -20, max: 40 }),
    maxDrawdown: toBounds(items.map((item) => Math.abs(metricValue(item.max_drawdown)) * 100), {
      min: 0,
      max: 60,
    }),
    winRate: toBounds(
      items.map((item) => {
        const normalized = normalizeWinRate(item.win_rate);
        return normalized == null ? 0 : normalized * 100;
      }),
      { min: 0, max: 100 },
    ),
  };
}

function cloneFilters(source: NumericFiltersState): NumericFiltersState {
  return {
    totalReturn: { ...source.totalReturn },
    sharpe: { ...source.sharpe },
    psr: { ...source.psr },
    pValue: { ...source.pValue },
    cagr: { ...source.cagr },
    maxDrawdown: { ...source.maxDrawdown },
    winRate: { ...source.winRate },
  };
}

function boundsToFilters(bounds: NumericBoundsState): NumericFiltersState {
  return cloneFilters({
    totalReturn: bounds.totalReturn,
    sharpe: bounds.sharpe,
    psr: bounds.psr,
    pValue: bounds.pValue,
    cagr: bounds.cagr,
    maxDrawdown: bounds.maxDrawdown,
    winRate: bounds.winRate,
  });
}

async function loadBacktestsAllCached(name = "", force = false): Promise<BacktestCardModel[]> {
  const normalizedName = name.trim();
  if (normalizedName) {
    const rows = await getBacktestsAll(normalizedName);
    return (rows ?? []).map((row) => normalizeBacktestRow(row as BacktestMetadataItem & { equity_preview?: number[] }));
  }
  if (force) {
    backtestsAllCache = null;
    backtestsAllPromise = null;
  }
  if (backtestsAllCache) return cloneRows(backtestsAllCache);

  if (!backtestsAllPromise) {
    backtestsAllPromise = getBacktestsAll()
      .then((rows) => {
        const normalized = (rows ?? []).map((row) => normalizeBacktestRow(row as BacktestMetadataItem & { equity_preview?: number[] }));
        backtestsAllCache = cloneRows(normalized);
        return cloneRows(backtestsAllCache);
      })
      .finally(() => {
        backtestsAllPromise = null;
      });
  }
  return backtestsAllPromise;
}

function updateFilter(
  current: NumericFiltersState,
  key: keyof NumericFiltersState,
  min: number,
  max: number,
): NumericFiltersState {
  return {
    ...current,
    [key]: { min, max },
  };
}

export function BacktestsPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [items, setItems] = useState<BacktestCardModel[]>([]);
  const [loading, setLoading] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);

  const [nameFilter, setNameFilter] = useState("");
  const [selectedStrategies, setSelectedStrategies] = useState<string[]>([]);
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([]);
  const [selectedTimeframes, setSelectedTimeframes] = useState<string[]>([]);
  const [symbolDraft, setSymbolDraft] = useState("");

  const [sortField, setSortField] = useState<SortField>("created_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(DEFAULT_PER_PAGE);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const [numericBounds, setNumericBounds] = useState<NumericBoundsState>(() =>
    buildNumericBounds([]),
  );
  const [numericFilters, setNumericFilters] = useState<NumericFiltersState>(() =>
    boundsToFilters(buildNumericBounds([])),
  );
  const [duplicatesInfo, setDuplicatesInfo] = useState<BacktestsDuplicatesResponse | null>(null);
  const [showDuplicatesModal, setShowDuplicatesModal] = useState(false);
  const [loadingDuplicates, setLoadingDuplicates] = useState(false);
  const [runningDeduplicate, setRunningDeduplicate] = useState(false);

  const loadAllMetadata = useCallback(
    async (force = false) => {
      setLoading(true);
      try {
        const rows = await loadBacktestsAllCached(nameFilter, force);
        const bounds = buildNumericBounds(rows);
        setItems(rows);
        setNumericBounds(bounds);
        setNumericFilters(boundsToFilters(bounds));
        setSelectedIds(new Set());
      } catch (error: unknown) {
        showToast(toErrorMessage(error, "Failed to load backtests metadata."), "error");
      } finally {
        setLoading(false);
      }
    },
    [nameFilter, showToast],
  );

  useEffect(() => {
    void loadAllMetadata(nameFilter.trim().length === 0);
  }, [loadAllMetadata]);

  const allStrategies = useMemo(
    () =>
      [...new Set(items.map((item) => item.strategy_name).filter(Boolean))].sort((a, b) =>
        a.localeCompare(b),
      ),
    [items],
  );

  const allSymbols = useMemo(
    () =>
      [...new Set(items.flatMap((item) => item.symbols ?? []).filter(Boolean))].sort((a, b) =>
        a.localeCompare(b),
      ),
    [items],
  );

  const allTimeframes = useMemo(
    () =>
      [...new Set(items.map((item) => item.timeframe).filter(Boolean))].sort((a, b) =>
        a.localeCompare(b),
      ),
    [items],
  );

  const filteredSorted = useMemo(() => {
    const result = items.filter((item) => {
      if (selectedStrategies.length > 0 && !selectedStrategies.includes(item.strategy_name)) {
        return false;
      }
      if (selectedTimeframes.length > 0 && !selectedTimeframes.includes(item.timeframe)) {
        return false;
      }
      if (selectedSymbols.length > 0) {
        const symbolSet = new Set((item.symbols ?? []).map((entry) => entry.toUpperCase()));
        if (!selectedSymbols.every((entry) => symbolSet.has(entry.toUpperCase()))) {
          return false;
        }
      }

      const totalReturn = metricValue(item.total_return) * 100;
      const sharpe = metricValue(item.sharpe_ratio);
      const psr = metricValue(item.psr);
      const pValue = metricValue(item.p_value, 1);
      const cagr = metricValue(item.cagr) * 100;
      const maxDrawdown = Math.abs(metricValue(item.max_drawdown)) * 100;
      const winRate = (normalizeWinRate(item.win_rate) ?? 0) * 100;

      return (
        totalReturn >= numericFilters.totalReturn.min &&
        totalReturn <= numericFilters.totalReturn.max &&
        sharpe >= numericFilters.sharpe.min &&
        sharpe <= numericFilters.sharpe.max &&
        psr >= numericFilters.psr.min &&
        psr <= numericFilters.psr.max &&
        pValue >= numericFilters.pValue.min &&
        pValue <= numericFilters.pValue.max &&
        cagr >= numericFilters.cagr.min &&
        cagr <= numericFilters.cagr.max &&
        maxDrawdown >= numericFilters.maxDrawdown.min &&
        maxDrawdown <= numericFilters.maxDrawdown.max &&
        winRate >= numericFilters.winRate.min &&
        winRate <= numericFilters.winRate.max
      );
    });

    const direction = sortOrder === "asc" ? 1 : -1;
    result.sort((left, right) => {
      if (sortField === "created_at") {
        const leftTime = Date.parse(left.created_at || "") || 0;
        const rightTime = Date.parse(right.created_at || "") || 0;
        if (leftTime === rightTime) return left.id.localeCompare(right.id) * direction;
        return (leftTime - rightTime) * direction;
      }
      if (sortField === "win_rate") {
        const leftWin = normalizeWinRate(left.win_rate) ?? -1;
        const rightWin = normalizeWinRate(right.win_rate) ?? -1;
        if (leftWin === rightWin) return left.id.localeCompare(right.id) * direction;
        return (leftWin - rightWin) * direction;
      }
      const leftValue = Number(left[sortField] ?? 0);
      const rightValue = Number(right[sortField] ?? 0);
      if (leftValue === rightValue) return left.id.localeCompare(right.id) * direction;
      return (leftValue - rightValue) * direction;
    });
    return result;
  }, [
    items,
    numericFilters,
    selectedStrategies,
    selectedSymbols,
    selectedTimeframes,
    sortField,
    sortOrder,
  ]);

  const totalPages = Math.max(1, Math.ceil(filteredSorted.length / perPage));
  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const currentPageItems = useMemo(() => {
    const start = (page - 1) * perPage;
    return filteredSorted.slice(start, start + perPage);
  }, [filteredSorted, page, perPage]);

  useEffect(() => {
    const valid = new Set(filteredSorted.map((item) => item.id));
    setSelectedIds((current) => {
      if (current.size === 0) return current;
      const next = new Set<string>();
      current.forEach((id) => {
        if (valid.has(id)) next.add(id);
      });
      return next.size === current.size ? current : next;
    });
  }, [filteredSorted]);

  const selectedCount = selectedIds.size;
  const allVisibleSelected =
    currentPageItems.length > 0 && currentPageItems.every((item) => selectedIds.has(item.id));

  const toggleSelected = (id: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectedVisible = () => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (allVisibleSelected) {
        currentPageItems.forEach((item) => next.delete(item.id));
      } else {
        currentPageItems.forEach((item) => next.add(item.id));
      }
      return next;
    });
  };

  const addSymbolDraft = useCallback(() => {
    const normalized = symbolDraft.trim().toUpperCase();
    if (!normalized) return;
    if (!allSymbols.some((entry) => entry.toUpperCase() === normalized)) return;
    setSelectedSymbols((current) => (current.includes(normalized) ? current : [...current, normalized]));
    setSymbolDraft("");
    setPage(1);
  }, [allSymbols, symbolDraft]);

  const removeSelectedSymbol = useCallback((symbol: string) => {
    setSelectedSymbols((current) => current.filter((entry) => entry !== symbol));
    setPage(1);
  }, []);

  const resetAllFilters = useCallback(() => {
    setNameFilter("");
    setSelectedStrategies([]);
    setSelectedSymbols([]);
    setSelectedTimeframes([]);
    setSortField("created_at");
    setSortOrder("desc");
    setPage(1);
    setNumericFilters(boundsToFilters(numericBounds));
  }, [numericBounds]);

  const handleBulkDelete = async () => {
    if (selectedCount === 0) return;
    const ids = [...selectedIds];
    const confirmed = window.confirm(`Delete ${ids.length} selected backtests?`);
    if (!confirmed) return;

    try {
      const result = await deleteBacktestsBulk(ids);
      showToast(`Deleted ${result.deleted} backtest(s).`, "success");
      await loadAllMetadata(true);
    } catch (error: unknown) {
      showToast(toErrorMessage(error, "Bulk delete failed."), "error");
    }
  };

  const handleCompare = () => {
    if (selectedCount !== 2) {
      showToast("Select exactly 2 backtests to compare.", "info");
      return;
    }
    const ids = [...selectedIds];
    navigate(`/backtests/compare?ids=${ids.join(",")}`);
  };

  const handleShowDuplicates = async () => {
    setLoadingDuplicates(true);
    try {
      const payload = await getBacktestDuplicates();
      setDuplicatesInfo(payload);
      setShowDuplicatesModal(true);
    } catch (error: unknown) {
      showToast(toErrorMessage(error, "Could not check duplicates."), "error");
    } finally {
      setLoadingDuplicates(false);
    }
  };

  const handleDeduplicate = async () => {
    setRunningDeduplicate(true);
    try {
      const payload = await deduplicateBacktests();
      showToast(`Removed ${payload.deleted_ids.length} duplicate backtest(s).`, "success");
      setShowDuplicatesModal(false);
      await loadAllMetadata(true);
    } catch (error: unknown) {
      showToast(toErrorMessage(error, "Could not remove duplicates."), "error");
    } finally {
      setRunningDeduplicate(false);
    }
  };

  const filterPanelContent = useMemo(() => (
    <div style={FILTER_PANEL_GRID_STYLE}>
      <div style={FILTER_PANEL_CONTROLS_STYLE}>
        <div style={{ minWidth: "220px" }}>
          <label style={{ display: "grid", gap: "6px", fontSize: "0.72rem", color: "var(--color-text-muted)" }}>
            Name
            <input
              value={nameFilter}
              onChange={(event) => {
                setNameFilter(event.target.value);
                setPage(1);
              }}
              placeholder="Partial name..."
              style={{
                borderRadius: "10px",
                border: "1px solid rgba(143, 154, 184, 0.28)",
                background: "rgba(13, 19, 32, 0.95)",
                color: "var(--color-text)",
                padding: "9px 10px",
                fontSize: "0.76rem",
              }}
            />
          </label>
        </div>

        <MultiSelectDropdown
          label="Strategy"
          options={allStrategies.map((value) => ({ label: value, value }))}
          selected={selectedStrategies}
          onChange={(next) => {
            setSelectedStrategies(next);
            setPage(1);
          }}
          searchable
          searchPlaceholder="Search strategy..."
          buttonWidth="100%"
        />

        <MultiSelectDropdown
          label="Timeframe"
          options={allTimeframes.map((value) => ({ label: value, value }))}
          selected={selectedTimeframes}
          onChange={(next) => {
            setSelectedTimeframes(next);
            setPage(1);
          }}
          buttonWidth="100%"
        />

        <div style={{ minWidth: "220px" }}>
          <label style={{ display: "grid", gap: "6px", fontSize: "0.72rem", color: "var(--color-text-muted)" }}>
            Symbol (autocomplete)
            <div style={{ display: "flex", gap: "6px" }}>
              <input
                list="backtests-symbols"
                value={symbolDraft}
                onChange={(event) => setSymbolDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    addSymbolDraft();
                  }
                }}
                placeholder="Type symbol..."
                style={{
                  flex: 1,
                  borderRadius: "10px",
                  border: "1px solid rgba(143, 154, 184, 0.28)",
                  background: "rgba(13, 19, 32, 0.95)",
                  color: "var(--color-text)",
                  padding: "9px 10px",
                  fontSize: "0.76rem",
                }}
              />
              <button
                type="button"
                onClick={addSymbolDraft}
                style={{
                  borderRadius: "10px",
                  border: "1px solid rgba(143, 154, 184, 0.34)",
                  background: "rgba(17, 24, 39, 0.95)",
                  color: "var(--color-text)",
                  padding: "8px 10px",
                  fontSize: "0.73rem",
                  cursor: "pointer",
                }}
              >
                Add
              </button>
            </div>
          </label>
          <datalist id="backtests-symbols">
            {allSymbols.map((value) => (
              <option key={value} value={value} />
            ))}
          </datalist>
          {selectedSymbols.length > 0 ? (
            <div style={{ display: "flex", gap: "6px", marginTop: "7px", flexWrap: "wrap" }}>
              {selectedSymbols.map((symbol) => (
                <button
                  key={symbol}
                  type="button"
                  onClick={() => removeSelectedSymbol(symbol)}
                  style={{
                    borderRadius: "999px",
                    border: "1px solid rgba(88,166,255,0.42)",
                    background: "rgba(88,166,255,0.14)",
                    color: "#dbeafe",
                    padding: "3px 8px",
                    fontSize: "0.68rem",
                    cursor: "pointer",
                  }}
                >
                  {symbol}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gap: "8px",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
        }}
      >
        <DualRangeSlider
          label="Sharpe (higher = better)"
          min={numericBounds.sharpe.min}
          max={numericBounds.sharpe.max}
          valueMin={numericFilters.sharpe.min}
          valueMax={numericFilters.sharpe.max}
          onChange={(min, max) => {
            setNumericFilters((current) => updateFilter(current, "sharpe", min, max));
            setPage(1);
          }}
          step={0.01}
        />
        <DualRangeSlider
          label="PSR (higher = better)"
          min={numericBounds.psr.min}
          max={numericBounds.psr.max}
          valueMin={numericFilters.psr.min}
          valueMax={numericFilters.psr.max}
          onChange={(min, max) => {
            setNumericFilters((current) => updateFilter(current, "psr", min, max));
            setPage(1);
          }}
          step={0.001}
        />
        <DualRangeSlider
          label="p-Value (lower = better)"
          min={numericBounds.pValue.min}
          max={numericBounds.pValue.max}
          valueMin={numericFilters.pValue.min}
          valueMax={numericFilters.pValue.max}
          onChange={(min, max) => {
            setNumericFilters((current) => updateFilter(current, "pValue", min, max));
            setPage(1);
          }}
          step={0.001}
        />
        <DualRangeSlider
          label="CAGR (higher = better)"
          min={numericBounds.cagr.min}
          max={numericBounds.cagr.max}
          valueMin={numericFilters.cagr.min}
          valueMax={numericFilters.cagr.max}
          onChange={(min, max) => {
            setNumericFilters((current) => updateFilter(current, "cagr", min, max));
            setPage(1);
          }}
          step={0.1}
          formatValue={(value) => `${value.toFixed(2)}%`}
        />
        <DualRangeSlider
          label="Max DD (lower = better)"
          min={numericBounds.maxDrawdown.min}
          max={numericBounds.maxDrawdown.max}
          valueMin={numericFilters.maxDrawdown.min}
          valueMax={numericFilters.maxDrawdown.max}
          onChange={(min, max) => {
            setNumericFilters((current) => updateFilter(current, "maxDrawdown", min, max));
            setPage(1);
          }}
          step={0.1}
          formatValue={(value) => `${value.toFixed(2)}%`}
        />
        <DualRangeSlider
          label="Total Return %"
          min={numericBounds.totalReturn.min}
          max={numericBounds.totalReturn.max}
          valueMin={numericFilters.totalReturn.min}
          valueMax={numericFilters.totalReturn.max}
          onChange={(min, max) => {
            setNumericFilters((current) => updateFilter(current, "totalReturn", min, max));
            setPage(1);
          }}
          step={0.1}
          formatValue={(value) => `${value.toFixed(2)}%`}
        />
        <DualRangeSlider
          label="Win Rate (higher = better)"
          min={numericBounds.winRate.min}
          max={numericBounds.winRate.max}
          valueMin={numericFilters.winRate.min}
          valueMax={numericFilters.winRate.max}
          onChange={(min, max) => {
            setNumericFilters((current) => updateFilter(current, "winRate", min, max));
            setPage(1);
          }}
          step={0.1}
          formatValue={(value) => `${value.toFixed(1)}%`}
        />
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button
          type="button"
          onClick={resetAllFilters}
          style={{
            borderRadius: "10px",
            border: "1px solid rgba(143, 154, 184, 0.3)",
            background: "rgba(13, 19, 32, 0.92)",
            color: "var(--color-text-muted)",
            padding: "8px 12px",
            fontSize: "0.74rem",
            cursor: "pointer",
          }}
        >
          Reset All
        </button>
      </div>
    </div>
  ), [
    addSymbolDraft,
    allStrategies,
    allSymbols,
    allTimeframes,
    numericBounds.cagr.max,
    numericBounds.cagr.min,
    numericBounds.maxDrawdown.max,
    numericBounds.maxDrawdown.min,
    numericBounds.pValue.max,
    numericBounds.pValue.min,
    numericBounds.psr.max,
    numericBounds.psr.min,
    numericBounds.sharpe.max,
    numericBounds.sharpe.min,
    numericBounds.totalReturn.max,
    numericBounds.totalReturn.min,
    numericBounds.winRate.max,
    numericBounds.winRate.min,
    numericFilters.cagr.max,
    numericFilters.cagr.min,
    numericFilters.maxDrawdown.max,
    numericFilters.maxDrawdown.min,
    numericFilters.pValue.max,
    numericFilters.pValue.min,
    numericFilters.psr.max,
    numericFilters.psr.min,
    numericFilters.sharpe.max,
    numericFilters.sharpe.min,
    numericFilters.totalReturn.max,
    numericFilters.totalReturn.min,
    numericFilters.winRate.max,
    numericFilters.winRate.min,
    nameFilter,
    removeSelectedSymbol,
    resetAllFilters,
    selectedStrategies,
    selectedSymbols,
    selectedTimeframes,
    symbolDraft,
  ]);

  const resultSummaryLabel = useMemo(
    () => `${filteredSorted.length} results · ${items.length} total backtests`,
    [filteredSorted.length, items.length],
  );

  return (
    <div style={{ padding: "20px 22px 120px", minHeight: "100%", background: "#0d1322" }}>
      <div
        style={{
          borderRadius: "18px",
          border: "1px solid rgba(90, 105, 138, 0.24)",
          background:
            "radial-gradient(900px 360px at 12% -18%, rgba(88,166,255,0.24), rgba(13,19,34,0)), #101729",
          padding: "16px",
          marginBottom: "14px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "10px",
            flexWrap: "wrap",
            marginBottom: "10px",
          }}
        >
          <div>
            <h1 style={{ margin: 0, fontSize: "1.35rem", fontWeight: 700 }}>Backtests</h1>
            <div style={{ marginTop: "4px", color: "var(--color-text-muted)", fontSize: "0.78rem" }}>
              Card layout with equity previews · server-side name filter · client-side metric filters
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={() => void handleShowDuplicates()}
              disabled={loadingDuplicates}
              style={{
                borderRadius: "10px",
                border: "1px solid rgba(248, 113, 113, 0.45)",
                background: "rgba(127, 29, 29, 0.22)",
                color: "#fecaca",
                padding: "8px 12px",
                fontSize: "0.76rem",
                cursor: loadingDuplicates ? "not-allowed" : "pointer",
                opacity: loadingDuplicates ? 0.6 : 1,
              }}
            >
              {loadingDuplicates ? "Checking..." : "Remove Duplicates"}
            </button>
            <button
              type="button"
              onClick={() => setShowCreateModal(true)}
              style={{
                borderRadius: "10px",
                border: "1px solid rgba(88, 166, 255, 0.5)",
                background: "rgba(88, 166, 255, 0.16)",
                color: "#dbeafe",
                padding: "8px 12px",
                fontSize: "0.76rem",
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
              }}
            >
              <Plus size={13} />
              New Backtest
            </button>
          </div>
        </div>

        <FilterBar
          searchValue={nameFilter}
          onSearchChange={(value) => {
            setNameFilter(value);
            setPage(1);
          }}
          searchPlaceholder="Name (server-side contains)..."
          sortOptions={BACKTEST_SORT_OPTIONS}
          sortValue={sortField}
          onSortChange={(value) => {
            setSortField(value as SortField);
            setPage(1);
          }}
          filterPanelContent={filterPanelContent}
          filterButtonLabel="Filters"
        />

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
          <div style={{ color: "var(--color-text-muted)", fontSize: "0.78rem" }}>
            {resultSummaryLabel}
          </div>
          <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={() => {
                setSortOrder((current) => (current === "desc" ? "asc" : "desc"));
                setPage(1);
              }}
              style={{
                borderRadius: "10px",
                border: "1px solid rgba(143, 154, 184, 0.34)",
                background: "rgba(17, 24, 39, 0.95)",
                color: "var(--color-text)",
                padding: "8px 10px",
                fontSize: "0.74rem",
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
              }}
            >
              <ArrowUpDown size={14} />
              {sortOrder === "desc" ? "Desc" : "Asc"}
            </button>
            <label style={SELECT_ALL_LABEL_STYLE}>
              <input
                type="checkbox"
                checked={allVisibleSelected}
                onChange={toggleSelectedVisible}
                style={{ cursor: "pointer", accentColor: "#58a6ff" }}
              />
              Select All (visible)
            </label>
          </div>
        </div>
      </div>

      {loading ? (
        <div
          style={{
            borderRadius: "14px",
            border: "1px solid rgba(90, 105, 138, 0.24)",
            background: "rgba(13, 19, 32, 0.92)",
            padding: "28px",
            textAlign: "center",
            color: "var(--color-text-muted)",
          }}
        >
          Loading backtests...
        </div>
      ) : currentPageItems.length === 0 ? (
        <div
          style={{
            borderRadius: "14px",
            border: "1px solid rgba(90, 105, 138, 0.24)",
            background: "rgba(13, 19, 32, 0.92)",
            padding: "28px",
            textAlign: "center",
            color: "var(--color-text-muted)",
          }}
        >
          No backtests for the current filter selection.
        </div>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
            gap: "16px",
          }}
        >
          {currentPageItems.map((item) => {
            const selected = selectedIds.has(item.id);
            const totalReturnPct = metricValue(item.total_return) * 100;
            const cagrPct = metricValue(item.cagr) * 100;
            const maxDrawdownPct = Math.abs(metricValue(item.max_drawdown)) * 100;
            const winRatePct = (normalizeWinRate(item.win_rate) ?? 0) * 100;
            const sparkline = item.equity_preview ?? [];

            return (
              <article
                key={item.id}
                onClick={() => navigate(`/backtests/${item.id}`)}
                style={{
                  position: "relative",
                  borderRadius: "12px",
                  border: selected
                    ? "1px solid rgba(88, 166, 255, 0.72)"
                    : "1px solid rgba(148, 163, 184, 0.26)",
                  background: "rgba(11, 18, 30, 0.96)",
                  boxShadow: selected
                    ? "0 16px 34px rgba(59, 130, 246, 0.2)"
                    : "0 12px 28px rgba(0, 0, 0, 0.34)",
                  padding: "16px",
                  cursor: "pointer",
                  transition: "transform 140ms ease, border-color 140ms ease, box-shadow 160ms ease",
                }}
                onMouseEnter={(event) => {
                  event.currentTarget.style.transform = "translateY(-2px)";
                  if (!selected) {
                    event.currentTarget.style.borderColor = "rgba(88, 166, 255, 0.46)";
                    event.currentTarget.style.boxShadow = "0 16px 34px rgba(0, 0, 0, 0.42)";
                  }
                }}
                onMouseLeave={(event) => {
                  event.currentTarget.style.transform = "translateY(0px)";
                  if (!selected) {
                    event.currentTarget.style.borderColor = "rgba(148, 163, 184, 0.26)";
                    event.currentTarget.style.boxShadow = "0 12px 28px rgba(0, 0, 0, 0.34)";
                  }
                }}
              >
                <input
                  type="checkbox"
                  checked={selected}
                  onClick={(event) => event.stopPropagation()}
                  onChange={() => toggleSelected(item.id)}
                  style={{
                    position: "absolute",
                    top: "12px",
                    left: "12px",
                    zIndex: 2,
                    accentColor: "#58a6ff",
                    cursor: "pointer",
                  }}
                />

                <div
                  style={{
                    height: "112px",
                    borderRadius: "10px",
                    border: "1px solid rgba(90, 105, 138, 0.2)",
                    background: "linear-gradient(180deg, rgba(18, 27, 44, 0.92), rgba(11, 17, 30, 0.92))",
                    padding: "10px 10px 2px",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    marginBottom: "12px",
                    marginInlineStart: "24px",
                  }}
                >
                  {sparkline.length > 1 ? (
                    <Sparkline data={sparkline} width={300} height={92} />
                  ) : (
                    <span style={{ color: "var(--color-text-muted)", fontSize: "0.72rem" }}>
                      No equity preview
                    </span>
                  )}
                </div>

                <div style={{ marginBottom: "12px" }}>
                  <div style={{ fontWeight: 800, fontSize: "1.02rem", marginBottom: "5px" }}>
                    {item.strategy_name}
                  </div>
                  <div
                    style={{
                      color: "var(--color-text-muted)",
                      fontSize: "0.76rem",
                      fontFamily: NUMERIC_FONT,
                      display: "flex",
                      gap: "6px",
                      flexWrap: "wrap",
                    }}
                  >
                    <span>{(item.symbols ?? []).join(", ") || "N/A"}</span>
                    <span>·</span>
                    <span>{item.timeframe || "N/A"}</span>
                  </div>
                  <div
                    style={{
                      marginTop: "6px",
                      fontSize: "0.72rem",
                      color: "#c7d2fe",
                      fontFamily: NUMERIC_FONT,
                      fontWeight: 600,
                    }}
                  >
                    Period: {formatPeriodDate(item.start_date)} → {formatPeriodDate(item.end_date)}
                  </div>
                  <div
                    style={{
                      marginTop: "3px",
                      fontSize: "0.67rem",
                      color: "var(--color-text-muted)",
                      fontFamily: NUMERIC_FONT,
                    }}
                  >
                    Created: {formatPeriodDate(item.created_at)}
                  </div>
                  {Array.isArray(item.tags) && item.tags.length > 0 ? (
                    <div
                      style={{
                        marginTop: "6px",
                        display: "flex",
                        gap: "5px",
                        flexWrap: "wrap",
                      }}
                    >
                      {item.tags.slice(0, 4).map((tag) => (
                        <span
                          key={`${item.id}-${tag}`}
                          style={{
                            borderRadius: "999px",
                            border: "1px solid rgba(167, 139, 250, 0.45)",
                            background: "rgba(91, 33, 182, 0.24)",
                            color: "#ddd6fe",
                            padding: "2px 8px",
                            fontSize: "0.66rem",
                            fontFamily: NUMERIC_FONT,
                          }}
                        >
                          {tag}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "8px" }}>
                  <StatBadge label="Return" value={percentage(totalReturnPct)} color={statColorPositive(totalReturnPct)} />
                  <StatBadge
                    label="Sharpe (higher = better)"
                    value={formatNumber(item.sharpe_ratio)}
                    color={sharpeColor(item.sharpe_ratio)}
                  />
                  <StatBadge
                    label="PSR (higher = better)"
                    value={item.psr == null ? "N/A" : `${(item.psr * 100).toFixed(1)}%`}
                    color={psrColor(item.psr)}
                  />
                  <StatBadge label="CAGR (higher = better)" value={percentage(cagrPct)} color={statColorPositive(cagrPct)} />
                  <StatBadge label="Max DD (lower = better)" value={percentage(-maxDrawdownPct)} color="#f87171" />
                  <StatBadge
                    label="Win Rate (higher = better)"
                    value={`${winRatePct.toFixed(1)}%`}
                    color={statColorPositive(winRatePct)}
                  />
                </div>

                <div
                  style={{
                    marginTop: "10px",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: "8px",
                    flexWrap: "wrap",
                  }}
                >
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      borderRadius: "999px",
                      border: "1px solid rgba(147, 197, 253, 0.32)",
                      background: "rgba(30, 58, 138, 0.28)",
                      color: "#dbeafe",
                      padding: "5px 10px",
                      fontSize: "0.72rem",
                      fontFamily: NUMERIC_FONT,
                    }}
                  >
                    Trades {item.trade_count ?? 0}
                  </span>
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      borderRadius: "999px",
                      border: "1px solid rgba(148, 163, 184, 0.3)",
                      background: "rgba(15, 23, 42, 0.9)",
                      color: pValueColor(item.p_value),
                      padding: "5px 10px",
                      fontSize: "0.72rem",
                      fontFamily: NUMERIC_FONT,
                    }}
                  >
                    p-Value (lower = better) {formatPValue(item.p_value)}
                  </span>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <div
        style={{
          marginTop: "12px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "10px",
          flexWrap: "wrap",
        }}
      >
        <span style={{ color: "var(--color-text-muted)", fontSize: "0.76rem" }}>
          Page {page} of {totalPages}
        </span>

        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
          <label style={{ fontSize: "0.74rem", color: "var(--color-text-muted)" }}>
            Per Page{" "}
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
                background: "#111827",
                color: "var(--color-text)",
                padding: "6px 8px",
                fontSize: "0.74rem",
              }}
            >
              <option value={12}>12</option>
              <option value={24}>24</option>
              <option value={48}>48</option>
            </select>
          </label>

          <PagerButton onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={page <= 1}>
            {"<"}
          </PagerButton>
          {buildPageNumbers(page, totalPages).map((entry, index) =>
            typeof entry === "number" ? (
              <PagerButton key={`page-${entry}`} onClick={() => setPage(entry)} active={entry === page}>
                {entry}
              </PagerButton>
            ) : (
              <span key={`ellipsis-${index}`} style={{ color: "var(--color-text-muted)" }}>
                ...
              </span>
            ),
          )}
          <PagerButton onClick={() => setPage((current) => Math.min(totalPages, current + 1))} disabled={page >= totalPages}>
            {">"}
          </PagerButton>
        </div>
      </div>

      {selectedCount > 0 ? (
        <div
          style={BULK_BAR_STYLE}
        >
          <span style={{ fontSize: "0.76rem", color: "var(--color-text-muted)" }}>
            {selectedCount} selected
          </span>
          <button
            type="button"
            onClick={() => void handleBulkDelete()}
            style={{
              borderRadius: "999px",
              border: "1px solid rgba(248, 113, 113, 0.45)",
              background: "rgba(127, 29, 29, 0.28)",
              color: "#fecaca",
              padding: "7px 11px",
              fontSize: "0.74rem",
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              gap: "6px",
            }}
          >
            <Trash2 size={13} />
            Delete Selected
          </button>
          {selectedCount === 2 ? (
            <button
              type="button"
              onClick={handleCompare}
              style={{
                borderRadius: "999px",
                border: "1px solid rgba(147, 197, 253, 0.5)",
                background: "rgba(30, 58, 138, 0.3)",
                color: "#dbeafe",
                padding: "7px 11px",
                fontSize: "0.74rem",
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
              }}
            >
              <Scale size={13} />
              Compare Selected
            </button>
          ) : null}
        </div>
      ) : null}

      <Modal
        open={showDuplicatesModal}
        onClose={() => {
          if (runningDeduplicate) return;
          setShowDuplicatesModal(false);
        }}
        title="Remove Backtest Duplicates"
        width="520px"
      >
        <div style={{ display: "grid", gap: "14px" }}>
          <div style={{ color: "var(--color-text-muted)", fontSize: "0.84rem", lineHeight: 1.45 }}>
            Found <b>{duplicatesInfo?.total_groups ?? 0}</b> duplicate group(s) across{" "}
            <b>{duplicatesInfo?.total_backtests ?? 0}</b> backtests.
          </div>
          {duplicatesInfo && duplicatesInfo.total_groups > 0 ? (
            <div
              style={{
                maxHeight: "180px",
                overflowY: "auto",
                border: "1px solid var(--color-border)",
                borderRadius: "8px",
                padding: "8px 10px",
                fontSize: "0.76rem",
                color: "var(--color-text-muted)",
              }}
            >
              {duplicatesInfo.groups.slice(0, 6).map((group) => (
                <div key={group.fingerprint} style={{ padding: "5px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
                  {group.strategy_name || "Unknown"} · {(group.symbols || []).join(", ") || "N/A"} · {group.timeframe || "N/A"} ·{" "}
                  {group.backtests.length} entries
                </div>
              ))}
            </div>
          ) : null}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
            <button
              type="button"
              onClick={() => setShowDuplicatesModal(false)}
              disabled={runningDeduplicate}
              style={{
                borderRadius: "8px",
                border: "1px solid var(--color-border)",
                background: "transparent",
                color: "var(--color-text-muted)",
                padding: "8px 12px",
                cursor: runningDeduplicate ? "not-allowed" : "pointer",
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleDeduplicate()}
              disabled={runningDeduplicate || (duplicatesInfo?.total_groups ?? 0) === 0}
              style={{
                borderRadius: "8px",
                border: "1px solid rgba(248,113,113,0.4)",
                background: "rgba(127,29,29,0.24)",
                color: "#fecaca",
                padding: "8px 12px",
                cursor: runningDeduplicate ? "not-allowed" : "pointer",
                opacity: runningDeduplicate || (duplicatesInfo?.total_groups ?? 0) === 0 ? 0.6 : 1,
              }}
            >
              {runningDeduplicate ? "Removing..." : "Confirm Remove Duplicates"}
            </button>
          </div>
        </div>
      </Modal>

      <CreateBacktestModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onTaskStarted={() => setShowCreateModal(false)}
      />
    </div>
  );
}

const StatBadge = memo(function StatBadge({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color: string;
}) {
  return (
    <div
      style={{
        borderRadius: "8px",
        border: "1px solid rgba(148, 163, 184, 0.24)",
        background: "rgba(15, 23, 42, 0.9)",
        padding: "7px 9px",
        fontFamily: NUMERIC_FONT,
        fontSize: "0.72rem",
      }}
    >
      <div style={{ color: "var(--color-text-muted)", marginBottom: "3px", fontSize: "0.67rem" }}>{label}</div>
      <div style={{ color, fontWeight: 700 }}>{value}</div>
    </div>
  );
});

function PagerButton({
  children,
  onClick,
  disabled = false,
  active = false,
}: {
  children: string | number;
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
        border: active ? "1px solid rgba(88, 166, 255, 0.66)" : "1px solid rgba(148, 163, 184, 0.24)",
        background: active ? "rgba(88, 166, 255, 0.2)" : "rgba(15, 23, 42, 0.96)",
        color: active ? "#dbeafe" : "var(--color-text)",
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
