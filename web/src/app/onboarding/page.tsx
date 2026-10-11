"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Building2,
  Check,
  CheckCircle2,
  CircleDashed,
  FileUp,
  Link2,
  Lock,
  LogOut,
  PartyPopper,
  Plug,
  Plus,
  Send,
  ShieldCheck,
  Trash2,
  Upload,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { GROUP_ICONS, ITEM_ICONS } from "@/components/app/group-icons";
import { Logo } from "@/components/logo";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Textarea } from "@/components/ui/input";
import { Notice, Progress, Skeleton } from "@/components/ui/misc";
import { ApiError, useApi } from "@/lib/api";
import { useAuthState } from "@/lib/auth-context";
import { providerLabel } from "@/lib/labels";
import { homeFor, useMembership } from "@/lib/session";
import type { CoverageItem, OnboardingState, Profile } from "@/lib/types";
import { cn, date, money } from "@/lib/utils";

type Step = "profile" | "connect" | "documents" | "review";
const STEPS: { id: Step; label: string; icon: typeof Building2 }[] = [
  { id: "profile", label: "Your business", icon: Building2 },
  { id: "connect", label: "Connect accounts", icon: Plug },
  { id: "documents", label: "Documents & links", icon: FileUp },
  { id: "review", label: "Review & submit", icon: Send },
];

export default function OnboardingPage() {
  const auth = useAuthState();
  const router = useRouter();
  const { membership, loading, error } = useMembership();

  useEffect(() => {
    if (auth.ready && !auth.signedIn) router.replace("/sign-in?next=/onboarding");
  }, [auth.ready, auth.signedIn, router]);
  useEffect(() => {
    if (membership && membership.role !== "borrower") router.replace(homeFor(membership.role));
  }, [membership, router]);

  if (!auth.ready || !auth.signedIn || loading || (membership && membership.role !== "borrower")) {
    return <Frame>{<Skeleton className="h-[480px] rounded-2xl" />}</Frame>;
  }
  if (error || !membership) {
    return (
      <Frame>
        <Card className="mx-auto max-w-lg p-8 text-center">
          <h1 className="text-[20px] font-semibold">No invite found for this account</h1>
          <p className="mt-2 text-[14px] text-muted">Open the link in your lender&apos;s email to get started.</p>
        </Card>
      </Frame>
    );
  }
  return (
    <Frame lender={membership.tenant_name}>
      <Wizard />
    </Frame>
  );
}

