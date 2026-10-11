"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Check, Eye, Lock, Mail, Plus, Send, Sparkles, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useDeferredValue, useMemo, useState } from "react";

import { CopyLink } from "@/components/app/copy-link";
import { GROUP_ICONS, KIND_ICONS, KIND_LABELS } from "@/components/app/group-icons";
import { Page, PageHeader } from "@/components/app/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Notice, Skeleton } from "@/components/ui/misc";
import { Switch } from "@/components/ui/switch";
import { ApiError, useApi } from "@/lib/api";
import { useMembership } from "@/lib/session";
import type { Catalog, CatalogItem } from "@/lib/types";
import { cn } from "@/lib/utils";

type Custom = { label: string; kind: "upload" | "field" };
type Sent = { id: string; link: string; borrower_id: string };

export default function NewInvitePage() {
  const api = useApi();
  const router = useRouter();
  const { membership } = useMembership();
  const catalog = useQuery({ queryKey: ["catalog"], queryFn: () => api<Catalog>("/api/invites/catalog") });

  const [email, setEmail] = useState("");
  const [business, setBusiness] = useState("");
  const [contact, setContact] = useState("");
  const [amount, setAmount] = useState("");
  const [message, setMessage] = useState("");
  const [picked, setPicked] = useState<Set<string> | null>(null);
  const [custom, setCustom] = useState<Custom[]>([]);
  const [sent, setSent] = useState<Sent | null>(null);
  const [error, setError] = useState<string | null>(null);

  const chosen = useMemo(() => {
    if (picked) return picked;
    return new Set((catalog.data?.items ?? []).filter((i) => i.locked || i.default_on).map((i) => i.id));
  }, [picked, catalog.data]);

  function toggle(id: string, on: boolean) {
    const next = new Set(chosen);
    if (on) next.add(id);
    else next.delete(id);
    setPicked(next);
  }
  function toggleGroup(items: CatalogItem[], on: boolean) {
    const next = new Set(chosen);
    for (const i of items) {
      if (i.locked) continue;
      if (on) next.add(i.id);
      else next.delete(i.id);
    }
    setPicked(next);
  }

  const body = {
    email,
    business_name: business,
    contact_name: contact || null,
    loan_amount: amount.replace(/[$,\s]/g, "") || null,
    message: message || null,
    items: [...chosen],
    custom: custom.filter((c) => c.label.trim()),
  };
  const deferred = useDeferredValue(JSON.stringify(body));
  const preview = useQuery({
    queryKey: ["invite-preview", deferred],
    queryFn: () => api<{ subject: string; body_html: string }>("/api/invites/preview", { json: JSON.parse(deferred) }),
    enabled: !!catalog.data && membership?.role === "credit_admin",
    placeholderData: (prev) => prev,
    retry: false,
  });

  const send = useMutation({
    mutationFn: () => api<Sent>("/api/invites", { json: body }),
    onSuccess: (r) => {
      setSent(r);
      setError(null);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Could not send the invite"),
  });

  if (membership && membership.role !== "credit_admin") {
    return (
      <Page>
        <Notice tone="review">Only a credit admin can invite businesses.</Notice>
      </Page>
    );
  }

  const optional = (catalog.data?.items ?? []).filter((i) => !i.locked && chosen.has(i.id)).length;

  return (
    <Page wide>
      <PageHeader
        crumbs={[{ href: "/app/dashboard", label: "Dashboard" }]}
        title="Invite a business"
        description="Choose what you want beyond standard financials. The business gets one secure link to connect and upload everything."
      />
      <form
        className="grid grid-cols-[minmax(0,1fr)] gap-6 xl:grid-cols-[minmax(0,1fr)_460px]"
        onSubmit={(e) => {
          e.preventDefault();
          send.mutate();
        }}
      >
        <div className="min-w-0 space-y-6">
          <Card>
            <CardHeader title="Who are you inviting?" />
            <CardBody className="grid gap-4 sm:grid-cols-2">
              <Field label="Business name" htmlFor="business">
                <Input id="business" required value={business} onChange={(e) => setBusiness(e.target.value)} placeholder="Acme Hardware" autoComplete="off" />
              </Field>
              <Field label="Contact email" htmlFor="email">
                <Input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="owner@acme.com" autoComplete="off" />
              </Field>
              <Field label="Contact name" hint="optional" htmlFor="contact">
                <Input id="contact" value={contact} onChange={(e) => setContact(e.target.value)} placeholder="Rosa Martinez" autoComplete="off" />
              </Field>
              <Field label="Loan amount" hint="optional" htmlFor="amount">
                <Input id="amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="$75,000.00" />
              </Field>
              <Field label="Personal note" hint="optional, shown in the email" htmlFor="message" className="sm:col-span-2">
                <Textarea id="message" rows={3} value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Thanks for considering us. This replaces the usual statement chase." />
              </Field>
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="What do you want to see?"
              description={`Standard financials are always requested. ${optional} additional ${optional === 1 ? "item" : "items"} selected.`}
              action={catalog.data && <Badge tone="outline">catalog {catalog.data.version}</Badge>}
            />
            <CardBody className="space-y-3">
              {catalog.isLoading && <Skeleton className="h-64" />}
              {catalog.data?.groups.map((g) => {
                const items = catalog.data.items.filter((i) => i.group === g.id);
                return <GroupCard key={g.id} group={g} items={items} chosen={chosen} onToggle={toggle} onToggleGroup={toggleGroup} />;
              })}
              <CustomRequests custom={custom} setCustom={setCustom} max={catalog.data?.max_custom ?? 5} />
            </CardBody>
          </Card>
        </div>

        <div className="space-y-4 xl:sticky xl:top-6 xl:self-start">
          <Card className="overflow-hidden">
            <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-3">
              <span className="flex items-center gap-2 text-[13px] font-medium">
                <Eye className="size-4 text-faint" /> Email preview
              </span>
              {preview.isFetching && <span className="text-[12px] text-faint">Updating…</span>}
            </div>
            <div className="border-b border-line bg-surface-2 px-5 py-2.5 text-[12.5px]">
              <p className="truncate">
                <span className="text-faint">To </span>
                {email || "owner@business.com"}
              </p>
              <p className="truncate font-medium">{preview.data?.subject ?? "…"}</p>
            </div>
            <div className="h-[520px] bg-[#f4f4f2]">
              {preview.data ? (
                <iframe title="Email preview" sandbox="allow-same-origin" srcDoc={preview.data.body_html} className="h-full w-full" />
              ) : (
                <div className="p-6">
                  <Skeleton className="h-full min-h-80" />
                </div>
              )}
            </div>
          </Card>
          {error && <Notice tone="hold">{error}</Notice>}
          <Button type="submit" variant="primary" size="lg" className="w-full" disabled={send.isPending || !email || !business}>
            <Send /> {send.isPending ? "Sending…" : "Send invite"}
          </Button>
          <p className="flex items-start gap-2 text-[12px] leading-relaxed text-faint">
            <Mail className="mt-0.5 size-3.5 shrink-0" />
            Non-production: the email lands in the Outbox, where you can copy the link. Links expire after 14 days.
          </p>
        </div>
      </form>

      <Dialog open={!!sent} onOpenChange={(o) => !o && sent && router.push(`/app/invites/${sent.id}`)}>
        {sent && (
          <DialogContent title="Invite sent" description={`${business} will find this link in their email.`}>
            <CopyLink link={sent.link} />
            <div className="mt-5 flex justify-end gap-2">
              <Button asChild>
                <Link href="/app/outbox">View in outbox</Link>
              </Button>
              <Button variant="primary" asChild>
                <Link href={`/app/invites/${sent.id}`}>Track progress</Link>
              </Button>
            </div>
          </DialogContent>
        )}
      </Dialog>
    </Page>
  );
}

function GroupCard({
  group,
  items,
  chosen,
  onToggle,
  onToggleGroup,
}: {
  group: { id: string; label: string; blurb: string };
  items: CatalogItem[];
  chosen: Set<string>;
  onToggle: (id: string, on: boolean) => void;
  onToggleGroup: (items: CatalogItem[], on: boolean) => void;
}) {
  const Icon = GROUP_ICONS[group.id] ?? Sparkles;
  // A group with any always-requested item (standard financials, with optional payroll) has no
  // group switch: an "off" switch beside "always requested" reads as a contradiction.
  const locked = items.some((i) => i.locked);
  const optional = items.filter((i) => !i.locked);
  const allOn = optional.length > 0 && optional.every((i) => chosen.has(i.id));
  const someOn = items.some((i) => chosen.has(i.id));
  const intangible = items.some((i) => i.feeds === "intangibles");
  return (
    <div className={cn("rounded-xl border transition-colors", someOn ? "border-line-strong bg-surface" : "border-line bg-bg")}>
      <div className="flex items-center gap-3 px-4 py-3">
        <span className={cn("grid size-9 shrink-0 place-items-center rounded-lg", someOn ? "bg-brand-soft text-brand-ink" : "bg-surface-2 text-faint")}>
          <Icon className="size-[18px]" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-2 text-[14px] font-medium">
            {group.label}
            {locked && (
              <Badge tone="neutral">
                <Lock /> Always included
              </Badge>
            )}
            {intangible && <Badge tone="info">Intangibles</Badge>}
          </p>
          <p className="text-[12.5px] text-muted">{group.blurb}</p>
        </div>
        {!locked && (
          <Switch checked={allOn} onCheckedChange={(on) => onToggleGroup(items, on)} aria-label={`Request all of ${group.label}`} />
        )}
      </div>
      <ul className="divide-y divide-line border-t border-line">
        {items.map((i) => {
          const KindIcon = KIND_ICONS[i.kind];
          const on = chosen.has(i.id);
          return (
            <li key={i.id}>
              <label className={cn("flex items-center gap-3 px-4 py-2.5", !i.locked && "cursor-pointer hover:bg-surface-2/60")}>
                <span className="min-w-0 flex-1">
                  <span className={cn("block text-[13.5px]", on ? "text-ink" : "text-muted")}>{i.label}</span>
                  <span className="block text-[12px] text-faint">{i.help}</span>
                </span>
                <span className="hidden items-center gap-1 text-[11.5px] text-faint sm:flex">
                  <KindIcon className="size-3.5" />
                  {KIND_LABELS[i.kind]}
                </span>
                {i.locked ? (
                  <Check className="size-4 text-brand" aria-label="Always requested" />
                ) : (
                  <Switch checked={on} onCheckedChange={(v) => onToggle(i.id, v)} aria-label={i.label} />
                )}
              </label>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function CustomRequests({ custom, setCustom, max }: { custom: Custom[]; setCustom: (c: Custom[]) => void; max: number }) {
  return (
    <div className="rounded-xl border border-dashed border-line-strong p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-[14px] font-medium">Anything else?</p>
          <p className="text-[12.5px] text-muted">Ask for a specific document or answer, up to {max}.</p>
        </div>
        <Button
          type="button"
          size="sm"
          onClick={() => setCustom([...custom, { label: "", kind: "upload" }])}
          disabled={custom.length >= max}
        >
          <Plus /> Add request
        </Button>
      </div>
      {custom.length > 0 && (
        <ul className="mt-3 space-y-2">
          {custom.map((c, n) => (
            <li key={n} className="flex gap-2">
              <Input
                value={c.label}
                maxLength={120}
                onChange={(e) => setCustom(custom.map((x, i) => (i === n ? { ...x, label: e.target.value } : x)))}
                placeholder="e.g. Current lease agreement"
                aria-label={`Custom request ${n + 1}`}
              />
              <Select
                value={c.kind}
                className="w-32"
                onChange={(e) => setCustom(custom.map((x, i) => (i === n ? { ...x, kind: e.target.value as Custom["kind"] } : x)))}
                aria-label="How they answer"
              >
                <option value="upload">Upload</option>
                <option value="field">Answer</option>
              </Select>
              <Button type="button" variant="ghost" size="icon" onClick={() => setCustom(custom.filter((_, i) => i !== n))} aria-label="Remove">
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
