"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRight,
  Ban,
  CheckCircle2,
  Circle,
  ExternalLink,
  FileText,
  Mail,
  Plug,
  RefreshCw,
  ScanSearch,
} from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { CopyLink } from "@/components/app/copy-link";
import { GROUP_ICONS } from "@/components/app/group-icons";
import { Page, PageHeader } from "@/components/app/page-header";
import { StageStepper } from "@/components/app/stage-stepper";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/input";
import { Empty, Notice, Progress, Skeleton } from "@/components/ui/misc";
import { ApiError, useApi } from "@/lib/api";
import { EVENT_LABELS, STAGE_TONE, providerLabel, stageLabel } from "@/lib/labels";
import { useMembership } from "@/lib/session";
import type { CoverageItem, InviteDetail } from "@/lib/types";
import { ago, date, money, pct } from "@/lib/utils";

const LINK = /https?:\/\/\S+\/invite\/[A-Za-z0-9_-]+/;

export default function InvitePage() {
  const { id } = useParams<{ id: string }>();
  const api = useApi();
  const { membership } = useMembership();
  const q = useQuery({
    queryKey: ["invite", id],
    queryFn: () => api<InviteDetail>(`/api/invites/${id}`),
    refetchInterval: 10_000,
  });
  const admin = membership?.role === "credit_admin";

  if (q.error) {
    return (
      <Page>
        <Notice tone="hold">{(q.error as Error).message}</Notice>
      </Page>
    );
  }
  if (!q.data) {
    return (
      <Page>
        <Skeleton className="h-10 w-72" />
        <Skeleton className="mt-6 h-96" />
      </Page>
    );
  }
  const d = q.data;
  const live = d.state === "live";
  const link = d.sent_email?.body_text?.match(LINK)?.[0] ?? null;

  return (
    <Page wide>
      <PageHeader
        crumbs={[{ href: "/app/dashboard", label: "Dashboard" }]}
        title={d.business_name}
        eyebrow={
          <span className="flex flex-wrap items-center gap-2">
            {live ? (
              <Badge tone={STAGE_TONE[d.stage]}>{stageLabel(d.stage)}</Badge>
            ) : (
              <Badge tone={d.state === "revoked" ? "hold" : "neutral"}>{d.state === "expired" ? "Expired" : d.state === "replaced" ? "Replaced by a newer invite" : "Revoked"}</Badge>
            )}
            <span>
              {d.contact_name ? `${d.contact_name} · ` : ""}
              {d.email}
            </span>
          </span>
        }
        actions={
          admin && (
            <>
              {d.stage === "submitted" && !d.case_id && <OpenCase invite={d} />}
              {d.case_id && d.case_status === "signed" && (
                <Button variant="primary" asChild>
                  <Link href={`/app/diligence/${d.case_id}`}>
                    <ScanSearch /> View signed report
                  </Link>
                </Button>
              )}
              {live && d.stage !== "in_diligence" && d.stage !== "signed" && <Resend id={d.id} />}
              {live && !d.case_id && <Revoke id={d.id} />}
            </>
          )
        }
      />

      {d.stage === "in_diligence" && (
        <Notice tone="info" icon={<ScanSearch />} className="mb-4">
          <strong>In diligence.</strong> The assigned analyst is working the case; you&apos;ll see the full report here
          once it&apos;s signed.
        </Notice>
      )}
      <Card className="mb-6 p-5">
        <StageStepper stage={d.stage} muted={!live} />
        <div className="mt-5 grid gap-4 text-[13px] sm:grid-cols-4">
          <Fact label="Loan">{money(d.loan_amount)}</Fact>
          <Fact label="Invited">{date(d.created_at)} by {d.created_by}</Fact>
          <Fact label="Link expires">{d.stage === "invited" || d.stage === "opened" ? date(d.expires_at) : "Accepted"}</Fact>
          <Fact label="Answered">
            <span className="flex items-center gap-2">
              <Progress value={d.requested ? (d.answered / d.requested) * 100 : 0} className="w-24" />
              <span className="tabular">
                {d.answered} of {d.requested}
              </span>
            </span>
          </Fact>
        </div>
      </Card>

      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="min-w-0 space-y-6">
          <Checklist items={d.items} />
          <ProfileCard d={d} />
        </div>
        <div className="space-y-6">
          {live && link && !["submitted", "in_diligence", "signed"].includes(d.stage) && (
            <Card>
              <CardHeader title="Invite link" description="Share it if the email didn't arrive." />
              <CardBody>
                <CopyLink link={link} />
              </CardBody>
            </Card>
          )}
          <Timeline d={d} />
          {d.sent_email && <EmailCard d={d} />}
        </div>
      </div>
    </Page>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-[12px] text-faint">{label}</p>
      <div className="mt-0.5 font-medium text-ink">{children}</div>
    </div>
  );
}

