import { useCallback, useEffect, useRef, useState } from "react";

export type WebSocketStatus =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected"
  | "error";

export interface UseWebSocketOptions<TMessage> {
  enabled?: boolean;
  reconnectDelayMs?: number;
  maxReconnectDelayMs?: number;
  heartbeatIntervalMs?: number;
  heartbeatTimeoutMs?: number;
  onMessage?: (message: TMessage) => void;
  /** Invoked on every successful (re)connection, after the socket is open. */
  onOpen?: () => void;
  /**
   * "json" (default) parses frames as JSON and drops non-JSON frames.
   * "text" delivers every non-empty string frame as-is (plain-text feeds).
   */
  messageFormat?: "json" | "text";
}

export interface UseWebSocketResult<TMessage> {
  status: WebSocketStatus;
  isConnected: boolean;
  lastMessage: TMessage | null;
  error: string | null;
  sendJson: (payload: unknown) => boolean;
}

const DEFAULT_RECONNECT_DELAY_MS = 1_000;
const DEFAULT_MAX_RECONNECT_DELAY_MS = 15_000;
const DEFAULT_HEARTBEAT_INTERVAL_MS = 15_000;
const DEFAULT_HEARTBEAT_TIMEOUT_MS = 45_000;

/** Builds an absolute ws(s):// URL for a same-origin server path. */
export function buildWebSocketUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}

function parseIncomingMessage<TMessage>(value: unknown): TMessage | null {
  if (typeof value !== "string") {
    return null;
  }
  try {
    return JSON.parse(value) as TMessage;
  } catch {
    return null;
  }
}

