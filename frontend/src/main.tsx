import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import App from "./App";
import { StrategiesProvider } from "./components/StrategiesProvider";
import { ToastProvider } from "./components/ToastProvider";
import { captureFrontendException, initFrontendSentry } from "./lib/sentryConfig";
import { queryClient } from "./lib/queryClient";

void initFrontendSentry().then(() => {
  window.addEventListener("error", (event) => {
    captureFrontendException(event.error ?? new Error(event.message));
  });
  window.addEventListener("unhandledrejection", (event) => {
    captureFrontendException(event.reason);
  });
});

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
