import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { ArrowLeft, PencilLine, Plus, ScrollText } from "lucide-react";
import { getStrategyDetail, renameStrategy } from "../api/client";
import { CreateBacktestModal } from "../components/CreateBacktestModal";
import { psrTone } from "../lib/statFormat";
import { RenameModal } from "../components/RenameModal";

interface MarkdownCodeProps {
  inline?: boolean;
  children?: React.ReactNode;
}

/** Shared react-markdown `code` renderer used by the notes/markdown panels. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function markdownCodeRenderer({ inline, children, ...props }: MarkdownCodeProps & Record<string, any>) {
  return inline ? (
    <code
      style={{
        background: "rgba(88,166,255,0.1)",
        color: "#79c0ff",
        borderRadius: "6px",
        padding: "2px 6px",
        fontSize: "0.8rem",
      }}
      {...props}
    >
      {children}
    </code>
  ) : (
    <code
      style={{
        display: "block",
        background: "#0d1117",
        border: "1px solid var(--color-border)",
        borderRadius: "10px",
        padding: "12px",
        overflowX: "auto",
        color: "#c9d1d9",
        fontSize: "0.82rem",
      }}
      {...props}
    >
      {children}
    </code>
  );
}

const compactMarkdownComponents = {
  p: (props: React.ComponentProps<"p">) => (
    <p
      style={{
        margin: "0 0 10px",
        color: "var(--color-text-muted)",
        lineHeight: 1.65,
      }}
      {...props}
    />
  ),
  ul: (props: React.ComponentProps<"ul">) => (
    <ul style={{ margin: "0 0 10px", paddingLeft: "18px" }} {...props} />
  ),
  li: (props: React.ComponentProps<"li">) => (
    <li
      style={{
        color: "var(--color-text-muted)",
        marginBottom: "4px",
      }}
      {...props}
    />
  ),
  a: (props: React.ComponentProps<"a">) => (
    <a target="_blank" rel="noreferrer" style={{ color: "#58a6ff" }} {...props} />
  ),
  code: markdownCodeRenderer,
};

const richMarkdownComponents = {
  h1: (props: React.ComponentProps<"h1">) => (
    <h2
      style={{
        fontSize: "1.35rem",
        margin: "0 0 14px",
        letterSpacing: "-0.02em",
      }}
      {...props}
    />
  ),
  h2: (props: React.ComponentProps<"h2">) => (
    <h3
      style={{
        fontSize: "1.05rem",
        margin: "24px 0 10px",
        color: "#c9d1d9",
      }}
      {...props}
    />
  ),
  h3: (props: React.ComponentProps<"h3">) => (
    <h4
      style={{
        fontSize: "0.92rem",
        margin: "18px 0 8px",
        color: "#c9d1d9",
      }}
      {...props}
    />
  ),
  p: (props: React.ComponentProps<"p">) => (
    <p
      style={{
        color: "var(--color-text-muted)",
        lineHeight: 1.75,
        margin: "0 0 14px",
      }}
      {...props}
    />
  ),
  ul: (props: React.ComponentProps<"ul">) => (
    <ul style={{ margin: "0 0 14px", paddingLeft: "18px" }} {...props} />
  ),
  ol: (props: React.ComponentProps<"ol">) => (
    <ol style={{ margin: "0 0 14px", paddingLeft: "18px" }} {...props} />
  ),
  li: (props: React.ComponentProps<"li">) => (
    <li
      style={{
        color: "var(--color-text-muted)",
        lineHeight: 1.7,
        marginBottom: "6px",
      }}
      {...props}
    />
  ),
  a: (props: React.ComponentProps<"a">) => (
    <a
      target="_blank"
      rel="noreferrer"
      style={{ color: "#58a6ff" }}
      {...props}
    />
  ),
  code: markdownCodeRenderer,
  pre: (props: React.ComponentProps<"pre">) => (
    <pre style={{ margin: "0 0 14px", whiteSpace: "pre-wrap" }} {...props} />
  ),
  blockquote: (props: React.ComponentProps<"blockquote">) => (
    <blockquote
      style={{
        margin: "0 0 14px",
        padding: "12px 16px",
        borderLeft: "3px solid #58a6ff",
        background: "rgba(88,166,255,0.06)",
        color: "#c9d1d9",
      }}
      {...props}
    />
  ),
};

export function StrategyDetailPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showRenameModal, setShowRenameModal] = useState(false);
  const [renameError, setRenameError] = useState("");
  const [savingRename, setSavingRename] = useState(false);

  useEffect(() => {
    if (!name) return;
    setLoading(true);
    getStrategyDetail(name)
      .then((data) => {
        setDetail(data);
        setRenameError("");
      })
      .catch(() => {
        setDetail(null);
      })
      .finally(() => setLoading(false));
  }, [name]);

  const renderedMarkdown = useMemo(
    () => (detail?.documentation_markdown || "").trim(),
    [detail?.documentation_markdown],
  );
  const sourceUrl = useMemo(
    () => (detail?.source_url || detail?.tags?.link || "").trim(),
    [detail?.source_url, detail?.tags?.link],
  );
  const renderedNotes = useMemo(
    () => (detail?.notes || "").trim(),
    [detail?.notes],
  );

  const handleRename = async (nextName: string) => {
    if (!detail?.slug) return;
    setSavingRename(true);
    setRenameError("");
    try {
      const updated = await renameStrategy(detail.slug, nextName);
      setDetail(updated);
      setShowRenameModal(false);
    } catch (error: any) {
      setRenameError(error.message || "Could not rename strategy");
    } finally {
      setSavingRename(false);
    }
  };

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

  const titleButtonStyle: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    width: "34px",
    height: "34px",
    borderRadius: "999px",
    border: "1px solid rgba(88,166,255,0.18)",
    background: "rgba(88,166,255,0.08)",
    color: "#58a6ff",
    cursor: "pointer",
  };

  return (
    <div style={{ padding: "24px" }}>
      <div
        style={{
          background:
            "radial-gradient(circle at top right, rgba(88,166,255,0.12), transparent 40%), var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "14px",
          padding: "24px",
          marginBottom: "20px",
          boxShadow: "0 12px 32px rgba(0, 0, 0, 0.16)",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "start",
            justifyContent: "space-between",
            gap: "16px",
            flexWrap: "wrap",
            marginBottom: "16px",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "start",
              gap: "12px",
              flexWrap: "wrap",
            }}
          >
            <button
              type="button"
              onClick={() => navigate(-1)}
              style={{
                background: "none",
                border: "none",
                color: "var(--color-text-muted)",
                cursor: "pointer",
                padding: "4px",
              }}
            >
              <ArrowLeft size={20} />
            </button>
            <div style={{ display: "grid", gap: "10px" }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "10px",
                  flexWrap: "wrap",
                }}
              >
                <h1
                  style={{
                    fontSize: "1.75rem",
                    lineHeight: 1.05,
                    margin: 0,
                    fontWeight: 700,
                    letterSpacing: "-0.03em",
                  }}
                >
                  {detail.name}
                </h1>
                <button
                  type="button"
                  onClick={() => setShowRenameModal(true)}
                  style={titleButtonStyle}
                  aria-label="Rename strategy"
                >
                  <PencilLine size={15} />
                </button>
                <span
                  style={{
                    background: "var(--color-surface2)",
                    border: "1px solid var(--color-border)",
                    padding: "3px 10px",
                    borderRadius: "999px",
                    fontSize: "0.72rem",
                    color: "var(--color-text-muted)",
                  }}
                >
                  v{detail.version || "1.0.0"}
                </span>
                <span
                  style={{
                    background: "rgba(88,166,255,0.12)",
                    color: "#58a6ff",
                    padding: "3px 10px",
                    borderRadius: "999px",
                    fontSize: "0.72rem",
                    border: "1px solid rgba(88,166,255,0.18)",
                  }}
                >
                  slug: {detail.slug}
                </span>
              </div>

              <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                {(detail.tags?.type || []).map((tag: string) => (
                  <span
                    key={tag}
                    style={{
                      background: "rgba(88,166,255,0.12)",
                      color: "#58a6ff",
                      fontSize: "0.72rem",
                      padding: "4px 10px",
                      borderRadius: "999px",
                    }}
                  >
                    {tag}
                  </span>
                ))}
                {(detail.tags?.asset_class || []).map((asset: string) => (
                  <span
                    key={asset}
                    style={{
                      background: "rgba(63,185,80,0.12)",
                      color: "#3fb950",
                      fontSize: "0.72rem",
                      padding: "4px 10px",
                      borderRadius: "999px",
                    }}
                  >
                    {asset}
                  </span>
                ))}
                {detail.tags?.complexity && (
                  <span
                    style={{
                      background: "rgba(210,153,34,0.12)",
                      color: "#d29922",
                      fontSize: "0.72rem",
                      padding: "4px 10px",
                      borderRadius: "999px",
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
                      padding: "4px 10px",
                      borderRadius: "999px",
                    }}
                  >
                    {detail.tags.source}
                  </span>
                )}
              </div>

              <div
                style={{
                  display: "flex",
                  gap: "10px",
                  flexWrap: "wrap",
                  color: "var(--color-text-muted)",
                  fontSize: "0.78rem",
                }}
              >
                {detail.created_at ? (
                  <span>
                    Added {new Date(detail.created_at).toLocaleDateString("en-US")}
                  </span>
                ) : null}
                <span>{detail.backtest_count || 0} backtests</span>
                <span>{detail.engine}</span>
                <span>{detail.file}</span>
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={() => setShowCreateModal(true)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "8px",
              background: "linear-gradient(135deg, #58a6ff, #7c3aed)",
              color: "#fff",
              border: "none",
              borderRadius: "10px",
              padding: "10px 16px",
              fontSize: "0.85rem",
              cursor: "pointer",
              boxShadow: "0 12px 28px rgba(88, 166, 255, 0.22)",
            }}
          >
            <Plus size={16} /> Run Backtest
          </button>
        </div>

        {sourceUrl ? (
          <div
            style={{
              marginTop: "6px",
              padding: "10px 12px",
              borderRadius: "10px",
              background: "rgba(88,166,255,0.08)",
              border: "1px solid rgba(88,166,255,0.2)",
            }}
          >
            <div
              style={{
                fontSize: "0.7rem",
                letterSpacing: "0.08em",
                textTransform: "uppercase",
                color: "#79c0ff",
                marginBottom: "6px",
              }}
            >
              Source URL
            </div>
            <a
              href={sourceUrl}
              target="_blank"
              rel="noreferrer"
              style={{
                color: "#58a6ff",
                fontSize: "0.9rem",
                textDecoration: "none",
                wordBreak: "break-all",
              }}
            >
              {sourceUrl}
            </a>
          </div>
        ) : null}

        {renderedNotes ? (
          <div
            style={{
              marginTop: "14px",
              padding: "14px 16px",
              borderRadius: "12px",
              background: "rgba(210,153,34,0.08)",
              border: "1px solid rgba(210,153,34,0.22)",
            }}
          >
            <div
              style={{
                fontSize: "0.72rem",
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                color: "#d29922",
                marginBottom: "8px",
              }}
            >
              Strategy Notes
            </div>
            <ReactMarkdown components={compactMarkdownComponents}>
              {renderedNotes}
            </ReactMarkdown>
          </div>
        ) : null}
      </div>

      {renderedMarkdown ? (
        <div
          style={{
            background: "var(--color-surface)",
            border: "1px solid var(--color-border)",
            borderRadius: "14px",
            padding: "22px",
            marginBottom: "20px",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "10px",
              marginBottom: "16px",
              color: "var(--color-text-muted)",
              textTransform: "uppercase",
              letterSpacing: "0.12em",
              fontSize: "0.72rem",
            }}
          >
            <ScrollText size={15} color="#58a6ff" />
            Strategy Dossier
          </div>
          <div
            style={{
              padding: "18px",
              borderRadius: "12px",
              background:
                "linear-gradient(180deg, rgba(13,17,23,0.25), rgba(13,17,23,0.05))",
              border: "1px solid rgba(255,255,255,0.03)",
            }}
          >
            <ReactMarkdown components={richMarkdownComponents}>
              {renderedMarkdown}
            </ReactMarkdown>
          </div>
        </div>
      ) : null}

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "12px",
          padding: "20px",
          marginBottom: detail.portfolios?.length ? "20px" : 0,
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
          <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>
            Backtests ({detail.backtests?.length || 0})
          </h2>
        </div>
        {detail.backtests?.length > 0 ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
              gap: "10px",
            }}
          >
            {detail.backtests.map((backtest: any) => (
              <Link
                key={backtest.id}
                to={`/backtests/${backtest.id}`}
                style={{
                  background: "var(--color-surface2)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "10px",
                  padding: "14px",
                  textDecoration: "none",
                  color: "inherit",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: "10px",
                    marginBottom: "8px",
                  }}
                >
                  <span style={{ fontSize: "0.9rem", fontWeight: 600 }}>
                    {backtest.title || backtest.id}
                  </span>
                  <span
                    style={{
                      fontSize: "0.72rem",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    {backtest.trade_count} trades
                  </span>
                </div>
                <div
                  style={{
                    fontSize: "0.75rem",
                    color: "var(--color-text-muted)",
                    marginBottom: "8px",
                  }}
                >
                  {backtest.symbols?.join(", ")} • {(backtest.timeframes || []).join(", ")}
                </div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: "10px",
                    fontSize: "0.72rem",
                    color: "var(--color-text-muted)",
                  }}
                >
                  <span>{backtest.engine}</span>
                  <span>
                    {backtest.created_at
                      ? new Date(backtest.created_at).toLocaleDateString("en-US")
                      : ""}
                  </span>
                </div>
                <div
                  style={{
                    marginTop: "8px",
                    display: "flex",
                    gap: "8px",
                    flexWrap: "wrap",
                  }}
                >
                  {backtest.psr != null ? (
                    <span
                      style={{
                        borderRadius: "999px",
                        border: "1px solid rgba(148, 163, 184, 0.32)",
                        padding: "3px 8px",
                        fontSize: "0.7rem",
                        fontFamily: '"SFMono-Regular", "Cascadia Code", ui-monospace, monospace',
                        color: psrTone(Number(backtest.psr)),
                      }}
                    >
                      PSR {(Number(backtest.psr) * 100).toFixed(0)}%
                    </span>
                  ) : null}
                  {backtest.p_value != null ? (
                    <span
                      style={{
                        borderRadius: "999px",
                        border: "1px solid rgba(148, 163, 184, 0.32)",
                        padding: "3px 8px",
                        fontSize: "0.7rem",
                        fontFamily: '"SFMono-Regular", "Cascadia Code", ui-monospace, monospace',
                        color: Number(backtest.p_value) <= 0.05 ? "#3fb950" : "#f85149",
                      }}
                    >
                      p-Value {Number(backtest.p_value).toFixed(3)}
                    </span>
                  ) : null}
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <div style={{ color: "var(--color-text-muted)", fontSize: "0.85rem" }}>
            No backtests yet
          </div>
        )}
      </div>

      {detail.portfolios?.length ? (
        <div
          style={{
            background: "var(--color-surface)",
            border: "1px solid var(--color-border)",
            borderRadius: "12px",
            padding: "20px",
          }}
        >
          <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: "0 0 16px" }}>
            Portfolios ({detail.portfolios.length})
          </h2>
          <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
            {detail.portfolios.map((portfolio: any) => (
              <Link
                key={portfolio.id}
                to={`/portfolios/${portfolio.id}`}
                style={{
                  textDecoration: "none",
                  color: "inherit",
                  background: "var(--color-surface2)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "999px",
                  padding: "8px 12px",
                  fontSize: "0.82rem",
                }}
              >
                {portfolio.name || portfolio.id}
              </Link>
            ))}
          </div>
        </div>
      ) : null}

      <RenameModal
        open={showRenameModal}
        title="Rename Strategy"
        label="Displayed strategy name"
        value={detail.name}
        helperText={renameError || "This updates STRATEGY_META.name while keeping the strategy slug stable."}
        saving={savingRename}
        onClose={() => {
          setShowRenameModal(false);
          setRenameError("");
        }}
        onSave={handleRename}
      />

      <CreateBacktestModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        preselectedStrategy={detail.name}
      />
    </div>
  );
}
