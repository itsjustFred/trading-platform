import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowUpDown, Plus, RefreshCw, Trash2, X } from "lucide-react";

import {
  deduplicateBacktests,
  deleteBacktestsBulk,
  getBacktestsAll,
  getBacktestDuplicates,
  getBacktestEquityPreview,
} from "../api/client";
import type { BacktestMetadataItem } from "../api/types";
import { MultiSelectDropdown } from "../components/MultiSelectDropdown";
import { CreateBacktestModal } from "../components/CreateBacktestModal";
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

interface RangeFilters {
  totalReturnMin: string;
  totalReturnMax: string;
  sharpeMin: string;
  sharpeMax: string;
  psrMin: string;
  psrMax: string;
  pValueMin: string;
  pValueMax: string;
  cagrMin: string;
  cagrMax: string;
  maxDrawdownMin: string;
  maxDrawdownMax: string;
  winRateMin: string;
  winRateMax: string;
  tradesMin: string;
  tradesMax: string;
}

const NUMERIC_FONT = '"SFMono-Regular", "Cascadia Code", "Roboto Mono", ui-monospace, monospace';
const DEFAULT_PER_PAGE = 20;

const SORT_OPTIONS: Array<{ value: SortField; label: string }> = [
  { value: "created_at", label: "Created (newest)" },
  { value: "total_return", label: "Total Return" },
  { value: "sharpe_ratio", label: "Sharpe" },
  { value: "psr", label: "PSR" },
  { value: "p_value", label: "p-Value" },
  { value: "cagr", label: "CAGR" },
  { value: "max_drawdown", label: "Max Drawdown" },
  { value: "win_rate", label: "Win Rate" },
  { value: "trade_count", label: "Trades" },
];

let backtestsAllCache: BacktestMetadataItem[] | null = null;
let backtestsAllPromise: Promise<BacktestMetadataItem[]> | null = null;

function cloneMetadataRows(rows: BacktestMetadataItem[]): BacktestMetadataItem[] {
  return rows.map((row) => ({
    ...row,
    symbols: Array.isArray(row.symbols) ? [...row.symbols] : [],
  }));
}

async function loadBacktestsAllCached(force = false): Promise<BacktestMetadataItem[]> {
  if (force) {
    backtestsAllCache = null;
    backtestsAllPromise = null;
  }
  if (backtestsAllCache) {
    return cloneMetadataRows(backtestsAllCache);
  }
  if (!backtestsAllPromise) {
    backtestsAllPromise = getBacktestsAll()
      .then((rows) => {
        backtestsAllCache = cloneMetadataRows(rows ?? []);
        return cloneMetadataRows(backtestsAllCache);
      })
      .finally(() => {
        backtestsAllPromise = null;
      });
  }
  return backtestsAllPromise;
}

function parseOptionalNumber(value: string): number | null {
  const normalized = value.trim();
  if (!normalized) return null;
  const numeric = Number(normalized);
  return Number.isFinite(numeric) ? numeric : null;
}

function normalizeWinRate(value: number | null): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  if (value > 1 && value <= 100) return value / 100;
  if (value < 0) return 0;
  if (value > 1) return 1;
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

function pValueLabel(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "N/A";
  return value.toFixed(4);
}

function statColorPositive(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--color-text-muted)";
  return value >= 0 ? "#3fb950" : "#f85149";
}

function sharpeColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--color-text-muted)";
  if (value >= 1) return "#3fb950";
  if (value >= 0) return "#d4b263";
  return "#f85149";
}

function psrColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--color-text-muted)";
  if (value >= 0.95) return "#3fb950";
  if (value >= 0.5) return "#d4b263";
  return "#f85149";
}

function pValueColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--color-text-muted)";
  return value <= 0.05 ? "#3fb950" : "#f85149";
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

function rangePasses(value: number, min: number | null, max: number | null): boolean {
  if (min != null && value < min) return false;
  if (max != null && value > max) return false;
  return true;
}

function toErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  return fallback;
}

