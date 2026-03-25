import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  deduplicateBacktests,
  getBacktests,
  getBacktestDuplicates,
} from "../api/client";
import { FilterBar } from "../components/FilterBar";
import { CreateBacktestModal } from "../components/CreateBacktestModal";
import { Plus, GitCompare, Trash2 } from "lucide-react";
import { Sparkline } from "../components/Sparkline";
import { formatPValue, pValueColor } from "../utils/stats";
import type { BacktestListItem } from "../api/types";
import { useToast } from "../components/ToastProvider";

type SortValue =
  | "created_desc"
  | "created_asc"
  | "name_asc"
  | "name_desc"
  | "trades_desc"
  | "trades_asc"
  | "sharpe"
  | "cagr";

function sortQueryFor(value: SortValue): { sort: string; order: "asc" | "desc" } {
  switch (value) {
    case "created_asc":
      return { sort: "created_at", order: "asc" };
    case "created_desc":
      return { sort: "created_at", order: "desc" };
    case "name_asc":
      return { sort: "strategy_name", order: "asc" };
    case "name_desc":
      return { sort: "strategy_name", order: "desc" };
    case "trades_asc":
      return { sort: "trade_count", order: "asc" };
    case "trades_desc":
      return { sort: "trade_count", order: "desc" };
    case "cagr":
      return { sort: "total_return", order: "desc" };
    case "sharpe":
    default:
      return { sort: "sharpe_ratio", order: "desc" };
  }
}

