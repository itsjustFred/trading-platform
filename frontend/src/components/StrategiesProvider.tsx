import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { getStrategies } from "../api/client";
import type { StrategyMeta } from "../api/types";

interface StrategiesContextValue {
  strategies: StrategyMeta[];
  loading: boolean;
  refresh: () => Promise<void>;
}

const StrategiesContext = createContext<StrategiesContextValue | null>(null);

export function StrategiesProvider({ children }: { children: ReactNode }) {
  const [strategies, setStrategies] = useState<StrategyMeta[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await getStrategies();
      setStrategies(payload);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({
      strategies,
      loading,
      refresh,
    }),
    [strategies, loading, refresh],
  );

  return <StrategiesContext.Provider value={value}>{children}</StrategiesContext.Provider>;
}

export function useStrategiesCache() {
  const context = useContext(StrategiesContext);
  if (!context) {
    throw new Error("useStrategiesCache must be used inside StrategiesProvider");
  }
  return context;
}

