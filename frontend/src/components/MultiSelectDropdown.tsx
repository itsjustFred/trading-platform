import { Check, ChevronDown, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

export interface MultiSelectOption {
  label: string;
  value: string;
}

interface MultiSelectDropdownProps {
  label: string;
  options: MultiSelectOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  searchable?: boolean;
  searchPlaceholder?: string;
  buttonWidth?: string;
}

const PANEL_BACKGROUND = "#151b28";

export function MultiSelectDropdown({
  label,
  options,
  selected,
  onChange,
  searchable = false,
  searchPlaceholder = "Filter options...",
  buttonWidth = "220px",
}: MultiSelectDropdownProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const wrapperRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleOutsideClick = (event: MouseEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleOutsideClick);
    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
    };
  }, []);

  const selectedSet = useMemo(() => new Set(selected), [selected]);

  const normalizedOptions = useMemo(() => {
    const deduped = new Map<string, string>();
    options.forEach((option) => {
      if (!option.value.trim()) return;
      deduped.set(option.value, option.label);
    });
    return [...deduped.entries()]
      .map(([value, text]) => ({ label: text, value }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [options]);

  const visibleOptions = useMemo(() => {
    if (!query.trim()) return normalizedOptions;
    const needle = query.trim().toLowerCase();
    return normalizedOptions.filter(
      (option) =>
        option.label.toLowerCase().includes(needle) ||
        option.value.toLowerCase().includes(needle),
    );
  }, [normalizedOptions, query]);

  const selectedPreview = useMemo(() => {
    const preview = normalizedOptions
      .filter((option) => selectedSet.has(option.value))
      .slice(0, 2)
      .map((option) => option.label);
    return preview.join(", ");
  }, [normalizedOptions, selectedSet]);

  const buttonLabel = selected.length === 0 ? label : `${label} (${selected.length})`;
  const buttonSuffix = selected.length > 0 && selectedPreview ? ` • ${selectedPreview}` : "";

  const toggleOption = (value: string) => {
    const next = new Set(selected);
    if (next.has(value)) {
      next.delete(value);
    } else {
      next.add(value);
    }
    onChange([...next]);
  };

  return (
    <div ref={wrapperRef} style={{ position: "relative", minWidth: buttonWidth }}>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        style={{
          width: "100%",
          border: "1px solid rgba(142, 155, 187, 0.25)",
          background: "rgba(19, 23, 34, 0.86)",
          color: "var(--color-text)",
          borderRadius: "10px",
          fontSize: "0.78rem",
          padding: "9px 12px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "8px",
          cursor: "pointer",
        }}
      >
        <span
          style={{
            display: "block",
            textAlign: "left",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {buttonLabel}
          {buttonSuffix}
        </span>
        <ChevronDown size={14} />
      </button>

      {open ? (
        <div
          style={{
            position: "absolute",
            insetInlineStart: 0,
            marginTop: "6px",
            width: "min(360px, 92vw)",
            border: "1px solid rgba(142, 155, 187, 0.3)",
            borderRadius: "12px",
            background: PANEL_BACKGROUND,
            boxShadow: "0 14px 34px rgba(0, 0, 0, 0.45)",
            zIndex: 30,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: "8px",
              padding: "10px 12px",
              borderBottom: "1px solid rgba(142, 155, 187, 0.2)",
            }}
          >
            <span style={{ fontSize: "0.74rem", color: "var(--color-text-muted)" }}>
              {selected.length} selected
            </span>
            {selected.length > 0 ? (
              <button
                type="button"
                onClick={() => onChange([])}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "4px",
                  fontSize: "0.72rem",
                  border: "none",
                  background: "transparent",
                  color: "#9cb5ff",
                  cursor: "pointer",
                }}
              >
                <X size={12} /> Clear
              </button>
            ) : null}
          </div>

          {searchable ? (
            <div
              style={{
                padding: "10px 12px",
                borderBottom: "1px solid rgba(142, 155, 187, 0.14)",
                position: "relative",
              }}
            >
              <Search
                size={14}
                style={{
                  position: "absolute",
                  insetInlineStart: "20px",
                  top: "50%",
                  transform: "translateY(-50%)",
                  color: "var(--color-text-muted)",
                }}
              />
              <input
                type="text"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={searchPlaceholder}
                style={{
                  width: "100%",
                  borderRadius: "8px",
                  border: "1px solid rgba(142, 155, 187, 0.25)",
                  background: "rgba(19, 23, 34, 0.92)",
                  color: "var(--color-text)",
                  fontSize: "0.75rem",
                  padding: "8px 10px 8px 28px",
                }}
              />
            </div>
          ) : null}

          <div style={{ maxHeight: "240px", overflowY: "auto" }}>
            {visibleOptions.length === 0 ? (
              <div
                style={{
                  padding: "12px",
                  color: "var(--color-text-muted)",
                  fontSize: "0.76rem",
                }}
              >
                No matching options
              </div>
            ) : (
              visibleOptions.map((option) => {
                const active = selectedSet.has(option.value);
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => toggleOption(option.value)}
                    style={{
                      width: "100%",
                      border: "none",
                      background: active ? "rgba(88, 166, 255, 0.14)" : "transparent",
                      color: active ? "#dbe7ff" : "var(--color-text)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: "8px",
                      padding: "9px 12px",
                      fontSize: "0.75rem",
                      cursor: "pointer",
                    }}
                  >
                    <span>{option.label}</span>
                    {active ? <Check size={14} /> : null}
                  </button>
                );
              })
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
