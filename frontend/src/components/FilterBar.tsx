import { Search, Filter, ArrowUpDown } from "lucide-react";
import { useState } from "react";

interface FilterOption {
  label: string;
  value: string;
}

interface FilterConfig {
  key: string;
  label: string;
  options: FilterOption[];
}

interface FilterBarProps {
  searchValue: string;
  onSearchChange: (v: string) => void;
  searchPlaceholder?: string;
  filters?: FilterConfig[];
  filterValues?: Record<string, string>;
  onFilterChange?: (key: string, value: string) => void;
  sortOptions?: FilterOption[];
  sortValue?: string;
  onSortChange?: (v: string) => void;
}

export function FilterBar({
  searchValue,
  onSearchChange,
  searchPlaceholder = "Search...",
  filters = [],
  filterValues = {},
  onFilterChange,
  sortOptions,
  sortValue,
  onSortChange,
}: FilterBarProps) {
  const [showFilters, setShowFilters] = useState(false);

  const inputStyle: React.CSSProperties = {
    background: "var(--color-surface2)",
    border: "1px solid var(--color-border)",
    color: "var(--color-text)",
    padding: "6px 10px",
    borderRadius: "4px",
    fontSize: "0.8rem",
    outline: "none",
  };

  const btnStyle: React.CSSProperties = {
    ...inputStyle,
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    gap: "4px",
  };

  return (
    <div style={{ marginBottom: "16px" }}>
      <div
        style={{
          display: "flex",
          gap: "8px",
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        {/* Search */}
        <div style={{ position: "relative", flex: "1", minWidth: "200px" }}>
          <Search
            size={14}
            style={{
              position: "absolute",
              left: "8px",
              top: "50%",
              transform: "translateY(-50%)",
              color: "var(--color-text-muted)",
            }}
          />
          <input
            type="text"
            value={searchValue}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder={searchPlaceholder}
            style={{ ...inputStyle, width: "100%", paddingLeft: "28px" }}
          />
        </div>

        {/* Filter toggle */}
        {filters.length > 0 && (
          <button
            onClick={() => setShowFilters((s) => !s)}
            style={{
              ...btnStyle,
              color: showFilters
                ? "var(--color-blue)"
                : "var(--color-text-muted)",
              borderColor: showFilters
                ? "var(--color-blue)"
                : "var(--color-border)",
            }}
          >
            <Filter size={14} />
            Filter
          </button>
        )}

        {/* Sort */}
        {sortOptions && onSortChange && (
          <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
            <ArrowUpDown
              size={14}
              style={{ color: "var(--color-text-muted)" }}
            />
            <select
              value={sortValue}
              onChange={(e) => onSortChange(e.target.value)}
              style={inputStyle}
            >
              {sortOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* Filter dropdowns */}
      {showFilters && filters.length > 0 && (
        <div
          style={{
            display: "flex",
            gap: "8px",
            marginTop: "8px",
            flexWrap: "wrap",
            padding: "8px 12px",
            background: "var(--color-surface)",
            border: "1px solid var(--color-border)",
            borderRadius: "6px",
          }}
        >
          {filters.map((f) => (
            <div
              key={f.key}
              style={{ display: "flex", alignItems: "center", gap: "4px" }}
            >
              <label
                style={{
                  color: "var(--color-text-muted)",
                  fontSize: "0.75rem",
                }}
              >
                {f.label}:
              </label>
              <select
                value={filterValues[f.key] || ""}
                onChange={(e) => onFilterChange?.(f.key, e.target.value)}
                style={inputStyle}
              >
                <option value="">All</option>
                {f.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
