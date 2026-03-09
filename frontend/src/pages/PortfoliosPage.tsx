import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import {
  getPortfolios,
  getStrategies,
  createPortfolio,
  deletePortfolio,
} from "../api/client";
import { FilterBar } from "../components/FilterBar";
import { Modal } from "../components/Modal";
import { Plus, Trash2 } from "lucide-react";
import { Sparkline } from "../components/Sparkline";
import { formatPValue, metricValue, pValueColor, pValueMetric } from "../utils/stats";

export function PortfoliosPage() {
  const navigate = useNavigate();
  const [portfolios, setPortfolios] = useState<any[]>([]);
  const [strategies, setStrategies] = useState<any[]>([]);
  const [search, setSearch] = useState("");
  const [sortBy, setSortBy] = useState("sharpe");
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);

  // Create form state
  const [formName, setFormName] = useState("");
  const [formCapital, setFormCapital] = useState("100000");
  const [selectedStrategies, setSelectedStrategies] = useState<
    { name: string; risk: string; maxPos: string }[]
  >([]);

  const load = () => {
    setLoading(true);
    Promise.all([getPortfolios(), getStrategies()])
      .then(([p, s]) => {
        setPortfolios(p);
        setStrategies(s);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    let result = portfolios.filter((p) => {
      const q = search.toLowerCase();
      return (
        !q ||
        p.name?.toLowerCase().includes(q) ||
        p.id?.toLowerCase().includes(q)
      );
    });
    result.sort((a, b) => {
      switch (sortBy) {
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
  }, [portfolios, search, sortBy]);

  const toggleStrategy = (name: string) => {
    setSelectedStrategies((prev) => {
      if (prev.find((s) => s.name === name))
        return prev.filter((s) => s.name !== name);
      return [...prev, { name, risk: "1.0", maxPos: "5" }];
    });
  };

  const updateStrategyField = (
    name: string,
    field: "risk" | "maxPos",
    value: string,
  ) => {
    setSelectedStrategies((prev) =>
      prev.map((s) => (s.name === name ? { ...s, [field]: value } : s)),
    );
  };

  const handleCreate = async () => {
    if (!formName || selectedStrategies.length === 0) return;
    const body = {
      name: formName,
      account_capital: parseFloat(formCapital),
      strategies: selectedStrategies.map((s) => ({
        name: s.name,
        risk_per_trade_pct: parseFloat(s.risk),
        max_positions: parseInt(s.maxPos),
      })),
    };
    try {
      const result = await createPortfolio(body);
      setShowCreate(false);
      setFormName("");
      setSelectedStrategies([]);
      if (result?.id) {
        navigate(`/portfolios/${result.id}`);
      } else {
        load();
      }
    } catch (err: any) {
      alert(err.message || "Failed to create portfolio");
    }
  };

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (confirm("Delete this portfolio?")) {
      await deletePortfolio(id);
      load();
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
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Search portfolios..."
        sortOptions={[
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
          No portfolios yet. Create one!
        </div>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
            gap: "12px",
          }}
        >
          {filtered.map((p) => (
            <div
              key={p.id}
              onClick={() => navigate(`/portfolios/${p.id}`)}
              style={{
                background: "var(--color-surface)",
                border: "1px solid var(--color-border)",
                borderRadius: "8px",
                padding: "16px",
                cursor: "pointer",
                position: "relative",
              }}
              onMouseEnter={(e) =>
                (e.currentTarget.style.borderColor = "rgba(88,166,255,0.4)")
              }
              onMouseLeave={(e) =>
                (e.currentTarget.style.borderColor = "var(--color-border)")
              }
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "start",
                }}
              >
                <div style={{ fontWeight: 600, fontSize: "0.95rem" }}>
                  {p.name}
                </div>
                <button
                  onClick={(e) => handleDelete(p.id, e)}
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
                {p.strategy_count || p.strategies?.length || 0} strategies • $
                {(p.account_capital || 100000).toLocaleString()}
              </div>
              {p.equity_mini && p.equity_mini.length > 1 && (
                <div style={{ margin: "6px 0" }}>
                  <Sparkline data={p.equity_mini} width={260} height={44} />
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
                {p.sharpe != null && p.sharpe !== 0 && (
                  <span>
                    Sharpe{" "}
                    <b
                      style={{
                        color: p.sharpe >= 1 ? "#3fb950" : "var(--color-text)",
                      }}
                    >
                      {Number(p.sharpe).toFixed(2)}
                    </b>
                  </span>
                )}
                {p.cagr != null && p.cagr !== 0 && (
                  <span>
                    CAGR{" "}
                    <b style={{ color: p.cagr >= 0 ? "#3fb950" : "#f85149" }}>
                      {Number(p.cagr).toFixed(1)}%
                    </b>
                  </span>
                )}
                {p.max_drawdown_pct != null && p.max_drawdown_pct !== 0 && (
                  <span>
                    DD{" "}
                    <b style={{ color: "#f85149" }}>
                      -{Math.abs(Number(p.max_drawdown_pct)).toFixed(1)}%
                    </b>
                  </span>
                )}
                {p.net_profit_pct != null && p.net_profit_pct !== 0 && (
                  <span>
                    PnL{" "}
                    <b
                      style={{
                        color: p.net_profit_pct >= 0 ? "#3fb950" : "#f85149",
                      }}
                    >
                      {p.net_profit_pct >= 0 ? "+" : ""}
                      {Number(p.net_profit_pct).toFixed(1)}%
                    </b>
                  </span>
                )}
                {p.psr != null && (
                  <span>
                    PSR{" "}
                    <b
                      style={{
                        color:
                          Number(p.psr) > 0.95
                            ? "#3fb950"
                            : Number(p.psr) > 0.5
                              ? "#d29922"
                              : "#f85149",
                      }}
                    >
                      {(Number(p.psr) * 100).toFixed(0)}%
                    </b>
                  </span>
                )}
                <span>
                  P-Value{" "}
                  <b style={{ color: pValueColor(p.p_value) }}>
                    {formatPValue(p.p_value)}
                  </b>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create Modal */}
      <Modal
        open={showCreate}
        onClose={() => setShowCreate(false)}
        title="Create Portfolio"
        width="700px"
      >
        <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
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
              placeholder="My Portfolio"
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
          <div>
            <label
              style={{
                color: "var(--color-text-muted)",
                fontSize: "0.75rem",
                display: "block",
                marginBottom: "8px",
              }}
            >
              Select Strategies
            </label>
            <div
              style={{
                maxHeight: "300px",
                overflowY: "auto",
                border: "1px solid var(--color-border)",
                borderRadius: "6px",
              }}
            >
              {strategies.map((s) => {
                const sel = selectedStrategies.find((ss) => ss.name === s.name);
                return (
                  <div
                    key={s.name}
                    style={{
                      padding: "8px 12px",
                      borderBottom: "1px solid var(--color-border)",
                      background: sel ? "rgba(88,166,255,0.06)" : "transparent",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                        marginBottom: sel ? "6px" : 0,
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={!!sel}
                        onChange={() => toggleStrategy(s.name)}
                        style={{ accentColor: "#58a6ff" }}
                      />
                      <span style={{ fontSize: "0.85rem", fontWeight: 500 }}>
                        {s.name}
                      </span>
                      <div
                        style={{
                          display: "flex",
                          gap: "4px",
                          marginLeft: "auto",
                        }}
                      >
                        {(s.tags?.type || []).map((t: string) => (
                          <span
                            key={t}
                            style={{
                              background: "rgba(88,166,255,0.12)",
                              color: "#58a6ff",
                              fontSize: "0.62rem",
                              padding: "1px 5px",
                              borderRadius: "3px",
                            }}
                          >
                            {t}
                          </span>
                        ))}
                      </div>
                    </div>
                    {sel && (
                      <div
                        style={{
                          display: "flex",
                          gap: "12px",
                          marginLeft: "24px",
                        }}
                      >
                        <div>
                          <label
                            style={{
                              color: "var(--color-text-muted)",
                              fontSize: "0.65rem",
                            }}
                          >
                            Risk/Trade %
                          </label>
                          <input
                            type="number"
                            step="0.1"
                            value={sel.risk}
                            onChange={(e) =>
                              updateStrategyField(
                                s.name,
                                "risk",
                                e.target.value,
                              )
                            }
                            style={{ ...inputStyle, width: "80px" }}
                          />
                        </div>
                        <div>
                          <label
                            style={{
                              color: "var(--color-text-muted)",
                              fontSize: "0.65rem",
                            }}
                          >
                            Max Positions
                          </label>
                          <input
                            type="number"
                            value={sel.maxPos}
                            onChange={(e) =>
                              updateStrategyField(
                                s.name,
                                "maxPos",
                                e.target.value,
                              )
                            }
                            style={{ ...inputStyle, width: "80px" }}
                          />
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
          <button
            onClick={handleCreate}
            disabled={!formName || selectedStrategies.length === 0}
            style={{
              background:
                !formName || selectedStrategies.length === 0
                  ? "#333"
                  : "#58a6ff",
              color: "#fff",
              border: "none",
              borderRadius: "6px",
              padding: "8px",
              fontSize: "0.85rem",
              cursor: "pointer",
            }}
          >
            Create Portfolio
          </button>
        </div>
      </Modal>
    </div>
  );
}