function Frame({ children, lender }: { children: React.ReactNode; lender?: string }) {
  const auth = useAuthState();
  const router = useRouter();
  const qc = useQueryClient();
  return (
    <div className="min-h-dvh bg-bg">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-4 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <Link href="/">
              <Logo />
            </Link>
            {lender && (
              <span className="hidden truncate border-l border-line pl-3 text-[13px] text-muted sm:block">
                for {lender}
              </span>
            )}
          </div>
          {auth.signedIn && (
            <Button
              variant="ghost"
              size="sm"
              onClick={async () => {
                qc.clear();
                await auth.signOut();
                router.replace("/");
              }}
            >
              <LogOut /> Sign out
            </Button>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">{children}</main>
    </div>
  );
}

function useOnboarding() {
  const api = useApi();
  return useQuery({ queryKey: ["onboarding"], queryFn: () => api<OnboardingState>("/api/onboarding/state") });
}

function useAct<T = OnboardingState>() {
  const qc = useQueryClient();
  return {
    onSuccess: (s: T) => qc.setQueryData(["onboarding"], s),
    onError: (e: unknown) => toast.error(e instanceof ApiError ? e.message : "Something went wrong"),
  };
}

function Wizard() {
  const q = useOnboarding();
  const [step, setStep] = useState<Step | null>(null);

  if (q.error) return <Notice tone="hold">{(q.error as Error).message}</Notice>;
  if (!q.data) return <Skeleton className="h-[480px] rounded-2xl" />;
  const s = q.data;
  if (s.submitted_at) return <Submitted s={s} />;

  const connectors = s.items.filter((i) => i.kind === "connector");
  const docs = s.items.filter((i) => i.kind === "upload" || i.kind === "link" || (i.kind === "field" && i.group === "custom"));
  const profileDone = !!s.profile?.legal_name;
  const doneBy: Record<Step, boolean> = {
    profile: profileDone,
    connect: connectors.length > 0 && connectors.every((i) => i.done),
    documents: docs.length > 0 && docs.every((i) => i.done),
    review: false,
  };
  const current: Step = step ?? (!profileDone ? "profile" : !doneBy.connect ? "connect" : "documents");
  const idx = STEPS.findIndex((x) => x.id === current);
  const answered = s.items.filter((i) => i.done).length;
  const go = (n: number) => setStep(STEPS[Math.max(0, Math.min(STEPS.length - 1, n))].id);

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-8 lg:grid-cols-[260px_minmax(0,1fr)]">
      <aside className="lg:sticky lg:top-8 lg:self-start">
        <p className="text-[13px] text-muted">{s.lender_name} · {s.loan_amount ? money(s.loan_amount) : "loan application"}</p>
        <h1 className="mt-1 text-[22px] leading-tight font-semibold tracking-tight">{s.business_name}</h1>
        <div className="mt-4">
          <div className="mb-1.5 flex justify-between text-[12px] text-muted">
            <span>Progress</span>
            <span className="tabular">
              {answered}/{s.items.length}
            </span>
          </div>
          <Progress value={(answered / Math.max(1, s.items.length)) * 100} />
        </div>
        <ol className="mt-6 flex gap-1 overflow-x-auto lg:flex-col">
          {STEPS.map((st, n) => (
            <li key={st.id} className="shrink-0">
              <button
                onClick={() => setStep(st.id)}
                aria-current={current === st.id ? "step" : undefined}
                className={cn(
                  "flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-[13.5px] transition-colors",
                  current === st.id ? "bg-surface font-medium text-ink shadow-card" : "text-muted hover:bg-surface/60",
                )}
              >
                <span
                  className={cn(
                    "grid size-6 shrink-0 place-items-center rounded-full text-[11px] font-semibold",
                    doneBy[st.id] ? "bg-brand text-brand-fg" : current === st.id ? "bg-brand-soft text-brand-ink" : "bg-surface-2 text-faint",
                  )}
                >
                  {doneBy[st.id] ? <Check className="size-3.5" /> : n + 1}
                </span>
                <span className="whitespace-nowrap">{st.label}</span>
              </button>
            </li>
          ))}
        </ol>
        <p className="mt-6 hidden items-start gap-2 text-[12px] leading-relaxed text-faint lg:flex">
          <ShieldCheck className="mt-0.5 size-4 shrink-0 text-brand" />
          Connections are read-only. You can see everything you&apos;ve shared here, and your progress saves as you go.
        </p>
      </aside>

      <section className="min-w-0">
        {s.message && current === "profile" && (
          <Notice tone="neutral" className="mb-5">
            <strong>{s.lender_name}:</strong> {s.message}
          </Notice>
        )}
        {current === "profile" && (
          <ProfileStep
            profile={s.profile}
            askImports={s.items.some((i) => i.id === "import_mix")}
            onDone={() => go(1)}
          />
        )}
        {current === "connect" && <ConnectStep items={connectors} />}
        {current === "documents" && <DocumentsStep items={docs} />}
        {current === "review" && <ReviewStep s={s} goto={setStep} />}
        {current !== "profile" && (
          <div className="mt-6 flex justify-between">
            <Button onClick={() => go(idx - 1)}>
              <ArrowLeft /> Back
            </Button>
            {current !== "review" && (
              <Button variant="primary" onClick={() => go(idx + 1)}>
                Continue <ArrowRight />
              </Button>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------- step 1: profile

type ImportRow = { hs_chapter: string; country: string; share: string };

function ProfileStep({
  profile,
  askImports,
  onDone,
}: {
  profile: Profile | null;
  askImports: boolean;
  onDone: () => void;
}) {
  const api = useApi();
  const act = useAct();
  const [f, setF] = useState({
    legal_name: profile?.legal_name ?? "",
    dba: profile?.dba ?? "",
    state: profile?.state ?? "",
    year_founded: profile?.year_founded?.toString() ?? "",
    website: profile?.website ?? "",
    phone: profile?.phone ?? "",
    address: profile?.address ?? "",
    naics: profile?.naics ?? "",
    stated_revenue_90d: profile?.stated_revenue_90d ?? "",
  });
  const [noImports, setNoImports] = useState(profile?.import_mix?.length === 0);
  const [imports, setImports] = useState<ImportRow[]>(
    (profile?.import_mix ?? []).map((x) => ({
      hs_chapter: x.hs_chapter,
      country: x.country,
      share: String(Math.round(Number(x.share_of_cogs) * 10000) / 100),
    })),
  );
  const importMix = !askImports
    ? (profile?.import_mix ?? null)
    : noImports
      ? []
      : imports.filter((r) => r.hs_chapter || r.country || r.share).length
        ? imports
            .filter((r) => r.hs_chapter || r.country || r.share)
            .map((r) => ({ hs_chapter: r.hs_chapter, country: r.country, share_of_cogs: Number(r.share) / 100 }))
        : null;
  const save = useMutation({
    mutationFn: () =>
      api<OnboardingState>("/api/onboarding/profile", {
        json: {
          ...f,
          stated_revenue_90d: f.stated_revenue_90d.replace(/[$,\s]/g, "") || null,
          import_mix: importMix,
        },
      }),
    onSuccess: (s) => {
      act.onSuccess(s);
      toast.success("Saved");
      onDone();
    },
    onError: act.onError,
  });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });

  return (
    <Card>
      <CardHeader title="Tell us about your business" description="As it appears on your registration. You can change this later." />
      <CardBody>
        <form
          className="grid gap-4 sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <Field label="Legal name" htmlFor="legal_name" className="sm:col-span-2">
            <Input id="legal_name" required value={f.legal_name} onChange={set("legal_name")} placeholder="Acme Hardware LLC" />
          </Field>
          <Field label="Trading as (DBA)" hint="optional" htmlFor="dba">
            <Input id="dba" value={f.dba} onChange={set("dba")} placeholder="Acme Hardware" />
          </Field>
          <Field label="State of registration" htmlFor="state">
            <Input id="state" value={f.state} onChange={set("state")} maxLength={2} placeholder="OR" className="uppercase" />
          </Field>
          <Field label="Year founded" htmlFor="year">
            <Input id="year" inputMode="numeric" value={f.year_founded} onChange={set("year_founded")} placeholder="2009" />
          </Field>
          <Field label="Website" hint="optional" htmlFor="website">
            <Input id="website" value={f.website} onChange={set("website")} placeholder="acmehardware.com" />
          </Field>
          <Field label="Business phone" hint="optional" htmlFor="phone">
            <Input id="phone" value={f.phone} onChange={set("phone")} placeholder="(503) 555-0142" />
          </Field>
          <Field label="NAICS code" hint="optional" htmlFor="naics">
            <Input id="naics" value={f.naics} onChange={set("naics")} maxLength={6} placeholder="444130" />
          </Field>
          <Field label="Business address" hint="optional" htmlFor="address" className="sm:col-span-2">
            <Input id="address" value={f.address} onChange={set("address")} placeholder="212 Main St, Portland" />
          </Field>
          <Field label="Revenue, last 90 days" hint="your estimate" htmlFor="rev">
            <Input id="rev" inputMode="decimal" value={f.stated_revenue_90d} onChange={set("stated_revenue_90d")} placeholder="$120,000.00" />
          </Field>
          {askImports && (
            <ImportsEditor rows={imports} setRows={setImports} none={noImports} setNone={setNoImports} />
          )}
          <div className="flex items-end justify-end sm:col-span-2">
            <Button variant="primary" disabled={save.isPending || !f.legal_name}>
              Save and continue <ArrowRight />
            </Button>
          </div>
        </form>
      </CardBody>
    </Card>
  );
}

function ImportsEditor({
  rows,
  setRows,
  none,
  setNone,
}: {
  rows: ImportRow[];
  setRows: (r: ImportRow[]) => void;
  none: boolean;
  setNone: (v: boolean) => void;
}) {
  const set = (n: number, k: keyof ImportRow, v: string) => setRows(rows.map((r, i) => (i === n ? { ...r, [k]: v } : r)));
  return (
    <div className="rounded-xl border border-line p-4 sm:col-span-2">
      <p className="text-[14px] font-medium">Imported goods</p>
      <p className="text-[12.5px] text-muted">
        Your lender asked what you import, so known tariff actions can be stress-tested. Use the two-digit HS chapter
        (for example 94 for furniture) and the country the goods come from.
      </p>
      <label className="mt-3 flex items-center gap-2 text-[13px]">
        <input type="checkbox" checked={none} onChange={(e) => setNone(e.target.checked)} className="accent-[var(--brand)]" />
        We don&apos;t import goods
      </label>
      {!none && (
        <div className="mt-3 space-y-2">
          {rows.map((r, n) => (
            <div key={n} className="grid grid-cols-[1fr_1fr_1fr_auto] gap-2">
              <Input value={r.hs_chapter} onChange={(e) => set(n, "hs_chapter", e.target.value)} placeholder="HS chapter, e.g. 94" maxLength={2} aria-label="HS chapter" />
              <Input value={r.country} onChange={(e) => set(n, "country", e.target.value.toUpperCase())} placeholder="Country, e.g. CN" maxLength={2} aria-label="Country" />
              <Input value={r.share} onChange={(e) => set(n, "share", e.target.value)} placeholder="% of cost of goods" inputMode="decimal" aria-label="Share of cost of goods (%)" />
              <Button type="button" variant="ghost" size="icon" onClick={() => setRows(rows.filter((_, i) => i !== n))} aria-label="Remove">
                <Trash2 />
              </Button>
            </div>
          ))}
          <Button type="button" size="sm" onClick={() => setRows([...rows, { hs_chapter: "", country: "", share: "" }])} disabled={rows.length >= 20}>
            <Plus /> Add a line
          </Button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- step 2: connect

function ConnectStep({ items }: { items: CoverageItem[] }) {
  if (!items.length) return <Notice tone="neutral">Nothing to connect for this application.</Notice>;
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-[20px] font-semibold tracking-tight">Connect your accounts</h2>
        <p className="mt-1 text-[14px] text-muted">Read-only. Pick the system you use for each; you can add more than one.</p>
      </div>
      {items.map((i) => (
        <ConnectorCard key={i.id} item={i} />
      ))}
    </div>
  );
}

const SCOPES: Record<string, string[]> = {
  bank: ["Transactions", "Balances"],
  accounting: ["Invoices", "Bills", "General ledger"],
  sales: ["Orders", "Refunds", "Payouts"],
  inventory_system: ["Inventory levels and cost"],
  customers: ["Orders", "Customer ids (no contact details)"],
};

function ConnectorCard({ item }: { item: CoverageItem }) {
  const [provider, setProvider] = useState<string | null>(null);
  const connected = item.responses.filter((r) => r.kind === "source_connected").map((r) => String(r.detail.provider));
  const Icon = ITEM_ICONS[item.id] ?? GROUP_ICONS[item.group] ?? Plug;
  return (
    <Card className={cn(item.done && "border-ok/40")}>
      <div className="flex items-start gap-4 p-5">
        <span className={cn("grid size-10 shrink-0 place-items-center rounded-xl", item.done ? "bg-ok-soft text-ok" : "bg-brand-soft text-brand-ink")}>
          {item.done ? <CheckCircle2 className="size-5" /> : <Icon className="size-5" />}
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-2 text-[15px] font-medium">
            {item.label}
            {item.locked && <Badge tone="neutral">Standard</Badge>}
            {connected.length > 0 && <Badge tone="ok">{connected.length} connected</Badge>}
          </p>
          <p className="text-[13px] text-muted">{item.help}</p>
          <div className="mt-4 flex flex-wrap gap-2">
            {item.providers.map((p) => (
              <Button
                key={p}
                size="sm"
                variant={connected.includes(p) ? "soft" : "secondary"}
                onClick={() => setProvider(p)}
              >
                {connected.includes(p) ? <Check /> : <Plug />} {providerLabel(p)}
              </Button>
            ))}
          </div>
        </div>
      </div>
      <ConsentDialog item={item} provider={provider} onClose={() => setProvider(null)} />
    </Card>
  );
}

function ConsentDialog({ item, provider, onClose }: { item: CoverageItem; provider: string | null; onClose: () => void }) {
  const api = useApi();
  const act = useAct();
  const [stage, setStage] = useState<"consent" | "connecting">("consent");
  const connect = useMutation({
    mutationFn: () => api<OnboardingState>(`/api/onboarding/items/${item.id}/connect`, { json: { provider } }),
    onSuccess: (s) => {
      act.onSuccess(s);
      toast.success(`${providerLabel(provider!)} connected`);
      setStage("consent");
      onClose();
    },
    onError: (e) => {
      setStage("consent");
      act.onError(e);
    },
  });
  return (
    <Dialog open={!!provider} onOpenChange={(o) => !o && onClose()}>
      {provider && (
        <DialogContent title={`Connect ${providerLabel(provider)}`} description="Tangible will get read-only access to:">
          <ul className="space-y-2">
            {(SCOPES[item.id] ?? ["Read-only data"]).map((s) => (
              <li key={s} className="flex items-center gap-2.5 text-[14px]">
                <Check className="size-4 text-brand" /> {s}
              </li>
            ))}
          </ul>
          <div className="mt-4 flex items-start gap-2 rounded-lg bg-surface-2 p-3 text-[12.5px] text-muted">
            <Lock className="mt-0.5 size-3.5 shrink-0" />
            Tangible can&apos;t move money, change records or see passwords. You can disconnect at any time.
          </div>
          <Notice tone="review" className="mt-3">
            Simulated in this build: your consent is recorded but no data is pulled.
          </Notice>
          <div className="mt-5 flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button
              variant="primary"
              disabled={connect.isPending}
              onClick={() => {
                setStage("connecting");
                setTimeout(() => connect.mutate(), 700);
              }}
            >
              {stage === "connecting" ? (
                <>
                  <CircleDashed className="animate-spin" /> Connecting…
                </>
              ) : (
                <>Allow read-only access</>
              )}
            </Button>
          </div>
        </DialogContent>
      )}
    </Dialog>
  );
}

// ---------------------------------------------------------------- step 3: documents & links

function DocumentsStep({ items }: { items: CoverageItem[] }) {
  if (!items.length) return <Notice tone="neutral">No documents or links were requested.</Notice>;
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-[20px] font-semibold tracking-tight">Documents & links</h2>
        <p className="mt-1 text-[14px] text-muted">Upload what can&apos;t be connected and point us to where your business lives online.</p>
      </div>
      {items.map((i) =>
        i.kind === "upload" ? <UploadCard key={i.id} item={i} /> : <AnswerCard key={i.id} item={i} />,
      )}
    </div>
  );
}

function ItemShell({ item, icon: Icon, children }: { item: CoverageItem; icon: typeof Upload; children: React.ReactNode }) {
  return (
    <Card className={cn(item.done && "border-ok/40")}>
      <div className="flex items-start gap-4 p-5">
        <span className={cn("grid size-10 shrink-0 place-items-center rounded-xl", item.done ? "bg-ok-soft text-ok" : "bg-surface-2 text-muted")}>
          {item.done ? <CheckCircle2 className="size-5" /> : <Icon className="size-5" />}
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-2 text-[15px] font-medium">
            {item.label}
            {item.group === "custom" && <Badge tone="brand">Asked by your lender</Badge>}
            {item.locked && <Badge tone="neutral">Fallback for the bank</Badge>}
          </p>
          <p className="text-[13px] text-muted">{item.help}</p>
          {children}
        </div>
      </div>
    </Card>
  );
}

function UploadCard({ item }: { item: CoverageItem }) {
  const api = useApi();
  const act = useAct();
  const ref = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const up = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api<OnboardingState>(`/api/onboarding/items/${item.id}/documents`, { form });
    },
    onSuccess: (s) => {
      act.onSuccess(s);
      toast.success("Uploaded");
    },
    onError: act.onError,
  });
  const files = item.responses.filter((r) => r.kind === "document_added");
  return (
    <ItemShell item={item} icon={Upload}>
      {files.length > 0 && (
        <ul className="mt-2 space-y-1">
          {files.map((f, n) => (
            <li key={n} className="flex items-center gap-2 text-[13px]">
              <Check className="size-3.5 text-ok" /> {String(f.detail.filename)}
              <span className="text-[11.5px] text-faint">{Math.ceil(Number(f.detail.bytes) / 1024)} KB</span>
            </li>
          ))}
        </ul>
      )}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          const f = e.dataTransfer.files?.[0];
          if (f) up.mutate(f);
        }}
        className={cn(
          "mt-3 flex flex-col items-center justify-center rounded-xl border border-dashed px-4 py-5 text-center transition-colors",
          drag ? "border-brand bg-brand-soft/50" : "border-line-strong",
        )}
      >
        <p className="text-[13px] text-muted">
          {up.isPending ? "Uploading…" : "Drop a file here, or"}{" "}
          {!up.isPending && (
            <button type="button" onClick={() => ref.current?.click()} className="font-medium text-brand hover:underline">
              browse
            </button>
          )}
        </p>
        <p className="mt-0.5 text-[11.5px] text-faint">PDF, CSV or spreadsheet, up to 25 MB</p>
        <input
          ref={ref}
          type="file"
          className="sr-only"
          aria-label={`Upload ${item.label}`}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) up.mutate(f);
            e.target.value = "";
          }}
        />
      </div>
    </ItemShell>
  );
}

