import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, PencilLine } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AreaSeries,
  ColorType,
  createChart,
  CrosshairMode,
  HistogramSeries,
} from "lightweight-charts";
import type { IChartApi, Time } from "lightweight-charts";

import type {
  PortfolioContribution,
  PortfolioDetail,
  PortfolioTrade,
} from "../api/types";
import {
  deletePortfolio,
  getPortfolioContributions,
  getPortfolioDetail,
  getPortfolioTrades,
  renamePortfolio,
} from "../api/client";
import { RenameModal } from "../components/RenameModal";
import { StatCard } from "../components/StatCard";
import { TradeTable, type TradeTableRow } from "../components/TradeTable";
import { useToast } from "../components/ToastProvider";
import { formatPValue, pValueColor } from "../utils/stats";
import { psrTone } from "../lib/statFormat";

type GroupBy = "component" | "strategy" | "symbol" | "timeframe";

type ContributionSort = {
  key: keyof PortfolioContribution;
  direction: "asc" | "desc";
};

type EquityPoint = { time: string; equity: number };
type PortfolioStatsView = NonNullable<PortfolioDetail["combined_stats"]>;
type ChartTooltip = {
  x: number;
  y: number;
  time: string;
  equity: string;
  dd: string;
  ret: string;
};

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function filterUnique(values: string[]): string[] {
  return [...new Set(values.filter((value) => value.trim()))].sort((a, b) =>
    a.localeCompare(b),
  );
}

function resolveChartLocale(): string {
  if (typeof navigator === "undefined") {
    return "en-US";
  }
  const language = String(navigator.language || "").trim();
  return language && language !== "undefined" ? language : "en-US";
}

function tradeChartLink(trade: PortfolioTrade): string | null {
  const backtestId = String(trade.backtest_id || "").trim();
  const nr = toFiniteNumber(trade.nr);
  if (!backtestId || nr == null) return null;
  return `/backtests/${encodeURIComponent(backtestId)}/trade/${Math.trunc(nr)}`;
}

