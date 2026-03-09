import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { getStrategies } from "../api/client";
import { FilterBar } from "../components/FilterBar";

export function StrategiesPage() {
  const navigate = useNavigate();
  const [strategies, setStrategies] = useState<any[]>([]);
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [sortBy, setSortBy] = useState("name_asc");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getStrategies()
      .then((data) => {
        setStrategies(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  const types = useMemo(
    () => [...new Set(strategies.flatMap((s) => s.tags?.type || []))],
    [strategies],
  );
  const sources = useMemo(
    () => [...new Set(strategies.map((s) => s.tags?.source).filter(Boolean))],
    [strategies],
  );
  const assetClasses = useMemo(
    () => [...new Set(strategies.flatMap((s) => s.tags?.asset_class || []))],
    [strategies],
  );
  const complexities = useMemo(
    () => [
      ...new Set(strategies.map((s) => s.tags?.complexity).filter(Boolean)),
    ],
    [strategies],
  );

  const filtered = useMemo(() => {
    let result = strategies.filter((s) => {
      const q = search.toLowerCase();
      const matchSearch =
        !q ||
        s.name?.toLowerCase().includes(q) ||
        s.slug?.toLowerCase().includes(q) ||
        s.tags?.source?.toLowerCase().includes(q);
      const matchType =
        !filters.type || (s.tags?.type || []).includes(filters.type);
      const matchSource = !filters.source || s.tags?.source === filters.source;
      const matchAsset =
        !filters.asset_class ||
        (s.tags?.asset_class || []).includes(filters.asset_class);
      const matchComplexity =
        !filters.complexity || s.tags?.complexity === filters.complexity;
      return (
        matchSearch && matchType && matchSource && matchAsset && matchComplexity
      );
    });
    result.sort((a, b) => {
      switch (sortBy) {
        case "name_desc":
          return (b.name || "").localeCompare(a.name || "");
        case "created_desc":
          return Date.parse(b.created_at || "") - Date.parse(a.created_at || "");
        case "created_asc":
          return Date.parse(a.created_at || "") - Date.parse(b.created_at || "");
        case "complexity":
          return (a.tags?.complexity || "").localeCompare(
            b.tags?.complexity || "",
          );
        case "backtests_asc":
          return (a.backtest_count || 0) - (b.backtest_count || 0);
        case "backtests_desc":
          return (b.backtest_count || 0) - (a.backtest_count || 0);
        default:
          return (a.name || "").localeCompare(b.name || "");
      }
    });
    return result;
  }, [strategies, search, filters, sortBy]);

  return (
    <div style={{ padding: "24px" }}>
      <h1 style={{ fontSize: "1.5rem", fontWeight: 600, marginBottom: "20px" }}>
        Strategies
      </h1>

      <FilterBar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Search strategies..."
        filters={[
          {
            key: "type",
            label: "Type",
            options: types.map((t) => ({ label: t, value: t })),
          },
          {
            key: "source",
            label: "Source",
            options: sources.map((s) => ({ label: s, value: s })),
          },
          {
            key: "asset_class",
            label: "Asset Class",
            options: assetClasses.map((a) => ({ label: a, value: a })),
          },
          {
            key: "complexity",
            label: "Complexity",
            options: complexities.map((c) => ({ label: c, value: c })),
          },
        ]}
        filterValues={filters}
        onFilterChange={(k, v) => setFilters((prev) => ({ ...prev, [k]: v }))}
        sortOptions={[
          { label: "Name (A-Z)", value: "name_asc" },
          { label: "Name (Z-A)", value: "name_desc" },
          { label: "Creation Date (Newest First)", value: "created_desc" },
          { label: "Creation Date (Oldest First)", value: "created_asc" },
          { label: "Trade Count (High to Low)", value: "backtests_desc" },
          { label: "Trade Count (Low to High)", value: "backtests_asc" },
          { label: "Complexity", value: "complexity" },
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
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
            gap: "12px",
          }}
        >
          {filtered.map((s) => (
            <div
              key={s.slug || s.name}
              onClick={() => navigate(`/strategies/${encodeURIComponent(s.slug || s.name)}`)}
              style={{
                background: "var(--color-surface)",
                border: "1px solid var(--color-border)",
                borderRadius: "8px",
                padding: "16px",
                cursor: "pointer",
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
                  fontWeight: 600,
                  fontSize: "0.95rem",
                  marginBottom: "6px",
                }}
              >
                {s.name}
              </div>
              <div
                style={{
                  display: "flex",
                  gap: "6px",
                  flexWrap: "wrap",
                  marginBottom: "8px",
                }}
              >
                {(s.tags?.type || []).map((t: string) => (
                  <span
                    key={t}
                    style={{
                      background: "rgba(88,166,255,0.12)",
                      color: "#58a6ff",
                      fontSize: "0.68rem",
                      padding: "2px 8px",
                      borderRadius: "3px",
                    }}
                  >
                    {t}
                  </span>
                ))}
                {(s.tags?.asset_class || []).map((a: string) => (
                  <span
                    key={a}
                    style={{
                      background: "rgba(63,185,80,0.12)",
                      color: "#3fb950",
                      fontSize: "0.68rem",
                      padding: "2px 8px",
                      borderRadius: "3px",
                    }}
                  >
                    {a}
                  </span>
                ))}
                {s.tags?.complexity && (
                  <span
                    style={{
                      background: "rgba(210,153,34,0.12)",
                      color: "#d29922",
                      fontSize: "0.68rem",
                      padding: "2px 8px",
                      borderRadius: "3px",
                    }}
                  >
                    {s.tags.complexity}
                  </span>
                )}
              </div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: "0.75rem",
                  color: "var(--color-text-muted)",
                  gap: "8px",
                  flexWrap: "wrap",
                }}
              >
                <span>{s.tags?.source || "unknown"}</span>
                <span>{s.backtest_count || 0} backtests</span>
                {s.created_at ? (
                  <span>
                    Added {new Date(s.created_at).toLocaleDateString("en-US")}
                  </span>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
