import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Returns `value` only after it has stayed unchanged for `delayMs`.
 * Use to keep request storms (search inputs, per-keystroke effects) off the wire.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  const lastValueRef = useRef({ value, debounced });

  useEffect(() => {
    lastValueRef.current = { value, debounced };
  });

  // Timer-based effect (not a synchronous write): the value is only adopted after
  // the delay has elapsed, so this is a subscription to "value went quiet".
  useEffect(() => {
    const adopt = () => {
      if (lastValueRef.current.debounced !== value) {
        setDebounced(value);
      }
    };
    if (delayMs <= 0) {
      const immediate = window.setTimeout(adopt, 0);
      return () => {
        window.clearTimeout(immediate);
      };
    }
    const timer = window.setTimeout(adopt, delayMs);
    return () => {
      window.clearTimeout(timer);
    };
  }, [value, delayMs]);

  return debounced;
}

/**
 * Debounces a callback: the latest invocation wins after `delayMs` of quiet, but at
 * latest after `maxWaitMs` — a trailing-only debounce starves when calls keep
 * arriving faster than the quiet period (e.g. a chatty WebSocket stream).
 * The callback identity may change every render; the latest one is always used.
 */
export function useDebouncedCallback<Args extends unknown[]>(
  callback: (...args: Args) => void,
  delayMs: number,
  maxWaitMs?: number,
): (...args: Args) => void {
  const callbackRef = useRef(callback);
  const timerRef = useRef<number | null>(null);
  const maxWaitTimerRef = useRef<number | null>(null);
  const pendingArgsRef = useRef<Args | null>(null);

  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  const clearTimers = () => {
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (maxWaitTimerRef.current != null) {
      window.clearTimeout(maxWaitTimerRef.current);
      maxWaitTimerRef.current = null;
    }
  };
  const clearTimersRef = useRef(clearTimers);

  useEffect(
    () => () => {
      clearTimersRef.current();
    },
    [],
  );

  const flush = useCallback(() => {
    const pending = pendingArgsRef.current;
    pendingArgsRef.current = null;
    clearTimersRef.current();
    if (pending == null) {
      return;
    }
    callbackRef.current(...pending);
  }, []);

  // Stable identity across renders: consumers may list this in effect deps, and a
  // per-render identity would re-trigger their effects (and with a setState in the
  // callback, that becomes a refresh loop).
  const debounced = useCallback(
    (...args: Args) => {
      pendingArgsRef.current = args;
      if (maxWaitTimerRef.current == null) {
        const wait = Math.max(0, maxWaitMs ?? delayMs);
        maxWaitTimerRef.current = window.setTimeout(flush, wait);
      }
      if (timerRef.current != null) {
        window.clearTimeout(timerRef.current);
      }
      timerRef.current = window.setTimeout(flush, delayMs);
    },
    [delayMs, flush, maxWaitMs],
  );

  return debounced;
}