function AnswerCard({ item }: { item: CoverageItem }) {
  const api = useApi();
  const act = useAct();
  const [value, setValue] = useState("");
  const add = useMutation({
    mutationFn: () => api<OnboardingState>(`/api/onboarding/items/${item.id}/answer`, { json: { value } }),
    onSuccess: (s) => {
      act.onSuccess(s);
      setValue("");
      toast.success("Added");
    },
    onError: act.onError,
  });
  const link = item.kind === "link";
  const given = item.responses.map((r) => String(r.detail.url ?? r.detail.text ?? ""));
  if (item.done && given.length === 0) given.push("From your business profile");
  return (
    <ItemShell item={item} icon={link ? Link2 : FileUp}>
      {given.length > 0 && (
        <ul className="mt-2 space-y-1">
          {given.map((g, n) => (
            <li key={n} className="flex items-center gap-2 truncate text-[13px]">
              <Check className="size-3.5 shrink-0 text-ok" /> <span className="truncate">{g}</span>
            </li>
          ))}
        </ul>
      )}
      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) add.mutate();
        }}
      >
        {link ? (
          <Input value={value} onChange={(e) => setValue(e.target.value)} placeholder="https://" aria-label={item.label} />
        ) : (
          <Textarea value={value} onChange={(e) => setValue(e.target.value)} rows={2} aria-label={item.label} />
        )}
        <Button disabled={add.isPending || !value.trim()} className="shrink-0 self-start">
          Add
        </Button>
      </form>
    </ItemShell>
  );
}

