import { useMemo } from "react";

const MONO_FONT = '"SFMono-Regular", "Cascadia Code", "Roboto Mono", ui-monospace, monospace';

interface DualRangeSliderProps {
  min: number;
  max: number;
  valueMin: number;
  valueMax: number;
  onChange: (min: number, max: number) => void;
  step?: number;
  label: string;
  formatValue?: (value: number) => string;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function normalizeStep(step: number | undefined, min: number, max: number): number {
  if (step && Number.isFinite(step) && step > 0) return step;
  const span = Math.abs(max - min);
  if (span <= 2) return 0.01;
  if (span <= 20) return 0.1;
  if (span <= 200) return 1;
  return 5;
}

function defaultFormatter(value: number): string {
  if (!Number.isFinite(value)) return "N/A";
  if (Math.abs(value) >= 1000) return value.toFixed(0);
  if (Math.abs(value) >= 100) return value.toFixed(1);
  if (Math.abs(value) >= 1) return value.toFixed(2);
  return value.toFixed(3);
}

export function DualRangeSlider({
  min,
  max,
  valueMin,
  valueMax,
  onChange,
  step,
  label,
  formatValue = defaultFormatter,
}: DualRangeSliderProps) {
  const normalizedMin = Number.isFinite(min) ? min : 0;
  const normalizedMax = Number.isFinite(max) ? max : 1;
  const resolvedStep = normalizeStep(step, normalizedMin, normalizedMax);
  const safeValueMin = clamp(Math.min(valueMin, valueMax), normalizedMin, normalizedMax);
  const safeValueMax = clamp(Math.max(valueMin, valueMax), normalizedMin, normalizedMax);
  const span = Math.max(normalizedMax - normalizedMin, Number.EPSILON);
  const leftPercent = ((safeValueMin - normalizedMin) / span) * 100;
  const rightPercent = ((safeValueMax - normalizedMin) / span) * 100;

  const trackStyle = useMemo<React.CSSProperties>(
    () => ({
      position: "absolute",
      insetBlockStart: "7px",
      insetInlineStart: `${leftPercent}%`,
      width: `${Math.max(0, rightPercent - leftPercent)}%`,
      height: "4px",
      borderRadius: "999px",
      background: "linear-gradient(90deg, rgba(88,166,255,0.85), rgba(56,189,248,0.9))",
      pointerEvents: "none",
    }),
    [leftPercent, rightPercent],
  );

  const updateMin = (next: number) => {
    const clamped = clamp(next, normalizedMin, safeValueMax);
    onChange(clamped, safeValueMax);
  };

  const updateMax = (next: number) => {
    const clamped = clamp(next, safeValueMin, normalizedMax);
    onChange(safeValueMin, clamped);
  };

  return (
    <div
      style={{
        borderRadius: "12px",
        border: "1px solid rgba(92, 105, 135, 0.28)",
        background: "linear-gradient(180deg, rgba(18, 24, 37, 0.96), rgba(12, 17, 28, 0.96))",
        padding: "10px 12px",
        display: "grid",
        gap: "9px",
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
        <span style={{ fontSize: "0.75rem", color: "var(--color-text-muted)" }}>{label}</span>
        <span style={{ fontSize: "0.72rem", color: "#9cb5ff", fontFamily: MONO_FONT }}>
          {formatValue(safeValueMin)} to {formatValue(safeValueMax)}
        </span>
      </div>

      <div style={{ position: "relative", height: "18px" }}>
        <div
          style={{
            position: "absolute",
            insetBlockStart: "7px",
            insetInlineStart: 0,
            right: 0,
            height: "4px",
            borderRadius: "999px",
            background: "rgba(100, 116, 139, 0.35)",
            pointerEvents: "none",
          }}
        />
        <div style={trackStyle} />
        <input
          type="range"
          min={normalizedMin}
          max={normalizedMax}
          step={resolvedStep}
          value={safeValueMin}
          onChange={(event) => updateMin(Number(event.target.value))}
          style={{
            position: "absolute",
            inset: 0,
            width: "100%",
            background: "transparent",
            appearance: "none",
            pointerEvents: "auto",
            accentColor: "#58a6ff",
          }}
        />
        <input
          type="range"
          min={normalizedMin}
          max={normalizedMax}
          step={resolvedStep}
          value={safeValueMax}
          onChange={(event) => updateMax(Number(event.target.value))}
          style={{
            position: "absolute",
            inset: 0,
            width: "100%",
            background: "transparent",
            appearance: "none",
            pointerEvents: "auto",
            accentColor: "#58a6ff",
          }}
        />
      </div>
    </div>
  );
}
