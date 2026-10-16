"use client";

import { useSyncExternalStore } from "react";

/**
 * The shared demo login (NEXT_PUBLIC_DEMO_LOGIN=1, API TANGIBLE_DEMO_LOGIN_*): a token from
 * /api/demo-login, kept in this browser beside Clerk. A plain cookie mirrors it so the proxy lets
 * the visitor into /app; the API still checks the token on every call.
 */
export const DEMO_LOGIN = process.env.NEXT_PUBLIC_DEMO_LOGIN === "1";
export const DEMO_COOKIE = "tangible_demo";

const KEY = "tangible.demo";
const CHANGED = "tangible:demo-session";
const SERVER = "\u0000ssr";

export type DemoSession = { token: string; expires_at: number };

function subscribe(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  window.addEventListener(CHANGED, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(CHANGED, onChange);
  };
}

function snapshot(): string {
  try {
    return window.localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

function parse(raw: string): DemoSession | null {
  if (!raw || raw === SERVER) return null;
  try {
    const s = JSON.parse(raw) as DemoSession;
    return s.expires_at * 1000 > Date.now() ? s : null;
  } catch {
    return null;
  }
}

export function setDemoSession(value: DemoSession | null): void {
  try {
    if (value) window.localStorage.setItem(KEY, JSON.stringify(value));
    else window.localStorage.removeItem(KEY);
  } catch {
    /* private mode: the demo session lasts until reload */
  }
  const secure = window.location.protocol === "https:" ? "; secure" : "";
  const age = value ? Math.max(0, Math.floor(value.expires_at - Date.now() / 1000)) : 0;
  document.cookie = `${DEMO_COOKIE}=${value ? "1" : ""}; path=/; max-age=${age}; samesite=lax${secure}`;
  window.dispatchEvent(new Event(CHANGED));
}

/** The live demo session, or null; `ready` is false until the browser has been read. */
export function useDemoSession(): { ready: boolean; session: DemoSession | null } {
  const raw = useSyncExternalStore(subscribe, DEMO_LOGIN ? snapshot : () => "", () => SERVER);
  return { ready: raw !== SERVER, session: parse(raw) };
}
