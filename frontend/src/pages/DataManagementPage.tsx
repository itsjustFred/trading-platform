import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, MouseEvent, ReactNode } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Database,
  Download,
  FolderTree,
  HardDriveDownload,
  Info,
  Loader2,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Table2,
  Trash2,
} from "lucide-react";

import {
  deleteDataSymbol,
  getDataCategory,
  getDataOverview,
  getDataTask,
} from "../api/client";
import type { DataCategorySummary, DataSymbolEntry } from "../api/types";
import { DownloadModal } from "../components/DownloadModal";
import { ResampleModal } from "../components/ResampleModal";
import { SmartHarvestModal } from "../components/SmartHarvestModal";

type TreeSelection =
  | { kind: "category"; category: string }
  | { kind: "symbol"; category: string; symbol: string };

type SortDirection = "asc" | "desc";

type ValidationState = "OK" | "Warning";

interface SymbolGroup {
  symbol: string;
  entries: DataSymbolEntry[];
  totalSizeMb: number;
  start: string | null;
  end: string | null;
  sources: string[];
  validation: ValidationState;
  warningMessage: string | null;
}

interface SymbolTableRow {
  id: string;
  timeframe: string;
  start: string | null;
  end: string | null;
  size_mb: number;
  validation: ValidationState;
  warningMessage: string | null;
  source: string;
  path: string;
  entry: DataSymbolEntry;
}

interface CategoryInventoryRow {
  symbol: string;
  timeframes: number;
  sources: string[];
  start: string | null;
  end: string | null;
  size_mb: number;
  validation: ValidationState;
  warningMessage: string | null;
  group: SymbolGroup;
}

interface TreeCategoryModel {
  category: DataCategorySummary;
  symbols: SymbolGroup[];
  visibleSymbols: SymbolGroup[];
}

interface TableColumn<T> {
  key: string;
  label: string;
  width?: string;
  align?: "left" | "center" | "right";
  sortable?: boolean;
  sortValue?: (row: T) => string | number;
  render: (row: T) => ReactNode;
}

const TIMEFRAME_ORDER = [
  "tick",
  "s1",
  "m1",
  "m5",
  "m15",
  "m30",
  "h1",
  "h4",
  "4h",
  "d1",
  "w1",
  "mn1",
  "repo",
];

const FINAL_DATA_TASK_STATUSES = new Set(["completed", "failed", "cancelled"]);

function formatSizeMB(sizeMb: number) {
  if (!Number.isFinite(sizeMb) || sizeMb <= 0) {
    return "0.00 MB";
  }
  if (sizeMb >= 1024) {
    return `${(sizeMb / 1024).toFixed(2)} GB`;
  }
  return `${sizeMb.toFixed(2)} MB`;
}

function timeframeRank(timeframe: string) {
  const rank = TIMEFRAME_ORDER.indexOf((timeframe || "").toLowerCase());
  return rank === -1 ? 99 : rank;
}

/** Renders an unknown table cell as a comparable string without `[object Object]`. */
function stringifySortableCell(value: unknown): string {
  if (value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value) ?? "";
}

function compareMaybeString(a: string | number, b: string | number) {
  if (typeof a === "number" && typeof b === "number") {
    return a - b;
  }
  return String(a).localeCompare(String(b));
}

function humanizeSource(source: string | undefined) {
  const normalized = (source || "").trim();
  if (!normalized) {
    return "Unknown Source";
  }
  const aliases: Record<string, string> = {
    yfinance: "YFinance",
    dolthub: "Dolthub",
    dukascopy: "Dukascopy",
    jforex: "JForex",
    huggingface: "HuggingFace",
    alpaca: "Alpaca",
  };
  if (aliases[normalized.toLowerCase()]) {
    return aliases[normalized.toLowerCase()];
  }
  return normalized
    .replace(
      /(^|[_-])(\w)/g,
      (_, prefix, letter: string) =>
        `${prefix ? " " : ""}${letter.toUpperCase()}`,
    )
    .trim();
}

function categoryDefaultSource(category: string) {
  const normalized = category.toLowerCase();
  if (normalized === "dolthub repos") {
    return "dolthub";
  }
  if (["equities", "etfs"].includes(normalized)) {
    return "yfinance";
  }
  return "dukascopy";
}

function categoryDefaultAssetClass(category: string) {
  const normalized = category.toLowerCase();
  if (normalized.includes("metals")) return "metals";
  if (normalized.includes("indices")) return "indices";
  if (normalized.includes("energy")) return "energy";
  if (normalized.includes("forex")) return "forex";
  if (normalized.includes("equities") || normalized.includes("equity")) {
    return "equity";
  }
  if (normalized.includes("etf")) return "etf";
  if (normalized.includes("crypto")) return "crypto";
  return "";
}

function deriveValidation(
  entry: Pick<
    DataSymbolEntry,
    "start" | "end" | "error" | "validation" | "repo"
  >,
): ValidationState {
  if (entry.validation?.status && entry.validation.status !== "ok") {
    return "Warning";
  }
  if (entry.repo) {
    return entry.error ? "Warning" : "OK";
  }
  if (entry.error || !entry.start || !entry.end) {
    return "Warning";
  }
  return "OK";
}

function boundDate(
  values: Array<string | null | undefined>,
  pick: "first" | "last",
): string | null {
  const filtered = values.filter((value): value is string => Boolean(value));
  if (filtered.length === 0) {
    return null;
  }
  const sorted = filtered.sort((a, b) => a.localeCompare(b));
  return pick === "first" ? sorted[0] : sorted.at(-1) ?? null;
}

function minDate(values: Array<string | null | undefined>) {
  return boundDate(values, "first");
}

function maxDate(values: Array<string | null | undefined>) {
  return boundDate(values, "last");
}

function formatDateLabel(dateValue: string | null | undefined) {
  return dateValue || "—";
}

function rangeLabel(
  start: string | null | undefined,
  end: string | null | undefined,
) {
  if (!start && !end) {
    return "Date range pending";
  }
  return `${formatDateLabel(start)} → ${formatDateLabel(end)}`;
}

function mergeWarningMessages(messages: Array<string | null | undefined>) {
  const unique = [
    ...new Set(
      messages
        .map((message) => message?.trim())
        .filter((message): message is string => Boolean(message)),
    ),
  ];
  return unique.length > 0 ? unique.join(" | ") : null;
}

