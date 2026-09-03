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
 * Debounces a callback: the latest invocation wins after `delayMs` of quiet.
 * The callback identity may change every render; the latest one is always used.
 */
export function useDebouncedCallback<Args extends unknown[]>(
  callback: (...args: Args) => void,
  delayMs: number,
): (...args: Args) => void {
  const callbackRef = useRef(callback);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(
    () => () => {
      if (timerRef.current != null) {
        window.clearTimeout(timerRef.current);
      }
    },
    [],
  );

  // Stable identity across renders: consumers may list this in effect deps, and a
  // per-render identity would re-trigger their effects (and with a setState in the
  // callback, that becomes a refresh loop).
  const debounced = useCallback(
    (...args: Args) => {
      if (timerRef.current != null) {
        window.clearTimeout(timerRef.current);
      }
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        callbackRef.current(...args);
      }, delayMs);
    },
    [delayMs],
  );

  return debounced;
}
