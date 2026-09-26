import { createContext, useContext } from "react";

import type { StrategyMeta } from "../api/types";

export interface StrategiesContextValue {
  strategies: StrategyMeta[];
  loading: boolean;
  refresh: () => Promise<void>;
  ensureLoaded: () => Promise<void>;
}

export const StrategiesContext = createContext<StrategiesContextValue | null>(
  null,
);

export function useStrategiesCache() {
  const context = useContext(StrategiesContext);
  if (!context) {
    throw new Error(
      "useStrategiesCache must be used inside StrategiesProvider",
    );
  }
  return context;
}
