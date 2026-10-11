"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  ArrowUpRight,
  CheckCircle2,
  CircleDashed,
  FileCheck2,
  Hourglass,
  Plus,
  Search,
  Send,
  Users,
} from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Page, PageHeader } from "@/components/app/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Empty, Notice, Progress, Skeleton } from "@/components/ui/misc";
import { useApi } from "@/lib/api";
import { EVENT_LABELS, STAGES, STAGE_TONE, providerLabel, stageLabel } from "@/lib/labels";
import { useMembership } from "@/lib/session";
import type { Dashboard, InviteSummary, Stage } from "@/lib/types";
import { ago, cn, money } from "@/lib/utils";

export default function DashboardPage() {
  const api = useApi();
  const { membership } = useMembership();
  const q = useQuery({
    queryKey: ["dashboard", membership?.tenant_id],
    queryFn: () => api<Dashboard>("/api/dashboard"),
    refetchInterval: 15_000,
  });
  const canInvite = membership?.role === "credit_admin";

  return (
    <Page wide>
      <PageHeader
        eyebrow={q.data?.lender_name ?? membership?.tenant_name}
        title="Portfolio pipeline"
        description="Every business you've invited, from first email to signed report."
        actions={
          canInvite && (
            <Button variant="primary" asChild>
              <Link href="/app/invites/new">
                <Plus /> Invite a business
              </Link>
            </Button>
          )
        }
      />
      {q.error && <Notice tone="hold">{(q.error as Error).message}</Notice>}
      {q.isLoading && <DashboardSkeleton />}
      {q.data && (
        <div className="space-y-6">
          <Kpis d={q.data} />
          <div className="grid gap-6 xl:grid-cols-[1fr_340px]">
            <Pipeline d={q.data} canInvite={canInvite} />
            <Activity d={q.data} />
          </div>
        </div>
      )}
    </Page>
  );
}

function Kpis({ d }: { d: Dashboard }) {
  const tiles = [
    { label: "Active invites", value: d.kpis.invited, icon: Send, hint: "Live, not revoked" },
    { label: "Awaiting the business", value: d.kpis.awaiting_borrower, icon: Hourglass, hint: "Invited → connecting" },
    { label: "Ready for diligence", value: d.kpis.ready_for_diligence, icon: FileCheck2, hint: "Submitted, no case yet", accent: d.kpis.ready_for_diligence > 0 },
    { label: "In diligence", value: d.kpis.in_diligence, icon: CircleDashed, hint: "Open cases" },
    { label: "Signed · 30 days", value: d.kpis.signed_30d, icon: CheckCircle2, hint: "Frozen assessments" },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
      {tiles.map((t) => (
        <Card key={t.label} className={cn("p-4", t.accent && "border-review/40 bg-review-soft/40")}>
          <div className="flex items-center justify-between text-muted">
            <span className="text-[12.5px] font-medium">{t.label}</span>
            <t.icon className={cn("size-4", t.accent ? "text-review" : "text-faint")} />
          </div>
          <p className="tabular mt-2 text-[30px] leading-none font-semibold tracking-tight">{t.value}</p>
          <p className="mt-1.5 text-[12px] text-faint">{t.hint}</p>
        </Card>
      ))}
    </div>
  );
}

function Funnel({ d, active, onPick }: { d: Dashboard; active: Stage | null; onPick: (s: Stage | null) => void }) {
  const total = Math.max(1, STAGES.reduce((n, s) => n + (d.by_stage[s.id] ?? 0), 0));
  return (
    <div className="px-5 pb-4">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-surface-2" aria-hidden>
        {STAGES.map((s, i) => {
          const n = d.by_stage[s.id] ?? 0;
          if (!n) return null;
          return (
            <div
              key={s.id}
              className="h-full border-r-2 border-surface last:border-r-0"
              style={{
                width: `${(n / total) * 100}%`,
                background: `color-mix(in oklab, var(--brand) ${25 + i * 12}%, var(--surface-3))`,
              }}
            />
          );
        })}
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        <StageChip label="All" n={STAGES.reduce((n, s) => n + (d.by_stage[s.id] ?? 0), 0)} on={active === null} onClick={() => onPick(null)} />
        {STAGES.map((s) => (
          <StageChip key={s.id} label={s.short} n={d.by_stage[s.id] ?? 0} on={active === s.id} onClick={() => onPick(active === s.id ? null : s.id)} />
        ))}
      </div>
    </div>
  );
}

function StageChip({ label, n, on, onClick }: { label: string; n: number; on: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={on}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[12.5px] transition-colors",
        on ? "border-brand bg-brand-soft text-brand-ink" : "border-line text-muted hover:bg-surface-2",
      )}
    >
      {label}
      <span className="tabular rounded-full bg-surface-2 px-1.5 text-[11px] font-medium text-ink">{n}</span>
    </button>
  );
}