function buildSymbolGroups(entries: DataSymbolEntry[]): SymbolGroup[] {
  const groups = new Map<string, DataSymbolEntry[]>();
  for (const entry of entries) {
    const symbol = entry.symbol || "Unknown";
    const existing = groups.get(symbol) || [];
    existing.push(entry);
    groups.set(symbol, existing);
  }

  return [...groups.entries()]
    .map(([symbol, groupEntries]) => {
      const validation: ValidationState = groupEntries.every(
        (item) => deriveValidation(item) === "OK",
      )
        ? "OK"
        : "Warning";
      return {
        symbol,
        entries: [...groupEntries].sort(
          (a, b) => timeframeRank(a.timeframe) - timeframeRank(b.timeframe),
        ),
        totalSizeMb: groupEntries.reduce(
          (total, item) => total + (item.size_mb || 0),
          0,
        ),
        start: minDate(groupEntries.map((item) => item.start)),
        end: maxDate(groupEntries.map((item) => item.end)),
        sources: [
          ...new Set(groupEntries.map((item) => item.source).filter(Boolean)),
        ].sort((a, b) => a.localeCompare(b)),
        validation,
        warningMessage: mergeWarningMessages(
          groupEntries.map(
            (item) =>
              item.warning_message || item.validation_message || item.error,
          ),
        ),
      };
    })
    .sort((a, b) => a.symbol.localeCompare(b.symbol));
}

function pickResamplePath(entries: DataSymbolEntry[]) {
  const candidate = [...entries]
    .filter((entry) => !entry.repo && entry.path)
    .sort((a, b) => timeframeRank(a.timeframe) - timeframeRank(b.timeframe))[0];
  return candidate?.path || "";
}

function preferredTimeframes(entries: DataSymbolEntry[]) {
  const timeframes = [
    ...new Set(entries.map((entry) => entry.timeframe).filter(Boolean)),
  ];
  return timeframes.length > 0
    ? timeframes.sort((a, b) => timeframeRank(a) - timeframeRank(b))
    : ["d1"];
}

function primarySource(entries: DataSymbolEntry[], category: string) {
  const uniqueSources = [
    ...new Set(entries.map((entry) => entry.source).filter(Boolean)),
  ];
  return uniqueSources[0] || categoryDefaultSource(category);
}

function backdropStyle(): CSSProperties {
  return {
    minHeight: "100%",
    padding: "24px",
    background:
      "radial-gradient(circle at top right, rgba(14, 165, 233, 0.12), transparent 32%), radial-gradient(circle at bottom left, rgba(56, 189, 248, 0.09), transparent 34%), linear-gradient(180deg, rgba(15, 23, 42, 0.18) 0%, rgba(2, 6, 23, 0) 100%)",
  };
}