export function PortfolioDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [detail, setDetail] = useState<PortfolioDetail | null>(null);
  const [contributions, setContributions] = useState<PortfolioContribution[]>(
    [],
  );
  const [trades, setTrades] = useState<PortfolioTrade[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleting, setDeleting] = useState(false);
  const [showRenameModal, setShowRenameModal] = useState(false);
  const [savingRename, setSavingRename] = useState(false);
  const [renameError, setRenameError] = useState("");

  const [groupBy, setGroupBy] = useState<GroupBy>("component");
  const [strategyFilter, setStrategyFilter] = useState("");
  const [symbolFilter, setSymbolFilter] = useState("");
  const [timeframeFilter, setTimeframeFilter] = useState("");
  const [contributionSort, setContributionSort] = useState<ContributionSort>({
    key: "pnl_total",
    direction: "desc",
  });

  useEffect(() => {
    if (!id) return;
    let active = true;
    setLoading(true);
    Promise.all([
      getPortfolioDetail(id),
      getPortfolioContributions(id, "component"),
      getPortfolioTrades(id),
    ])
      .then(([detailPayload, contributionPayload, tradePayload]) => {
        if (!active) return;
        setDetail(detailPayload);
        setContributions(contributionPayload);
        setTrades(tradePayload);
      })
      .catch((error: unknown) => {
        if (!active) return;
        const message =
          error instanceof Error ? error.message : "Failed to load portfolio";
        showToast(message, "error");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [id, showToast]);

  useEffect(() => {
    if (!id || !detail) return;
    let active = true;
    getPortfolioContributions(id, groupBy)
      .then((payload) => {
        if (!active) return;
        setContributions(payload);
      })
      .catch((error: unknown) => {
        if (!active) return;
        const message =
          error instanceof Error
            ? error.message
            : "Failed to load contributions";
        showToast(message, "error");
      });
    return () => {
      active = false;
    };
  }, [id, detail, groupBy, showToast]);

  useEffect(() => {
    if (!id || !detail) return;
    let active = true;
    getPortfolioTrades(id, {
      strategy: strategyFilter || undefined,
      symbol: symbolFilter || undefined,
      timeframe: timeframeFilter || undefined,
    })
      .then((payload) => {
        if (!active) return;
        setTrades(payload);
      })
      .catch((error: unknown) => {
        if (!active) return;
        const message =
          error instanceof Error
            ? error.message
            : "Failed to load filtered trades";
        showToast(message, "error");
      });
    return () => {
      active = false;
    };
  }, [id, detail, strategyFilter, symbolFilter, timeframeFilter, showToast]);

  const stats = useMemo<PortfolioStatsView>(
    () =>
      detail?.combined_stats || {
        sharpe: 0,
        cagr: 0,
        max_drawdown: 0,
        net_profit: 0,
        correlation_matrix: {},
      },
    [detail],
  );
  const equity = useMemo(() => detail?.combined_equity || [], [detail]);
  const components = useMemo(() => detail?.components || [], [detail]);

  const strategyOptions = useMemo(
    () =>
      filterUnique([
        ...components.map((item) => String(item.strategy_name || "")),
        ...trades.map((item) =>
          String(item.strategy_name || item.strategy || ""),
        ),
      ]),
    [components, trades],
  );
  const symbolOptions = useMemo(
    () =>
      filterUnique([
        ...components.map((item) => String(item.symbol || "").toUpperCase()),
        ...trades.map((item) => String(item.symbol || "").toUpperCase()),
      ]),
    [components, trades],
  );
  const timeframeOptions = useMemo(
    () =>
      filterUnique([
        ...components.map((item) => String(item.timeframe || "")),
        ...trades.map((item) => String(item.timeframe || "")),
      ]),
    [components, trades],
  );

  const filteredContributions = useMemo(() => {
    const strategyNeedle = strategyFilter.trim().toLowerCase();
    const symbolNeedle = symbolFilter.trim().toUpperCase();
    const timeframeNeedle = timeframeFilter.trim().toLowerCase();
    return contributions
      .filter((item) => {
        const strategyName = String(
          item.strategy_name || item.group_value || "",
        ).toLowerCase();
        const symbol = String(item.symbol || "").toUpperCase();
        const timeframe = String(item.timeframe || "").toLowerCase();
        if (strategyNeedle && !strategyName.includes(strategyNeedle))
          return false;
        if (symbolNeedle && symbol !== symbolNeedle) return false;
        if (timeframeNeedle && timeframe !== timeframeNeedle) return false;
        return true;
      })
      .sort((a, b) => {
        const left = a[contributionSort.key];
        const right = b[contributionSort.key];
        const leftNum = toFiniteNumber(left);
        const rightNum = toFiniteNumber(right);
        if (leftNum != null && rightNum != null) {
          return contributionSort.direction === "asc"
            ? leftNum - rightNum
            : rightNum - leftNum;
        }
        const leftText = String(left || "");
        const rightText = String(right || "");
        return contributionSort.direction === "asc"
          ? leftText.localeCompare(rightText)
          : rightText.localeCompare(leftText);
      });
  }, [
    contributions,
    strategyFilter,
    symbolFilter,
    timeframeFilter,
    contributionSort,
  ]);

  const chartData = useMemo(
    () =>
      filteredContributions.map((item) => ({
        name: String(item.group_value || item.strategy_name || "Unknown"),
        return: Number(item.return_contribution || 0),
        drawdown: Number(item.drawdown_contribution || 0) * -1,
        pnl: Number(item.pnl_total || 0),
      })),
    [filteredContributions],
  );

  const tradeRows = useMemo<TradeTableRow[]>(
    () =>
      trades.map((trade, index) => {
        const timestamp = String(
          trade.time || trade.exit_time_iso || trade.entry_time_iso || "",
        );
        const parsed = Date.parse(timestamp);
        const date = Number.isFinite(parsed)
          ? new Date(parsed).toISOString().slice(0, 10)
          : "";
        const time = Number.isFinite(parsed)
          ? new Date(parsed).toISOString().slice(11, 19)
          : timestamp;
        const strategyName = String(
          trade.strategy_name || trade.strategy || "",
        ).trim();
        const symbol = String(trade.symbol || "").toUpperCase();
        const timeframe = String(trade.timeframe || "").trim();
        const strategyLabel =
          String(trade.strategy_label || "").trim() ||
          (strategyName && (symbol || timeframe)
            ? `${strategyName} (${[symbol, timeframe].filter(Boolean).join(", ")})`
            : strategyName);
        return {
          id: String(
            trade.id ||
              `${trade.symbol || "trade"}-${trade.time || index}-${index}`,
          ),
          date,
          time,
          symbol,
          strategy: strategyLabel,
          side: String(trade.direction || "LONG"),
          size: toFiniteNumber(
            trade.size ?? trade.position_size ?? trade.quantity,
          ),
          entryPrice: toFiniteNumber(trade.entry_price),
          exitPrice: toFiniteNumber(trade.exit_price),
          pnl: toFiniteNumber(trade.pnl),
          pnlPct: toFiniteNumber(trade.pnl_pct),
          exitReason: String(trade.exit_reason || ""),
          duration: String(trade.duration || ""),
        };
      }),
    [trades],
  );

  const handleDelete = async () => {
    if (!id || deleting) return;
    if (!window.confirm("Delete this portfolio?")) return;
    setDeleting(true);
    try {
      await deletePortfolio(id);
      showToast("Portfolio deleted.", "success");
      navigate("/portfolios", { replace: true });
    } catch (error: unknown) {
      const message =
        error instanceof Error ? error.message : "Failed to delete portfolio";
      showToast(message, "error");
      setDeleting(false);
    }
  };

  const handleRename = async (nextName: string) => {
    if (!id) return;
    setSavingRename(true);
    setRenameError("");
    try {
      await renamePortfolio(id, nextName);
      const refreshed = await getPortfolioDetail(id);
      setDetail(refreshed);
      setShowRenameModal(false);
    } catch (error: unknown) {
      const message =
        error instanceof Error ? error.message : "Failed to rename portfolio";
      setRenameError(message);
    } finally {
      setSavingRename(false);
    }
  };

  const setSortKey = (key: keyof PortfolioContribution) => {
    setContributionSort((current) =>
      current.key === key
        ? { key, direction: current.direction === "asc" ? "desc" : "asc" }
        : { key, direction: "desc" },
    );
  };

  if (loading) {
    return (
      <div style={{ padding: "20px", color: "var(--color-text-muted)" }}>
        Loading...
      </div>
    );
  }
  if (!detail) {
    return (
      <div style={{ padding: "20px", color: "var(--color-red)" }}>
        Portfolio not found
      </div>
    );
  }

  return (
    <div
      style={{
        padding: "24px",
        display: "flex",
        flexDirection: "column",
        gap: "20px",
      }}
    >
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "24px",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "12px",
            justifyContent: "space-between",
            flexWrap: "wrap",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <button
              type="button"
              onClick={() => navigate(-1)}
              style={{
                background: "none",
                border: "none",
                color: "var(--color-text-muted)",
                cursor: "pointer",
              }}
            >
              <ArrowLeft size={20} />
            </button>
            <div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "10px",
                  flexWrap: "wrap",
                }}
              >
                <h1 style={{ fontSize: "1.5rem", fontWeight: 600 }}>
                  {detail.name || id}
                </h1>
                <button
                  type="button"
                  onClick={() => setShowRenameModal(true)}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    width: "32px",
                    height: "32px",
                    borderRadius: "999px",
                    border: "1px solid rgba(88,166,255,0.2)",
                    background: "rgba(88,166,255,0.1)",
                    color: "#58a6ff",
                    cursor: "pointer",
                  }}
                  aria-label="Rename portfolio"
                >
                  <PencilLine size={14} />
                </button>
              </div>
              <div
                style={{
                  color: "var(--color-text-muted)",
                  fontSize: "0.82rem",
                  marginTop: "4px",
                }}
              >
                {components.length} components • $
                {(detail.account_capital || 100000).toLocaleString()} capital •{" "}
                {trades.length} trades
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void handleDelete()}
            disabled={deleting}
            style={{
              borderRadius: "8px",
              border: "1px solid rgba(248,113,113,0.4)",
              background: "rgba(127,29,29,0.2)",
              color: "#fecaca",
              padding: "8px 12px",
              fontSize: "0.78rem",
              cursor: deleting ? "not-allowed" : "pointer",
              opacity: deleting ? 0.65 : 1,
            }}
          >
            {deleting ? "Deleting..." : "Delete Portfolio"}
          </button>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(170px, 1fr))",
          gap: "12px",
        }}
      >
        <StatCard
          label="Total Return"
          value={stats.total_return ?? 0}
          format="percent"
          percentScale="fraction"
        />
        <StatCard label="Net Profit" value={stats.net_profit || 0} format="currency" />
        <StatCard
          label="Sharpe (higher = better)"
          value={(stats.sharpe || 0).toFixed(3)}
          color={stats.sharpe > 1 ? "#3fb950" : "#d29922"}
        />
        <StatCard label="Sortino" value={(stats.sortino || 0).toFixed(3)} color="#58a6ff" />
        <StatCard
          label="CAGR (higher = better)"
          value={stats.cagr ?? 0}
          format="percent"
          percentScale="fraction"
        />
        <StatCard
          label="PSR (higher = better)"
          value={stats.psr}
          format="percent"
          percentScale="fraction"
          color={Number(stats.psr) > 0.95 ? "#3fb950" : Number(stats.psr) > 0.5 ? "#d29922" : "#f85149"}
        />
        <StatCard
          label="p-Value (lower = better)"
          value={formatPValue(stats.p_value)}
          color={pValueColor(stats.p_value)}
        />
        <StatCard
          label="Max DD (lower = better)"
          value={stats.max_drawdown ?? 0}
          format="percent"
          percentScale="fraction"
          color="#f85149"
        />
        <StatCard
          label="Win Rate (higher = better)"
          value={stats.win_rate}
          format="percent"
          percentScale="fraction"
          color={(stats.win_rate || 0) >= 0.5 ? "#3fb950" : "#f85149"}
        />
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
        }}
      >
        <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "16px" }}>
          Combined Equity Curve
        </h2>
        {equity.length > 1 ? (
          <PortfolioEquityChart data={equity} />
        ) : (
          <div
            style={{
              height: "300px",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--color-text-muted)",
            }}
          >
            No equity data available
          </div>
        )}
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "10px",
            flexWrap: "wrap",
            marginBottom: "14px",
          }}
        >
          <h2 style={{ fontSize: "1rem", fontWeight: 600 }}>
            Contribution Analysis
          </h2>
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            {(
              ["component", "strategy", "symbol", "timeframe"] as GroupBy[]
            ).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setGroupBy(option)}
                style={{
                  border: "1px solid var(--color-border)",
                  background:
                    option === groupBy ? "rgba(88,166,255,0.2)" : "transparent",
                  color:
                    option === groupBy ? "#9ecbff" : "var(--color-text-muted)",
                  borderRadius: "999px",
                  padding: "5px 10px",
                  fontSize: "0.72rem",
                  textTransform: "capitalize",
                  cursor: "pointer",
                }}
              >
                {option}
              </button>
            ))}
          </div>
        </div>

        <div
          style={{
            display: "flex",
            gap: "8px",
            flexWrap: "wrap",
            marginBottom: "12px",
          }}
        >
          <select
            value={strategyFilter}
            onChange={(event) => setStrategyFilter(event.target.value)}
            style={filterSelectStyle}
          >
            <option value="">All Strategies</option>
            {strategyOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
          <select
            value={symbolFilter}
            onChange={(event) => setSymbolFilter(event.target.value)}
            style={filterSelectStyle}
          >
            <option value="">All Symbols</option>
            {symbolOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
          <select
            value={timeframeFilter}
            onChange={(event) => setTimeframeFilter(event.target.value)}
            style={filterSelectStyle}
          >
            <option value="">All Timeframes</option>
            {timeframeOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>

        <div style={{ marginBottom: "16px" }}>
          <ContributionTable
            rows={filteredContributions}
            groupBy={groupBy}
            sort={contributionSort}
            onSort={setSortKey}
          />
        </div>

        <div style={{ height: "310px", width: "100%" }}>
          <ResponsiveContainer
            width="100%"
            height="100%"
            minWidth={0}
            minHeight={1}
          >
            <BarChart data={chartData}>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="rgba(255,255,255,0.06)"
              />
              <XAxis dataKey="name" stroke="#8b949e" tick={{ fontSize: 12 }} />
              <YAxis stroke="#8b949e" tick={{ fontSize: 12 }} />
              <RechartsTooltip
                contentStyle={{
                  background: "#111827",
                  border: "1px solid rgba(255,255,255,0.14)",
                  borderRadius: "8px",
                }}
              />
              <Legend />
              <Bar
                dataKey="pnl"
                name="PnL"
                fill="#58a6ff"
                radius={[4, 4, 0, 0]}
              />
              <Bar
                dataKey="return"
                name="Return"
                fill="#3fb950"
                radius={[4, 4, 0, 0]}
              />
              <Bar
                dataKey="drawdown"
                name="Drawdown"
                fill="#f85149"
                radius={[4, 4, 0, 0]}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "14px",
            flexWrap: "wrap",
            gap: "10px",
          }}
        >
          <h2 style={{ fontSize: "1rem", fontWeight: 600 }}>
            Trades ({trades.length})
          </h2>
          <div
            style={{ color: "var(--color-text-muted)", fontSize: "0.76rem" }}
          >
            Click trade row to open the trade chart context.
          </div>
        </div>
        <TradeTable
          trades={tradeRows}
          onTradeClick={(row) => {
            const sourceTrade = trades.find(
              (item) => String(item.id || "") === row.id,
            );
            const target = sourceTrade ? tradeChartLink(sourceTrade) : null;
            if (!target) {
              showToast("Trade has no linked backtest chart.", "error");
              return;
            }
            navigate(target);
          }}
        />
      </div>

      <RenameModal
        open={showRenameModal}
        title="Rename Portfolio"
        label="Portfolio name"
        value={detail.name || id || ""}
        helperText={
          renameError ||
          "This updates the portfolio display name without changing its ID."
        }
        saving={savingRename}
        onClose={() => {
          setShowRenameModal(false);
          setRenameError("");
        }}
        onSave={handleRename}
      />
    </div>
  );
}

