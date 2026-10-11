"use client";

import { SignUp } from "@clerk/nextjs";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowRight, Clock3, Lock, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { RequestedList } from "@/components/app/requested-list";
import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { Notice, Skeleton } from "@/components/ui/misc";
import { ApiError, useApi } from "@/lib/api";
import { storeTenant, useAuthState } from "@/lib/auth-context";
import type { InvitePreview } from "@/lib/types";
import { date, money } from "@/lib/utils";

export default function InviteLanding() {
  const { token } = useParams<{ token: string }>();
  const api = useApi();
  const preview = useQuery({
    queryKey: ["invite-preview-public", token],
    queryFn: () => api<InvitePreview>(`/api/onboarding/invite/${token}`, { auth: false }),
    retry: false,
  });

  return (
    <div className="min-h-dvh bg-bg">
      <header className="mx-auto flex h-16 max-w-5xl items-center justify-between px-4 sm:px-6">
        <Link href="/">
          <Logo />
        </Link>
        <span className="flex items-center gap-1.5 text-[12.5px] text-faint">
          <Lock className="size-3.5" /> Secure invite
        </span>
      </header>
      <main className="mx-auto max-w-5xl px-4 pt-6 pb-20 sm:px-6">
        {preview.isLoading && <Skeleton className="h-96 rounded-2xl" />}
        {preview.error && (
          <Ended
            title="This invite link isn't valid"
            body={
              preview.error instanceof ApiError && preview.error.status === 404
                ? "Check that you copied the whole link from your email, or ask your lender for a new one."
                : (preview.error as Error).message
            }
          />
        )}
        {preview.data && preview.data.state !== "live" && (
          <Ended
            title={preview.data.state === "expired" ? "This invite has expired" : "This invite is no longer active"}
            body={`Ask ${preview.data.lender_name} to send you a new link.`}
          />
        )}
        {preview.data && preview.data.state === "live" && <Live p={preview.data} token={token} />}
      </main>
    </div>
  );
}

function Ended({ title, body }: { title: string; body: string }) {
  return (
    <Card className="mx-auto max-w-lg p-8 text-center">
      <Clock3 className="mx-auto size-8 text-faint" />
      <h1 className="mt-4 text-[20px] font-semibold">{title}</h1>
      <p className="mt-2 text-[14px] text-muted">{body}</p>
    </Card>
  );
}

function Live({ p, token }: { p: InvitePreview; token: string }) {
  return (
    <div className="grid gap-8 lg:grid-cols-[1fr_420px] lg:items-start">
      <section>
        <p className="text-[13px] font-medium text-brand">{p.lender_name} invited you</p>
        <h1 className="mt-2 font-display text-[38px] leading-[1.08] tracking-tight sm:text-[46px]">
          Share {p.business_name}&apos;s story, securely.
        </h1>
        <p className="mt-4 max-w-xl text-[15.5px] leading-relaxed text-muted">
          {p.contact_name ? `Hi ${p.contact_name}. ` : ""}
          {p.lender_name} is reviewing {p.loan_amount ? `a ${money(p.loan_amount)} loan` : "a loan application"} for{" "}
          {p.business_name}. Instead of emailing statements, connect your systems once and add a few details. It takes
          about ten minutes.
        </p>
        {p.message && (
          <blockquote className="mt-6 rounded-xl border-l-[3px] border-brand bg-surface px-5 py-4 text-[14.5px] leading-relaxed whitespace-pre-line shadow-card">
            {p.message}
            <footer className="mt-2 text-[12.5px] text-faint">— {p.lender_name}</footer>
          </blockquote>
        )}
        <h2 className="mt-10 mb-3 text-[15px] font-semibold">What {p.lender_name} asked for</h2>
        <RequestedList items={p.items} />
        <ul className="mt-8 grid gap-3 text-[13px] text-muted sm:grid-cols-3">
          <li className="flex gap-2">
            <ShieldCheck className="size-4 shrink-0 text-brand" /> Read-only connections
          </li>
          <li className="flex gap-2">
            <Lock className="size-4 shrink-0 text-brand" /> You see each permission first
          </li>
          <li className="flex gap-2">
            <Clock3 className="size-4 shrink-0 text-brand" /> Link valid until {date(p.expires_at)}
          </li>
        </ul>
      </section>
      <aside className="lg:sticky lg:top-6">
        <Join p={p} token={token} />
      </aside>
    </div>
  );
}

function Join({ p, token }: { p: InvitePreview; token: string }) {
  const auth = useAuthState();
  if (!auth.ready) return <Skeleton className="h-80 rounded-2xl" />;
  if (auth.mode === "clerk") {
    return auth.signedIn ? (
      <AcceptCard p={p} token={token} />
    ) : (
      <div className="flex justify-center">
        <SignUp routing="hash" forceRedirectUrl={`/invite/${token}`} signInForceRedirectUrl={`/invite/${token}`} initialValues={{ emailAddress: p.email }} />
      </div>
    );
  }
  return <DevJoin p={p} token={token} />;
}

function AcceptCard({ p, token }: { p: InvitePreview; token: string }) {
  const api = useApi();
  const router = useRouter();
  const accept = useMutation({
    mutationFn: () => api<{ tenant_id: string }>("/api/onboarding/accept", { json: { token } }),
    onSuccess: (r) => {
      storeTenant(r.tenant_id);
      router.push("/onboarding");
    },
  });
  return (
    <Card className="p-6">
      <h2 className="text-[18px] font-semibold tracking-tight">You&apos;re signed in</h2>
      <p className="mt-1 text-[13.5px] text-muted">Continue to connect {p.business_name}&apos;s accounts.</p>
      {accept.error && (
        <Notice tone="hold" className="mt-4">
          {(accept.error as Error).message}
        </Notice>
      )}
      <Button variant="primary" size="lg" className="mt-5 w-full" onClick={() => accept.mutate()} disabled={accept.isPending}>
        Continue <ArrowRight />
      </Button>
    </Card>
  );
}

/** Dev mode: "creating an account" is choosing a dev identity tied to an email. */
function DevJoin({ p, token }: { p: InvitePreview; token: string }) {
  const auth = useAuthState();
  const router = useRouter();
  const [email, setEmail] = useState(p.email);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function join(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const subject = `dev:${email.trim().toLowerCase()}`;
    try {
      const res = await fetch("/api/onboarding/accept", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Tangible-User": subject, "X-Tangible-Email": email.trim() },
        body: JSON.stringify({ token }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${res.status})`);
      auth.devSignIn?.(subject, body.tenant_id, email.trim());
      router.push("/onboarding");
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <Card className="p-6">
      <h2 className="text-[18px] font-semibold tracking-tight">Create your account</h2>
      <p className="mt-1 text-[13.5px] text-muted">You&apos;ll use it to connect accounts and check what you&apos;ve shared.</p>
      <form onSubmit={join} className="mt-5 space-y-4">
        <Field label="Work email" htmlFor="email">
          <Input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        {error && <Notice tone="hold">{error}</Notice>}
        <Button variant="primary" size="lg" className="w-full" disabled={busy}>
          Create account and start <ArrowRight />
        </Button>
      </form>
      <p className="mt-4 text-[11.5px] leading-relaxed text-faint">
        Development mode: no password is set. With Clerk enabled, this is a real sign-up.
      </p>
    </Card>
  );
}
