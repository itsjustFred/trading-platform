import { memo, useMemo, useState } from "react";

/**
 * Sparkline — tiny SVG equity curve for card previews.
 * Green if last > first (profitable), red otherwise.
 */
export const Sparkline = memo(function Sparkline({
  data,
  width = 200,
  height = 50,
  trend,
  interactive = false,
  startDate,
  endDate,
}: {
  data: number[];
  width?: number;
  height?: number;
  trend?: "up" | "down";
  interactive?: boolean;
  startDate?: string;
  endDate?: string;
}) {
  const normalized = useMemo(
    () => (Array.isArray(data) ? data.filter((value) => Number.isFinite(value)).map((value) => Number(value)) : []),
    [data],
  );
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  if (normalized.length < 2) return null;

  const min = Math.min(...normalized);
  const max = Math.max(...normalized);
  const range = max - min || 1;
  const profitable =
    trend != null
      ? trend === "up"
      : normalized[normalized.length - 1] >= normalized[0];
  const color = profitable ? "#3fb950" : "#f85149";

  const points = normalized
    .map((v, i) => {
      const x = (i / (normalized.length - 1)) * width;
      const y = height - ((v - min) / range) * (height - 4) - 2;
      return `${x},${y}`;
    })
    .join(" ");

  // Fill area under the line
  const fillPoints = `0,${height} ${points} ${width},${height}`;
  const drawdowns = useMemo(() => {
    let peak = Number.NEGATIVE_INFINITY;
    return normalized.map((value) => {
      peak = Math.max(peak, value);
      if (!Number.isFinite(peak) || Math.abs(peak) < 1e-9) {
        return 0;
      }
      return ((value - peak) / Math.abs(peak)) * 100;
    });
  }, [normalized]);

  const hoverIdx = hoverIndex == null ? null : Math.max(0, Math.min(normalized.length - 1, hoverIndex));
  const hoverX = hoverIdx == null ? 0 : (hoverIdx / (normalized.length - 1)) * width;
  const hoverY = hoverIdx == null ? 0 : height - ((normalized[hoverIdx] - min) / range) * (height - 4) - 2;
  const hoverValue = hoverIdx == null ? null : normalized[hoverIdx];
  const hoverDrawdown = hoverIdx == null ? null : drawdowns[hoverIdx];
  const parsedStart = Date.parse(startDate || "");
  const parsedEnd = Date.parse(endDate || "");
  const hasDateRange = Number.isFinite(parsedStart) && Number.isFinite(parsedEnd) && parsedEnd >= parsedStart;
  const hoverDate =
    hoverIdx == null
      ? ""
      : hasDateRange
        ? new Date(parsedStart + ((parsedEnd - parsedStart) * hoverIdx) / (normalized.length - 1)).toLocaleDateString("de-DE")
        : "";
  const hoverPointLabel = hoverIdx == null ? "" : `Punkt ${hoverIdx + 1}`;

  return (
    <div style={{ position: "relative", width, height, overflow: "visible" }}>
      <svg
        width={width}
        height={height}
        style={{ display: "block" }}
        onMouseMove={
          interactive
            ? (event) => {
                const rect = event.currentTarget.getBoundingClientRect();
                const relativeX = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
                const ratio = rect.width > 0 ? relativeX / rect.width : 0;
                setHoverIndex(Math.round(ratio * (normalized.length - 1)));
              }
            : undefined
        }
        onMouseLeave={interactive ? () => setHoverIndex(null) : undefined}
      >
        <polygon points={fillPoints} fill={color} fillOpacity="0.08" />
        <polyline
          points={points}
          fill="none"
          stroke={color}
          strokeWidth="1.5"
          strokeLinejoin="round"
        />
        {interactive && hoverIdx != null ? (
          <>
            <line
              x1={hoverX}
              x2={hoverX}
              y1={2}
              y2={height - 2}
              stroke="rgba(255,255,255,0.35)"
              strokeWidth="1"
              strokeDasharray="2 2"
            />
            <circle cx={hoverX} cy={hoverY} r={2.5} fill={color} stroke="rgba(15,23,42,0.85)" strokeWidth="1" />
          </>
        ) : null}
      </svg>
      {interactive && hoverValue != null ? (
        <div
          style={{
            position: "absolute",
            top: 4,
            left: Math.max(4, Math.min(width - 176, hoverX - 86)),
            background: "rgba(15, 23, 42, 0.96)",
            border: "1px solid rgba(148, 163, 184, 0.3)",
            borderRadius: 6,
            padding: "4px 6px",
            fontSize: "0.66rem",
            color: "#e5e7eb",
            pointerEvents: "none",
            whiteSpace: "nowrap",
          }}
        >
          <div>{hoverDate || hoverPointLabel}</div>
          <div>
            {hoverValue.toLocaleString("en-US", { maximumFractionDigits: 0 })}
            {hoverDrawdown != null ? ` · DD ${hoverDrawdown.toFixed(2)}%` : ""}
          </div>
        </div>
      ) : null}
    </div>
  );
});
