import { JSDOM } from "jsdom";

import type { LiveKillSwitchState } from "../src/api/types";

// Shared jsdom install for the kill-switch behavioural tests. react-dom must
// be imported AFTER installJsdom() runs: it decides at module init whether
// `input` events are supported (canUseDOM), and without the DOM globals it
// falls back to the IE propertychange path where onChange can never fire.

const GLOBAL_KEYS = [
  "window",
  "document",
  "navigator",
  "HTMLElement",
  "Element",
  "Node",
  "Event",
  "CustomEvent",
  "MouseEvent",
  "KeyboardEvent",
  "InputEvent",
  "FocusEvent",
  "DocumentFragment",
  "SVGElement",
  "MutationObserver",
  "getComputedStyle",
  "requestAnimationFrame",
  "cancelAnimationFrame",
  "localStorage",
  "sessionStorage",
  "HTMLInputElement",
  "HTMLButtonElement",
  "NodeFilter",
  "DOMParser",
  "XMLSerializer",
] as const;

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

export function installJsdom() {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "http://localhost/",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  for (const key of GLOBAL_KEYS) {
    const value = (window as unknown as Record<string, unknown>)[key];
    if (value !== undefined) {
      // Some globals (navigator) are getter-only on Node's globalThis.
      Object.defineProperty(globalThis, key, {
        value,
        configurable: true,
        writable: true,
      });
    }
  }
  globalThis.window = window;
  globalThis.document = window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  return window;
}

export function killSwitchState(
  overrides: Partial<LiveKillSwitchState> = {},
): LiveKillSwitchState {
  return {
    readable: true,
    global_scope: "global",
    modes: ["entries", "orders", "flatten"],
    halts: [],
    halted_accounts: [],
    global_halted: false,
    halt_details: [],
    ...overrides,
  };
}