function initialRanges(): RangeFilters {
  return {
    totalReturnMin: "",
    totalReturnMax: "",
    sharpeMin: "",
    sharpeMax: "",
    psrMin: "",
    psrMax: "",
    pValueMin: "",
    pValueMax: "",
    cagrMin: "",
    cagrMax: "",
    maxDrawdownMin: "",
    maxDrawdownMax: "",
    winRateMin: "",
    winRateMax: "",
    tradesMin: "",
    tradesMax: "",
  };
}

export function BacktestsPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const previewRequestsRef = useRef<Set<string>>(new Set());

  const [items, setItems] = useState<BacktestMetadataItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [deduplicating, setDeduplicating] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [search, setSearch] = useState("");
  const [strategies, setStrategies] = useState<string[]>([]);
  const [selectedStrategies, setSelectedStrategies] = useState<string[]>([]);
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([]);
  const [selectedTimeframes, setSelectedTimeframes] = useState<string[]>([]);
  const [symbolDraft, setSymbolDraft] = useState("");
  const [ranges, setRanges] = useState<RangeFilters>(() => initialRanges());
  const [sortField, setSortField] = useState<SortField>("created_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(DEFAULT_PER_PAGE);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [equityPreviews, setEquityPreviews] = useState<Record<string, number[]>>({});

  const allSymbols = useMemo(
    () =>
      [...new Set(items.flatMap((item) => item.symbols ?? []).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b)),
    [items],
  );

  const allTimeframes = useMemo(
    () => [...new Set(items.map((item) => item.timeframe).filter(Boolean))].sort((a, b) => a.localeCompare(b)),
    [items],
  );

  useEffect(() => {
    setStrategies(
      [...new Set(items.map((item) => item.strategy_name).filter(Boolean))].sort((a, b) => a.localeCompare(b)),
    );
  }, [items]);

  const loadAllMetadata = useCallback(
    async (force = false) => {
      setLoading(true);
      try {
        const payload = await loadBacktestsAllCached(force);
        setItems(payload ?? []);
        setSelectedIds(new Set());
        if (force) {
          setEquityPreviews({});
        }
      } catch (error: unknown) {
        showToast(toErrorMessage(error, "Failed to load backtests metadata."), "error");
      } finally {
        setLoading(false);
      }
    },
    [showToast],
  );

  useEffect(() => {
    void loadAllMetadata(false);
  }, [loadAllMetadata]);

  const parsedRanges = useMemo(
    () => ({
      totalReturnMin: parseOptionalNumber(ranges.totalReturnMin),
      totalReturnMax: parseOptionalNumber(ranges.totalReturnMax),
      sharpeMin: parseOptionalNumber(ranges.sharpeMin),
      sharpeMax: parseOptionalNumber(ranges.sharpeMax),
      psrMin: parseOptionalNumber(ranges.psrMin),
      psrMax: parseOptionalNumber(ranges.psrMax),
      pValueMin: parseOptionalNumber(ranges.pValueMin),
      pValueMax: parseOptionalNumber(ranges.pValueMax),
      cagrMin: parseOptionalNumber(ranges.cagrMin),
      cagrMax: parseOptionalNumber(ranges.cagrMax),
      maxDrawdownMin: parseOptionalNumber(ranges.maxDrawdownMin),
      maxDrawdownMax: parseOptionalNumber(ranges.maxDrawdownMax),
      winRateMin: parseOptionalNumber(ranges.winRateMin),
      winRateMax: parseOptionalNumber(ranges.winRateMax),
      tradesMin: parseOptionalNumber(ranges.tradesMin),
      tradesMax: parseOptionalNumber(ranges.tradesMax),
    }),
    [ranges],
  );

  const filteredSorted = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const output = items.filter((item) => {
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
      if (needle) {
        const haystack = `${item.id} ${item.strategy_name} ${(item.symbols ?? []).join(" ")} ${item.timeframe}`.toLowerCase();
        if (!haystack.includes(needle)) return false;
      }

      const totalReturnPct = (item.total_return ?? 0) * 100;
      const sharpe = item.sharpe_ratio ?? 0;
      const psr = item.psr ?? 0;
      const pValue = item.p_value ?? 1;
      const cagrPct = (item.cagr ?? 0) * 100;
      const maxDrawdownPct = Math.abs((item.max_drawdown ?? 0) * 100);
      const winRatePct = (normalizeWinRate(item.win_rate) ?? 0) * 100;
      const trades = item.trade_count ?? 0;

      return (
        rangePasses(totalReturnPct, parsedRanges.totalReturnMin, parsedRanges.totalReturnMax) &&
        rangePasses(sharpe, parsedRanges.sharpeMin, parsedRanges.sharpeMax) &&
        rangePasses(psr, parsedRanges.psrMin, parsedRanges.psrMax) &&
        rangePasses(pValue, parsedRanges.pValueMin, parsedRanges.pValueMax) &&
        rangePasses(cagrPct, parsedRanges.cagrMin, parsedRanges.cagrMax) &&
        rangePasses(maxDrawdownPct, parsedRanges.maxDrawdownMin, parsedRanges.maxDrawdownMax) &&
        rangePasses(winRatePct, parsedRanges.winRateMin, parsedRanges.winRateMax) &&
        rangePasses(trades, parsedRanges.tradesMin, parsedRanges.tradesMax)
      );
    });

    output.sort((left, right) => {
      const direction = sortOrder === "asc" ? 1 : -1;
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
    return output;
  }, [
    items,
    search,
    parsedRanges,
    selectedStrategies,
    selectedSymbols,
    selectedTimeframes,
    sortField,
    sortOrder,
  ]);

  const totalPages = Math.max(1, Math.ceil(filteredSorted.length / perPage));
  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  const currentPageItems = useMemo(() => {
    const start = (page - 1) * perPage;
    return filteredSorted.slice(start, start + perPage);
  }, [filteredSorted, page, perPage]);

  useEffect(() => {
    const missing = currentPageItems
      .map((item) => item.id)
      .filter((id) => equityPreviews[id] == null && !previewRequestsRef.current.has(id));
    if (missing.length === 0) return;

    let cancelled = false;
    missing.forEach((id) => previewRequestsRef.current.add(id));

    const fetchPreviews = async () => {
      const updates: Record<string, number[]> = {};
      await Promise.all(
        missing.map(async (id) => {
          try {
            updates[id] = await getBacktestEquityPreview(id, 80);
          } catch {
            updates[id] = [];
          } finally {
            previewRequestsRef.current.delete(id);
          }
        }),
      );
      if (!cancelled) {
        setEquityPreviews((current) => ({ ...current, ...updates }));
      }
    };
    void fetchPreviews();

    return () => {
      cancelled = true;
    };
  }, [currentPageItems, equityPreviews]);

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
  const allOnPageSelected =
    currentPageItems.length > 0 && currentPageItems.every((item) => selectedIds.has(item.id));

  const toggleSelected = (id: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectedOnPage = () => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (allOnPageSelected) {
        currentPageItems.forEach((item) => next.delete(item.id));
      } else {
        currentPageItems.forEach((item) => next.add(item.id));
      }
      return next;
    });
  };

  const resetFilters = () => {
    setSearch("");
    setSelectedStrategies([]);
    setSelectedSymbols([]);
    setSelectedTimeframes([]);
    setRanges(initialRanges());
    setSortField("created_at");
    setSortOrder("desc");
    setPage(1);
  };

  const addDraftSymbol = () => {
    const normalized = symbolDraft.trim().toUpperCase();
    if (!normalized) return;
    if (!allSymbols.some((entry) => entry.toUpperCase() === normalized)) return;
    setSelectedSymbols((current) => (current.includes(normalized) ? current : [...current, normalized]));
    setSymbolDraft("");
    setPage(1);
  };

  const removeSelectedSymbol = (value: string) => {
    setSelectedSymbols((current) => current.filter((entry) => entry !== value));
    setPage(1);
  };

  const handleBulkDelete = async () => {
    if (selectedCount === 0) return;
    const ids = [...selectedIds];
    const confirmed = window.confirm(`Delete ${ids.length} selected backtests?`);
    if (!confirmed) return;

    try {
      const payload = await deleteBacktestsBulk(ids);
      showToast(`Deleted ${payload.deleted} backtest(s).`, "success");
      await loadAllMetadata(true);
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
        `Found ${duplicates.total_groups} duplicate groups. Remove older duplicates and keep newest?`,
      );
      if (!confirmed) return;
      const result = await deduplicateBacktests();
      showToast(
        `Removed ${result.deleted_ids.length} duplicate backtests across ${result.groups_affected} groups.`,
        "success",
      );
      await loadAllMetadata(true);
    } catch (error: unknown) {
      showToast(toErrorMessage(error, "Deduplication failed."), "error");
    } finally {
      setDeduplicating(false);
    }
  };

  return (
    <div style={{ padding: "20px 22px 80px", minHeight: "100%", background: "#0e1220" }}>
      <div
        style={{
          borderRadius: "16px",
          border: "1px solid rgba(90, 105, 138, 0.26)",
          background:
            "radial-gradient(1100px 440px at 18% -14%, rgba(88,166,255,0.22), rgba(14,18,32,0)), #111728",
          padding: "16px",
          marginBottom: "14px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "12px",
            flexWrap: "wrap",
            marginBottom: "12px",
          }}
        >
          <div>
            <h1 style={{ margin: 0, fontSize: "1.35rem", fontWeight: 700 }}>Backtests</h1>
            <div style={{ marginTop: "4px", color: "var(--color-text-muted)", fontSize: "0.78rem" }}>
              Fast cards view · one metadata request · client-side filters and sorting
            </div>
          </div>

          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
            {selectedCount > 0 ? (
              <button
                type="button"
                onClick={() => void handleBulkDelete()}
                style={{
                  borderRadius: "10px",
                  border: "1px solid rgba(248, 81, 73, 0.45)",
                  background: "rgba(248, 81, 73, 0.16)",
                  color: "#fecaca",
                  padding: "8px 12px",
                  fontSize: "0.76rem",
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                }}
              >
                <Trash2 size={13} />
                Delete Selected ({selectedCount})
              </button>
            ) : null}

            <button
              type="button"
              onClick={() => void handleDeduplicate()}
              disabled={deduplicating}
              style={{
                borderRadius: "10px",
                border: "1px solid rgba(251, 191, 36, 0.42)",
                background: "rgba(251, 191, 36, 0.12)",
                color: "#fde68a",
                padding: "8px 12px",
                fontSize: "0.76rem",
                cursor: deduplicating ? "not-allowed" : "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
              }}
            >
              <RefreshCw size={13} className={deduplicating ? "animate-spin" : ""} />
              {deduplicating ? "Removing..." : "Remove Duplicates"}
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

        <div style={{ display: "flex", flexWrap: "wrap", gap: "10px", marginBottom: "10px" }}>
          <MultiSelectDropdown
            label="Strategy"
            options={strategies.map((value) => ({ label: value, value }))}
            selected={selectedStrategies}
            onChange={(next) => {
              setSelectedStrategies(next);
              setPage(1);
            }}
            searchable
            searchPlaceholder="Search strategy..."
          />

          <MultiSelectDropdown
            label="Timeframe"
            options={allTimeframes.map((value) => ({ label: value, value }))}
            selected={selectedTimeframes}
            onChange={(next) => {
              setSelectedTimeframes(next);
              setPage(1);
            }}
          />

          <div style={{ minWidth: "250px", flex: "1 1 250px", maxWidth: "320px" }}>
            <label
              style={{
                display: "grid",
                gap: "6px",
                fontSize: "0.72rem",
                color: "var(--color-text-muted)",
              }}
            >
              Symbol (autocomplete)
              <div style={{ display: "flex", gap: "6px" }}>
                <input
                  list="backtest-symbol-options"
                  value={symbolDraft}
                  onChange={(event) => setSymbolDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      addDraftSymbol();
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
                  onClick={addDraftSymbol}
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
            <datalist id="backtest-symbol-options">
              {allSymbols.map((value) => (
                <option key={value} value={value} />
              ))}
            </datalist>
            {selectedSymbols.length > 0 ? (
              <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "6px" }}>
                {selectedSymbols.map((symbol) => (
                  <button
                    type="button"
                    key={symbol}
                    onClick={() => removeSelectedSymbol(symbol)}
                    style={{
                      borderRadius: "999px",
                      border: "1px solid rgba(88,166,255,0.42)",
                      background: "rgba(88,166,255,0.14)",
                      color: "#dbeafe",
                      padding: "3px 8px",
                      fontSize: "0.68rem",
                      cursor: "pointer",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "5px",
                    }}
                  >
                    {symbol}
                    <X size={12} />
                  </button>
                ))}
              </div>
            ) : null}
          </div>

          <div style={{ minWidth: "220px", flex: "1 1 220px", maxWidth: "320px" }}>
            <label style={{ display: "grid", gap: "6px", fontSize: "0.72rem", color: "var(--color-text-muted)" }}>
              Quick Search
              <input
                type="text"
                value={search}
                onChange={(event) => {
                  setSearch(event.target.value);
                  setPage(1);
                }}
                placeholder="Strategy, symbol or id..."
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

          <div style={{ minWidth: "250px", display: "grid", gap: "6px" }}>
            <span style={{ fontSize: "0.72rem", color: "var(--color-text-muted)" }}>Sort</span>
            <div style={{ display: "flex", gap: "6px" }}>
              <select
                value={sortField}
                onChange={(event) => {
                  setSortField(event.target.value as SortField);
                  setPage(1);
                }}
                style={{
                  flex: 1,
                  borderRadius: "10px",
                  border: "1px solid rgba(143, 154, 184, 0.28)",
                  background: "rgba(13, 19, 32, 0.95)",
                  color: "var(--color-text)",
                  padding: "9px 10px",
                  fontSize: "0.76rem",
                }}
              >
                {SORT_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>

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
            </div>
          </div>

          <button
            type="button"
            onClick={resetFilters}
            style={{
              alignSelf: "end",
              borderRadius: "10px",
              border: "1px solid rgba(143, 154, 184, 0.3)",
              background: "rgba(13, 19, 32, 0.92)",
              color: "var(--color-text-muted)",
              padding: "9px 12px",
              fontSize: "0.74rem",
              cursor: "pointer",
            }}
          >
            Reset
          </button>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
            gap: "8px",
          }}
        >
          <RangePair label="Total Return %" min={ranges.totalReturnMin} max={ranges.totalReturnMax} onMinChange={(value) => setRanges((current) => ({ ...current, totalReturnMin: value }))} onMaxChange={(value) => setRanges((current) => ({ ...current, totalReturnMax: value }))} />
          <RangePair label="Sharpe" min={ranges.sharpeMin} max={ranges.sharpeMax} onMinChange={(value) => setRanges((current) => ({ ...current, sharpeMin: value }))} onMaxChange={(value) => setRanges((current) => ({ ...current, sharpeMax: value }))} />
          <RangePair label="PSR" min={ranges.psrMin} max={ranges.psrMax} onMinChange={(value) => setRanges((current) => ({ ...current, psrMin: value }))} onMaxChange={(value) => setRanges((current) => ({ ...current, psrMax: value }))} />
          <RangePair label="p-Value" min={ranges.pValueMin} max={ranges.pValueMax} onMinChange={(value) => setRanges((current) => ({ ...current, pValueMin: value }))} onMaxChange={(value) => setRanges((current) => ({ ...current, pValueMax: value }))} />
          <RangePair label="CAGR %" min={ranges.cagrMin} max={ranges.cagrMax} onMinChange={(value) => setRanges((current) => ({ ...current, cagrMin: value }))} onMaxChange={(value) => setRanges((current) => ({ ...current, cagrMax: value }))} />
          <RangePair label="Max DD %" min={ranges.maxDrawdownMin} max={ranges.maxDrawdownMax} onMinChange={(value) => setRanges((current) => ({ ...current, maxDrawdownMin: value }))} onMaxChange={(value) => setRanges((current) => ({ ...current, maxDrawdownMax: value }))} />
          <RangePair label="Win Rate %" min={ranges.winRateMin} max={ranges.winRateMax} onMinChange={(value) => setRanges((current) => ({ ...current, winRateMin: value }))} onMaxChange={(value) => setRanges((current) => ({ ...current, winRateMax: value }))} />
          <RangePair label="Trades" min={ranges.tradesMin} max={ranges.tradesMax} onMinChange={(value) => setRanges((current) => ({ ...current, tradesMin: value }))} onMaxChange={(value) => setRanges((current) => ({ ...current, tradesMax: value }))} />
        </div>
      </div>

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "10px",
          marginBottom: "10px",
          flexWrap: "wrap",
        }}
      >
        <div style={{ color: "var(--color-text-muted)", fontSize: "0.78rem" }}>
          {filteredSorted.length} results · {items.length} total backtests
        </div>
        <label style={{ display: "inline-flex", gap: "6px", alignItems: "center", fontSize: "0.76rem", color: "var(--color-text-muted)" }}>
          <input
            type="checkbox"
            checked={allOnPageSelected}
            onChange={toggleSelectedOnPage}
            style={{ cursor: "pointer", accentColor: "#58a6ff" }}
          />
          Select page
        </label>
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
            gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
            gap: "12px",
          }}
        >
          {currentPageItems.map((item) => {
            const selected = selectedIds.has(item.id);
            const totalReturnPct = (item.total_return ?? 0) * 100;
            const cagrPct = (item.cagr ?? 0) * 100;
            const drawdownPct = Math.abs((item.max_drawdown ?? 0) * 100);
            const winRate = normalizeWinRate(item.win_rate);
            const winRatePct = winRate == null ? null : winRate * 100;
            const sparkline = equityPreviews[item.id];

            return (
              <article
                key={item.id}
                onClick={() => navigate(`/backtests/${item.id}`)}
                style={{
                  position: "relative",
                  borderRadius: "14px",
                  border: selected
                    ? "1px solid rgba(88,166,255,0.7)"
                    : "1px solid rgba(90, 105, 138, 0.24)",
                  background: "linear-gradient(180deg, rgba(17,24,39,0.97), rgba(9,13,24,0.97))",
                  boxShadow: selected
                    ? "0 16px 36px rgba(59,130,246,0.18)"
                    : "0 10px 26px rgba(0,0,0,0.35)",
                  padding: "12px",
                  cursor: "pointer",
                  transition: "transform 140ms ease, border-color 140ms ease, box-shadow 160ms ease",
                }}
                onMouseEnter={(event) => {
                  event.currentTarget.style.transform = "translateY(-2px)";
                  if (!selected) {
                    event.currentTarget.style.borderColor = "rgba(88,166,255,0.45)";
                    event.currentTarget.style.boxShadow = "0 16px 30px rgba(0,0,0,0.42)";
                  }
                }}
                onMouseLeave={(event) => {
                  event.currentTarget.style.transform = "translateY(0px)";
                  if (!selected) {
                    event.currentTarget.style.borderColor = "rgba(90, 105, 138, 0.24)";
                    event.currentTarget.style.boxShadow = "0 10px 26px rgba(0,0,0,0.35)";
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
                    top: "10px",
                    left: "10px",
                    zIndex: 2,
                    accentColor: "#58a6ff",
                    cursor: "pointer",
                  }}
                />

                <div
                  style={{
                    height: "64px",
                    borderRadius: "10px",
                    border: "1px solid rgba(90, 105, 138, 0.2)",
                    background: "rgba(15, 23, 42, 0.9)",
                    padding: "3px 8px 0",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    marginBottom: "10px",
                    marginInlineStart: "18px",
                  }}
                >
                  {sparkline && sparkline.length > 1 ? (
                    <Sparkline data={sparkline} width={220} height={56} />
                  ) : (
                    <span style={{ color: "var(--color-text-muted)", fontSize: "0.72rem" }}>
                      Loading equity preview...
                    </span>
                  )}
                </div>

                <div style={{ marginBottom: "10px" }}>
                  <div style={{ fontWeight: 700, fontSize: "0.95rem", marginBottom: "4px" }}>{item.strategy_name}</div>
                  <div
                    style={{
                      color: "var(--color-text-muted)",
                      fontSize: "0.74rem",
                      fontFamily: NUMERIC_FONT,
                      display: "flex",
                      gap: "8px",
                      flexWrap: "wrap",
                    }}
                  >
                    <span>{(item.symbols ?? []).join(", ") || "N/A"}</span>
                    <span>·</span>
                    <span>{item.timeframe || "N/A"}</span>
                  </div>
                </div>

                <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
                  <StatBadge label="Return" value={percentage(totalReturnPct)} color={statColorPositive(totalReturnPct)} />
                  <StatBadge label="Sharpe" value={formatNumber(item.sharpe_ratio)} color={sharpeColor(item.sharpe_ratio)} />
                  <StatBadge label="PSR" value={item.psr == null ? "N/A" : `${(item.psr * 100).toFixed(1)}%`} color={psrColor(item.psr)} />
                  <StatBadge label="p-Value" value={pValueLabel(item.p_value)} color={pValueColor(item.p_value)} />
                  <StatBadge label="CAGR" value={percentage(cagrPct)} color={statColorPositive(cagrPct)} />
                  <StatBadge label="Trades" value={String(item.trade_count ?? 0)} color="#93c5fd" />
                  <StatBadge label="Win Rate" value={winRatePct == null ? "N/A" : `${winRatePct.toFixed(1)}%`} color={statColorPositive(winRatePct)} />
                  <StatBadge label="Max DD" value={percentage(-drawdownPct)} color="#f87171" />
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
            Cards per page{" "}
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
              <option value={20}>20</option>
              <option value={40}>40</option>
              <option value={80}>80</option>
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

      <CreateBacktestModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onTaskStarted={() => setShowCreateModal(false)}
      />
    </div>
  );
}

function RangePair({
  label,
  min,
  max,
  onMinChange,
  onMaxChange,
}: {
  label: string;
  min: string;
  max: string;
  onMinChange: (value: string) => void;
  onMaxChange: (value: string) => void;
}) {
  return (
    <div
      style={{
        borderRadius: "10px",
        border: "1px solid rgba(90, 105, 138, 0.25)",
        background: "rgba(13, 19, 32, 0.92)",
        padding: "8px",
        display: "grid",
        gap: "5px",
      }}
    >
      <span style={{ fontSize: "0.7rem", color: "var(--color-text-muted)" }}>{label}</span>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px" }}>
        <input
          type="text"
          inputMode="decimal"
          placeholder="min"
          value={min}
          onChange={(event) => onMinChange(event.target.value)}
          style={{
            borderRadius: "8px",
            border: "1px solid rgba(143, 154, 184, 0.26)",
            background: "#101726",
            color: "var(--color-text)",
            padding: "6px 8px",
            fontSize: "0.73rem",
            fontFamily: NUMERIC_FONT,
          }}
        />
        <input
          type="text"
          inputMode="decimal"
          placeholder="max"
          value={max}
          onChange={(event) => onMaxChange(event.target.value)}
          style={{
            borderRadius: "8px",
            border: "1px solid rgba(143, 154, 184, 0.26)",
            background: "#101726",
            color: "var(--color-text)",
            padding: "6px 8px",
            fontSize: "0.73rem",
            fontFamily: NUMERIC_FONT,
          }}
        />
      </div>
    </div>
  );
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
        background: "rgba(11, 18, 30, 0.9)",
        padding: "2px 7px",
        fontFamily: NUMERIC_FONT,
        fontSize: "0.67rem",
      }}
    >
      <span style={{ color: "var(--color-text-muted)" }}>{label}</span>
      <span style={{ color, fontWeight: 700 }}>{value}</span>
    </span>
  );
}

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