export function DataManagementPage() {
  const [categories, setCategories] = useState<DataCategorySummary[]>([]);
  const [categoryEntries, setCategoryEntries] = useState<
    Record<string, DataSymbolEntry[]>
  >({});
  const [loading, setLoading] = useState(true);
  const [loadingCategories, setLoadingCategories] = useState<
    Record<string, boolean>
  >({});
  const [expandedCategories, setExpandedCategories] = useState<string[]>([]);
  const [selection, setSelection] = useState<TreeSelection | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [downloadModalOpen, setDownloadModalOpen] = useState(false);
  const [downloadDefaults, setDownloadDefaults] = useState<{
    source: string;
    symbols: string;
    timeframes: string[];
  }>({
    source: "dukascopy",
    symbols: "",
    timeframes: ["d1"],
  });
  const [resampleModalOpen, setResampleModalOpen] = useState(false);
  const [resampleDefaults, setResampleDefaults] = useState<{
    sourcePath: string;
    targetTimeframes: string;
  }>({
    sourcePath: "",
    targetTimeframes: "5m,15m,30m,1h,4h",
  });
  const [harvestModalOpen, setHarvestModalOpen] = useState(false);
  const [harvestDefaults, setHarvestDefaults] = useState<{
    source: string;
    symbol: string;
    assetClass: string;
    startDate: string;
  }>({
    source: "dukascopy",
    symbol: "",
    assetClass: "",
    startDate: "",
  });
  const [backgroundTaskId, setBackgroundTaskId] = useState<string | null>(null);
  const categoryEntriesRef = useRef<Record<string, DataSymbolEntry[]>>({});
  const loadingCategoriesRef = useRef<Record<string, boolean>>({});
  const selectionRef = useRef<TreeSelection | null>(null);
  const handledTaskRefreshIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    categoryEntriesRef.current = categoryEntries;
  }, [categoryEntries]);

  useEffect(() => {
    loadingCategoriesRef.current = loadingCategories;
  }, [loadingCategories]);

  useEffect(() => {
    selectionRef.current = selection;
  }, [selection]);

  const loadCategoryDetails = useCallback(
    async (categoryName: string, force = false) => {
      if (!categoryName) {
        return;
      }
      if (loadingCategoriesRef.current[categoryName]) {
        return;
      }
      if (!force && categoryEntriesRef.current[categoryName]) {
        return;
      }

      setLoadingCategories((prev) => {
        const next = { ...prev, [categoryName]: true };
        loadingCategoriesRef.current = next;
        return next;
      });
      try {
        const response = await getDataCategory(categoryName);
        setCategoryEntries((prev) => {
          const next = {
            ...prev,
            [categoryName]: response.symbols || [],
          };
          categoryEntriesRef.current = next;
          return next;
        });
      } finally {
        setLoadingCategories((prev) => {
          const next = { ...prev };
          delete next[categoryName];
          loadingCategoriesRef.current = next;
          return next;
        });
      }
    },
    [],
  );

  const loadOverview = useCallback(
    async (preferredSelection?: TreeSelection | null) => {
      setLoading(true);
      try {
        const response = await getDataOverview();
        const nextCategories = response.categories || [];
        setCategories(nextCategories);
        setExpandedCategories((prev) => {
          const valid = prev.filter((category) =>
            nextCategories.some((item) => item.name === category),
          );
          if (valid.length > 0) {
            return valid;
          }
          return nextCategories[0] ? [nextCategories[0].name] : [];
        });
        setSelection((prev) => {
          const candidate = preferredSelection ?? prev;
          if (
            candidate &&
            nextCategories.some(
              (category) => category.name === candidate.category,
            )
          ) {
            return candidate;
          }
          return nextCategories[0]
            ? { kind: "category", category: nextCategories[0].name }
            : null;
        });
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  const refreshCurrentSelection = useCallback(
    async (preferredSelection?: TreeSelection | null) => {
      const nextSelection = preferredSelection ?? selectionRef.current;
      await loadOverview(nextSelection);
      if (nextSelection?.category) {
        await loadCategoryDetails(nextSelection.category, true);
      }
    },
    [loadCategoryDetails, loadOverview],
  );

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  const categoryNames = useMemo(
    () => categories.map((category) => category.name),
    [categories],
  );

  useEffect(() => {
    categoryNames.forEach((categoryName) => {
      void loadCategoryDetails(categoryName);
    });
  }, [categoryNames, loadCategoryDetails]);

  const groupedByCategory = useMemo(() => {
    const next: Record<string, SymbolGroup[]> = {};
    Object.entries(categoryEntries).forEach(([categoryName, entries]) => {
      next[categoryName] = buildSymbolGroups(entries);
    });
    return next;
  }, [categoryEntries]);

  useEffect(() => {
    if (selection?.kind !== "symbol") {
      return;
    }
    const currentCategoryGroups = groupedByCategory[selection.category] || [];
    const symbolExists = currentCategoryGroups.some(
      (group) => group.symbol === selection.symbol,
    );
    if (!symbolExists) {
      setSelection({ kind: "category", category: selection.category });
    }
  }, [groupedByCategory, selection]);

  useEffect(() => {
    if (!backgroundTaskId) {
      return;
    }

    let active = true;
    let intervalId = 0;
    const trackedTaskId = backgroundTaskId;

    const poll = async () => {
      try {
        const payload = await getDataTask(trackedTaskId);
        if (!active) {
          return;
        }
        const nextStatus = payload.status || "unknown";
        if (FINAL_DATA_TASK_STATUSES.has(nextStatus)) {
          window.clearInterval(intervalId);
          setBackgroundTaskId((current) =>
            current === trackedTaskId ? null : current,
          );
          if (
            nextStatus === "completed" &&
            !handledTaskRefreshIdsRef.current.has(trackedTaskId)
          ) {
            handledTaskRefreshIdsRef.current.add(trackedTaskId);
            await refreshCurrentSelection(selectionRef.current);
          }
          if (!active) {
            return;
          }
        }
      } catch {
        if (!active) {
          return;
        }
      }
    };

    void poll();
    intervalId = window.setInterval(() => {
      void poll();
    }, 3000);

    return () => {
      active = false;
      window.clearInterval(intervalId);
    };
  }, [backgroundTaskId, refreshCurrentSelection]);

  const searchNeedle = searchTerm.trim().toLowerCase();
  const treeData = useMemo<TreeCategoryModel[]>(() => {
    return categories
      .map((category) => {
        const symbols = groupedByCategory[category.name] || [];
        const visibleSymbols = searchNeedle
          ? symbols.filter(
              (group) =>
                group.symbol.toLowerCase().includes(searchNeedle) ||
                category.name.toLowerCase().includes(searchNeedle),
            )
          : symbols;
        return { category, symbols, visibleSymbols };
      })
      .filter((item) => {
        if (!searchNeedle) {
          return true;
        }
        return (
          item.category.name.toLowerCase().includes(searchNeedle) ||
          item.visibleSymbols.length > 0
        );
      });
  }, [categories, groupedByCategory, searchNeedle]);

  const selectedCategoryName =
    selection?.category ?? categories[0]?.name ?? null;
  const selectedCategory = useMemo(
    () =>
      categories.find((category) => category.name === selectedCategoryName) ??
      null,
    [categories, selectedCategoryName],
  );
  const selectedGroups = useMemo(
    () =>
      selectedCategoryName ? groupedByCategory[selectedCategoryName] || [] : [],
    [groupedByCategory, selectedCategoryName],
  );
  const selectedGroup = useMemo(
    () =>
      selection?.kind === "symbol"
        ? (selectedGroups.find((group) => group.symbol === selection.symbol) ??
          null)
        : null,
    [selectedGroups, selection],
  );

  const categoryInventoryRows = useMemo<CategoryInventoryRow[]>(
    () =>
      selectedGroups.map((group) => ({
        symbol: group.symbol,
        timeframes: group.entries.length,
        sources: group.sources,
        start: group.start,
        end: group.end,
        size_mb: group.totalSizeMb,
        validation: group.validation,
        warningMessage: group.warningMessage,
        group,
      })),
    [selectedGroups],
  );

  const symbolRows = useMemo<SymbolTableRow[]>(
    () =>
      (selectedGroup?.entries || []).map((entry) => ({
        id: `${entry.path}:${entry.timeframe}`,
        timeframe: entry.timeframe,
        start: entry.start || null,
        end: entry.end || null,
        size_mb: entry.size_mb || 0,
        validation: deriveValidation(entry),
        warningMessage:
          entry.warning_message ||
          entry.validation_message ||
          entry.error ||
          null,
        source: entry.source,
        path: entry.path,
        entry,
      })),
    [selectedGroup],
  );

  const toggleCategory = (categoryName: string) => {
    setSelection({ kind: "category", category: categoryName });
    setExpandedCategories((prev) =>
      prev.includes(categoryName)
        ? prev.filter((item) => item !== categoryName)
        : [...prev, categoryName],
    );
    void loadCategoryDetails(categoryName);
  };

  const handleSelectSymbol = (categoryName: string, symbol: string) => {
    setSelection({ kind: "symbol", category: categoryName, symbol });
    setExpandedCategories((prev) =>
      prev.includes(categoryName) ? prev : [...prev, categoryName],
    );
    void loadCategoryDetails(categoryName);
  };

  const handleDeleteEntry = async (
    entry: DataSymbolEntry,
    event?: MouseEvent<HTMLButtonElement>,
  ) => {
    event?.stopPropagation();
    if (!window.confirm(`Delete ${entry.symbol} ${entry.timeframe}?`)) {
      return;
    }
    try {
      await deleteDataSymbol(entry.path);
      await refreshCurrentSelection(
        selection?.kind === "symbol"
          ? {
              kind: "symbol",
              category: selection.category,
              symbol: selection.symbol,
            }
          : selection,
      );
    } catch (err: unknown) {
      window.alert(
        err instanceof Error ? err.message : "Failed to delete file.",
      );
    }
  };

  const handleDeleteAllForSymbol = async (group: SymbolGroup) => {
    if (
      !window.confirm(
        `Delete all ${group.entries.length} file(s) for ${group.symbol}?`,
      )
    ) {
      return;
    }

    try {
      for (const entry of group.entries) {
        await deleteDataSymbol(entry.path);
      }
      await refreshCurrentSelection({
        kind: "category",
        category: selection?.category || selectedCategory?.name || "",
      });
    } catch (err: unknown) {
      window.alert(
        err instanceof Error ? err.message : "Failed to delete symbol files.",
      );
    }
  };

  const openDownloadForCurrent = () => {
    if (selection?.kind === "symbol" && selectedGroup) {
      setDownloadDefaults({
        source: primarySource(selectedGroup.entries, selection.category),
        symbols: selectedGroup.symbol,
        timeframes: preferredTimeframes(selectedGroup.entries),
      });
    } else if (selectedCategory) {
      setDownloadDefaults({
        source: categoryDefaultSource(selectedCategory.name),
        symbols: "",
        timeframes: ["d1"],
      });
    }
    setDownloadModalOpen(true);
  };

  const openResampleForCurrent = () => {
    if (selection?.kind === "symbol" && selectedGroup) {
      setResampleDefaults({
        sourcePath: pickResamplePath(selectedGroup.entries),
        targetTimeframes: "5m,15m,30m,1h,4h",
      });
    } else {
      setResampleDefaults({
        sourcePath: "",
        targetTimeframes: "5m,15m,30m,1h,4h",
      });
    }
    setResampleModalOpen(true);
  };

  const openSmartHarvestForCurrent = () => {
    if (selection?.kind === "symbol" && selectedGroup) {
      setHarvestDefaults({
        source: "dukascopy",
        symbol: selectedGroup.symbol,
        assetClass: categoryDefaultAssetClass(selection.category),
        startDate: "",
      });
    } else if (selectedCategory) {
      setHarvestDefaults({
        source: "dukascopy",
        symbol: "",
        assetClass: categoryDefaultAssetClass(selectedCategory.name),
        startDate: "",
      });
    }
    setHarvestModalOpen(true);
  };

  const handleTaskStarted = (taskId: string) => {
    setBackgroundTaskId(taskId);
  };

  if (loading) {
    return (
      <div style={backdropStyle()}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "12px",
            minHeight: "62vh",
            color: "var(--color-text-muted)",
          }}
        >
          <Loader2 className="animate-spin" />
          Loading data catalog...
        </div>
      </div>
    );
  }

  return (
    <div style={backdropStyle()}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: "18px",
          marginBottom: "22px",
        }}
      >
        <div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "12px",
              marginBottom: "8px",
            }}
          >
            <div
              style={{
                width: "44px",
                height: "44px",
                borderRadius: "14px",
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                background:
                  "linear-gradient(135deg, rgba(59, 130, 246, 0.24), rgba(14, 165, 233, 0.16))",
                border: "1px solid rgba(125, 211, 252, 0.16)",
              }}
            >
              <Database size={22} />
            </div>
            <div>
              <div
                style={{
                  fontSize: "0.76rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.2em",
                  color: "#7dd3fc",
                }}
              >
                Data Console
              </div>
              <h1 style={{ fontSize: "1.8rem", fontWeight: 700 }}>
                DBMS-style market data control room
              </h1>
            </div>
          </div>
          <div
            style={{
              color: "var(--color-text-muted)",
              maxWidth: "760px",
              lineHeight: 1.6,
            }}
          >
            Navigate the stored data tree, inspect symbol-level inventories, and
            trigger backend jobs without leaving the page.
          </div>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(220px, 1fr)",
            gap: "12px",
            alignItems: "start",
            width: "min(100%, 320px)",
            flex: "0 1 320px",
          }}
        >
          <MetricTile
            label="Categories"
            value={String(categories.length)}
            accent="#7dd3fc"
          />
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(300px, 25%) minmax(0, 1fr)",
          gap: "20px",
          alignItems: "start",
        }}
      >
        <aside
          style={{
            borderRadius: "22px",
            border: "1px solid rgba(148, 163, 184, 0.16)",
            background:
              "linear-gradient(180deg, rgba(15, 23, 42, 0.95), rgba(2, 6, 23, 0.92))",
            boxShadow: "0 24px 60px rgba(2, 6, 23, 0.34)",
            overflow: "hidden",
            position: "sticky",
            top: "22px",
          }}
        >
          <div
            style={{
              padding: "18px 18px 14px",
              borderBottom: "1px solid rgba(148, 163, 184, 0.12)",
            }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginBottom: "14px",
              }}
            >
              <div
                style={{ display: "flex", alignItems: "center", gap: "10px" }}
              >
                <FolderTree size={18} color="#7dd3fc" />
                <div>
                  <div
                    style={{
                      fontSize: "0.78rem",
                      textTransform: "uppercase",
                      letterSpacing: "0.14em",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    Tree navigator
                  </div>
                  <div style={{ fontSize: "1rem", fontWeight: 700 }}>
                    Categories & symbols
                  </div>
                </div>
              </div>
              <div
                style={{
                  fontSize: "0.74rem",
                  color: "var(--color-text-muted)",
                }}
              >
                {categories.length} nodes
              </div>
            </div>
            <div style={{ position: "relative" }}>
              <Search
                size={16}
                style={{
                  position: "absolute",
                  left: "12px",
                  top: "50%",
                  transform: "translateY(-50%)",
                  color: "var(--color-text-muted)",
                }}
              />
              <input
                value={searchTerm}
                onChange={(event) => setSearchTerm(event.target.value)}
                placeholder="Filter symbols in the tree..."
                style={{
                  width: "100%",
                  padding: "11px 12px 11px 38px",
                  borderRadius: "12px",
                  border: "1px solid rgba(148, 163, 184, 0.16)",
                  background: "rgba(15, 23, 42, 0.82)",
                  color: "var(--color-text)",
                }}
              />
            </div>
          </div>

          <div
            style={{
              maxHeight: "calc(100vh - 240px)",
              overflowY: "auto",
              padding: "10px",
            }}
          >
            {treeData.length === 0 ? (
              <div
                style={{
                  padding: "24px 18px",
                  color: "var(--color-text-muted)",
                  fontSize: "0.84rem",
                }}
              >
                No symbols matched the current filter.
              </div>
            ) : (
              treeData.map(({ category, symbols, visibleSymbols }) => {
                const isExpanded =
                  expandedCategories.includes(category.name) ||
                  (Boolean(searchNeedle) && visibleSymbols.length > 0);
                const selectedCategoryNode =
                  selection?.category === category.name &&
                  selection.kind === "category";
                return (
                  <div key={category.name} style={{ marginBottom: "6px" }}>
                    <button
                      type="button"
                      onClick={() => toggleCategory(category.name)}
                      style={{
                        width: "100%",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: "10px",
                        padding: "12px 12px",
                        borderRadius: "14px",
                        border: selectedCategoryNode
                          ? "1px solid rgba(96, 165, 250, 0.42)"
                          : "1px solid transparent",
                        background: selectedCategoryNode
                          ? "rgba(37, 99, 235, 0.14)"
                          : "transparent",
                        color: "var(--color-text)",
                        cursor: "pointer",
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "10px",
                          minWidth: 0,
                          textAlign: "left",
                        }}
                      >
                        {isExpanded ? (
                          <ChevronDown size={16} />
                        ) : (
                          <ChevronRight size={16} />
                        )}
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontWeight: 700, fontSize: "0.92rem" }}>
                            {category.name}
                          </div>
                          <div
                            style={{
                              fontSize: "0.74rem",
                              color: "var(--color-text-muted)",
                            }}
                          >
                            {category.symbol_count} symbols •{" "}
                            {formatSizeMB(category.total_size_mb || 0)}
                          </div>
                        </div>
                      </div>
                      <div
                        style={{
                          fontSize: "0.7rem",
                          color: "var(--color-text-muted)",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {rangeLabel(
                          category.start ?? null,
                          category.end ?? null,
                        )}
                      </div>
                    </button>

                    {isExpanded && (
                      <div style={{ padding: "6px 0 4px 24px" }}>
                        {loadingCategories[category.name] &&
                          symbols.length === 0 && (
                            <div
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: "8px",
                                padding: "10px 0",
                                color: "var(--color-text-muted)",
                                fontSize: "0.78rem",
                              }}
                            >
                              <Loader2 size={14} className="animate-spin" />{" "}
                              Indexing symbols...
                            </div>
                          )}
                        {!loadingCategories[category.name] &&
                          visibleSymbols.length === 0 && (
                            <div
                              style={{
                                padding: "10px 0",
                                color: "var(--color-text-muted)",
                                fontSize: "0.78rem",
                              }}
                            >
                              No symbol matches in this category.
                            </div>
                          )}
                        {(!loadingCategories[category.name] ||
                          symbols.length > 0) &&
                          visibleSymbols.map((group) => {
                            const isSelectedSymbol =
                              selection?.kind === "symbol" &&
                              selection.category === category.name &&
                              selection.symbol === group.symbol;
                            return (
                              <button
                                key={`${category.name}:${group.symbol}`}
                                type="button"
                                onClick={() =>
                                  handleSelectSymbol(
                                    category.name,
                                    group.symbol,
                                  )
                                }
                                style={{
                                  display: "flex",
                                  justifyContent: "space-between",
                                  alignItems: "center",
                                  gap: "10px",
                                  padding: "8px 10px",
                                  borderRadius: "8px",
                                  border: isSelectedSymbol
                                    ? "1px solid var(--color-blue)"
                                    : "1px solid transparent",
                                  background: isSelectedSymbol
                                    ? "rgba(88, 166, 255, 0.12)"
                                    : "transparent",
                                  cursor: "pointer",
                                  textAlign: "left",
                                  width: "100%",
                                }}
                              >
                                <span
                                  style={{
                                    fontSize: "0.82rem",
                                    color: "var(--color-text)",
                                  }}
                                >
                                  {group.symbol}
                                </span>
                                <div
                                  style={{
                                    fontSize: "0.72rem",
                                    color: "var(--color-text-muted)",
                                  }}
                                >
                                  {formatSizeMB(group.totalSizeMb)}
                                </div>
                              </button>
                            );
                          })}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </aside>

        <main
          style={{
            borderRadius: "24px",
            border: "1px solid rgba(148, 163, 184, 0.14)",
            background:
              "linear-gradient(180deg, rgba(15, 23, 42, 0.94), rgba(2, 6, 23, 0.9))",
            boxShadow: "0 24px 70px rgba(2, 6, 23, 0.38)",
            overflow: "hidden",
          }}
        >
          {(!selection || !selectedCategory) && <EmptyDataPanel />}
          {selection && selectedCategory && selection.kind === "category" && (
            <CategoryDetailPanel
              category={selectedCategory}
              rows={categoryInventoryRows}
              loading={
                Boolean(loadingCategories[selectedCategory.name]) &&
                categoryInventoryRows.length === 0
              }
              onDownload={openDownloadForCurrent}
              onResample={openResampleForCurrent}
              onHarvest={openSmartHarvestForCurrent}
              onSelectSymbol={(symbol) =>
                handleSelectSymbol(selectedCategory.name, symbol)
              }
            />
          )}
          {selection &&
            selectedCategory &&
            selection.kind !== "category" &&
            selectedGroup && (
              <SymbolDetailPanel
                category={selectedCategory}
                group={selectedGroup}
                rows={symbolRows}
                onDownload={openDownloadForCurrent}
                onResample={openResampleForCurrent}
                onHarvest={openSmartHarvestForCurrent}
                onDeleteAll={() => handleDeleteAllForSymbol(selectedGroup)}
                onDeleteRow={handleDeleteEntry}
              />
            )}
          {selection &&
            selectedCategory &&
            selection.kind !== "category" &&
            !selectedGroup && <EmptyDataPanel />}
        </main>
      </div>

      <DownloadModal
        open={downloadModalOpen}
        onClose={() => setDownloadModalOpen(false)}
        onTaskStarted={handleTaskStarted}
        defaultSource={downloadDefaults.source}
        defaultSymbols={downloadDefaults.symbols}
        defaultTimeframes={downloadDefaults.timeframes}
      />

      <ResampleModal
        open={resampleModalOpen}
        onClose={() => setResampleModalOpen(false)}
        onTaskStarted={handleTaskStarted}
        defaultSourcePath={resampleDefaults.sourcePath}
        defaultTargetTimeframes={resampleDefaults.targetTimeframes}
      />

      <SmartHarvestModal
        open={harvestModalOpen}
        onClose={() => setHarvestModalOpen(false)}
        onTaskStarted={handleTaskStarted}
        defaultSource={harvestDefaults.source}
        defaultSymbol={harvestDefaults.symbol}
        defaultAssetClass={harvestDefaults.assetClass}
        defaultStartDate={harvestDefaults.startDate}
      />
    </div>
  );
}

function EmptyDataPanel() {
  return (
    <div
      style={{
        minHeight: "68vh",
        display: "grid",
        placeItems: "center",
        padding: "32px",
      }}
    >
      <div style={{ maxWidth: "460px", textAlign: "center" }}>
        <div
          style={{
            width: "68px",
            height: "68px",
            margin: "0 auto 18px",
            borderRadius: "20px",
            display: "grid",
            placeItems: "center",
            background:
              "linear-gradient(135deg, rgba(59, 130, 246, 0.2), rgba(14, 165, 233, 0.12))",
            color: "#7dd3fc",
          }}
        >
          <HardDriveDownload size={28} />
        </div>
        <div
          style={{ fontSize: "1.3rem", fontWeight: 700, marginBottom: "10px" }}
        >
          Pick a category or symbol from the tree
        </div>
        <div style={{ color: "var(--color-text-muted)", lineHeight: 1.7 }}>
          Use the left-hand navigator to explore the dataset hierarchy, then
          inspect timeframes, ranges, and file actions in the main workspace.
        </div>
      </div>
    </div>
  );
}

function CategoryDetailPanel({
  category,
  rows,
  loading,
  onDownload,
  onResample,
  onHarvest,
  onSelectSymbol,
}: Readonly<{
  category: DataCategorySummary;
  rows: CategoryInventoryRow[];
  loading: boolean;
  onDownload: () => void;
  onResample: () => void;
  onHarvest: () => void;
  onSelectSymbol: (symbol: string) => void;
}>) {
  const columns: TableColumn<CategoryInventoryRow>[] = [
    {
      key: "symbol",
      label: "Symbol",
      width: "26%",
      sortValue: (row) => row.symbol,
      render: (row) => (
        <div>
          <div style={{ fontWeight: 700 }}>{row.symbol}</div>
          <div
            style={{ fontSize: "0.74rem", color: "var(--color-text-muted)" }}
          >
            {row.sources.map(humanizeSource).join(" · ")}
          </div>
        </div>
      ),
    },
    {
      key: "timeframes",
      label: "Timeframes",
      width: "14%",
      align: "center",
      sortValue: (row) => row.timeframes,
      render: (row) => row.timeframes,
    },
    {
      key: "start",
      label: "Start",
      width: "14%",
      sortValue: (row) => row.start || "",
      render: (row) => formatDateLabel(row.start),
    },
    {
      key: "end",
      label: "End",
      width: "14%",
      sortValue: (row) => row.end || "",
      render: (row) => formatDateLabel(row.end),
    },
    {
      key: "size_mb",
      label: "Storage",
      width: "16%",
      align: "right",
      sortValue: (row) => row.size_mb,
      render: (row) => formatSizeMB(row.size_mb),
    },
    {
      key: "validation",
      label: "Health",
      width: "16%",
      align: "center",
      sortValue: (row) => row.validation,
      render: (row) => (
        <ValidationIndicator
          status={row.validation}
          warningMessage={row.warningMessage}
          showMessage
        />
      ),
    },
  ];

  return (
    <div style={{ display: "grid", gap: "22px", padding: "24px" }}>
      <HeaderBlock
        eyebrow="Category overview"
        title={category.name}
        breadcrumbs={["Data Console", category.name]}
        actions={
          <div style={{ display: "flex", gap: "10px" }}>
            <ActionButton
              icon={<Download size={15} />}
              label="Download Data"
              onClick={onDownload}
              tone="primary"
            />
            <ActionButton
              icon={<RefreshCw size={15} />}
              label="Resample"
              onClick={onResample}
              tone="secondary"
            />
            <ActionButton
              icon={<Sparkles size={15} />}
              label="Smart Harvest (Auto-Tune)"
              onClick={onHarvest}
              tone="secondary"
            />
          </div>
        }
      >
        <div
          style={{
            color: "var(--color-text-muted)",
            maxWidth: "780px",
            lineHeight: 1.7,
          }}
        >
          Browse the full symbol inventory inside{" "}
          <strong>{category.name}</strong>, inspect storage coverage, and jump
          directly into symbol-level maintenance.
        </div>
      </HeaderBlock>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
          gap: "12px",
        }}
      >
        <MetricTile
          label="Symbols"
          value={String(category.symbol_count)}
          accent="#7dd3fc"
        />
        <MetricTile
          label="Files"
          value={String(category.file_count)}
          accent="#67e8f9"
        />
        <MetricTile
          label="Storage"
          value={formatSizeMB(category.total_size_mb || 0)}
          accent="#93c5fd"
        />
        <MetricTile
          label="Coverage"
          value={rangeLabel(category.start ?? null, category.end ?? null)}
          accent="#bfdbfe"
        />
      </div>

      <SectionPanel
        title="Symbol inventory"
        subtitle="Select a row to jump into the symbol detail workspace."
      >
        {loading ? (
          <LoadingMessage label="Building symbol inventory..." />
        ) : (
          <PanelTable
            rows={rows}
            columns={columns}
            getRowKey={(row) => `${category.name}:${row.symbol}`}
            defaultSortKey="symbol"
            onRowClick={(row) => onSelectSymbol(row.symbol)}
            emptyMessage="No symbols available in this category yet."
          />
        )}
      </SectionPanel>
    </div>
  );
}

function SymbolDetailPanel({
  category,
  group,
  rows,
  onDownload,
  onResample,
  onHarvest,
  onDeleteAll,
  onDeleteRow,
}: Readonly<{
  category: DataCategorySummary;
  group: SymbolGroup;
  rows: SymbolTableRow[];
  onDownload: () => void;
  onResample: () => void;
  onHarvest: () => void;
  onDeleteAll: () => void;
  onDeleteRow: (
    entry: DataSymbolEntry,
    event?: MouseEvent<HTMLButtonElement>,
  ) => Promise<void>;
}>) {
  const sourceLabel =
    group.sources.length === 1
      ? humanizeSource(group.sources[0])
      : "Multiple Sources";
  const isRepoSymbol = group.entries.some((entry) => entry.repo);
  const columns: TableColumn<SymbolTableRow>[] = [
    {
      key: "timeframe",
      label: "Timeframe",
      width: "18%",
      sortValue: (row) => timeframeRank(row.timeframe),
      render: (row) => (
        <div>
          <div
            style={{
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
              fontWeight: 700,
            }}
          >
            {row.timeframe}
          </div>
          <div
            style={{ fontSize: "0.74rem", color: "var(--color-text-muted)" }}
          >
            {humanizeSource(row.source)}
          </div>
        </div>
      ),
    },
    {
      key: "start",
      label: "Start Date",
      width: "16%",
      sortValue: (row) => row.start || "",
      render: (row) => formatDateLabel(row.start),
    },
    {
      key: "end",
      label: "End Date",
      width: "16%",
      sortValue: (row) => row.end || "",
      render: (row) => formatDateLabel(row.end),
    },
    {
      key: "size_mb",
      label: "Size",
      width: "16%",
      align: "right",
      sortValue: (row) => row.size_mb,
      render: (row) => formatSizeMB(row.size_mb),
    },
    {
      key: "validation",
      label: "Validation",
      width: "18%",
      align: "center",
      sortValue: (row) => row.validation,
      render: (row) => (
        <ValidationIndicator
          status={row.validation}
          warningMessage={row.warningMessage}
          showMessage
        />
      ),
    },
    {
      key: "actions",
      label: "Actions",
      width: "16%",
      align: "center",
      sortable: false,
      render: (row) =>
        row.entry.repo ? (
          <span style={{ color: "var(--color-text-muted)" }}>—</span>
        ) : (
          <button
            type="button"
            onClick={(event) => {
              void onDeleteRow(row.entry, event);
            }}
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              width: "34px",
              height: "34px",
              borderRadius: "10px",
              border: "1px solid rgba(248, 113, 113, 0.26)",
              background: "rgba(127, 29, 29, 0.18)",
              color: "#fca5a5",
              cursor: "pointer",
            }}
            title={`Delete ${row.timeframe}`}
          >
            <Trash2 size={14} />
          </button>
        ),
    },
  ];

  return (
    <div style={{ display: "grid", gap: "22px", padding: "24px" }}>
      <HeaderBlock
        eyebrow="Symbol workspace"
        title={group.symbol}
        breadcrumbs={[sourceLabel, category.name, group.symbol]}
        actions={
          <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
            <ActionButton
              icon={<Download size={15} />}
              label={isRepoSymbol ? "Update Repo" : "Download More"}
              onClick={onDownload}
              tone="primary"
            />
            <ActionButton
              icon={<RefreshCw size={15} />}
              label="Resample"
              onClick={onResample}
              tone="secondary"
              disabled={isRepoSymbol}
            />
            <ActionButton
              icon={<Sparkles size={15} />}
              label="Smart Harvest (Auto-Tune)"
              onClick={onHarvest}
              tone="secondary"
              disabled={isRepoSymbol}
            />
            <ActionButton
              icon={<Trash2 size={15} />}
              label="Delete All"
              onClick={onDeleteAll}
              tone="danger"
              disabled={isRepoSymbol}
            />
          </div>
        }
      >
        <div
          style={{
            color: "var(--color-text-muted)",
            maxWidth: "780px",
            lineHeight: 1.7,
          }}
        >
          Inspect the full timeframe coverage for{" "}
          <strong>{group.symbol}</strong>, including date bounds, storage
          footprint, and per-file lifecycle actions.
        </div>
      </HeaderBlock>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
          gap: "12px",
        }}
      >
        <MetricTile
          label="Sources"
          value={group.sources.map(humanizeSource).join(" · ")}
          accent="#7dd3fc"
        />
        <MetricTile
          label="Timeframes"
          value={String(group.entries.length)}
          accent="#67e8f9"
        />
        <MetricTile
          label="Coverage"
          value={rangeLabel(group.start, group.end)}
          accent="#93c5fd"
        />
        <MetricTile
          label="Total Size"
          value={formatSizeMB(group.totalSizeMb)}
          accent="#bfdbfe"
        />
      </div>

      <SectionPanel
        title="Available timeframes"
        subtitle="Sort the table by timeframe, date coverage, or storage footprint."
      >
        <PanelTable
          rows={rows}
          columns={columns}
          getRowKey={(row) => row.id}
          defaultSortKey="timeframe"
          emptyMessage="No timeframe files found for this symbol."
        />
      </SectionPanel>
    </div>
  );
}

