import { useQuery, type UseQueryOptions } from "@tanstack/react-query";

import { getStrategies, getStrategyDetail, getSymbols } from "./client";
import type { StrategyDetail, StrategyMeta } from "./types";

type SymbolsResponse = {
  symbols: string[];
  categories: Record<string, string[]>;
};

export function useStrategies(
  options?: Omit<UseQueryOptions<StrategyMeta[], Error>, "queryKey" | "queryFn">,
) {
  return useQuery({
    queryKey: ["strategies"],
    queryFn: getStrategies,
    staleTime: 60_000,
    ...options,
  });
}

export function useStrategyDetail(
  name: string,
  options?: Omit<UseQueryOptions<StrategyDetail, Error>, "queryKey" | "queryFn">,
) {
  return useQuery({
    queryKey: ["strategy-detail", name],
    queryFn: () => getStrategyDetail(name),
    enabled: Boolean(name) && (options?.enabled ?? true),
    staleTime: 60_000,
    ...options,
  });
}

export function useSymbols(
  options?: Omit<UseQueryOptions<SymbolsResponse, Error>, "queryKey" | "queryFn">,
) {
  return useQuery({
    queryKey: ["symbols"],
    queryFn: getSymbols,
    staleTime: 60_000,
    ...options,
  });
}
