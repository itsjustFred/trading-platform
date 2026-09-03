import { useEffect, useMemo, useState } from "react";

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

type StreamPayloadState<TData> = {
  /** URL the current payload/error was received on. */
  url: string | null;
  data: TData | null;
  error: string | null;
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

function isDataEnvelopeType(type: string): boolean {
  return type === "snapshot" || type === "update";
}

function useLiveStream<TData>(path: string | null): LiveStreamState<TData> {
  const wsUrl = useMemo(() => (path ? buildWebSocketUrl(path) : null), [path]);
  // Payload state is keyed by URL so a topic change (e.g. another account) does not
  // serve data from the previous stream, and so heartbeat/ack/error envelopes keep
  // the last snapshot intact instead of blanking it (downstream effects act on
  // `data`; transient nulls made them skip refreshes and charts flicker empty).
  const [payload, setPayload] = useState<StreamPayloadState<TData>>({
    url: wsUrl,
    data: null,
    error: null,
  });

  const stream = useWebSocket<LiveStreamEnvelope<TData>>(wsUrl, {
    enabled: Boolean(wsUrl),
    heartbeatIntervalMs: STREAM_HEARTBEAT_INTERVAL_MS,
    heartbeatTimeoutMs: STREAM_HEARTBEAT_TIMEOUT_MS,
    onMessage: (message) => {
      setPayload((current) => {
        const base =
          current.url === wsUrl ? current : { url: wsUrl, data: null, error: null };
        const messageType = String(message?.type || "").toLowerCase();
        if (messageType === "error") {
          return { ...base, error: message?.error || "Live stream error" };
        }
        if (isDataEnvelopeType(messageType) && message?.data !== undefined) {
          return {
            url: wsUrl,
            // A fresh snapshot means the stream is healthy again.
            error: messageType === "snapshot" ? null : base.error,
            data: message.data ?? null,
          };
        }
        return base;
      });
    },
  });

  useEffect(() => {
    // Deferred (not synchronous in the effect body): resets the payload when the
    // topic changes so a switch never serves the previous stream's data.
    const task = window.setTimeout(() => {
      setPayload({ url: wsUrl, data: null, error: null });
    }, 0);
    return () => {
      window.clearTimeout(task);
    };
  }, [wsUrl]);

  // Derived, not written: a topic switch must not serve the previous stream's data.
  const isCurrentTopic = payload.url === wsUrl;
  const data = isCurrentTopic ? payload.data : null;
  const error = (isCurrentTopic ? payload.error : null) ?? stream.error;

  return {
    data,
    isConnected: stream.isConnected,
    status: stream.status,
    error,
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