function HeaderBlock({
  eyebrow,
  title,
  breadcrumbs,
  actions,
  children,
}: Readonly<{
  eyebrow: string;
  title: string;
  breadcrumbs: string[];
  actions?: ReactNode;
  children?: ReactNode;
}>) {
  return (
    <div
      style={{
        display: "grid",
        gap: "14px",
        padding: "20px 22px",
        borderRadius: "22px",
        border: "1px solid rgba(96, 165, 250, 0.16)",
        background:
          "linear-gradient(135deg, rgba(30, 41, 59, 0.72), rgba(15, 23, 42, 0.94))",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: "16px",
          alignItems: "flex-start",
        }}
      >
        <div>
          <div
            style={{
              fontSize: "0.76rem",
              textTransform: "uppercase",
              letterSpacing: "0.18em",
              color: "#7dd3fc",
              marginBottom: "8px",
            }}
          >
            {eyebrow}
          </div>
          <div
            style={{
              fontSize: "1.85rem",
              fontWeight: 800,
              marginBottom: "10px",
            }}
          >
            {title}
          </div>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "8px",
              color: "var(--color-text-muted)",
              fontSize: "0.8rem",
            }}
          >
            {breadcrumbs.map((item, index) => (
              <span
                key={`${item}:${index}`}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "8px",
                }}
              >
                {index > 0 && (
                  <span style={{ color: "rgba(148, 163, 184, 0.56)" }}>›</span>
                )}
                {item}
              </span>
            ))}
          </div>
        </div>
        {actions}
      </div>
      {children}
    </div>
  );
}

