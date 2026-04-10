import { memo, useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowUpDown, Plus, Trash2, X } from "lucide-react";

import type {
  PortfolioSummary,
  PortfoliosDuplicatesResponse,
  PortfoliosSortField,
} from "../api/types";
import {
  createPortfolio,
  deduplicatePortfolios,
  deletePortfolio,
  deletePortfoliosBulk,
  getPortfolioDuplicates,
  getPortfolios,
  getStrategies,
  getSymbols,
} from "../api/client";
import { FilterBar } from "../components/FilterBar";
import { Modal } from "../components/Modal";
import { Sparkline } from "../components/Sparkline";
import { useToast } from "../components/ToastProvider";
import { formatPValue, pValueColor } from "../utils/stats";

function componentId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `component-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

type ComponentDraft = {
  id: string;
  strategy_name: string;
  symbol: string;
  timeframe: string;
  weight: string;
  risk_config: string;
  settings: string;
};

type StrategyOption = {
  name: string;
  tags?: Record<string, unknown>;
};

const DEFAULT_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];

const PORTFOLIO_SORT_OPTIONS: Array<{ label: string; value: PortfoliosSortField }> = [
  { label: "Created Date", value: "created_at" },
  { label: "Total Return", value: "total_return" },
  { label: "Sharpe (higher = better)", value: "sharpe_ratio" },
  { label: "PSR (higher = better)", value: "psr" },
  { label: "p-Value (lower = better)", value: "p_value" },
  { label: "CAGR (higher = better)", value: "cagr" },
  { label: "Max DD (lower = better)", value: "max_drawdown" },
  { label: "Win Rate (higher = better)", value: "win_rate" },
  { label: "Trade Count", value: "trade_count" },
];

const PortfolioCard = memo(function PortfolioCard({
  portfolio,
  onOpen,
  onDelete,
}: {
  portfolio: PortfolioSummary & { equity_mini?: number[]; max_drawdown_pct?: number; net_profit_pct?: number };
  onOpen: (id: string) => void;
  onDelete: (id: string, event: React.MouseEvent) => void;
}) {
  return (
    <div
      onClick={() => onOpen(portfolio.id)}
      style={{
        background: "var(--color-surface)",
        border: "1px solid var(--color-border)",
        borderRadius: "8px",
        padding: "16px",
        cursor: "pointer",
        position: "relative",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = "rgba(88,166,255,0.4)")}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = "var(--color-border)")}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "start",
        }}
      >
        <div style={{ fontWeight: 600, fontSize: "0.95rem" }}>{portfolio.name}</div>
        <button
          onClick={(event) => onDelete(portfolio.id, event)}
          style={{
            background: "none",
            border: "none",
            color: "var(--color-text-muted)",
            cursor: "pointer",
            padding: "2px",
          }}
        >
          <Trash2 size={14} />
        </button>
      </div>
      <div
        style={{
          fontSize: "0.75rem",
          color: "var(--color-text-muted)",
          marginTop: "4px",
        }}
      >
        {portfolio.strategy_count || 0} components • ${(portfolio.account_capital || 100000).toLocaleString()}
      </div>
      {portfolio.equity_mini && portfolio.equity_mini.length > 1 && (
        <div style={{ margin: "6px 0" }}>
          <Sparkline data={portfolio.equity_mini} width={260} height={44} />
        </div>
      )}
      <div
        style={{
          display: "flex",
          gap: "8px",
          fontSize: "0.7rem",
          color: "var(--color-text-muted)",
          flexWrap: "wrap",
        }}
      >
        {portfolio.sharpe !== 0 && (
          <span>
            Sharpe (higher = better){" "}
            <b style={{ color: portfolio.sharpe >= 1 ? "#3fb950" : "var(--color-text)" }}>
              {Number(portfolio.sharpe).toFixed(2)}
            </b>
          </span>
        )}
        {portfolio.cagr !== 0 && (
          <span>
            CAGR (higher = better){" "}
            <b style={{ color: portfolio.cagr >= 0 ? "#3fb950" : "#f85149" }}>
              {Number(portfolio.cagr).toFixed(1)}%
            </b>
          </span>
        )}
        {portfolio.max_drawdown_pct != null && portfolio.max_drawdown_pct !== 0 && (
          <span>
            Max DD (lower = better){" "}
            <b style={{ color: "#f85149" }}>-{Math.abs(Number(portfolio.max_drawdown_pct)).toFixed(1)}%</b>
          </span>
        )}
        {portfolio.net_profit_pct != null && portfolio.net_profit_pct !== 0 && (
          <span>
            PnL{" "}
            <b style={{ color: portfolio.net_profit_pct >= 0 ? "#3fb950" : "#f85149" }}>
              {portfolio.net_profit_pct >= 0 ? "+" : ""}
              {Number(portfolio.net_profit_pct).toFixed(1)}%
            </b>
          </span>
        )}
        {portfolio.psr != null && (
          <span>
            PSR (higher = better){" "}
            <b
              style={{
                color:
                  Number(portfolio.psr) > 0.95
                    ? "#3fb950"
                    : Number(portfolio.psr) > 0.5
                      ? "#d29922"
                      : "#f85149",
              }}
            >
              {(Number(portfolio.psr) * 100).toFixed(0)}%
            </b>
          </span>
        )}
        {portfolio.win_rate != null && (
          <span>
            Win Rate (higher = better){" "}
            <b style={{ color: Number(portfolio.win_rate) >= 50 ? "#3fb950" : "#f85149" }}>
              {Number(portfolio.win_rate).toFixed(1)}%
            </b>
          </span>
        )}
        <span>
          p-Value (lower = better){" "}
          <b style={{ color: pValueColor(portfolio.p_value) }}>{formatPValue(portfolio.p_value)}</b>
        </span>
      </div>
    </div>
  );
});

function createDefaultComponent(index = 0): ComponentDraft {
  return {
    id: componentId(),
    strategy_name: "",
    symbol: "",
    timeframe: "1h",
    weight: index === 0 ? "1.0" : "0.5",
    risk_config: "",
    settings: "",
  };
}

function parseOptionalJson(text: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  const parsed = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("JSON must be an object");
  }
  return parsed as Record<string, unknown>;
}

export function PortfoliosPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [portfolios, setPortfolios] = useState<PortfolioSummary[]>([]);
  const [strategies, setStrategies] = useState<StrategyOption[]>([]);
  const [symbols, setSymbols] = useState<string[]>([]);
  const [nameFilter, setNameFilter] = useState("");
  const [sortBy, setSortBy] = useState<PortfoliosSortField>("created_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [duplicatesInfo, setDuplicatesInfo] = useState<PortfoliosDuplicatesResponse | null>(null);
  const [showDuplicatesModal, setShowDuplicatesModal] = useState(false);
  const [loadingDuplicates, setLoadingDuplicates] = useState(false);
  const [runningDeduplicate, setRunningDeduplicate] = useState(false);

  const [formName, setFormName] = useState("");
  const [formCapital, setFormCapital] = useState("100000");
  const [componentsDraft, setComponentsDraft] = useState<ComponentDraft[]>([createDefaultComponent(0)]);

  const loadPortfolios = useCallback(() => {
    setLoading(true);
    getPortfolios({ name: nameFilter, sort: sortBy, order: sortOrder })
      .then((portfolioRows) => {
        setPortfolios(portfolioRows as PortfolioSummary[]);
        setSelectedIds((current) => {
          if (current.size === 0) return current;
          const valid = new Set((portfolioRows || []).map((item) => String(item.id)));
          const next = new Set<string>();
          current.forEach((id) => {
            if (valid.has(id)) next.add(id);
          });
          return next;
        });
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Failed to load portfolios";
        showToast(message, "error");
      })
      .finally(() => setLoading(false));
  }, [nameFilter, showToast, sortBy, sortOrder]);

  useEffect(() => {
    loadPortfolios();
  }, [loadPortfolios]);

  useEffect(() => {
    Promise.all([getStrategies(), getSymbols()])
      .then(([strategyRows, symbolPayload]) => {
        setStrategies((strategyRows || []) as StrategyOption[]);
        setSymbols(symbolPayload?.symbols || []);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Failed to load strategy references";
        showToast(message, "error");
      });
  }, [showToast]);

  const selectedCount = selectedIds.size;

  const setComponentField = useCallback((id: string, field: keyof ComponentDraft, value: string) => {
    setComponentsDraft((prev) => prev.map((row) => (row.id === id ? { ...row, [field]: value } : row)));
  }, []);

  const addComponentRow = useCallback(() => {
    setComponentsDraft((prev) => [...prev, createDefaultComponent(prev.length)]);
  }, []);

  const removeComponentRow = useCallback((id: string) => {
    setComponentsDraft((prev) => {
      if (prev.length <= 1) return prev;
      return prev.filter((row) => row.id !== id);
    });
  }, []);

  const resetCreateForm = useCallback(() => {
    setFormName("");
    setFormCapital("100000");
    setComponentsDraft([createDefaultComponent(0)]);
  }, []);

  const handleCreate = async () => {
    if (!formName.trim()) {
      showToast("Portfolio name is required", "error");
      return;
    }
    if (componentsDraft.length === 0) {
      showToast("Add at least one component", "error");
      return;
    }

    let parsedCapital = Number(formCapital);
    if (!Number.isFinite(parsedCapital) || parsedCapital <= 0) {
      parsedCapital = 100000;
    }

    try {
      const components = componentsDraft.map((row, index) => {
        const strategy_name = row.strategy_name.trim();
        const symbol = row.symbol.trim().toUpperCase();
        const timeframe = row.timeframe.trim() || "1h";
        const weightValue = Number(row.weight);
        const weight = Number.isFinite(weightValue) && weightValue > 0 ? weightValue : 1.0;
        if (!strategy_name) throw new Error(`Component #${index + 1}: strategy is required`);
        if (!symbol) throw new Error(`Component #${index + 1}: symbol is required`);
        return {
          strategy_name,
          symbol,
          timeframe,
          weight,
          risk_config: parseOptionalJson(row.risk_config),
          settings: parseOptionalJson(row.settings),
        };
      });

      const created = await createPortfolio({
        name: formName.trim(),
        account_capital: parsedCapital,
        components,
      });
      setShowCreate(false);
      resetCreateForm();
      if (created?.id) {
        navigate(`/portfolios/${created.id}`);
      } else {
        loadPortfolios();
      }
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "Failed to create portfolio";
      showToast(message, "error");
    }
  };

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm("Delete this portfolio?")) return;
    try {
      await deletePortfolio(id);
      setSelectedIds((current) => {
        const next = new Set(current);
        next.delete(id);
        return next;
      });
      showToast("Portfolio deleted.", "success");
      loadPortfolios();
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "Failed to delete portfolio";
      showToast(message, "error");
    }
  };

  const handleOpenPortfolio = useCallback(
    (id: string) => {
      navigate(`/portfolios/${id}`);
    },
    [navigate],
  );

  const toggleSelected = (id: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleBulkDelete = async () => {
    if (selectedCount === 0) return;
    const ids = [...selectedIds];
    if (!window.confirm(`Delete ${ids.length} selected portfolios?`)) return;
    try {
      const result = await deletePortfoliosBulk(ids);
      showToast(`Deleted ${result.deleted} portfolio(s).`, "success");
      setSelectedIds(new Set());
      loadPortfolios();
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "Bulk delete failed";
      showToast(message, "error");
    }
  };

  const handleShowDuplicates = async () => {
    setLoadingDuplicates(true);
    try {
      const payload = await getPortfolioDuplicates();
      setDuplicatesInfo(payload);
      setShowDuplicatesModal(true);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "Could not check duplicates";
      showToast(message, "error");
    } finally {
      setLoadingDuplicates(false);
    }
  };

  const handleDeduplicate = async () => {
    setRunningDeduplicate(true);
    try {
      const payload = await deduplicatePortfolios();
      showToast(`Removed ${payload.deleted_ids.length} duplicate portfolio(s).`, "success");
      setShowDuplicatesModal(false);
      await loadPortfolios();
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "Could not remove duplicates";
      showToast(message, "error");
    } finally {
      setRunningDeduplicate(false);
    }
  };

  const inputStyle: React.CSSProperties = {
    background: "var(--color-surface2)",
    border: "1px solid var(--color-border)",
    color: "var(--color-text)",
    padding: "6px 10px",
    borderRadius: "4px",
    fontSize: "0.82rem",
    width: "100%",
  };

  const createDisabled = !formName.trim() || componentsDraft.length === 0;

  return (
    <div style={{ padding: "24px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "20px",
        }}
      >
        <h1 style={{ fontSize: "1.5rem", fontWeight: 600 }}>Portfolios</h1>
        <button
          onClick={() => setShowCreate(true)}
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
          <Plus size={14} /> New Portfolio
        </button>
      </div>

      <FilterBar
        searchValue={nameFilter}
        onSearchChange={setNameFilter}
        searchPlaceholder="Name (server-side contains)..."
        sortOptions={PORTFOLIO_SORT_OPTIONS}
        sortValue={sortBy}
        onSortChange={(value) => {
          const next = value as PortfoliosSortField;
          setSortBy(next);
          if (next === "p_value" || next === "max_drawdown") {
            setSortOrder("asc");
          } else {
            setSortOrder("desc");
          }
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
      ) : portfolios.length === 0 ? (
        <div
          style={{
            textAlign: "center",
            color: "var(--color-text-muted)",
            padding: "60px 0",
          }}
        >
          No portfolios yet. Create one!
        </div>
      ) : (
        <>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "10px",
              gap: "10px",
              flexWrap: "wrap",
            }}
          >
            <span style={{ color: "var(--color-text-muted)", fontSize: "0.78rem" }}>
              {portfolios.length} result(s)
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
              <button
                type="button"
                onClick={() => setSortOrder((current) => (current === "desc" ? "asc" : "desc"))}
                style={{
                  borderRadius: "8px",
                  border: "1px solid rgba(143,154,184,0.34)",
                  background: "rgba(17,24,39,0.95)",
                  color: "var(--color-text)",
                  padding: "7px 11px",
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
              <button
                type="button"
                onClick={() => void handleShowDuplicates()}
                disabled={loadingDuplicates}
                style={{
                  borderRadius: "8px",
                  border: "1px solid rgba(248,113,113,0.4)",
                  background: "rgba(127,29,29,0.2)",
                  color: "#fecaca",
                  padding: "7px 11px",
                  fontSize: "0.74rem",
                  cursor: loadingDuplicates ? "not-allowed" : "pointer",
                  opacity: loadingDuplicates ? 0.6 : 1,
                }}
              >
                {loadingDuplicates ? "Checking..." : "Remove Duplicates"}
              </button>
              {selectedCount > 0 ? (
                <button
                  type="button"
                  onClick={() => void handleBulkDelete()}
                  style={{
                    borderRadius: "8px",
                    border: "1px solid rgba(248,113,113,0.4)",
                    background: "rgba(127,29,29,0.2)",
                    color: "#fecaca",
                    padding: "7px 11px",
                    fontSize: "0.74rem",
                    cursor: "pointer",
                  }}
                >
                  Delete Selected ({selectedCount})
                </button>
              ) : null}
            </div>
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
              gap: "12px",
            }}
          >
            {portfolios.map((portfolio) => (
              <div key={portfolio.id} style={{ position: "relative" }}>
                <input
                  type="checkbox"
                  checked={selectedIds.has(String(portfolio.id))}
                  onChange={() => toggleSelected(String(portfolio.id))}
                  style={{
                    position: "absolute",
                    top: "10px",
                    left: "10px",
                    zIndex: 2,
                    accentColor: "#58a6ff",
                    marginTop: "2px",
                  }}
                  aria-label={`Select portfolio ${portfolio.name || portfolio.id}`}
                />
                <PortfolioCard portfolio={portfolio} onOpen={handleOpenPortfolio} onDelete={handleDelete} />
              </div>
            ))}
          </div>
        </>
      )}

      <Modal
        open={showDuplicatesModal}
        onClose={() => {
          if (runningDeduplicate) return;
          setShowDuplicatesModal(false);
        }}
        title="Remove Portfolio Duplicates"
        width="520px"
      >
        <div style={{ display: "grid", gap: "14px" }}>
          <div style={{ color: "var(--color-text-muted)", fontSize: "0.84rem", lineHeight: 1.45 }}>
            Found <b>{duplicatesInfo?.total_groups ?? 0}</b> duplicate group(s) across{" "}
            <b>{duplicatesInfo?.total_portfolios ?? 0}</b> portfolios.
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
              {duplicatesInfo.groups.slice(0, 8).map((group) => (
                <div key={group.fingerprint} style={{ padding: "5px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
                  {group.name || "Untitled"} · {group.component_count} components · {group.portfolios.length} entries
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

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="Create Portfolio" width="1100px">
        <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 240px", gap: "10px" }}>
            <div>
              <label
                style={{
                  color: "var(--color-text-muted)",
                  fontSize: "0.75rem",
                  display: "block",
                  marginBottom: "4px",
                }}
              >
                Portfolio Name
              </label>
              <input
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                style={inputStyle}
                placeholder="My Multi-Strategy Portfolio"
              />
            </div>
            <div>
              <label
                style={{
                  color: "var(--color-text-muted)",
                  fontSize: "0.75rem",
                  display: "block",
                  marginBottom: "4px",
                }}
              >
                Account Capital ($)
              </label>
              <input
                type="number"
                value={formCapital}
                onChange={(e) => setFormCapital(e.target.value)}
                style={inputStyle}
              />
            </div>
          </div>

          <div
            style={{
              border: "1px solid var(--color-border)",
              borderRadius: "8px",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1.2fr 1fr 110px 100px 1fr 1fr 44px",
                gap: "8px",
                padding: "10px 10px",
                fontSize: "0.7rem",
                color: "var(--color-text-muted)",
                borderBottom: "1px solid var(--color-border)",
                background: "var(--color-surface2)",
                textTransform: "uppercase",
                letterSpacing: "0.02em",
              }}
            >
              <div>Strategy</div>
              <div>Symbol</div>
              <div>Timeframe</div>
              <div>Weight</div>
              <div>Risk Config (JSON)</div>
              <div>Settings (JSON)</div>
              <div />
            </div>
            <div style={{ maxHeight: "360px", overflowY: "auto" }}>
              {componentsDraft.map((row) => (
                <div
                  key={row.id}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1.2fr 1fr 110px 100px 1fr 1fr 44px",
                    gap: "8px",
                    padding: "10px",
                    borderBottom: "1px solid rgba(255,255,255,0.04)",
                  }}
                >
                  <div>
                    <input
                      list="portfolio-strategy-options"
                      value={row.strategy_name}
                      onChange={(e) => setComponentField(row.id, "strategy_name", e.target.value)}
                      style={inputStyle}
                      placeholder="Select strategy"
                    />
                  </div>
                  <div>
                    <input
                      list="portfolio-symbol-options"
                      value={row.symbol}
                      onChange={(e) => setComponentField(row.id, "symbol", e.target.value.toUpperCase())}
                      style={inputStyle}
                      placeholder="EURUSD"
                    />
                  </div>
                  <div>
                    <select
                      value={row.timeframe}
                      onChange={(e) => setComponentField(row.id, "timeframe", e.target.value)}
                      style={inputStyle}
                    >
                      {DEFAULT_TIMEFRAMES.map((tf) => (
                        <option key={tf} value={tf}>
                          {tf}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <input
                      type="number"
                      step="0.01"
                      min="0.01"
                      value={row.weight}
                      onChange={(e) => setComponentField(row.id, "weight", e.target.value)}
                      style={inputStyle}
                      placeholder="1.0"
                    />
                  </div>
                  <div>
                    <input
                      value={row.risk_config}
                      onChange={(e) => setComponentField(row.id, "risk_config", e.target.value)}
                      style={inputStyle}
                      placeholder='{"max_positions": 3}'
                    />
                  </div>
                  <div>
                    <input
                      value={row.settings}
                      onChange={(e) => setComponentField(row.id, "settings", e.target.value)}
                      style={inputStyle}
                      placeholder='{"variant":"A"}'
                    />
                  </div>
                  <button
                    type="button"
                    onClick={() => removeComponentRow(row.id)}
                    style={{
                      border: "1px solid var(--color-border)",
                      borderRadius: "6px",
                      background: "transparent",
                      color: "var(--color-text-muted)",
                      cursor: componentsDraft.length <= 1 ? "not-allowed" : "pointer",
                      opacity: componentsDraft.length <= 1 ? 0.4 : 1,
                    }}
                    disabled={componentsDraft.length <= 1}
                    title="Remove component"
                  >
                    <X size={14} />
                  </button>
                </div>
              ))}
            </div>
          </div>

          <datalist id="portfolio-strategy-options">
            {strategies.map((strategy) => (
              <option key={strategy.name} value={strategy.name} />
            ))}
          </datalist>
          <datalist id="portfolio-symbol-options">
            {symbols.map((symbol) => (
              <option key={symbol} value={symbol} />
            ))}
          </datalist>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "10px" }}>
            <button
              type="button"
              onClick={addComponentRow}
              style={{
                border: "1px solid rgba(88,166,255,0.45)",
                borderRadius: "6px",
                background: "rgba(88,166,255,0.12)",
                color: "#9ecbff",
                padding: "8px 12px",
                fontSize: "0.82rem",
                cursor: "pointer",
              }}
            >
              + Add Component
            </button>
            <button
              onClick={() => void handleCreate()}
              disabled={createDisabled}
              style={{
                background: createDisabled ? "#333" : "#58a6ff",
                color: "#fff",
                border: "none",
                borderRadius: "6px",
                padding: "9px 14px",
                fontSize: "0.85rem",
                cursor: createDisabled ? "not-allowed" : "pointer",
              }}
            >
              Create Portfolio
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
