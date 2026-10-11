"use client";

import { useCallback, useMemo, useSyncExternalStore } from "react";

import { AuthContext, type AuthState, storeTenant, storedTenant } from "@/lib/auth-context";

const DEV_KEY = "tangible.dev";

type DevSession = { subject: string; email?: string | null };

const SERVER = "\u0000ssr";
const CHANGED = "tangible:dev-session";

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
    return window.localStorage.getItem(DEV_KEY) ?? "";
  } catch {
    return "";
  }
}

function parse(raw: string): DevSession | null {
  if (!raw || raw === SERVER) return null;
  try {
    return JSON.parse(raw) as DevSession;
  } catch {
    return null;
  }
}

function write(value: DevSession | null): void {
  try {
    if (value) window.localStorage.setItem(DEV_KEY, JSON.stringify(value));
    else window.localStorage.removeItem(DEV_KEY);
  } catch {
    /* private mode */
  }
  window.dispatchEvent(new Event(CHANGED));
}

/**
 * Dev auth: the workbench trusts X-Tangible-User / X-Tangible-Tenant (TANGIBLE_DEV_AUTH=1).
 * Who you are is a choice on /dev-login, kept in this browser. Never for anything real.
 */
export function DevAuthProvider({ children }: { children: React.ReactNode }) {
  const raw = useSyncExternalStore(subscribe, snapshot, () => SERVER);
  const ready = raw !== SERVER;
  const session = useMemo(() => parse(raw), [raw]);

  const devSignIn = useCallback((subject: string, tenant?: string | null, email?: string | null) => {
    if (tenant !== undefined) storeTenant(tenant);
    write({ subject, email: email ?? null });
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      mode: "dev",
      ready,
      signedIn: !!session,
      devSubject: session?.subject ?? null,
      devSignIn,
      authHeaders: async () => {
        if (!session) return {};
        const h: Record<string, string> = { "X-Tangible-User": session.subject };
        if (session.email) h["X-Tangible-Email"] = session.email;
        const t = storedTenant();
        if (t) h["X-Tangible-Tenant"] = t;
        return h;
      },
      signOut: async () => {
        storeTenant(null);
        write(null);
      },
    }),
    [ready, session, devSignIn],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