// ---------------------------------------------------------------- step 4: review & submit

function ReviewStep({ s, goto }: { s: OnboardingState; goto: (s: Step) => void }) {
  const api = useApi();
  const act = useAct();
  const submit = useMutation({
    mutationFn: () => api<OnboardingState>("/api/onboarding/submit", { method: "POST" }),
    onSuccess: act.onSuccess,
    onError: act.onError,
  });
  const missing = s.items.filter((i) => !i.done);
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-[20px] font-semibold tracking-tight">Review & submit</h2>
        <p className="mt-1 text-[14px] text-muted">
          After you submit, {s.lender_name} starts its review. You can&apos;t add more without a new invite.
        </p>
      </div>
      <Card>
        <CardHeader title="Business" action={<Button size="sm" variant="ghost" onClick={() => goto("profile")}>Edit</Button>} />
        <CardBody className="text-[14px]">
          {s.profile ? (
            <p>
              <span className="font-medium">{s.profile.legal_name}</span>
              {s.profile.state && ` · ${s.profile.state}`}
              {s.profile.year_founded && ` · since ${s.profile.year_founded}`}
            </p>
          ) : (
            <p className="text-hold">Not provided</p>
          )}
        </CardBody>
      </Card>
      <Card>
        <CardHeader title="Everything requested" description={`${s.items.length - missing.length} of ${s.items.length} provided`} />
        <ul className="divide-y divide-line border-t border-line">
          {s.items.map((i) => (
            <li key={i.id} className="flex items-center gap-3 px-5 py-2.5 text-[13.5px]">
              {i.done ? <CheckCircle2 className="size-4 text-ok" /> : <CircleDashed className="size-4 text-faint" />}
              <span className={cn("flex-1", !i.done && "text-muted")}>{i.label}</span>
              {!i.done && (
                <button
                  className="text-[12.5px] font-medium text-brand hover:underline"
                  onClick={() => goto(i.kind === "connector" ? "connect" : i.kind === "field" && i.group !== "custom" ? "profile" : "documents")}
                >
                  Add
                </button>
              )}
            </li>
          ))}
        </ul>
      </Card>
      {s.missing_for_submit.length > 0 ? (
        <Notice tone="review">
          <strong>Still needed before you can submit:</strong> {s.missing_for_submit.join(", ")}.
        </Notice>
      ) : (
        missing.length > 0 && (
          <Notice tone="neutral">
            You can submit now. {missing.length} optional {missing.length === 1 ? "item is" : "items are"} still open;{" "}
            {s.lender_name} will see what was provided.
          </Notice>
        )
      )}
      <div className="flex justify-end">
        <Button variant="primary" size="lg" disabled={!s.can_submit || submit.isPending} onClick={() => submit.mutate()}>
          <Send /> Submit to {s.lender_name}
        </Button>
      </div>
    </div>
  );
}

function Submitted({ s }: { s: OnboardingState }) {
  const done = s.items.filter((i) => i.done);
  return (
    <Card className="mx-auto max-w-2xl overflow-hidden">
      <div className="bg-brand-soft px-8 py-10 text-center">
        <PartyPopper className="mx-auto size-9 text-brand" />
        <h1 className="mt-4 font-display text-[34px] leading-tight">You&apos;re all set</h1>
        <p className="mt-2 text-[14.5px] text-muted">
          Submitted to {s.lender_name} on {date(s.submitted_at)}. They&apos;ll be in touch if they need anything else.
        </p>
      </div>
      <CardBody className="pt-6">
        <p className="mb-3 text-[13px] font-medium text-muted">What you shared</p>
        <ul className="grid gap-2 sm:grid-cols-2">
          {done.map((i) => (
            <li key={i.id} className="flex items-center gap-2 text-[13.5px]">
              <CheckCircle2 className="size-4 shrink-0 text-ok" /> {i.label}
            </li>
          ))}
        </ul>
      </CardBody>
    </Card>
  );
}
