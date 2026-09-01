interface BadgeProps {
  children: React.ReactNode;
  color?: "green" | "red" | "blue" | "yellow" | "purple" | "muted";
}

const colorMap: Record<string, string> = {
  green: "bg-green/15 text-green border-green/30",
  red: "bg-red/15 text-red border-red/30",
  blue: "bg-blue/15 text-blue border-blue/30",
  yellow: "bg-yellow/15 text-yellow border-yellow/30",
  purple: "bg-purple/15 text-purple border-purple/30",
  muted: "bg-surface2 text-text-muted border-border",
};

export function Badge({ children, color = "muted" }: Readonly<BadgeProps>) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 text-xs rounded-full border ${colorMap[color]}`}
    >
      {children}
    </span>
  );
}
