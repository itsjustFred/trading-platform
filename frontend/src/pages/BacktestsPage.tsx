import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { getBacktests } from "../api/client";
import { FilterBar } from "../components/FilterBar";
import { CreateBacktestModal } from "../components/CreateBacktestModal";
import { Plus, GitCompare } from "lucide-react";
import { Sparkline } from "../components/Sparkline";
import { formatPValue, metricValue, pValueColor, pValueMetric } from "../utils/stats";

export function BacktestsPage() {
  const navigate = useNavigate();
  const [backtests, setBacktests] = useState<any[]>([]);
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [sortBy, setSortBy] = useState("sharpe");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);

  useEffect(() => {
    getBacktests()
      .then((data) => {
        setBacktests(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

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

  const filtered = useMemo(() => {
    let result = backtests.filter((bt) => {
      const s = search.toLowerCase();
      const matchSearch =
        !s ||
        bt.title?.toLowerCase().includes(s) ||
        bt.strategy_display_name?.toLowerCase().includes(s) ||
        bt.strategy?.toLowerCase().includes(s) ||
        bt.id?.toLowerCase().includes(s) ||
        bt.symbols?.some((sym: string) => sym.toLowerCase().includes(s));
      const matchEngine = !filters.engine || bt.engine === filters.engine;
      const matchStrategy =
        !filters.strategy || bt.strategy === filters.strategy;
      const matchTf =
        !filters.timeframe || (bt.timeframes || []).includes(filters.timeframe);
      return matchSearch && matchEngine && matchStrategy && matchTf;
    });

    result.sort((a, b) => {
      switch (sortBy) {
        case "created_desc":
          return Date.parse(b.created_at || "") - Date.parse(a.created_at || "");
        case "created_asc":
          return Date.parse(a.created_at || "") - Date.parse(b.created_at || "");
        case "name_asc":
          return (a.title || a.strategy_display_name || a.strategy || "").localeCompare(
            b.title || b.strategy_display_name || b.strategy || "",
          );
        case "name_desc":
          return (b.title || b.strategy_display_name || b.strategy || "").localeCompare(
            a.title || a.strategy_display_name || a.strategy || "",
          );
        case "trades_desc":
          return (b.trade_count || 0) - (a.trade_count || 0);
        case "trades_asc":
          return (a.trade_count || 0) - (b.trade_count || 0);
        case "psr":
          return metricValue(b.psr) - metricValue(a.psr);
        case "cagr":
          return metricValue(b.cagr) - metricValue(a.cagr);
        case "p_value":
          return pValueMetric(a.p_value) - pValueMetric(b.p_value);
        case "net_profit":
          return metricValue(b.net_profit) - metricValue(a.net_profit);
        default:
          return metricValue(b.sharpe) - metricValue(a.sharpe);
      }
    });
    return result;
  }, [backtests, search, filters, sortBy]);

  const toggleSelect = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div style={{ padding: "24px" }}>
      {/* Header */}
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
          {selected.size >= 2 && (
            <button
              onClick={() =>
                navigate(`/backtests/compare?ids=${[...selected].join(",")}`)
              }
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
          )}
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
          { label: "PSR (High to Low)", value: "psr" },
          { label: "CAGR (High to Low)", value: "cagr" },
          { label: "P-Value (Low to High)", value: "p_value" },
          { label: "Net Profit (High to Low)", value: "net_profit" },
        ]}
        sortValue={sortBy}
        onSortChange={setSortBy}
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
              onMouseEnter={(e) => {
                if (!selected.has(bt.id))
                  e.currentTarget.style.borderColor = "rgba(88,166,255,0.4)";
              }}
              onMouseLeave={(e) => {
                if (!selected.has(bt.id))
                  e.currentTarget.style.borderColor = "var(--color-border)";
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
                      {bt.title && bt.strategy_display_name && bt.title !== bt.strategy_display_name
                        ? ` • ${bt.strategy_display_name}`
                        : ""}
                    </div>
                  </div>
                <input
                  type="checkbox"
                  checked={selected.has(bt.id)}
                  onChange={() => {}}
                  onClick={(e) => toggleSelect(bt.id, e)}
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
                {bt.created_at ? (
                  <span>{new Date(bt.created_at).toLocaleDateString("en-US")}</span>
                ) : null}
              </div>
              {bt.equity_mini && bt.equity_mini.length > 1 && (
                <div style={{ margin: "6px 0" }}>
                  <Sparkline data={bt.equity_mini} width={280} height={44} />
                </div>
              )}
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
                {bt.sharpe != null && (
                  <span>
                    Sharpe{" "}
                    <b
                      style={{
                        color:
                          bt.sharpe >= 1
                            ? "#3fb950"
                            : bt.sharpe >= 0
                              ? "var(--color-text)"
                              : "#f85149",
                      }}
                    >
                      {bt.sharpe?.toFixed(2)}
                    </b>
                  </span>
                )}
                {bt.cagr != null && (
                  <span>
                    CAGR{" "}
                    <b style={{ color: bt.cagr >= 0 ? "#3fb950" : "#f85149" }}>
                      {bt.cagr?.toFixed(1)}%
                    </b>
                  </span>
                )}
                {bt.max_drawdown_pct != null && (
                  <span>
                    DD{" "}
                    <b style={{ color: "#f85149" }}>
                      -{bt.max_drawdown_pct?.toFixed(1)}%
                    </b>
                  </span>
                )}
                {bt.net_profit_pct != null && (
                  <span>
                    PnL{" "}
                    <b
                      style={{
                        color: bt.net_profit_pct >= 0 ? "#3fb950" : "#f85149",
                      }}
                    >
                      {bt.net_profit_pct >= 0 ? "+" : ""}
                      {bt.net_profit_pct?.toFixed(1)}%
                    </b>
                  </span>
                )}
                {bt.psr != null && (
                  <span>
                    PSR{" "}
                    <b
                      style={{
                        color:
                          bt.psr > 0.95
                            ? "#3fb950"
                            : bt.psr > 0.5
                              ? "#d29922"
                              : "#f85149",
                      }}
                    >
                      {(bt.psr * 100).toFixed(0)}%
                    </b>
                  </span>
                )}
                <span>
                  P-Value{" "}
                  <b style={{ color: pValueColor(bt.p_value) }}>
                    {formatPValue(bt.p_value)}
                  </b>
                </span>
              </div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: "0.8rem",
                }}
              >
                <span style={{ color: "var(--color-text-muted)" }}>
                  {bt.trade_count} trades
                </span>
                <span
                  style={{
                    color: "var(--color-text-muted)",
                    fontSize: "0.72rem",
                  }}
                >
                  {bt.start?.slice(0, 10) || "?"} →{" "}
                  {bt.end?.slice(0, 10) || "?"}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      <CreateBacktestModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onTaskStarted={() => setShowCreateModal(false)}
      />
    </div>
  );
}
