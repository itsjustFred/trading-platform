import { useQuery, type UseQueryOptions } from "@tanstack/react-query";

import {
  getLivePositions,
  getLiveQuote,
  getLiveSignalLog,
  getStrategies,
  getStrategyDetail,
  getSymbols,
} from "./client";
import type {
  LivePositionsResponse,
  LiveQuoteResponse,
  LiveSignalLogResponse,
  StrategyDetail,
  StrategyMeta,
} from "./types";

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
    staleTime: 300_000,
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

export function useLiveQuotes(
  symbol: string,
  options?: Omit<UseQueryOptions<LiveQuoteResponse, Error>, "queryKey" | "queryFn">,
) {
  return useQuery({
    queryKey: ["live-quotes", symbol],
    queryFn: () => getLiveQuote(symbol),
    enabled: Boolean(symbol) && (options?.enabled ?? true),
    refetchInterval: 30_000,
    staleTime: 30_000,
    ...options,
  });
}

export function useLivePositions(
  accountId: string,
  options?: Omit<UseQueryOptions<LivePositionsResponse, Error>, "queryKey" | "queryFn">,
) {
  return useQuery({
    queryKey: ["live-positions", accountId],
    queryFn: () => getLivePositions(accountId),
    enabled: Boolean(accountId) && (options?.enabled ?? true),
    refetchInterval: 30_000,
    staleTime: 30_000,
    ...options,
  });
}

export function useLiveSignalLog(
  accountId: string,
  limit = 200,
  options?: Omit<UseQueryOptions<LiveSignalLogResponse, Error>, "queryKey" | "queryFn">,
) {
  return useQuery({
    queryKey: ["live-signal-log", accountId, limit],
    queryFn: () => getLiveSignalLog(accountId, limit),
    enabled: Boolean(accountId) && (options?.enabled ?? true),
    refetchInterval: 30_000,
    staleTime: 30_000,
    ...options,
  });
}
