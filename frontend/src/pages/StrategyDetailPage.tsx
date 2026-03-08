import { useState, useEffect } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { getStrategyDetail } from "../api/client";
import { CreateBacktestModal } from "../components/CreateBacktestModal";
import { ArrowLeft, Plus } from "lucide-react";

export function StrategyDetailPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);

  useEffect(() => {
    if (!name) return;
    getStrategyDetail(name)
      .then(setDetail)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [name]);

  if (loading)
    return (
      <div style={{ padding: "20px", color: "var(--color-text-muted)" }}>
        Loading...
      </div>
    );
  if (!detail)
    return (
      <div style={{ padding: "20px", color: "var(--color-red)" }}>
        Strategy not found
      </div>
    );

  return (
    <div style={{ padding: "24px" }}>
      {/* Header */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "24px",
          marginBottom: "20px",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "12px",
            marginBottom: "12px",
          }}
        >
          <button
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
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600 }}>{detail.name}</h1>
          <span
            style={{
              background: "var(--color-surface2)",
              border: "1px solid var(--color-border)",
              padding: "2px 8px",
              borderRadius: "4px",
              fontSize: "0.72rem",
              color: "var(--color-text-muted)",
            }}
          >
            v{detail.version || "1.0.0"}
          </span>
        </div>

        {/* Tags */}
        <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
          {(detail.tags?.type || []).map((t: string) => (
            <span
              key={t}
              style={{
                background: "rgba(88,166,255,0.12)",
                color: "#58a6ff",
                fontSize: "0.72rem",
                padding: "3px 10px",
                borderRadius: "4px",
              }}
            >
              {t}
            </span>
          ))}
          {(detail.tags?.asset_class || []).map((a: string) => (
            <span
              key={a}
              style={{
                background: "rgba(63,185,80,0.12)",
                color: "#3fb950",
                fontSize: "0.72rem",
                padding: "3px 10px",
                borderRadius: "4px",
              }}
            >
              {a}
            </span>
          ))}
          {detail.tags?.complexity && (
            <span
              style={{
                background: "rgba(210,153,34,0.12)",
                color: "#d29922",
                fontSize: "0.72rem",
                padding: "3px 10px",
                borderRadius: "4px",
              }}
            >
              {detail.tags.complexity}
            </span>
          )}
          {detail.tags?.source && (
            <span
              style={{
                background: "var(--color-surface2)",
                color: "var(--color-text-muted)",
                fontSize: "0.72rem",
                padding: "3px 10px",
                borderRadius: "4px",
              }}
            >
              📦 {detail.tags.source}
            </span>
          )}
        </div>
        {detail.tags?.link && (
          <div style={{ marginTop: "8px" }}>
            <a
              href={detail.tags.link}
              target="_blank"
              rel="noreferrer"
              style={{ color: "#58a6ff", fontSize: "0.8rem" }}
            >
              🔗 {detail.tags.link}
            </a>
          </div>
        )}
      </div>

      {/* Backtests */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
          marginBottom: "20px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "16px",
          }}
        >
          <h2 style={{ fontSize: "1rem", fontWeight: 600 }}>
            Backtests ({detail.backtests?.length || 0})
          </h2>
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
            <Plus size={14} /> Run Backtest
          </button>
        </div>
        {detail.backtests?.length > 0 ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
              gap: "10px",
            }}
          >
            {detail.backtests.map((bt: any) => (
              <Link
                key={bt.id}
                to={`/backtests/${bt.id}`}
                style={{
                  background: "var(--color-surface2)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "6px",
                  padding: "12px",
                  textDecoration: "none",
                  color: "inherit",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    marginBottom: "4px",
                  }}
                >
                  <span style={{ fontSize: "0.82rem", fontWeight: 600 }}>
                    {bt.engine}
                  </span>
                  <span
                    style={{
                      fontSize: "0.72rem",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    {bt.trade_count} trades
                  </span>
                </div>
                <div
                  style={{
                    fontSize: "0.72rem",
                    color: "var(--color-text-muted)",
                  }}
                >
                  {bt.symbols?.join(", ")} • {(bt.timeframes || []).join(", ")}
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <div
            style={{ color: "var(--color-text-muted)", fontSize: "0.85rem" }}
          >
            No backtests yet
          </div>
        )}
      </div>

      <CreateBacktestModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        preselectedStrategy={detail.name}
      />
    </div>
  );
}
