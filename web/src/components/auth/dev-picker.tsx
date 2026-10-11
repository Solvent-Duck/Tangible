"use client";

import { useQuery } from "@tanstack/react-query";
import { Building2, ChevronRight, FlaskConical, UserRound } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Empty, Notice, Skeleton } from "@/components/ui/misc";
import { useApi } from "@/lib/api";
import { useAuthState } from "@/lib/auth-context";
import { homeFor } from "@/lib/session";
import { ago, cn, roleLabel } from "@/lib/utils";

type DevTenant = {
  id: string;
  name: string;
  created_at: string;
  users: { subject: string; name: string; role: string }[];
};

/** Dev sign-in: pick a tenant, then a person. Only works against a TANGIBLE_DEV_AUTH=1 API. */
export function DevPicker() {
  const api = useApi();
  const auth = useAuthState();
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next");
  const [open, setOpen] = useState<string | null>(params.get("tenant"));
  const tenants = useQuery({
    queryKey: ["dev-tenants"],
    queryFn: () => api<DevTenant[]>("/api/dev/tenants", { auth: false }),
  });

  function choose(t: DevTenant, u: DevTenant["users"][number]) {
    auth.devSignIn?.(u.subject, t.id);
    router.push(next && next.startsWith("/") ? next : homeFor(u.role));
  }

  return (
    <div className="w-full max-w-lg">
      <div className="mb-6 text-center">
        <h1 className="font-display text-[34px] leading-tight tracking-tight">Sign in</h1>
        <p className="mt-1 text-[14px] text-muted">Development mode: choose who you are.</p>
      </div>
      <Notice tone="review" icon={<FlaskConical />} className="mb-4">
        <strong>Dev auth.</strong> Anyone can act as anyone. Set <code>NEXT_PUBLIC_AUTH_MODE=clerk</code> for
        real sign-in.
      </Notice>
      <Card className="overflow-hidden">
        {tenants.isLoading && (
          <div className="space-y-3 p-5">
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
          </div>
        )}
        {tenants.error && (
          <div className="p-5">
            <Notice tone="hold">
              Could not reach the workbench API ({(tenants.error as Error).message}). Is it running with{" "}
              <code>TANGIBLE_DEV_AUTH=1</code>?
            </Notice>
          </div>
        )}
        {tenants.data && tenants.data.length === 0 && (
          <Empty icon={<Building2 />} title="No tenants yet">
            Run <code>make demo</code> to seed one.
          </Empty>
        )}
        <ul className="divide-y divide-line">
          {tenants.data?.map((t) => {
            const staff = t.users.filter((u) => u.role !== "borrower");
            const borrowers = t.users.filter((u) => u.role === "borrower");
            const isOpen = open === t.id || tenants.data?.length === 1;
            return (
              <li key={t.id}>
                <button
                  className="flex w-full items-center gap-3 px-5 py-3.5 text-left hover:bg-surface-2"
                  onClick={() => setOpen(isOpen ? null : t.id)}
                  aria-expanded={isOpen}
                >
                  <span className="grid size-8 place-items-center rounded-lg bg-surface-2 text-muted">
                    <Building2 className="size-4" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[14px] font-medium">{t.name}</span>
                    <span className="text-[12px] text-faint">
                      {t.users.length} people · created {ago(t.created_at)}
                    </span>
                  </span>
                  <ChevronRight className={cn("size-4 text-faint transition-transform", isOpen && "rotate-90")} />
                </button>
                {isOpen && (
                  <div className="space-y-1 bg-bg px-3 pt-1 pb-3">
                    {[...staff, ...borrowers].map((u) => (
                      <button
                        key={u.subject}
                        onClick={() => choose(t, u)}
                        className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left hover:bg-surface"
                      >
                        <UserRound className="size-4 text-faint" />
                        <span className="flex-1 text-[14px]">
                          {u.name} <span className="font-mono text-[12px] text-faint">{u.subject}</span>
                        </span>
                        <Badge tone={u.role === "borrower" ? "info" : u.role === "analyst" ? "brand" : "neutral"}>
                          {roleLabel(u.role)}
                        </Badge>
                      </button>
                    ))}
                    {t.users.length === 0 && <p className="px-3 py-2 text-[13px] text-muted">No users.</p>}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </Card>
    </div>
  );
}
