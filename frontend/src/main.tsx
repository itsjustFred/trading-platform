import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import App from "./App";
import { StrategiesProvider } from "./components/StrategiesProvider";
import { ToastProvider } from "./components/ToastProvider";
import { initFrontendSentry } from "./lib/sentryConfig";
import { queryClient } from "./lib/queryClient";

try {
  await initFrontendSentry();
} catch (error) {
  console.error("Sentry initialization failed", error);
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <StrategiesProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </StrategiesProvider>
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
