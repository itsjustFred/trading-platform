import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./index.css";
import App from "./App";
import { StrategiesProvider } from "./components/StrategiesProvider";
import { ToastProvider } from "./components/ToastProvider";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ToastProvider>
      <StrategiesProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </StrategiesProvider>
    </ToastProvider>
  </StrictMode>,
);