function ActionButton({
  icon,
  label,
  onClick,
  tone,
  disabled = false,
}: Readonly<{
  icon: ReactNode;
  label: string;
  onClick: () => void;
  tone: "primary" | "secondary" | "danger";
  disabled?: boolean;
}>) {
  const tones: Record<string, CSSProperties> = {
    primary: {
      background: "linear-gradient(135deg, #2563eb, #0ea5e9)",
      color: "#eff6ff",
    },
    secondary: {
      background: "rgba(15, 23, 42, 0.82)",
      color: "var(--color-text)",
      border: "1px solid rgba(148, 163, 184, 0.18)",
    },
    danger: {
      background: "rgba(127, 29, 29, 0.22)",
      color: "#fecaca",
      border: "1px solid rgba(248, 113, 113, 0.2)",
    },
  };

  return (
    <button
      type="button"
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "8px",
        padding: "10px 14px",
        borderRadius: "12px",
        border: "none",
        cursor: disabled ? "not-allowed" : "pointer",
        fontWeight: 700,
        opacity: disabled ? 0.45 : 1,
        ...tones[tone],
      }}
    >
      {icon}
      {label}
    </button>
  );
}

const MetricTile = memo(function MetricTile({
  label,
  value,
  accent,
}: Readonly<{
  label: string;
  value: string;
  accent: string;
}>) {
  return (
    <div
      style={{
        padding: "14px 16px",
        borderRadius: "16px",
        border: "1px solid rgba(148, 163, 184, 0.12)",
        background: "rgba(15, 23, 42, 0.7)",
      }}
    >
      <div
        style={{
          fontSize: "0.72rem",
          textTransform: "uppercase",
          letterSpacing: "0.16em",
          color: "var(--color-text-muted)",
          marginBottom: "8px",
        }}
      >
        {label}
      </div>
      <div style={{ fontSize: "1.05rem", fontWeight: 700, color: accent }}>
        {value}
      </div>
    </div>
  );
});