/** Arrow suffix for the active sort column. */
function sortArrow(
  sort: { key: keyof PortfolioContribution; direction: "asc" | "desc" },
  key: keyof PortfolioContribution,
): string {
  if (sort.key !== key) {
    return "";
  }
  return sort.direction === "asc" ? " ↑" : " ↓";
}

function ContributionTable({
  rows,
  groupBy,
  sort,
  onSort,
}: Readonly<{
  rows: PortfolioContribution[];
  groupBy: GroupBy;
  sort: { key: keyof PortfolioContribution; direction: "asc" | "desc" };
  onSort: (key: keyof PortfolioContribution) => void;
}>) {
  const groupLabel: Record<GroupBy, string> = {
    component: "Component",
    strategy: "Strategy",
    symbol: "Symbol",
    timeframe: "Timeframe",
  };

  const headerButton = (label: string, key: keyof PortfolioContribution) => (
    <button
      type="button"
      onClick={() => onSort(key)}
      style={{
        background: "none",
        border: "none",
        color: "var(--color-text-muted)",
        fontSize: "0.69rem",
        textTransform: "uppercase",
        letterSpacing: "0.02em",
        cursor: "pointer",
        padding: 0,
        textAlign: "left",
      }}
    >
      {label}
      {sortArrow(sort, key)}
    </button>
  );

  return (
    <div
      style={{
        overflowX: "auto",
        border: "1px solid var(--color-border)",
        borderRadius: "8px",
      }}
    >
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: "0.78rem",
        }}
      >
        <thead style={{ background: "var(--color-surface2)" }}>
          <tr>
            <th style={thStyle}>
              {headerButton(groupLabel[groupBy], "group_value")}
            </th>
            <th style={thStyle}>
              {headerButton("Return", "return_contribution")}
            </th>
            <th style={thStyle}>
              {headerButton("Drawdown", "drawdown_contribution")}
            </th>
            <th style={thStyle}>
              {headerButton("Sharpe (higher = better)", "sharpe_ratio")}
            </th>
            <th style={thStyle}>
              {headerButton("PSR (higher = better)", "psr")}
            </th>
            <th style={thStyle}>
              {headerButton("p-Value (lower = better)", "p_value")}
            </th>
            <th style={thStyle}>
              {headerButton("Max DD (lower = better)", "max_drawdown")}
            </th>
            <th style={thStyle}>
              {headerButton("CAGR (higher = better)", "cagr")}
            </th>
            <th style={thStyle}>
              {headerButton("Win Rate (higher = better)", "win_rate")}
            </th>
            <th style={thStyle}>{headerButton("Trades", "trade_count")}</th>
            <th style={thStyle}>{headerButton("PnL", "pnl_total")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.group_value}-${index}`}>
              <td style={tdStyle}>
                {row.group_value || row.strategy_name || "—"}
              </td>
              <td style={tdStyle}>
                {(row.return_contribution * 100).toFixed(2)}%
              </td>
              <td style={{ ...tdStyle, color: "#f85149" }}>
                {(row.drawdown_contribution * 100).toFixed(2)}%
              </td>
              <td style={tdStyle}>
                {Number(row.sharpe_ratio ?? 0).toFixed(3)}
              </td>
              <td style={tdStyle}>
                {row.psr != null
                  ? `${(Number(row.psr) * 100).toFixed(1)}%`
                  : "—"}
              </td>
              <td style={tdStyle}>
                {row.p_value != null ? Number(row.p_value).toFixed(4) : "—"}
              </td>
              <td style={{ ...tdStyle, color: "#f85149" }}>
                {row.max_drawdown != null
                  ? `-${(Number(row.max_drawdown) * 100).toFixed(2)}%`
                  : "—"}
              </td>
              <td
                style={{
                  ...tdStyle,
                  color: Number(row.cagr || 0) >= 0 ? "#3fb950" : "#f85149",
                }}
              >
                {row.cagr != null
                  ? `${(Number(row.cagr) * 100).toFixed(2)}%`
                  : "—"}
              </td>
              <td style={tdStyle}>{row.win_rate.toFixed(2)}%</td>
              <td style={tdStyle}>{row.trade_count}</td>
              <td
                style={{
                  ...tdStyle,
                  color: row.pnl_total >= 0 ? "#3fb950" : "#f85149",
                }}
              >
                {row.pnl_total >= 0 ? "+" : ""}$
                {Math.abs(row.pnl_total).toLocaleString("en-US", {
                  maximumFractionDigits: 2,
                })}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td
                colSpan={11}
                style={{ ...tdStyle, color: "var(--color-text-muted)" }}
              >
                No contribution rows match current filters.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

const thStyle: React.CSSProperties = {
  padding: "10px 12px",
  textAlign: "left",
  borderBottom: "1px solid var(--color-border)",
};

const tdStyle: React.CSSProperties = {
  padding: "10px 12px",
  borderBottom: "1px solid rgba(255,255,255,0.06)",
};

const filterSelectStyle: React.CSSProperties = {
  background: "var(--color-surface2)",
  color: "var(--color-text)",
  border: "1px solid var(--color-border)",
  borderRadius: "999px",
  fontSize: "0.74rem",
  padding: "6px 10px",
};

function PortfolioEquityChart({ data }: Readonly<{ data: EquityPoint[] }>) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [tooltip, setTooltip] = useState<ChartTooltip | null>(null);

  useEffect(() => {
    if (!containerRef.current || !data.length) return;

    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    const container = containerRef.current;
    const w = container.clientWidth || 1100;
    const locale = resolveChartLocale();
    const chart = createChart(container, {
      width: w,
      height: 340,
      localization: { locale },
      layout: {
        background: { type: ColorType.Solid, color: "#0d1117" },
        textColor: "#8b949e",
      },
      grid: {
        vertLines: { color: "#1c2128" },
        horzLines: { color: "#1c2128" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { borderColor: "#30363d", timeVisible: false },
      rightPriceScale: { borderColor: "#30363d" },
    });
    chartRef.current = chart;

    const parsed = data
      .map((d) => ({
        time: (new Date(d.time).getTime() / 1000) as Time,
        equity: d.equity,
      }))
      .filter(
        (item) =>
          Number.isFinite(item.time as number) && Number.isFinite(item.equity),
      );
    if (parsed.length < 2) {
      chart.remove();
      chartRef.current = null;
      return;
    }

    const isProfit = (parsed.at(-1)?.equity ?? 0) >= (parsed[0]?.equity ?? 0);
    const lineColor = isProfit ? "#3fb950" : "#f85149";

    const equitySeries = chart.addSeries(AreaSeries, {
      lineColor,
      topColor: isProfit ? "rgba(63,185,80,0.25)" : "rgba(248,81,73,0.25)",
      bottomColor: "rgba(0,0,0,0)",
      lineWidth: 2,
      priceFormat: {
        type: "custom",
        formatter: (p: number) =>
          "$" + p.toLocaleString("en-US", { maximumFractionDigits: 0 }),
      },
    });
    equitySeries.setData(
      parsed.map((d) => ({ time: d.time, value: d.equity })),
    );

    let peak = parsed[0].equity;
    const ddData = parsed.map((d) => {
      if (d.equity > peak) peak = d.equity;
      const dd = (d.equity - peak) / peak;
      return {
        time: d.time,
        value: dd,
        color: dd < -0.1 ? "rgba(248,81,73,0.5)" : "rgba(248,81,73,0.25)",
      };
    });
    const ddSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "dd",
      priceFormat: {
        type: "custom",
        formatter: (p: number) => (p * 100).toFixed(1) + "%",
      },
    });
    ddSeries.setData(ddData);
    chart
      .priceScale("dd")
      .applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point) {
        setTooltip(null);
        return;
      }
      const eqVal = param.seriesData.get(equitySeries);
      if (!eqVal || !("value" in eqVal)) {
        setTooltip(null);
        return;
      }
      const eq = Number((eqVal as { value: number }).value);
      if (!Number.isFinite(eq)) {
        setTooltip(null);
        return;
      }
      const initial = parsed[0].equity;
      const ret = (((eq - initial) / initial) * 100).toFixed(2);
      let p = initial;
      for (const d of parsed) {
        if ((d.time as number) > (param.time as number)) break;
        if (d.equity > p) p = d.equity;
      }
      const dd = (((eq - p) / p) * 100).toFixed(2);
      const dateStr = new Date((param.time as number) * 1000)
        .toISOString()
        .slice(0, 10);
      setTooltip({
        x: param.point.x,
        y: param.point.y,
        time: dateStr,
        equity: "$" + eq.toLocaleString("en-US", { maximumFractionDigits: 0 }),
        dd: dd + "%",
        ret: ret + "%",
      });
    });

    chart.timeScale().fitContent();
    const resizeObserver = new ResizeObserver(() => {
      if (container.clientWidth > 0) chart.resize(container.clientWidth, 340);
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, [data]);

  return (
    <div style={{ position: "relative" }}>
      <div ref={containerRef} style={{ width: "100%", height: "340px" }} />
      {tooltip && (
        <div
          style={{
            position: "absolute",
            left: Math.min(
              tooltip.x + 16,
              (containerRef.current?.clientWidth || 800) - 180,
            ),
            top: Math.max(tooltip.y - 80, 4),
            background: "rgba(22,27,34,0.95)",
            border: "1px solid var(--color-border)",
            borderRadius: "6px",
            padding: "8px 12px",
            fontSize: "0.75rem",
            pointerEvents: "none",
            zIndex: 10,
            lineHeight: 1.6,
          }}
        >
          <div style={{ color: "#8b949e" }}>{tooltip.time}</div>
          <div>
            <span style={{ color: "#8b949e" }}>Equity: </span>
            <b>{tooltip.equity}</b>
          </div>
          <div>
            <span style={{ color: "#8b949e" }}>Return: </span>
            <span
              style={{
                color:
                  Number.parseFloat(tooltip.ret) >= 0 ? "#3fb950" : "#f85149",
              }}
            >
              {tooltip.ret}
            </span>
          </div>
          <div>
            <span style={{ color: "#8b949e" }}>Drawdown: </span>
            <span style={{ color: "#f85149" }}>{tooltip.dd}</span>
          </div>
        </div>
      )}
    </div>
  );
}
