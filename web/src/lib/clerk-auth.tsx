"use client";

import { useAuth } from "@clerk/nextjs";
import { useMemo } from "react";

import { AuthContext, type AuthState, storeTenant, storedTenant } from "@/lib/auth-context";
import { setDemoSession, useDemoSession } from "@/lib/demo-session";

/** Clerk auth: the API gets the Clerk session token as a Bearer header. A shared demo login, when
 * one is active in this browser, takes precedence. */
export function ClerkAuthBridge({ children }: { children: React.ReactNode }) {
  const { isLoaded, isSignedIn, getToken, signOut } = useAuth();
  const demo = useDemoSession();
  const demoToken = demo.session?.token ?? null;

  const value = useMemo<AuthState>(() => {
    if (demoToken) {
      return {
        mode: "clerk",
        demo: true,
        ready: true,
        signedIn: true,
        authHeaders: async () => {
          const h: Record<string, string> = { Authorization: `Bearer ${demoToken}` };
          const t = storedTenant();
          if (t) h["X-Tangible-Tenant"] = t;
          return h;
        },
        signOut: async () => {
          storeTenant(null);
          setDemoSession(null);
        },
      };
    }
    return {
      mode: "clerk",
      ready: isLoaded && demo.ready,
      signedIn: !!isSignedIn,
      authHeaders: async () => {
        const token = await getToken();
        const h: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
        const t = storedTenant();
        if (t) h["X-Tangible-Tenant"] = t;
        return h;
      },
      signOut: async () => {
        storeTenant(null);
        await signOut({ redirectUrl: "/" });
      },
    };
  }, [demoToken, demo.ready, isLoaded, isSignedIn, getToken, signOut]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
