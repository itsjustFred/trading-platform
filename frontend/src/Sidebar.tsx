import { useState } from "react";
import { NavLink } from "react-router-dom";
import {
  BarChart3,
  LineChart,
  Briefcase,
  Bot,
  ScrollText,
  Users,
  Radio,
  PanelLeftClose,
  PanelLeftOpen,
  Database,
  Target,
  Activity,
} from "lucide-react";

const navItems = [
  { path: "/strategies", label: "Strategies", icon: LineChart },
  { path: "/backtests", label: "Backtests", icon: BarChart3 },
  { path: "/optimizations", label: "Optimizations", icon: Target },
  { path: "/live", label: "Live Hub", icon: Radio },
  { path: "/portfolios", label: "Portfolio", icon: Briefcase },
  { path: "/lab", label: "Strategy Lab", icon: Bot },
  { path: "/data", label: "Data", icon: Database },
  { path: "/tasks", label: "Task Hub", icon: Activity },
  { path: "/logging", label: "Logging", icon: ScrollText },
  { path: "/accounts", label: "Accounts", icon: Users, disabled: true },
];

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <aside
      className={`flex flex-col bg-surface border-r border-border shrink-0 transition-all duration-200 ${
        collapsed ? "w-[52px]" : "w-[200px]"
      }`}
      style={{ minHeight: "100vh" }}
    >
      {/* Collapse toggle — top */}
      <button
        onClick={() => setCollapsed((c) => !c)}
        className="flex items-center justify-center h-11 border-b border-border text-text-muted hover:text-text hover:bg-surface2 transition-colors"
        title={collapsed ? "Expand" : "Collapse"}
      >
        {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
      </button>

      {/* Nav */}
      <nav className="flex-1 py-1">
        {navItems.map((item) => {
          const Icon = item.icon;
          if (item.disabled) {
            return (
              <div
                key={item.path}
                className={`flex items-center gap-3 h-10 text-text-muted/30 cursor-not-allowed ${
                  collapsed ? "justify-center px-0" : "px-4"
                }`}
                title="Coming soon"
              >
                <Icon size={18} className="shrink-0" />
                {!collapsed && (
                  <span className="text-[0.82rem]">{item.label}</span>
                )}
              </div>
            );
          }
          return (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) =>
                `flex items-center gap-3 h-10 transition-colors ${
                  collapsed ? "justify-center px-0" : "px-4"
                } ${
                  isActive
                    ? "text-blue bg-blue/10 border-r-2 border-blue"
                    : "text-text-muted hover:text-text hover:bg-surface2"
                }`
              }
            >
              <Icon size={18} className="shrink-0" />
              {!collapsed && (
                <span className="text-[0.82rem]">{item.label}</span>
              )}
            </NavLink>
          );
        })}
      </nav>
    </aside>
  );
}