function responseText(item: CoverageItem): string | null {
  const r = item.responses[item.responses.length - 1];
  if (!r) return null;
  const d = r.detail as Record<string, string>;
  if (r.kind === "source_connected") return `${providerLabel(d.provider)} connected`;
  if (r.kind === "document_added") return d.filename;
  if (r.kind === "link_added") return d.url;
  if (r.kind === "answer_added") return d.text;
  return null;
}

function Checklist({ items }: { items: CoverageItem[] }) {
  const groups: { id: string; items: CoverageItem[] }[] = [];
  for (const i of items) {
    const g = groups.find((x) => x.id === i.group);
    if (g) g.items.push(i);
    else groups.push({ id: i.group, items: [i] });
  }
  const label: Record<string, string> = {
    financials: "Standard financials",
    inventory: "Inventory",
    presence: "Digital presence",
    reputation: "Reputation & loyalty",
    longevity: "Longevity & entity",
    community: "Community",
    supply: "Supply chain & tariffs",
    custom: "Your custom requests",
  };
  return (
    <Card>
      <CardHeader title="What you asked for" description="Updates as the business connects and uploads." />
      <div className="divide-y divide-line border-t border-line">
        {groups.map((g) => {
          const Icon = GROUP_ICONS[g.id] ?? FileText;
          return (
            <div key={g.id} className="px-5 py-4">
              <p className="mb-2 flex items-center gap-2 text-[12.5px] font-medium text-muted">
                <Icon className="size-4" />
                {label[g.id] ?? g.id}
              </p>
              <ul className="space-y-1.5">
                {g.items.map((i) => {
                  const text = responseText(i);
                  return (
                    <li key={i.id} className="flex items-start gap-3 text-[13.5px]">
                      {i.done ? (
                        <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" />
                      ) : (
                        <Circle className="mt-0.5 size-4 shrink-0 text-line-strong" />
                      )}
                      <span className="min-w-0 flex-1">
                        <span className={i.done ? "text-ink" : "text-muted"}>{i.label}</span>
                        {text && (
                          <span className="block truncate text-[12px] text-faint">
                            {text}
                            {i.responses.length > 1 && ` · +${i.responses.length - 1} more`}
                          </span>
                        )}
                      </span>
                      {i.feeds === "intangibles" && <Badge tone="info">intangible</Badge>}
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function ProfileCard({ d }: { d: InviteDetail }) {
  const p = d.profile;
  return (
    <Card>
      <CardHeader title="Business profile" description="As the business describes itself. Unverified until diligence." />
      {!p ? (
        <Empty title="Not provided yet" className="pt-0" />
      ) : (
        <CardBody>
          <dl className="grid gap-x-6 gap-y-3 text-[13.5px] sm:grid-cols-2">
            <Row k="Legal name" v={p.legal_name} />
            <Row k="Trading as" v={p.dba} />
            <Row k="State" v={p.state} />
            <Row k="Founded" v={p.year_founded?.toString()} />
            <Row k="Phone" v={p.phone} />
            <Row k="NAICS" v={p.naics} />
            <Row k="Address" v={p.address} />
            <Row k="Stated revenue (90 days)" v={p.stated_revenue_90d ? money(p.stated_revenue_90d) : null} />
            <Row
              k="Imports (share of cost of goods)"
              v={
                p.import_mix === null
                  ? null
                  : p.import_mix.length === 0
                    ? "None"
                    : p.import_mix
                        .map((x) => `HS ${x.hs_chapter} from ${x.country}: ${pct(x.share_of_cogs)}`)
                        .join("; ")
              }
            />
            <Row
              k="Website"
              v={
                p.website ? (
                  <a href={p.website} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex items-center gap-1 text-brand hover:underline">
                    {p.website.replace(/^https?:\/\//, "")} <ExternalLink className="size-3" />
                  </a>
                ) : null
              }
            />
          </dl>
          <p className="mt-4 text-[12px] text-faint">Updated {ago(p.observed_at)}</p>
        </CardBody>
      )}
    </Card>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[12px] text-faint">{k}</dt>
      <dd className="mt-0.5 break-words">{v || <span className="text-faint">—</span>}</dd>
    </div>
  );
}

function Timeline({ d }: { d: InviteDetail }) {
  const events = [...d.events].reverse();
  return (
    <Card>
      <CardHeader title="Timeline" />
      <ol className="px-5 pb-5">
        {events.map((e, i) => {
          const det = e.detail as Record<string, string>;
          const extra =
            e.kind === "source_connected"
              ? providerLabel(det.provider)
              : e.kind === "document_added"
                ? det.filename
                : e.kind === "link_added"
                  ? det.url
                  : e.kind === "revoked" && det.reason
                    ? det.reason
                    : null;
          return (
            <li key={i} className="relative flex gap-3 pb-4 last:pb-0">
              <span className="relative mt-1.5 flex flex-col items-center">
                <span className={`size-2 rounded-full ${e.kind === "revoked" ? "bg-hold" : e.kind === "submitted" ? "bg-review" : "bg-brand"}`} />
                {i < events.length - 1 && <span className="absolute top-3 h-[calc(100%+4px)] w-px bg-line" />}
              </span>
              <div className="min-w-0 text-[13px]">
                <p className="font-medium">{EVENT_LABELS[e.kind] ?? e.kind}</p>
                {extra && <p className="truncate text-muted">{extra}</p>}
                <p className="text-[11.5px] text-faint">
                  {date(e.at, true)}
                  {e.actor && ` · ${e.actor}`}
                </p>
              </div>
            </li>
          );
        })}
      </ol>
      {d.connections.length > 0 && (
        <div className="border-t border-line px-5 py-4">
          <p className="mb-2 text-[12px] font-medium text-muted">Connections</p>
          <ul className="space-y-1.5">
            {d.connections.map((c, i) => (
              <li key={i} className="flex items-center gap-2 text-[13px]">
                <Plug className="size-3.5 text-faint" />
                <span className="flex-1">{providerLabel(c.source)}</span>
                <span className="text-[11.5px] text-faint">{c.scopes.join(", ")}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[11.5px] text-faint">Simulated in this build: consent is recorded, no data is pulled.</p>
        </div>
      )}
    </Card>
  );
}

function EmailCard({ d }: { d: InviteDetail }) {
  const [open, setOpen] = useState(false);
  const e = d.sent_email!;
  return (
    <Card>
      <CardHeader title="Email" description={`${e.transport === "smtp" ? "Delivered over SMTP" : "In the outbox"} · ${ago(e.created_at)}`} />
      <CardBody>
        <button onClick={() => setOpen(true)} className="flex w-full items-start gap-3 rounded-lg border border-line p-3 text-left hover:bg-surface-2">
          <Mail className="mt-0.5 size-4 shrink-0 text-faint" />
          <span className="min-w-0">
            <span className="block truncate text-[13px] font-medium">{e.subject}</span>
            <span className="block truncate text-[12px] text-faint">To {e.to_addr}</span>
          </span>
        </button>
        {e.error && <Notice tone="hold" className="mt-3">Delivery failed: {e.error}</Notice>}
      </CardBody>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent title={e.subject} description={`To ${e.to_addr}`} className="max-w-2xl">
          <iframe title="Email" sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox" srcDoc={e.body_html?.replace("<head>", '<head><base target="_blank">')} className="h-[65vh] w-full rounded-lg border border-line bg-[#f4f4f2]" />
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function useInviteAction<T>(path: string, ok: string, after?: (r: T) => void) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (json: unknown) => api<T>(path, { json }),
    onSuccess: (r) => {
      toast.success(ok);
      qc.invalidateQueries({ queryKey: ["invite"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      after?.(r);
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Something went wrong"),
  });
}

function Resend({ id }: { id: string }) {
  const router = useRouter();
  const m = useInviteAction<{ id: string }>(`/api/invites/${id}/resend`, "New link sent; the old one no longer works", (r) =>
    router.push(`/app/invites/${r.id}`),
  );
  return (
    <Button onClick={() => m.mutate({})} disabled={m.isPending}>
      <RefreshCw /> Re-send
    </Button>
  );
}

function Revoke({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const m = useInviteAction(`/api/invites/${id}/revoke`, "Invite revoked", () => setOpen(false));
  return (
    <>
      <Button variant="danger" onClick={() => setOpen(true)}>
        <Ban /> Revoke
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent title="Revoke this invite?" description="The link stops working. Anything already shared stays on record.">
          <Field label="Reason" hint="optional" htmlFor="reason">
            <Input id="reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Sent to the wrong address" />
          </Field>
          <div className="mt-5 flex justify-end gap-2">
            <Button onClick={() => setOpen(false)}>Cancel</Button>
            <Button variant="danger" onClick={() => m.mutate({ reason })} disabled={m.isPending}>
              Revoke invite
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function OpenCase({ invite }: { invite: InviteDetail }) {
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [analyst, setAnalyst] = useState("");
  const lookups = useQuery({
    queryKey: ["lookups"],
    queryFn: () => api<{ analysts: { subject: string; name: string }[] }>("/api/cases/lookups"),
    enabled: open,
  });
  // Lender staff see a case once it is signed; until then it belongs to the analyst.
  const m = useInviteAction<{ id: string }>(`/api/invites/${invite.id}/open-case`, "Diligence started", () =>
    setOpen(false),
  );
  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>
        <ScanSearch /> Start diligence
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          title={`Start diligence on ${invite.business_name}`}
          description="Opens a case dated now, so everything the business has shared is in scope."
        >
          <Field label="Assign an analyst" htmlFor="analyst">
            <Select id="analyst" value={analyst} onChange={(e) => setAnalyst(e.target.value)}>
              <option value="">Assign later</option>
              {lookups.data?.analysts.map((a) => (
                <option key={a.subject} value={a.subject}>
                  {a.name}
                </option>
              ))}
            </Select>
          </Field>
          <div className="mt-5 flex justify-end gap-2">
            <Button onClick={() => setOpen(false)}>Cancel</Button>
            <Button variant="primary" onClick={() => m.mutate({ assigned_analyst: analyst || null })} disabled={m.isPending}>
              <ArrowUpRight /> Open case
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
