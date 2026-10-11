"use client";

import { UserButton } from "@clerk/nextjs";
import { useQueryClient } from "@tanstack/react-query";
import {
  Briefcase,
  ChartColumn,
  ChevronsUpDown,
  Inbox,
  LayoutDashboard,
  LogOut,
  Menu,
  Moon,
  Send,
  Sun,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Avatar, Notice, Skeleton } from "@/components/ui/misc";
import { storeTenant, useAuthState } from "@/lib/auth-context";
import { useMembership } from "@/lib/session";
import type { Membership } from "@/lib/types";
import { cn, roleLabel } from "@/lib/utils";

type NavItem = { href: string; label: string; icon: typeof LayoutDashboard; roles: string[] };

const NAV: NavItem[] = [
  { href: "/app/dashboard", label: "Dashboard", icon: LayoutDashboard, roles: ["credit_admin", "lender_underwriter"] },
  { href: "/app/invites/new", label: "Invite a business", icon: Send, roles: ["credit_admin"] },
  { href: "/app/diligence", label: "Due diligence", icon: Briefcase, roles: ["analyst", "credit_admin", "lender_underwriter"] },
  { href: "/app/outbox", label: "Outbox", icon: Inbox, roles: ["credit_admin", "lender_underwriter"] },
  { href: "/app/backtest", label: "Backtest", icon: ChartColumn, roles: ["analyst", "credit_admin", "lender_underwriter"] },
];

/** Guards: signed in, a member, and staff (borrowers go to onboarding). */
export function AppShell({ children }: { children: React.ReactNode }) {
  const auth = useAuthState();
  const router = useRouter();
  const pathname = usePathname();
  const { membership, me, loading, error } = useMembership();

  useEffect(() => {
    if (auth.ready && !auth.signedIn) router.replace(`/sign-in?next=${encodeURIComponent(pathname)}`);
  }, [auth.ready, auth.signedIn, pathname, router]);
  useEffect(() => {
    if (membership?.role === "borrower") router.replace("/onboarding");
  }, [membership, router]);

  if (!auth.ready || !auth.signedIn || loading || membership?.role === "borrower") return <ShellSkeleton />;
  if (error || !membership) return <NoAccess subject={me?.subject} error={error} />;
  return (
    <Frame membership={membership} memberships={me?.memberships ?? []}>
      {children}
    </Frame>
  );
}

function Frame({
  membership,
  memberships,
  children,
}: {
  membership: Membership;
  memberships: Membership[];
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const auth = useAuthState();
  const nav = NAV.filter((n) => n.roles.includes(membership.role));

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[248px_1fr]">
      {/* mobile top bar */}
      <div className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-line bg-bg/90 px-4 backdrop-blur lg:hidden">
        <Link href="/app">
          <Logo />
        </Link>
        <div className="flex items-center gap-1">
          {auth.mode === "clerk" && <UserButton />}
          <Button variant="ghost" size="icon" onClick={() => setOpen(true)} aria-label="Open navigation">
            <Menu />
          </Button>
        </div>
      </div>
      {open && <div className="fixed inset-0 z-40 bg-black/30 lg:hidden" onClick={() => setOpen(false)} />}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-[264px] flex-col border-r border-line bg-surface transition-transform lg:sticky lg:top-0 lg:h-dvh lg:w-auto lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-16 items-center justify-between px-5">
          <Link href="/app" aria-label="Tangible">
            <Logo />
          </Link>
          <Button variant="ghost" size="icon" className="lg:hidden" onClick={() => setOpen(false)} aria-label="Close navigation">
            <X />
          </Button>
        </div>
        <TenantSwitcher membership={membership} memberships={memberships} />
        <nav className="mt-4 flex-1 space-y-0.5 px-3" aria-label="Main">
          {nav.map((n) => {
            const active =
              pathname === n.href || (n.href !== "/app/invites/new" && pathname.startsWith(n.href + "/"));
            return (
              <Link
                key={n.href}
                href={n.href}
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-[14px] transition-colors",
                  active ? "bg-brand-soft font-medium text-brand-ink" : "text-muted hover:bg-surface-2 hover:text-ink",
                )}
                aria-current={active ? "page" : undefined}
                onClick={() => setOpen(false)}
              >
                <n.icon className="size-[18px]" />
                {n.label}
              </Link>
            );
          })}
        </nav>
        <UserFooter membership={membership} />
      </aside>
      <main className="min-w-0">{children}</main>
    </div>
  );
}

