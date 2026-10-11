"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";

import { useApi } from "@/lib/api";
import { storeTenant, storedTenant, useAuthState } from "@/lib/auth-context";
import type { Me, Membership } from "@/lib/types";

/** The signed-in identity and its memberships. */
export function useMe() {
  const api = useApi();
  const auth = useAuthState();
  return useQuery({
    queryKey: ["me", auth.mode, auth.devSubject ?? null],
    queryFn: () => api<Me>("/api/me"),
    enabled: auth.ready && auth.signedIn,
    staleTime: 60_000,
    retry: false,
  });
}

/**
 * The membership in use: the remembered tenant if the user still belongs to it, else the first.
 * Keeps the remembered tenant in sync so every API call sends it.
 */
export function useMembership(): { membership: Membership | null; me: Me | undefined; loading: boolean; error: unknown } {
  const me = useMe();
  const membership = useMemo(() => {
    const list = me.data?.memberships ?? [];
    if (!list.length) return null;
    const want = typeof window !== "undefined" ? storedTenant() : null;
    return list.find((m) => m.tenant_id === want) ?? list[0];
  }, [me.data]);
  useEffect(() => {
    if (membership && storedTenant() !== membership.tenant_id) storeTenant(membership.tenant_id);
  }, [membership]);
  return { membership, me: me.data, loading: me.isLoading, error: me.error };
}

export function homeFor(role: string | undefined | null): string {
  if (role === "borrower") return "/onboarding";
  if (role === "analyst") return "/app/diligence";
  return "/app/dashboard";
}
