import { useState, useEffect, useMemo, useRef } from "react";
import { useSymbols } from "../api/queries";
import { ChevronDown, ChevronRight } from "lucide-react";

interface SymbolInputProps {
  value: string;
  onChange: (val: string) => void;
  placeholder?: string;
  style?: React.CSSProperties;
}

const CATEGORY_LABELS: Record<string, string> = {
  equity: "📈 Equities",
  forex: "💱 Forex",
  indices: "📊 Indices",
  metals: "🥇 Metals",
  energy: "⛽ Energy",
  etf: "📦 ETFs",
  crypto: "₿ Crypto",
  other: "📁 Other",
};

export function SymbolInput({
  value,
  onChange,
  placeholder,
  style,
}: Readonly<SymbolInputProps>) {
  const [query, setQuery] = useState("");
  const [showDropdown, setShowDropdown] = useState(false);
  const [expandedCats, setExpandedCats] = useState<Record<string, boolean>>({});
  const wrapperRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const symbolsQuery = useSymbols();

  const categories = useMemo(
    () => symbolsQuery.data?.categories || {},
    [symbolsQuery.data],
  );
  // A short category list starts fully expanded; an explicit toggle wins.
  const defaultExpanded = useMemo(
    () => Object.keys(categories).length < 5,
    [categories],
  );
  const isExpanded = (cat: string) => expandedCats[cat] ?? defaultExpanded;

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(e.target as Node)
      ) {
        setShowDropdown(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const handleChange = (text: string) => {
    onChange(text);
    const parts = text.split(",").map((s) => s.trim());
    const current = parts.at(-1) || "";
    setQuery(current);
    setShowDropdown(true);

    // Auto-expand categories that have matches
    if (current) {
      const upper = current.toUpperCase();
      const newExpanded = { ...expandedCats };
      Object.entries(categories).forEach(([cat, syms]) => {
        if (syms.some((s) => s.toUpperCase().includes(upper))) {
          newExpanded[cat] = true;
        }
      });
      setExpandedCats(newExpanded);
    }
  };

  const selectSymbol = (sym: string) => {
    const parts = value.split(",").map((s) => s.trim());
    const completed = parts.slice(0, -1).filter(Boolean);
    const newVal = [...completed, sym].join(", ") + ", ";
    onChange(newVal);
    setShowDropdown(false);
    setQuery("");
    inputRef.current?.focus();
  };

  const toggleCategory = (cat: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setExpandedCats((prev) => ({
      ...prev,
      [cat]: !(prev[cat] ?? defaultExpanded),
    }));
  };

  // Filter categories based on query
  const filteredCategories = Object.entries(categories)
    .map(([cat, syms]) => {
      const matches = query
        ? syms.filter((s) => s.toUpperCase().includes(query.toUpperCase()))
        : syms;
      return { cat, syms: matches };
    })
    .filter((g) => g.syms.length > 0)
    .sort((a, b) => a.cat.localeCompare(b.cat));

  return (
    <div ref={wrapperRef} style={{ position: "relative" }}>
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => handleChange(e.target.value)}
        onFocus={() => setShowDropdown(true)}
        placeholder={placeholder || "AAPL, MSFT"}
        style={style}
      />
      {showDropdown && (
        <div
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            right: 0,
            zIndex: 100,
            maxHeight: "300px",
            overflowY: "auto",
            background: "var(--color-surface)",
            border: "1px solid var(--color-border)",
            borderRadius: "0 0 6px 6px",
            boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
          }}
        >
          {filteredCategories.length === 0 ? (
            <div
              style={{
                padding: "12px",
                color: "var(--color-text-muted)",
                fontSize: "0.8rem",
              }}
            >
              No symbols found
            </div>
          ) : (
            filteredCategories.map(({ cat, syms }) => (
              <div
                key={cat}
                style={{ borderBottom: "1px solid var(--color-border)" }}
              >
                <button
                  type="button"
                  onClick={(e) => toggleCategory(cat, e)}
                  style={{
                    width: "100%",
                    textAlign: "left",
                    padding: "8px 10px",
                    cursor: "pointer",
                    background: "var(--color-surface2)",
                    border: "none",
                    color: "inherit",
                    font: "inherit",
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    fontSize: "0.8rem",
                    fontWeight: 600,
                    userSelect: "none",
                  }}
                >
                  {isExpanded(cat) ? (
                    <ChevronDown size={14} />
                  ) : (
                    <ChevronRight size={14} />
                  )}
                  <span>{CATEGORY_LABELS[cat] || cat}</span>
                  <span
                    style={{
                      marginLeft: "auto",
                      fontSize: "0.7rem",
                      color: "var(--color-text-muted)",
                      background: "rgba(255,255,255,0.1)",
                      padding: "1px 6px",
                      borderRadius: "10px",
                    }}
                  >
                    {syms.length}
                  </span>
                </button>
                {isExpanded(cat) && (
                  <div style={{ padding: "4px 0" }}>
                    {syms.slice(0, 100).map((sym) => (
                      <button
                        key={sym}
                        type="button"
                        onClick={() => selectSymbol(sym)}
                        style={{
                          display: "block",
                          width: "100%",
                          textAlign: "left",
                          padding: "6px 28px",
                          cursor: "pointer",
                          fontSize: "0.85rem",
                          border: "none",
                          background: "transparent",
                          color: "var(--color-text)",
                        }}
                        onMouseEnter={(e) =>
                          (e.currentTarget.style.background =
                            "rgba(255,255,255,0.05)")
                        }
                        onMouseLeave={(e) =>
                          (e.currentTarget.style.background = "transparent")
                        }
                      >
                        {sym}
                      </button>
                    ))}
                    {syms.length > 100 && (
                      <div
                        style={{
                          padding: "6px 28px",
                          fontSize: "0.75rem",
                          color: "var(--color-text-muted)",
                          fontStyle: "italic",
                        }}
                      >
                        ...and {syms.length - 100} more
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
