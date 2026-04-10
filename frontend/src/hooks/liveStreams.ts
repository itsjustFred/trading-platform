import { useMemo } from "react";

import type {
  LiveAccount,
  LivePositionsResponse,
  LiveSignalLogResponse,
  LiveTradesResponse,
} from "../api/types";
import { useWebSocket, type WebSocketStatus } from "./useWebSocket";

type LiveStreamEnvelope<TData> = {
  type: string;
  topic: string;
  data?: TData;
  error?: string;
  ts?: number;
};

type LiveStreamState<TData> = {
  data: TData | null;
  isConnected: boolean;
  status: WebSocketStatus;
  error: string | null;
  lastMessage: LiveStreamEnvelope<TData> | null;
};

const STREAM_HEARTBEAT_INTERVAL_MS = 15_000;
const STREAM_HEARTBEAT_TIMEOUT_MS = 45_000;

export type LiveAccountsStreamPayload = {
  accounts: LiveAccount[];
};

function buildWebSocketUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}

function useLiveStream<TData>(path: string | null): LiveStreamState<TData> {
  const wsUrl = useMemo(() => (path ? buildWebSocketUrl(path) : null), [path]);
  const stream = useWebSocket<LiveStreamEnvelope<TData>>(wsUrl, {
    enabled: Boolean(wsUrl),
    heartbeatIntervalMs: STREAM_HEARTBEAT_INTERVAL_MS,
    heartbeatTimeoutMs: STREAM_HEARTBEAT_TIMEOUT_MS,
  });

  const data = useMemo(() => {
    const message = stream.lastMessage;
    if (!message) {
      return null;
    }
    const messageType = String(message.type || "").toLowerCase();
    if ((messageType === "snapshot" || messageType === "update") && message.data !== undefined) {
      return message.data ?? null;
    }
    return null;
  }, [stream.lastMessage]);

  return {
    data,
    isConnected: stream.isConnected,
    status: stream.status,
    error: stream.error,
    lastMessage: stream.lastMessage,
  };
}

export function useLiveAccountsStream(): LiveStreamState<LiveAccountsStreamPayload> {
  return useLiveStream<LiveAccountsStreamPayload>("/ws/live/accounts");
}

export function useLivePositionsStream(accountId: string): LiveStreamState<LivePositionsResponse> {
  const path = accountId
    ? `/ws/live/${encodeURIComponent(accountId)}/positions`
    : null;
  return useLiveStream<LivePositionsResponse>(path);
}

export function useLiveSignalsStream(accountId: string): LiveStreamState<LiveSignalLogResponse> {
  const path = accountId
    ? `/ws/live/${encodeURIComponent(accountId)}/signals`
    : null;
  return useLiveStream<LiveSignalLogResponse>(path);
}

export function useLiveExecutionsStream(accountId: string): LiveStreamState<LiveTradesResponse> {
  const path = accountId
    ? `/ws/live/${encodeURIComponent(accountId)}/executions`
    : null;
  return useLiveStream<LiveTradesResponse>(path);
}
