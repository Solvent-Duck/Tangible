"use client";

import { createContext, useContext } from "react";

/** What the rest of the app needs from auth, whichever provider is behind it. */
export type AuthState = {
  mode: "dev" | "clerk";
  ready: boolean;
  signedIn: boolean;
  /** Headers that authenticate an API call (Bearer token, or the dev headers). */
  authHeaders: () => Promise<Record<string, string>>;
  signOut: () => Promise<void>;
  /** Dev mode only: act as `subject` (optionally in `tenant`). */
  devSignIn?: (subject: string, tenant?: string | null, email?: string | null) => void;
  devSubject?: string | null;
};

export const AuthContext = createContext<AuthState | null>(null);

export function useAuthState(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuthState outside AuthProvider");
  return ctx;
}

export const AUTH_MODE: "dev" | "clerk" =
  process.env.NEXT_PUBLIC_AUTH_MODE === "clerk" ? "clerk" : "dev";

// ---------------------------------------------------------------- tenant choice (per browser)

const TENANT_KEY = "tangible.tenant";

export function storedTenant(): string | null {
  try {
    return window.localStorage.getItem(TENANT_KEY);
  } catch {
    return null;
  }
}

export function storeTenant(tenant: string | null): void {
  try {
    if (tenant) window.localStorage.setItem(TENANT_KEY, tenant);
    else window.localStorage.removeItem(TENANT_KEY);
  } catch {
    /* private mode: the tenant is re-chosen on the next load */
  }
}
