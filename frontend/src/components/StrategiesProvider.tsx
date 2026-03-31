import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";

import { getStrategies } from "../api/client";
import { useStrategies } from "../api/queries";
import type { StrategyMeta } from "../api/types";

interface StrategiesContextValue {
  strategies: StrategyMeta[];
  loading: boolean;
  refresh: () => Promise<void>;
  ensureLoaded: () => Promise<void>;
}

const StrategiesContext = createContext<StrategiesContextValue | null>(null);

export function StrategiesProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const strategiesQuery = useStrategies({ enabled: false });
  const strategies = strategiesQuery.data ?? [];
  const loading = strategiesQuery.isFetching;
  const loaded = strategiesQuery.data !== undefined;
  const pendingLoadRef = useRef<Promise<void> | null>(null);

  const refresh = useCallback(async () => {
    try {
      await queryClient.fetchQuery({
        queryKey: ["strategies"],
        queryFn: getStrategies,
      });
      pendingLoadRef.current = null;
    } catch (error) {
      pendingLoadRef.current = null;
      throw error;
    }
  }, [queryClient]);

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