function TenantSwitcher({ membership, memberships }: { membership: Membership; memberships: Membership[] }) {
  const qc = useQueryClient();
  const router = useRouter();
  const staff = memberships.filter((m) => m.role !== "borrower");
  return (
    <div className="px-3">
      <label className="sr-only" htmlFor="tenant">
        Lender
      </label>
      <div className="relative">
        <select
          id="tenant"
          value={membership.tenant_id}
          disabled={staff.length < 2}
          onChange={(e) => {
            storeTenant(e.target.value);
            qc.clear();
            router.push("/app");
          }}
          className="w-full appearance-none truncate rounded-lg border border-line bg-bg py-2 pr-8 pl-3 text-[13.5px] font-medium text-ink disabled:cursor-default disabled:opacity-100"
        >
          {staff.map((m) => (
            <option key={m.tenant_id} value={m.tenant_id}>
              {m.tenant_name}
            </option>
          ))}
        </select>
        {staff.length > 1 && (
          <ChevronsUpDown className="pointer-events-none absolute top-1/2 right-2.5 size-4 -translate-y-1/2 text-faint" />
        )}
      </div>
    </div>
  );
}

function UserFooter({ membership }: { membership: Membership }) {
  const auth = useAuthState();
  const router = useRouter();
  const qc = useQueryClient();
  // The shell only renders in the browser (it waits for auth), so reading the DOM here is safe.
  const [dark, setDark] = useState<boolean>(() => {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  });
  function toggleTheme() {
    const next = !dark;
    document.documentElement.dataset.theme = next ? "dark" : "light";
    try {
      localStorage.setItem("tangible.theme", next ? "dark" : "light");
    } catch {
      /* ignore */
    }
    setDark(next);
  }
  return (
    <div className="border-t border-line p-3">
      <div className="flex items-center gap-3 rounded-lg px-2 py-2">
        {auth.mode === "clerk" ? <UserButton /> : <Avatar name={membership.display_name} />}
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13.5px] font-medium">{membership.display_name}</p>
          <p className="truncate text-[12px] text-faint">{roleLabel(membership.role)}</p>
        </div>
        <Button variant="ghost" size="icon" onClick={toggleTheme} aria-label="Toggle dark mode" className="size-8">
          {dark ? <Sun /> : <Moon />}
        </Button>
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="mt-1 w-full justify-start px-3"
        onClick={async () => {
          qc.clear();
          await auth.signOut();
          router.replace("/");
        }}
      >
        <LogOut /> Sign out
      </Button>
    </div>
  );
}

function ShellSkeleton() {
  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[248px_1fr]">
      <div className="hidden border-r border-line bg-surface p-5 lg:block">
        <Skeleton className="h-7 w-28" />
        <Skeleton className="mt-6 h-9" />
        <div className="mt-6 space-y-2">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-8" />
          ))}
        </div>
      </div>
      <div className="p-6 lg:p-10">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="mt-6 h-40" />
      </div>
    </div>
  );
}

function NoAccess({ subject, error }: { subject?: string; error: unknown }) {
  const auth = useAuthState();
  return (
    <div className="grid min-h-dvh place-items-center px-4">
      <div className="w-full max-w-md text-center">
        <Logo className="justify-center" />
        <h1 className="mt-8 font-display text-[30px] leading-tight">You&apos;re signed in, but not set up yet</h1>
        <p className="mt-2 text-[14px] text-muted">
          If a lender invited your business, open the link in your invite email. Staff accounts are added by an
          administrator.
        </p>
        {subject && (
          <Notice tone="neutral" className="mt-5 text-left">
            Your account id is <code className="font-mono">{subject}</code>. An administrator can add you with{" "}
            <code className="font-mono">tangible-admin add-user</code>.
          </Notice>
        )}
        {error ? <p className="mt-3 text-[12px] text-faint">{(error as Error).message}</p> : null}
        <Button className="mt-6" onClick={() => auth.signOut()}>
          Sign out
        </Button>
      </div>
    </div>
  );
}