function Pipeline({ d, canInvite }: { d: Dashboard; canInvite: boolean }) {
  const [stage, setStage] = useState<Stage | null>(null);
  const [text, setText] = useState("");
  const rows = useMemo(() => {
    const t = text.trim().toLowerCase();
    return d.pipeline.filter(
      (r) =>
        (!stage || (r.state === "live" && r.stage === stage)) &&
        (!t || r.business_name.toLowerCase().includes(t) || r.email.toLowerCase().includes(t)),
    );
  }, [d.pipeline, stage, text]);

  return (
    <Card className="min-w-0 overflow-hidden">
      <CardHeader
        title="Pipeline"
        description={`${d.pipeline.length} ${d.pipeline.length === 1 ? "business" : "businesses"}`}
        action={
          <div className="relative w-44 sm:w-56">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-faint" />
            <Input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Search businesses"
              className="h-8 pl-8 text-[13px]"
              aria-label="Search businesses"
            />
          </div>
        }
      />
      <Funnel d={d} active={stage} onPick={setStage} />
      {d.pipeline.length === 0 ? (
        <Empty icon={<Users />} title="No businesses yet" className="border-t border-line">
          {canInvite ? (
            <>
              Invite your first borrower to start.{" "}
              <Link href="/app/invites/new" className="font-medium text-brand hover:underline">
                Compose an invite
              </Link>
            </>
          ) : (
            "A credit admin sends invites."
          )}
        </Empty>
      ) : rows.length === 0 ? (
        <Empty title="Nothing matches" className="border-t border-line" />
      ) : (
        <div className="overflow-x-auto border-t border-line">
          <table className="w-full min-w-[720px] text-[13.5px]">
            <thead>
              <tr className="text-left text-[12px] text-faint">
                <th className="px-5 py-2.5 font-medium">Business</th>
                <th className="px-3 py-2.5 font-medium">Stage</th>
                <th className="px-3 py-2.5 font-medium">Requested items</th>
                <th className="px-3 py-2.5 text-right font-medium">Loan</th>
                <th className="px-3 py-2.5 font-medium">Last activity</th>
                <th className="w-10 px-3 py-2.5" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.map((r) => (
                <PipelineRow key={r.id} r={r} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function PipelineRow({ r }: { r: InviteSummary }) {
  const pct = r.requested ? (r.answered / r.requested) * 100 : 0;
  const dead = r.state !== "live";
  return (
    <tr className="group hover:bg-surface-2/60">
      <td className="px-5 py-3">
        <Link href={`/app/invites/${r.id}`} className="block min-w-0">
          <span className="block truncate font-medium text-ink group-hover:text-brand">{r.business_name}</span>
          <span className="block truncate text-[12px] text-faint">{r.contact_name ? `${r.contact_name} · ` : ""}{r.email}</span>
        </Link>
      </td>
      <td className="px-3 py-3">
        {dead ? (
          <Badge tone={r.state === "revoked" ? "hold" : "neutral"}>{r.state === "expired" ? "Expired" : "Revoked"}</Badge>
        ) : (
          <Badge tone={STAGE_TONE[r.stage]}>{stageLabel(r.stage)}</Badge>
        )}
      </td>
      <td className="px-3 py-3">
        <div className="flex items-center gap-2.5">
          <Progress value={pct} className="w-20" />
          <span className="tabular text-[12px] text-muted">
            {r.answered}/{r.requested}
          </span>
        </div>
      </td>
      <td className="tabular px-3 py-3 text-right">{money(r.loan_amount)}</td>
      <td className="px-3 py-3 text-[12.5px] text-muted">{ago(r.last_activity)}</td>
      <td className="px-3 py-3">
        {r.case_id && r.case_status === "signed" ? (
          <Link href={`/app/diligence/${r.case_id}`} className="text-faint hover:text-brand" aria-label="Open case">
            <ArrowUpRight className="size-4" />
          </Link>
        ) : (
          <Link href={`/app/invites/${r.id}`} className="text-faint hover:text-brand" aria-label="Open invite">
            <ArrowRight className="size-4" />
          </Link>
        )}
      </td>
    </tr>
  );
}

function Activity({ d }: { d: Dashboard }) {
  return (
    <Card className="h-fit">
      <CardHeader title="Recent activity" />
      {d.activity.length === 0 ? (
        <Empty title="Quiet so far" className="pt-2" />
      ) : (
        <ol className="space-y-0 px-5 pb-4">
          {d.activity.slice(0, 14).map((a, i) => (
            <li key={i} className="relative flex gap-3 pb-4 last:pb-0">
              <span className="relative mt-1.5 flex flex-col items-center">
                <span className={cn("size-2 rounded-full", a.kind === "submitted" ? "bg-review" : a.kind === "revoked" ? "bg-hold" : "bg-brand")} />
                {i < Math.min(d.activity.length, 14) - 1 && <span className="absolute top-3 h-[calc(100%+4px)] w-px bg-line" />}
              </span>
              <div className="min-w-0 text-[13px]">
                <Link href={`/app/invites/${a.invite_id}`} className="font-medium hover:text-brand">
                  {a.business_name}
                </Link>
                <p className="text-muted">
                  {EVENT_LABELS[a.kind] ?? a.kind}
                  {a.kind === "source_connected" && typeof a.detail.provider === "string" && ` ${providerLabel(a.detail.provider)}`}
                  {a.kind === "document_added" && typeof a.detail.filename === "string" && ` ${a.detail.filename}`}
                </p>
                <p className="text-[11.5px] text-faint">{ago(a.at)}</p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

function DashboardSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {[0, 1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-[104px] rounded-xl" />
        ))}
      </div>
      <Skeleton className="h-80 rounded-xl" />
    </div>
  );
}