function SectionPanel({
  title,
  subtitle,
  children,
}: Readonly<{
  title: string;
  subtitle?: string;
  children: ReactNode;
}>) {
  return (
    <div
      style={{
        padding: "18px",
        borderRadius: "20px",
        border: "1px solid rgba(148, 163, 184, 0.12)",
        background: "rgba(15, 23, 42, 0.76)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: "12px",
          alignItems: "center",
          marginBottom: "14px",
        }}
      >
        <div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "10px",
              marginBottom: "4px",
            }}
          >
            <Table2 size={16} color="#7dd3fc" />
            <h2 style={{ fontSize: "1rem", fontWeight: 700 }}>{title}</h2>
          </div>
          {subtitle && (
            <div
              style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}
            >
              {subtitle}
            </div>
          )}
        </div>
      </div>
      {children}
    </div>
  );
}

function LoadingMessage({ label }: { label: string }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "10px",
        color: "var(--color-text-muted)",
        padding: "20px 8px",
      }}
    >
      <Loader2 size={16} className="animate-spin" />
      {label}
    </div>
  );
}

function ValidationIndicator({
  status,
  warningMessage,
  compact = false,
  showMessage = false,
}: Readonly<{
  status: ValidationState;
  warningMessage?: string | null;
  compact?: boolean;
  showMessage?: boolean;
}>) {
  const hasMessage = Boolean(warningMessage) && status !== "OK";

  return (
    <div
      style={{
        display: "grid",
        gap: compact ? "0" : "6px",
        justifyItems: compact ? "start" : "center",
        textAlign: compact ? "left" : "center",
      }}
    >
      <div style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
        <ValidationBadge status={status} compact={compact} />
        {hasMessage && (
          <span
            title={warningMessage || undefined}
            style={{ display: "inline-flex", color: "var(--color-text-muted)" }}
          >
            <Info size={compact ? 12 : 14} />
          </span>
        )}
      </div>
      {hasMessage && showMessage && !compact && (
        <div
          style={{
            maxWidth: "240px",
            fontSize: "0.72rem",
            lineHeight: 1.5,
            color: "var(--color-text-muted)",
            whiteSpace: "normal",
          }}
        >
          {warningMessage}
        </div>
      )}
    </div>
  );
}

