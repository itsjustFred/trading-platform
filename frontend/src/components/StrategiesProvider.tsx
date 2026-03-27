import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { getStrategies } from "../api/client";
import type { StrategyMeta } from "../api/types";

interface StrategiesContextValue {
  strategies: StrategyMeta[];
  loading: boolean;
  refresh: () => Promise<void>;
  ensureLoaded: () => Promise<void>;
}

const StrategiesContext = createContext<StrategiesContextValue | null>(null);

export function StrategiesProvider({ children }: { children: ReactNode }) {
  const [strategies, setStrategies] = useState<StrategyMeta[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const pendingLoadRef = useRef<Promise<void> | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await getStrategies();
      setStrategies(payload);
      setLoaded(true);
      pendingLoadRef.current = null;
    } finally {
      setLoading(false);
    }
  }, []);

  const ensureLoaded = useCallback(async () => {
    if (loaded) {
      return;
    }
    if (pendingLoadRef.current) {
      await pendingLoadRef.current;
      return;
    }
    pendingLoadRef.current = (async () => {
      try {
        await refresh();
      } finally {
        pendingLoadRef.current = null;
      }
    })();
    await pendingLoadRef.current;
  }, [loaded, refresh]);

  const value = useMemo(
    () => ({
      strategies,
      loading,
      refresh,
      ensureLoaded,
    }),
    [strategies, loading, refresh, ensureLoaded],
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
