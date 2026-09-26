import { useState, useMemo } from "react";

import { stringifyUnknownValue } from "../lib/unknownValue";

interface Column<T> {
  key: string;
  label: string;
  sortable?: boolean;
  render?: (row: T) => React.ReactNode;
  align?: "left" | "right" | "center";
  width?: string;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  data: T[];
  onRowClick?: (row: T) => void;
  maxHeight?: string;
}

export function DataTable<T extends Record<string, unknown>>({
  columns,
  data,
  onRowClick,
  maxHeight = "600px",
}: Readonly<DataTableProps<T>>) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  const sorted = useMemo(() => {
    if (!sortKey) return data;
    return [...data].sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (av == null || bv == null) return 0;
      const cmp =
        typeof av === "number" && typeof bv === "number"
          ? av - bv
          : String(av).localeCompare(String(bv));
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [data, sortKey, sortDir]);

  const rowKey = (row: T, index: number): string => {
    for (const col of columns) {
      const value = row[col.key];
      if (typeof value === "string" || typeof value === "number") {
        return String(value);
      }
    }
    return `row-${index}`;
  };

  const handleSort = (key: string) => {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir("asc");
    }
  };

  return (
    <div
      style={{
        maxHeight,
        overflowY: "auto",
        border: "1px solid var(--color-border)",
        borderRadius: "6px",
      }}
    >
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: "0.78rem",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <thead style={{ position: "sticky", top: 0, zIndex: 2 }}>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                onClick={() => col.sortable !== false && handleSort(col.key)}
                style={{
                  background: "var(--color-surface2)",
                  color: "var(--color-text-muted)",
                  fontWeight: 600,
                  padding: "8px 6px",
                  textAlign: col.align || "center",
                  borderBottom: "2px solid var(--color-border)",
                  cursor: "pointer",
                  userSelect: "none",
                  whiteSpace: "nowrap",
                  width: col.width,
                }}
              >
                {col.label}
                {sortKey === col.key && (
                  <span style={{ fontSize: "0.65rem", marginLeft: "2px" }}>
                    {sortDir === "asc" ? "▲" : "▼"}
                  </span>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, i) => (
            <tr
              key={rowKey(row, i)}
              onClick={() => onRowClick?.(row)}
              style={{ cursor: onRowClick ? "pointer" : "default" }}
              onMouseEnter={(e) =>
                (e.currentTarget.style.background = "rgba(88, 166, 255, 0.06)")
              }
              onMouseLeave={(e) =>
                (e.currentTarget.style.background = "transparent")
              }
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  style={{
                    padding: "6px 6px",
                    textAlign: col.align || "center",
                    borderBottom: "1px solid var(--color-border)",
                    whiteSpace: "nowrap",
                  }}
                >
                  {col.render
                    ? col.render(row)
                    : stringifyUnknownValue(row[col.key], "\u2014")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
