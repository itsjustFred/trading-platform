import { memo } from "react";

/**
 * Sparkline — tiny SVG equity curve for card previews.
 * Green if last > first (profitable), red otherwise.
 */
export const Sparkline = memo(function Sparkline({
  data,
  width = 200,
  height = 50,
}: {
  data: number[];
  width?: number;
  height?: number;
}) {
  if (!data || data.length < 2) return null;

  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const profitable = data[data.length - 1] >= data[0];
  const color = profitable ? "#3fb950" : "#f85149";

  const points = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * width;
      const y = height - ((v - min) / range) * (height - 4) - 2;
      return `${x},${y}`;
    })
    .join(" ");

  // Fill area under the line
  const fillPoints = `0,${height} ${points} ${width},${height}`;

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <polygon points={fillPoints} fill={color} fillOpacity="0.08" />
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
});
