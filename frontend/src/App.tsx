import { Routes, Route, Navigate } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { BacktestsPage } from "./pages/BacktestsPage";
import { BacktestDetailPage } from "./pages/BacktestDetailPage";
import { TradeChartPage } from "./pages/TradeChartPage";
import { ComparePage } from "./pages/ComparePage";
import { StrategiesPage } from "./pages/StrategiesPage";
import { StrategyDetailPage } from "./pages/StrategyDetailPage";
import { PortfoliosPage } from "./pages/PortfoliosPage";
import { PortfolioDetailPage } from "./pages/PortfolioDetailPage";
import { LoggingPage } from "./pages/LoggingPage";
import { DataManagementPage } from "./pages/DataManagementPage";
import { StrategyLabPage } from "./pages/StrategyLabPage";

export default function App() {
  return (
    <div className="flex min-h-screen w-full bg-bg">
      <Sidebar />
      <main className="flex-1 overflow-auto min-w-0">
        <Routes>
          <Route path="/" element={<Navigate to="/strategies" replace />} />
          <Route path="/data" element={<DataManagementPage />} />
          <Route path="/backtests" element={<BacktestsPage />} />
          <Route path="/backtests/compare" element={<ComparePage />} />
          <Route path="/backtests/:id" element={<BacktestDetailPage />} />
          <Route path="/backtests/:id/trade/:nr" element={<TradeChartPage />} />
          <Route path="/strategies" element={<StrategiesPage />} />
          <Route path="/strategies/:name" element={<StrategyDetailPage />} />
          <Route path="/portfolios" element={<PortfoliosPage />} />
          <Route path="/portfolios/:id" element={<PortfolioDetailPage />} />
          <Route path="/lab" element={<StrategyLabPage />} />
          <Route path="/logging" element={<LoggingPage />} />
        </Routes>
      </main>
    </div>
  );
}