export function useWebSocket<TMessage = unknown>(
  url: string | null,
  options: UseWebSocketOptions<TMessage> = {},
): UseWebSocketResult<TMessage> {
  const {
    enabled = true,
    reconnectDelayMs = DEFAULT_RECONNECT_DELAY_MS,
    maxReconnectDelayMs = DEFAULT_MAX_RECONNECT_DELAY_MS,
    heartbeatIntervalMs = DEFAULT_HEARTBEAT_INTERVAL_MS,
    heartbeatTimeoutMs = DEFAULT_HEARTBEAT_TIMEOUT_MS,
    onMessage,
    onOpen,
    messageFormat = "json",
  } = options;

  const socketRef = useRef<WebSocket | null>(null);
  const onMessageRef = useRef<typeof onMessage>(onMessage);
  const onOpenRef = useRef<typeof onOpen>(onOpen);
  // Plain-text feeds cannot exchange JSON control frames; a non-positive
  // interval therefore disables the heartbeat timer entirely.
  const heartbeatEnabled = heartbeatIntervalMs > 0;
  const [status, setStatus] = useState<WebSocketStatus>("idle");
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<TMessage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    onMessageRef.current = onMessage;
    onOpenRef.current = onOpen;
  }, [onMessage, onOpen]);

  useEffect(() => {
    if (!enabled || !url) {
      return;
    }

    let disposed = false;
    let reconnectAttempts = 0;
    let reconnectTimer: number | null = null;
    let heartbeatTimer: number | null = null;
    let lastActivityAt = Date.now();
    let socket: WebSocket | null = null;

    const clearReconnectTimer = () => {
      if (reconnectTimer != null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const clearHeartbeatTimer = () => {
      if (heartbeatTimer != null) {
        window.clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
    };

    const closeSocket = () => {
      const currentSocket = socket;
      if (currentSocket == null) {
        return;
      }
      if (
        currentSocket.readyState === WebSocket.OPEN
        || currentSocket.readyState === WebSocket.CONNECTING
      ) {
        currentSocket.close();
      }
      if (socketRef.current === currentSocket) {
        socketRef.current = null;
      }
      socket = null;
    };

    const scheduleReconnect = () => {
      if (disposed || !enabled) {
        return;
      }
      reconnectAttempts += 1;
      const delay = Math.min(
        maxReconnectDelayMs,
        reconnectDelayMs * 2 ** (reconnectAttempts - 1),
      );
      setStatus("reconnecting");
      clearReconnectTimer();
      reconnectTimer = window.setTimeout(() => {
        if (disposed) {
          return;
        }
        connect();
      }, delay);
    };

    const handleControlMessage = (
      rawMessage: unknown,
      activeSocket: WebSocket,
    ): boolean => {
      if (!rawMessage || typeof rawMessage !== "object" || !("type" in rawMessage)) {
        return false;
      }
      const rawType = (rawMessage as { type?: unknown }).type;
      const msgType = (typeof rawType === "string" ? rawType : "").toLowerCase();
      if (msgType === "ping") {
        lastActivityAt = Date.now();
        if (activeSocket.readyState === WebSocket.OPEN) {
          activeSocket.send(JSON.stringify({ type: "pong", ts: Date.now() / 1000 }));
        }
        return true;
      }
      if (msgType === "pong") {
        lastActivityAt = Date.now();
        return true;
      }
      return false;
    };

    const startHeartbeat = () => {
      clearHeartbeatTimer();
      if (!heartbeatEnabled) {
        return;
      }
      heartbeatTimer = window.setInterval(() => {
        if (disposed || socket == null || socket.readyState !== WebSocket.OPEN) {
          return;
        }
        if (Date.now() - lastActivityAt > heartbeatTimeoutMs) {
          socket.close(4000, "heartbeat-timeout");
          return;
        }
        socket.send(JSON.stringify({ type: "ping", ts: Date.now() / 1000 }));
      }, heartbeatIntervalMs);
    };

    const connect = () => {
      if (disposed) {
        return;
      }
      closeSocket();
      setStatus(reconnectAttempts > 0 ? "reconnecting" : "connecting");
      setError(null);

      const activeSocket = new WebSocket(url);
      socket = activeSocket;
      socketRef.current = activeSocket;

      activeSocket.onopen = () => {
        if (disposed || socketRef.current !== activeSocket) {
          return;
        }
        reconnectAttempts = 0;
        lastActivityAt = Date.now();
        setStatus("connected");
        setIsConnected(true);
        setError(null);
        startHeartbeat();
        onOpenRef.current?.();
      };

      activeSocket.onmessage = (event) => {
        if (disposed || socketRef.current !== activeSocket) {
          return;
        }
        if (messageFormat === "text") {
          const line = typeof event.data === "string" ? event.data : "";
          if (!line) {
            return;
          }
          lastActivityAt = Date.now();
          onMessageRef.current?.(line as TMessage);
          return;
        }
        const parsed = parseIncomingMessage<TMessage | Record<string, unknown>>(event.data);
        if (parsed == null) {
          return;
        }
        if (handleControlMessage(parsed, activeSocket)) {
          return;
        }
        lastActivityAt = Date.now();
        const normalized = parsed as TMessage;
        setLastMessage(normalized);
        onMessageRef.current?.(normalized);
      };

      activeSocket.onerror = () => {
        if (disposed || socketRef.current !== activeSocket) {
          return;
        }
        setStatus("error");
        setIsConnected(false);
        setError("WebSocket connection error");
      };

      activeSocket.onclose = () => {
        if (disposed || socketRef.current !== activeSocket) {
          return;
        }
        clearHeartbeatTimer();
        socketRef.current = null;
        socket = null;
        setIsConnected(false);
        setStatus("disconnected");
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      disposed = true;
      clearReconnectTimer();
      clearHeartbeatTimer();
      closeSocket();
      if (socketRef.current === socket) {
        socketRef.current = null;
      }
      setIsConnected(false);
      setStatus("disconnected");
    };
  }, [
    enabled,
    heartbeatEnabled,
    heartbeatIntervalMs,
    heartbeatTimeoutMs,
    maxReconnectDelayMs,
    messageFormat,
    reconnectDelayMs,
    url,
  ]);

  const sendJson = useCallback((payload: unknown): boolean => {
    const socket = socketRef.current;
    if (socket == null || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    socket.send(JSON.stringify(payload));
    return true;
  }, []);

  // Derived: an unmounted/disabled hook reports idle rather than a stale socket state.
  const active = enabled && url != null;
  const resolvedStatus: WebSocketStatus = active ? status : "idle";
  const resolvedIsConnected = active && isConnected;

  return {
    status: resolvedStatus,
    isConnected: resolvedIsConnected,
    lastMessage: active ? lastMessage : null,
    error: active ? error : null,
    sendJson,
  };
}
