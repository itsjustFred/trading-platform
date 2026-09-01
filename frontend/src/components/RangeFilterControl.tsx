import { RotateCcw } from "lucide-react";

export interface RangeValue {
  min: number;
  max: number;
}

interface RangeFilterControlProps {
  label: string;
  value: RangeValue;
  bounds: RangeValue;
  step?: number;
  onChange: (next: RangeValue) => void;
  numberSuffix?: string;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function RangeFilterControl({
  label,
  value,
  bounds,
  step = 0.01,
  onChange,
  numberSuffix = "",
}: Readonly<RangeFilterControlProps>) {
  const updateMin = (nextMin: number) => {
    if (!Number.isFinite(nextMin)) return;
    const normalized = clamp(nextMin, bounds.min, value.max);
    onChange({ min: normalized, max: value.max });
  };

  const updateMax = (nextMax: number) => {
    if (!Number.isFinite(nextMax)) return;
    const normalized = clamp(nextMax, value.min, bounds.max);
    onChange({ min: value.min, max: normalized });
  };

  const reset = () => {
    onChange({ min: bounds.min, max: bounds.max });
  };

  return (
    <div
      style={{
        minWidth: "240px",
        borderRadius: "12px",
        border: "1px solid rgba(143, 154, 184, 0.2)",
        background: "rgba(19, 23, 34, 0.88)",
        padding: "10px 12px",
        display: "grid",
        gap: "8px",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "8px",
        }}
      >
        <span style={{ fontSize: "0.74rem", color: "var(--color-text-muted)" }}>{label}</span>
        <button
          type="button"
          onClick={reset}
          style={{
            border: "none",
            background: "transparent",
            color: "var(--color-text-muted)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: "4px",
            fontSize: "0.7rem",
          }}
        >
          <RotateCcw size={11} /> Reset
        </button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
        <label style={{ display: "grid", gap: "4px" }}>
          <span style={{ fontSize: "0.69rem", color: "var(--color-text-muted)" }}>Min</span>
          <input
            type="number"
            value={value.min}
            step={step}
            min={bounds.min}
            max={bounds.max}
            onChange={(event) => updateMin(Number(event.target.value))}
            style={{
              borderRadius: "8px",
              border: "1px solid rgba(143, 154, 184, 0.28)",
              background: "#101520",
              color: "var(--color-text)",
              fontSize: "0.74rem",
              padding: "6px 8px",
            }}
          />
        </label>

        <label style={{ display: "grid", gap: "4px" }}>
          <span style={{ fontSize: "0.69rem", color: "var(--color-text-muted)" }}>Max</span>
          <input
            type="number"
            value={value.max}
            step={step}
            min={bounds.min}
            max={bounds.max}
            onChange={(event) => updateMax(Number(event.target.value))}
            style={{
              borderRadius: "8px",
              border: "1px solid rgba(143, 154, 184, 0.28)",
              background: "#101520",
              color: "var(--color-text)",
              fontSize: "0.74rem",
              padding: "6px 8px",
            }}
          />
        </label>
      </div>

      <div style={{ display: "grid", gap: "6px" }}>
        <input
          type="range"
          value={value.min}
          min={bounds.min}
          max={bounds.max}
          step={step}
          onChange={(event) => updateMin(Number(event.target.value))}
          style={{ accentColor: "#58a6ff" }}
        />
        <input
          type="range"
          value={value.max}
          min={bounds.min}
          max={bounds.max}
          step={step}
          onChange={(event) => updateMax(Number(event.target.value))}
          style={{ accentColor: "#58a6ff" }}
        />
      </div>

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontFamily: '"SFMono-Regular", "Cascadia Code", ui-monospace, monospace',
          fontSize: "0.7rem",
          color: "var(--color-text-muted)",
        }}
      >
        <span>
          {value.min}
          {numberSuffix}
        </span>
        <span>
          {value.max}
          {numberSuffix}
        </span>
      </div>
    </div>
  );
}
