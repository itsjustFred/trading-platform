import { lazy, Suspense } from "react";
import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { TaskManager } from "./components/TaskManager";
import { LoadingSpinner } from "./components/LoadingSpinner";

const BacktestsPage = lazy(() =>
  import("./pages/BacktestsPage").then((mod) => ({ default: mod.BacktestsPage })),
);
const BacktestDetailPage = lazy(() =>
  import("./pages/BacktestDetailPage").then((mod) => ({
    default: mod.BacktestDetailPage,
  })),
);
const MatrixGridPage = lazy(() =>
  import("./pages/MatrixGridPage").then((mod) => ({ default: mod.MatrixGridPage })),
);
const TradeChartPage = lazy(() =>
  import("./pages/TradeChartPage").then((mod) => ({ default: mod.TradeChartPage })),
);
const ComparePage = lazy(() =>
  import("./pages/ComparePage").then((mod) => ({ default: mod.ComparePage })),
);
const StrategiesPage = lazy(() =>
  import("./pages/StrategiesPage").then((mod) => ({ default: mod.StrategiesPage })),
);
const StrategyDetailPage = lazy(() =>
  import("./pages/StrategyDetailPage").then((mod) => ({
    default: mod.StrategyDetailPage,
  })),
);
const PortfoliosPage = lazy(() =>
  import("./pages/PortfoliosPage").then((mod) => ({ default: mod.PortfoliosPage })),
);
const PortfolioDetailPage = lazy(() =>
  import("./pages/PortfolioDetailPage").then((mod) => ({
    default: mod.PortfolioDetailPage,
  })),
);
const LoggingPage = lazy(() =>
  import("./pages/LoggingPage").then((mod) => ({ default: mod.LoggingPage })),
);
const DataManagementPage = lazy(() =>
  import("./pages/DataManagementPage").then((mod) => ({
    default: mod.DataManagementPage,
  })),
);
const StrategyLabPage = lazy(() =>
  import("./pages/StrategyLabPage").then((mod) => ({ default: mod.StrategyLabPage })),
);
const OptimizationsPage = lazy(() =>
  import("./pages/OptimizationsPage").then((mod) => ({ default: mod.OptimizationsPage })),
);
const OptimizationResultsPage = lazy(() =>
  import("./pages/OptimizationResultsPage").then((mod) => ({
    default: mod.OptimizationResultsPage,
  })),
);
const LiveHubPage = lazy(() =>
  import("./pages/LiveHub").then((mod) => ({ default: mod.LiveHubPage })),
);
const ServicesPage = lazy(() =>
  import("./pages/Services").then((mod) => ({ default: mod.ServicesPage })),
);
const LiveDetailPage = lazy(() =>
  import("./pages/LiveDetail").then((mod) => ({ default: mod.LiveDetailPage })),
);
const LiveViewerPage = lazy(() =>
  import("./pages/LiveViewer").then((mod) => ({ default: mod.LiveViewerPage })),
);
const TaskHubPage = lazy(() =>
  import("./pages/TaskHub").then((mod) => ({ default: mod.TaskHubPage })),
);

export default function App() {
  const location = useLocation();
  const showShellTaskManager = !location.pathname.startsWith("/data");

  return (
    <div className="flex min-h-screen w-full bg-bg">
      <Sidebar />
      <main className="flex-1 overflow-auto min-w-0">
        {showShellTaskManager ? (
          <div
            style={{
              display: "flex",
              justifyContent: "flex-end",
              padding: "20px 24px 0",
            }}
          >
            <div style={{ width: "min(100%, 420px)" }}>
              <TaskManager />
            </div>
          </div>
        ) : null}
        <Suspense fallback={<LoadingSpinner />}>
          <Routes>
            <Route path="/" element={<Navigate to="/strategies" replace />} />
            <Route path="/data" element={<DataManagementPage />} />
            <Route path="/backtests" element={<BacktestsPage />} />
            <Route path="/backtests/compare" element={<ComparePage />} />
            <Route path="/backtests/:id" element={<BacktestDetailPage />} />
            <Route path="/backtests/:id/trade/:nr" element={<TradeChartPage />} />
            <Route path="/matrix-grid" element={<MatrixGridPage />} />
            <Route path="/optimizations" element={<OptimizationsPage />} />
            <Route
              path="/optimizations/:id"
              element={<OptimizationResultsPage />}
            />
            <Route path="/live" element={<LiveHubPage />} />
            <Route path="/services" element={<ServicesPage />} />
            <Route path="/live/:accountId" element={<LiveDetailPage />} />
            <Route path="/live/:accountId/viewer" element={<LiveViewerPage />} />
            <Route path="/strategies" element={<StrategiesPage />} />
            <Route path="/strategies/:name" element={<StrategyDetailPage />} />
            <Route path="/portfolios" element={<PortfoliosPage />} />
            <Route path="/portfolios/:id" element={<PortfolioDetailPage />} />
            <Route path="/lab" element={<StrategyLabPage />} />
            <Route path="/tasks" element={<TaskHubPage />} />
            <Route path="/logging" element={<LoggingPage />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  );
}
