"use client";

import { useAuth } from "@clerk/nextjs";
import { useMemo } from "react";

import { AuthContext, type AuthState, storeTenant, storedTenant } from "@/lib/auth-context";

/** Clerk auth: the API gets the Clerk session token as a Bearer header. */
export function ClerkAuthBridge({ children }: { children: React.ReactNode }) {
  const { isLoaded, isSignedIn, getToken, signOut } = useAuth();

  const value = useMemo<AuthState>(
    () => ({
      mode: "clerk",
      ready: isLoaded,
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
    }),
    [isLoaded, isSignedIn, getToken, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