export function BacktestsPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [backtests, setBacktests] = useState<BacktestListItem[]>([]);
  const [totalBacktests, setTotalBacktests] = useState(0);
  const [page, setPage] = useState(1);
  const [perPage] = useState(50);
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [sortBy, setSortBy] = useState<SortValue>("created_desc");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [deduplicating, setDeduplicating] = useState(false);

  const sortQuery = useMemo(() => sortQueryFor(sortBy), [sortBy]);

  const loadPage = useMemo(
    () => async () => {
      setLoading(true);
      try {
        const payload = await getBacktests({
          page,
          per_page: perPage,
          sort: sortQuery.sort,
          order: sortQuery.order,
        });
        setBacktests(payload.items || []);
        setTotalBacktests(payload.total || 0);
      } finally {
        setLoading(false);
      }
    },
    [page, perPage, sortQuery.order, sortQuery.sort],
  );

  useEffect(() => {
    void loadPage();
  }, [loadPage]);

  const engines = useMemo(
    () => [...new Set(backtests.map((b) => b.engine).filter(Boolean))],
    [backtests],
  );
  const strategies = useMemo(
    () => [...new Set(backtests.map((b) => b.strategy).filter(Boolean))],
    [backtests],
  );
  const timeframes = useMemo(
    () => [...new Set(backtests.flatMap((b) => b.timeframes || []))],
    [backtests],
  );

  const filtered = useMemo(
    () =>
      backtests.filter((bt) => {
        const s = search.toLowerCase();
        const matchSearch =
          !s ||
          bt.title?.toLowerCase().includes(s) ||
          bt.strategy_display_name?.toLowerCase().includes(s) ||
          bt.strategy?.toLowerCase().includes(s) ||
          bt.id?.toLowerCase().includes(s) ||
          bt.symbols?.some((sym: string) => sym.toLowerCase().includes(s));
        const matchEngine = !filters.engine || bt.engine === filters.engine;
        const matchStrategy = !filters.strategy || bt.strategy === filters.strategy;
        const matchTf = !filters.timeframe || (bt.timeframes || []).includes(filters.timeframe);
        return matchSearch && matchEngine && matchStrategy && matchTf;
      }),
    [backtests, search, filters],
  );

  const totalPages = Math.max(1, Math.ceil(totalBacktests / perPage));

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  const toggleSelect = (id: string, event: React.MouseEvent) => {
    event.stopPropagation();
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
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
        `Found ${duplicates.total_groups} duplicate groups. Remove older duplicates and keep only the newest run per fingerprint?`,
      );
      if (!confirmed) {
        return;
      }

      const result = await deduplicateBacktests();
      showToast(
        `Removed ${result.deleted_ids.length} duplicates across ${result.groups_affected} groups.`,
        "success",
      );
      setSelected(new Set());
      await loadPage();
    } catch (error: any) {
      showToast(error.message || "Failed to remove duplicates.", "error");
    } finally {
      setDeduplicating(false);
    }
  };

  return (
    <div style={{ padding: "24px" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "20px",
        }}
      >
        <h1 style={{ fontSize: "1.5rem", fontWeight: 600 }}>Backtests</h1>
        <div style={{ display: "flex", gap: "8px" }}>
          {selected.size >= 2 ? (
            <button
              onClick={() => navigate(`/backtests/compare?ids=${[...selected].join(",")}`)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                background: "rgba(88,166,255,0.1)",
                color: "#58a6ff",
                border: "1px solid rgba(88,166,255,0.3)",
                borderRadius: "6px",
                padding: "6px 12px",
                fontSize: "0.82rem",
                cursor: "pointer",
              }}
            >
              <GitCompare size={14} /> Compare ({selected.size})
            </button>
          ) : null}
          <button
            onClick={() => void handleDeduplicate()}
            disabled={deduplicating}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              background: "rgba(248,81,73,0.1)",
              color: "#f85149",
              border: "1px solid rgba(248,81,73,0.3)",
              borderRadius: "6px",
              padding: "6px 12px",
              fontSize: "0.82rem",
              cursor: deduplicating ? "not-allowed" : "pointer",
              opacity: deduplicating ? 0.7 : 1,
            }}
          >
            <Trash2 size={14} /> {deduplicating ? "Removing..." : "Remove Duplicates"}
          </button>
          <button
            onClick={() => setShowCreateModal(true)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              background: "#58a6ff",
              color: "#fff",
              border: "none",
              borderRadius: "6px",
              padding: "6px 12px",
              fontSize: "0.82rem",
              cursor: "pointer",
            }}
          >
            <Plus size={14} /> New Backtest
          </button>
        </div>
      </div>

      <FilterBar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Search by strategy, symbol, ID..."
        filters={[
          {
            key: "engine",
            label: "Engine",
            options: engines.map((e) => ({ label: e, value: e })),
          },
          {
            key: "strategy",
            label: "Strategy",
            options: strategies.map((s) => ({ label: s, value: s })),
          },
          {
            key: "timeframe",
            label: "Timeframe",
            options: timeframes.map((t) => ({ label: t, value: t })),
          },
        ]}
        filterValues={filters}
        onFilterChange={(k, v) => setFilters((prev) => ({ ...prev, [k]: v }))}
        sortOptions={[
          { label: "Creation Date (Newest First)", value: "created_desc" },
          { label: "Creation Date (Oldest First)", value: "created_asc" },
          { label: "Name (A-Z)", value: "name_asc" },
          { label: "Name (Z-A)", value: "name_desc" },
          { label: "Trade Count (High to Low)", value: "trades_desc" },
          { label: "Trade Count (Low to High)", value: "trades_asc" },
          { label: "Sharpe Ratio (High to Low)", value: "sharpe" },
          { label: "CAGR (High to Low)", value: "cagr" },
        ]}
        sortValue={sortBy}
        onSortChange={(value) => {
          setSortBy(value as SortValue);
          setPage(1);
        }}
      />

      {loading ? (
        <div
          style={{
            textAlign: "center",
            color: "var(--color-text-muted)",
            padding: "60px 0",
          }}
        >
          Loading...
        </div>
      ) : filtered.length === 0 ? (
        <div
          style={{
            textAlign: "center",
            color: "var(--color-text-muted)",
            padding: "60px 0",
          }}
        >
          No backtests found
        </div>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
            gap: "12px",
          }}
        >
          {filtered.map((bt) => (
            <div
              key={bt.id}
              onClick={() => navigate(`/backtests/${bt.id}`)}
              style={{
                background: "var(--color-surface)",
                border: `1px solid ${selected.has(bt.id) ? "#58a6ff" : "var(--color-border)"}`,
                borderRadius: "8px",
                padding: "16px",
                cursor: "pointer",
                transition: "border-color 0.15s",
              }}
              onMouseEnter={(event) => {
                if (!selected.has(bt.id)) {
                  event.currentTarget.style.borderColor = "rgba(88,166,255,0.4)";
                }
              }}
              onMouseLeave={(event) => {
                if (!selected.has(bt.id)) {
                  event.currentTarget.style.borderColor = "var(--color-border)";
                }
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "start",
                  marginBottom: "8px",
                }}
              >
                <div>
                  <div style={{ fontWeight: 600, fontSize: "0.9rem" }}>
                    {bt.title || bt.strategy_display_name || bt.strategy || "Unknown"}
                  </div>
                  <div
                    style={{
                      color: "var(--color-text-muted)",
                      fontSize: "0.75rem",
                    }}
                  >
                    {bt.engine}
                    {bt.title &&
                    bt.strategy_display_name &&
                    bt.title !== bt.strategy_display_name
                      ? ` • ${bt.strategy_display_name}`
                      : ""}
                  </div>
                </div>
                <input
                  type="checkbox"
                  checked={selected.has(bt.id)}
                  onChange={() => {}}
                  onClick={(event) => toggleSelect(bt.id, event)}
                  style={{ cursor: "pointer", accentColor: "#58a6ff" }}
                />
              </div>
              <div
                style={{
                  display: "flex",
                  gap: "12px",
                  fontSize: "0.75rem",
                  color: "var(--color-text-muted)",
                  marginBottom: "8px",
                }}
              >
                <span>{bt.symbols?.join(", ") || "—"}</span>
                <span>{(bt.timeframes || []).join(", ") || "—"}</span>
                {bt.created_at ? <span>{new Date(bt.created_at).toLocaleDateString("en-US")}</span> : null}
              </div>
              {bt.equity_mini && bt.equity_mini.length > 1 ? (
                <div style={{ margin: "6px 0" }}>
                  <Sparkline data={bt.equity_mini} width={280} height={44} />
                </div>
              ) : null}
              <div
                style={{
                  display: "flex",
                  gap: "8px",
                  fontSize: "0.7rem",
                  color: "var(--color-text-muted)",
                  marginBottom: "6px",
                  flexWrap: "wrap",
                }}
              >
                {bt.sharpe != null ? (
                  <span>
                    Sharpe{" "}
                    <b
                      style={{
                        color: bt.sharpe >= 1 ? "#3fb950" : bt.sharpe >= 0 ? "var(--color-text)" : "#f85149",
                      }}
                    >
                      {bt.sharpe?.toFixed(2)}
                    </b>
                  </span>
                ) : null}
                {bt.cagr != null ? (
                  <span>
                    CAGR <b style={{ color: bt.cagr >= 0 ? "#3fb950" : "#f85149" }}>{bt.cagr?.toFixed(1)}%</b>
                  </span>
                ) : null}
                {bt.max_drawdown_pct != null ? (
                  <span>
                    DD <b style={{ color: "#f85149" }}>-{bt.max_drawdown_pct?.toFixed(1)}%</b>
                  </span>
                ) : null}
                {bt.net_profit_pct != null ? (
                  <span>
                    PnL{" "}
                    <b style={{ color: bt.net_profit_pct >= 0 ? "#3fb950" : "#f85149" }}>
                      {bt.net_profit_pct >= 0 ? "+" : ""}
                      {bt.net_profit_pct?.toFixed(1)}%
                    </b>
                  </span>
                ) : null}
                <span>
                  P-Value <b style={{ color: pValueColor(bt.p_value) }}>{formatPValue(bt.p_value)}</b>
                </span>
              </div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: "0.8rem",
                }}
              >
                <span style={{ color: "var(--color-text-muted)" }}>{bt.trade_count} trades</span>
                <span
                  style={{
                    color: "var(--color-text-muted)",
                    fontSize: "0.72rem",
                  }}
                >
                  {bt.start?.slice(0, 10) || "?"} → {bt.end?.slice(0, 10) || "?"}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginTop: "16px",
          gap: "12px",
        }}
      >
        <span style={{ color: "var(--color-text-muted)", fontSize: "0.82rem" }}>
          Page {page} of {totalPages} • {totalBacktests} total
        </span>
        <div style={{ display: "flex", gap: "8px" }}>
          <button
            onClick={() => setPage((current) => Math.max(1, current - 1))}
            disabled={page <= 1 || loading}
            style={{
              padding: "6px 10px",
              borderRadius: "6px",
              border: "1px solid var(--color-border)",
              background: "var(--color-surface)",
              color: "var(--color-text)",
              cursor: page <= 1 || loading ? "not-allowed" : "pointer",
            }}
          >
            Prev
          </button>
          <button
            onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
            disabled={page >= totalPages || loading}
            style={{
              padding: "6px 10px",
              borderRadius: "6px",
              border: "1px solid var(--color-border)",
              background: "var(--color-surface)",
              color: "var(--color-text)",
              cursor: page >= totalPages || loading ? "not-allowed" : "pointer",
            }}
          >
            Next
          </button>
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