function ValidationBadge({
  status,
  compact = false,
}: Readonly<{
  status: ValidationState;
  compact?: boolean;
}>) {
  const tone =
    status === "OK"
      ? {
          background: "rgba(22, 163, 74, 0.16)",
          border: "rgba(74, 222, 128, 0.26)",
          color: "#86efac",
          icon: <ShieldCheck size={compact ? 12 : 14} />,
        }
      : {
          background: "rgba(127, 29, 29, 0.18)",
          border: "rgba(248, 113, 113, 0.28)",
          color: "#fca5a5",
          icon: <AlertTriangle size={compact ? 12 : 14} />,
        };

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: compact ? "5px" : "6px",
        padding: compact ? "3px 8px" : "5px 10px",
        borderRadius: "999px",
        fontSize: compact ? "0.68rem" : "0.74rem",
        fontWeight: 700,
        border: `1px solid ${tone.border}`,
        background: tone.background,
        color: tone.color,
      }}
    >
      {tone.icon}
      {status}
    </span>
  );
}

function PanelTable<T>({
  rows,
  columns,
  getRowKey,
  defaultSortKey,
  defaultSortDirection = "asc",
  onRowClick,
  emptyMessage,
}: Readonly<{
  rows: T[];
  columns: TableColumn<T>[];
  getRowKey: (row: T) => string;
  defaultSortKey?: string;
  defaultSortDirection?: SortDirection;
  onRowClick?: (row: T) => void;
  emptyMessage: string;
}>) {
  const [sortKey, setSortKey] = useState(defaultSortKey || "");
  const [sortDirection, setSortDirection] =
    useState<SortDirection>(defaultSortDirection);
  const [pageIndex, setPageIndex] = useState(0);
  const pageSize = 25;

  const sortedRows = useMemo(() => {
    if (!sortKey) {
      return rows;
    }
    const column = columns.find((item) => item.key === sortKey);
    if (!column) {
      return rows;
    }
    const sorted = [...rows].sort((a, b) => {
      const aValue = column.sortValue
        ? column.sortValue(a)
        : stringifySortableCell((a as Record<string, unknown>)[sortKey]);
      const bValue = column.sortValue
        ? column.sortValue(b)
        : stringifySortableCell((b as Record<string, unknown>)[sortKey]);
      return compareMaybeString(aValue, bValue);
    });
    return sortDirection === "asc" ? sorted : sorted.reverse();
  }, [columns, rows, sortDirection, sortKey]);

  const pageCount = Math.max(1, Math.ceil(sortedRows.length / pageSize));
  const safePageIndex = Math.min(pageIndex, pageCount - 1);

  const pagedRows = useMemo(
    () =>
      sortedRows.slice(
        safePageIndex * pageSize,
        safePageIndex * pageSize + pageSize,
      ),
    [pageSize, safePageIndex, sortedRows],
  );

  const handleSort = (column: TableColumn<T>) => {
    if (column.sortable === false) {
      return;
    }
    if (sortKey === column.key) {
      setSortDirection((prev) => (prev === "asc" ? "desc" : "asc"));
      setPageIndex(0);
      return;
    }
    setSortKey(column.key);
    setSortDirection("asc");
    setPageIndex(0);
  };

  if (rows.length === 0) {
    return (
      <div style={{ padding: "24px 8px", color: "var(--color-text-muted)" }}>
        {emptyMessage}
      </div>
    );
  }

  return (
    <div
      style={{
        borderRadius: "16px",
        border: "1px solid rgba(148, 163, 184, 0.12)",
        overflow: "hidden",
      }}
    >
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: "0.84rem",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <thead>
          <tr style={{ background: "rgba(15, 23, 42, 0.92)" }}>
            {columns.map((column) => (
              <th
                key={column.key}
                onClick={() => handleSort(column)}
                style={{
                  padding: "12px 14px",
                  textAlign: column.align || "left",
                  color: "var(--color-text-muted)",
                  fontWeight: 700,
                  fontSize: "0.73rem",
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  borderBottom: "1px solid rgba(148, 163, 184, 0.12)",
                  cursor: column.sortable === false ? "default" : "pointer",
                  width: column.width,
                }}
              >
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "8px",
                  }}
                >
                  {column.label}
                  {sortKey === column.key && (
                    <span>{sortDirection === "asc" ? "▲" : "▼"}</span>
                  )}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {pagedRows.map((row, index) => (
            <tr
              key={getRowKey(row)}
              onClick={() => onRowClick?.(row)}
              style={{
                background:
                  (safePageIndex * pageSize + index) % 2 === 0
                    ? "rgba(8, 15, 31, 0.46)"
                    : "rgba(15, 23, 42, 0.18)",
                cursor: onRowClick ? "pointer" : "default",
              }}
            >
              {columns.map((column) => (
                <td
                  key={column.key}
                  style={{
                    padding: "14px",
                    textAlign: column.align || "left",
                    borderBottom: "1px solid rgba(148, 163, 184, 0.08)",
                    verticalAlign: "middle",
                  }}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {sortedRows.length > pageSize && (
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "12px",
            padding: "12px 14px",
            borderTop: "1px solid rgba(148, 163, 184, 0.12)",
            background: "rgba(15, 23, 42, 0.9)",
          }}
        >
          <div
            style={{ color: "var(--color-text-muted)", fontSize: "0.76rem" }}
          >
            Showing {Math.min(safePageIndex * pageSize + 1, sortedRows.length)}–
            {Math.min((safePageIndex + 1) * pageSize, sortedRows.length)} of{" "}
            {sortedRows.length}
          </div>
          <div style={{ display: "flex", gap: "8px" }}>
            <button
              type="button"
              onClick={() =>
                setPageIndex((current) => Math.max(current - 1, 0))
              }
              disabled={safePageIndex === 0}
              style={{
                padding: "8px 12px",
                borderRadius: "10px",
                border: "1px solid rgba(148, 163, 184, 0.18)",
                background: "rgba(15, 23, 42, 0.82)",
                color: "var(--color-text)",
                cursor: safePageIndex === 0 ? "not-allowed" : "pointer",
                opacity: safePageIndex === 0 ? 0.45 : 1,
              }}
            >
              Previous
            </button>
            <button
              type="button"
              onClick={() =>
                setPageIndex((current) => Math.min(current + 1, pageCount - 1))
              }
              disabled={safePageIndex >= pageCount - 1}
              style={{
                padding: "8px 12px",
                borderRadius: "10px",
                border: "1px solid rgba(148, 163, 184, 0.18)",
                background: "rgba(15, 23, 42, 0.82)",
                color: "var(--color-text)",
                cursor:
                  safePageIndex >= pageCount - 1 ? "not-allowed" : "pointer",
                opacity: safePageIndex >= pageCount - 1 ? 0.45 : 1,
              }}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
