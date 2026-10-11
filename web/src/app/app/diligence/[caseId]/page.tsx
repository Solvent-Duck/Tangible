"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  CircleAlert,
  FileText,
  Layers,
  Lock,
  PenLine,
  Play,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  Upload,
} from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { Page, PageHeader } from "@/components/app/page-header";
import { FindingCard, SEV } from "@/components/diligence/finding-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Textarea } from "@/components/ui/input";
import { Empty, Notice, Skeleton } from "@/components/ui/misc";
import { ApiError, useApi } from "@/lib/api";
import { CASE_STATUS } from "@/lib/labels";
import type { CaseStatus, Finding, Plan, Workspace } from "@/lib/types";
import { cn, date, money, shortId } from "@/lib/utils";

const STEPS = [
  { id: "open", label: "Open" },
  { id: "planning", label: "Plan" },
  { id: "investigating", label: "Investigate" },
  { id: "review", label: "Review" },
  { id: "signed", label: "Signed" },
];
const ORDER: Record<string, number> = { open: 0, planning: 1, investigating: 2, review: 3, signed: 4, closed: 5 };

function useAction<T = unknown>(caseId: string, ok?: string, after?: (r: T) => void) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ path, json, form }: { path: string; json?: unknown; form?: FormData }) =>
      api<T>(`/api/cases/${caseId}${path}`, { json, form, method: "POST" }),
    onSuccess: (r) => {
      if (ok) toast.success(ok);
      qc.invalidateQueries({ queryKey: ["workspace", caseId] });
      qc.invalidateQueries({ queryKey: ["case-status", caseId] });
      after?.(r);
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Something went wrong"),
  });
}

export default function CasePage() {
  const { caseId } = useParams<{ caseId: string }>();
  const api = useApi();
  const qc = useQueryClient();
  const ws = useQuery({
    queryKey: ["workspace", caseId],
    queryFn: () => api<Workspace>(`/api/cases/${caseId}/workspace`),
  });
  const live = ws.data?.status === "investigating" && !ws.data?.run?.stuck;
  const status = useQuery({
    queryKey: ["case-status", caseId],
    queryFn: () => api<CaseStatus>(`/api/cases/${caseId}/status`),
    enabled: live,
    refetchInterval: live ? 2000 : false,
  });
  useEffect(() => {
    if (status.data && ws.data && (status.data.status !== ws.data.status || status.data.run?.stuck)) {
      qc.invalidateQueries({ queryKey: ["workspace", caseId] });
    }
  }, [status.data, ws.data, qc, caseId]);

  if (ws.error) {
    return (
      <Page>
        <Notice tone="hold">{(ws.error as Error).message}</Notice>
      </Page>
    );
  }
  if (!ws.data) {
    return (
      <Page wide>
        <Skeleton className="h-10 w-80" />
        <Skeleton className="mt-6 h-24" />
        <Skeleton className="mt-6 h-96" />
      </Page>
    );
  }
  const c = ws.data;
  const analyst = c.viewer_role === "analyst";
  const st = CASE_STATUS[c.status] ?? { label: c.status, tone: "neutral" as const };
  const a0 = c.assessments[0];

  return (
    <Page wide>
      <PageHeader
        crumbs={[{ href: "/app/diligence", label: "Due diligence" }]}
        title={c.borrower}
        eyebrow={
          <span className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <Badge tone={st.tone}>{st.label}</Badge>
            <span>
              Case <code className="font-mono text-[12px]">{shortId(c.id)}</code>
            </span>
            <span>As of {date(c.as_of, true)}</span>
            {c.loan_amount && <span>Loan {money(c.loan_amount)}</span>}
            <span>Analyst {c.assigned_analyst ?? "unassigned"}</span>
            {c.previous_case_id && (
              <Link href={`/app/diligence/${c.previous_case_id}`} className="text-brand hover:underline">
                Re-based from {shortId(c.previous_case_id)}
              </Link>
            )}
          </span>
        }
        actions={
          a0 && (
            <Button variant="primary" asChild>
              <Link href={`/app/assessments/${a0.id}`}>
                <span className="grid size-5 place-items-center rounded bg-brand-fg/20 text-[11px] font-bold">
                  {a0.decision_band ?? "–"}
                </span>
                Signed assessment <ArrowRight />
              </Link>
            </Button>
          )
        }
      />

      <StatusPanel c={c} live={live} analyst={analyst} />

      <div className={cn("mt-6 grid gap-6", analyst && "xl:grid-cols-[minmax(0,1fr)_340px]")}>
        <div className="min-w-0 space-y-6">
          <Findings c={c} reviewable={analyst && c.status === "review"} />
          {analyst && c.last_investigation?.wrap_up && <WrapUp text={c.last_investigation.wrap_up} />}
          <Metrics c={c} />
          {analyst && <PlanCard c={c} />}
          {analyst && <Documents c={c} />}
        </div>
        {analyst && (
          <aside className="space-y-4 xl:sticky xl:top-6 xl:self-start">
            <NextStep c={c} />
            {c.status === "review" && <SendBack caseId={c.id} />}
            <Questions c={c} />
            <RunCard c={c} />
          </aside>
        )}
      </div>
    </Page>
  );
}

// ---------------------------------------------------------------- status

function StatusPanel({ c, live, analyst }: { c: Workspace; live: boolean; analyst: boolean }) {
  const at = ORDER[c.status] ?? 0;
  const finished = c.status === "signed" || c.status === "closed";
  return (
    <Card className="p-5">
      <ol className="grid grid-cols-5 gap-2" aria-label="Case progress">
        {STEPS.map((s, i) => {
          const done = i < at || (finished && i <= at);
          const current = i === at && !done;
          return (
            <li key={s.id} aria-current={current ? "step" : undefined} className="min-w-0">
              <div className="relative h-1.5 overflow-hidden rounded-full bg-surface-3">
                <div
                  className={cn(
                    "absolute inset-y-0 left-0 rounded-full transition-all",
                    done ? "w-full bg-brand" : current ? (live ? "w-2/3 bg-brand/60 pulse-dot" : "w-1/3 bg-brand/60") : "w-0",
                  )}
                />
              </div>
              <p
                className={cn(
                  "mt-2 min-w-0 items-center gap-1 truncate text-[12.5px] sm:flex",
                  current ? "flex font-medium text-ink" : "hidden text-faint",
                )}
              >
                {done && <Check className="size-3.5 text-brand" />}
                {s.label}
              </p>
            </li>
          );
        })}
      </ol>
      {live && (
        <p className="mt-4 flex items-center gap-2 text-[13px] text-muted">
          <span className="pulse-dot size-2 rounded-full bg-brand" />
          The agent is investigating. Findings appear here as soon as it finishes.
          {c.run?.age_minutes != null && <span className="text-faint">Running {c.run.age_minutes} min.</span>}
        </p>
      )}
      {c.status === "closed" && (
        <p className="mt-4 text-[13px] text-muted">This case was re-based or closed; it is kept as a record.</p>
      )}
      {c.status === "investigating" && c.run?.stuck && <Stuck c={c} analyst={analyst} />}
      {c.last_failure && (
        <Notice tone="hold" icon={<CircleAlert />} className="mt-4">
          <strong>The last investigation failed</strong> {date(c.last_failure.at, true)}: {c.last_failure.message} The case
          went back to <strong>{c.last_failure.restored_status}</strong>.
        </Notice>
      )}
      {c.last_reset && (
        <Notice tone="info" icon={<RefreshCw />} className="mt-4">
          <strong>Investigation reset</strong> by {c.last_reset.by} {date(c.last_reset.at, true)} ({c.last_reset.reason}).
        </Notice>
      )}
    </Card>
  );
}

function Stuck({ c, analyst }: { c: Workspace; analyst: boolean }) {
  const [reason, setReason] = useState("");
  const m = useAction(c.id, "Investigation reset");
  return (
    <Notice tone="review" icon={<AlertTriangle />} className="mt-4">
      <p>
        This investigation looks stuck
        {c.run?.started_at
          ? `: it started ${date(c.run.started_at, true)} (${c.run.age_minutes} minutes ago) and hasn't finished.`
          : ": no start was recorded, so nothing is running."}{" "}
        Resetting puts the case back to <strong>{c.run?.restores_to}</strong>; nothing is deleted.
      </p>
      {analyst && (
        <div className="mt-2 flex gap-2">
          <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason, e.g. server restarted" className="h-8 bg-surface" />
          <Button size="sm" disabled={!reason.trim() || m.isPending} onClick={() => m.mutate({ path: "/reset-investigation", json: { reason } })}>
            <RefreshCw /> Reset
          </Button>
        </div>
      )}
    </Notice>
  );
}

// ---------------------------------------------------------------- findings

function Findings({ c, reviewable }: { c: Workspace; reviewable: boolean }) {
  const groups = (["hold", "review", "info"] as const)
    .map((sev) => ({ sev, items: c.findings.filter((f: Finding) => f.severity === sev) }))
    .filter((g) => g.items.length);
  return (
    <section aria-labelledby="findings-h">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 id="findings-h" className="text-[17px] font-semibold tracking-tight">
          Findings <span className="ml-1 text-[13px] font-normal text-faint">{c.findings.length}</span>
        </h2>
        <div className="flex gap-1.5">
          {groups.map((g) => (
            <a key={g.sev} href={`#sev-${g.sev}`} className={cn("rounded-full px-2.5 py-0.5 text-[12px] font-medium", SEV[g.sev].chip)}>
              {SEV[g.sev].label} {g.items.length}
            </a>
          ))}
        </div>
      </div>
      {groups.length === 0 ? (
        <Card>
          <Empty icon={<Search />} title="No findings">
            {c.status === "open" || c.status === "planning"
              ? "Findings appear once the investigation runs."
              : c.status === "investigating"
                ? "The agent is still working."
                : "Nothing was raised for this case."}
          </Empty>
        </Card>
      ) : (
        <div className="space-y-5">
          {groups.map((g) => (
            <div key={g.sev} id={`sev-${g.sev}`} className="space-y-2.5 scroll-mt-6">
              {g.items.map((f) => (
                <FindingCard key={f.id} f={f} caseId={c.id} reviewable={reviewable} />
              ))}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function WrapUp({ text }: { text: string }) {
  const [more, setMore] = useState(false);
  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <Sparkles className="size-4 text-brand" /> Agent&apos;s wrap-up
          </span>
        }
        description="The agent's own words. Its numbers are not verified; the findings above are what count."
      />
      <CardBody>
        <div className={cn("text-[13.5px] leading-relaxed whitespace-pre-line text-muted", !more && "line-clamp-6")}>{text}</div>
        <button className="mt-2 text-[12.5px] font-medium text-brand hover:underline" onClick={() => setMore(!more)}>
          {more ? "Show less" : "Show all"}
        </button>
      </CardBody>
    </Card>
  );
}

function Metrics({ c }: { c: Workspace }) {
  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <Layers className="size-4 text-faint" /> Baseline metrics
          </span>
        }
        description="Computed by versioned metric functions as of the case date."
      />
      {c.metrics.length === 0 ? (
        <Empty title="No metrics yet" className="pt-0">
          They appear after the investigation&apos;s baseline runs.
        </Empty>
      ) : (
        <div className="grid gap-px overflow-hidden border-t border-line bg-line sm:grid-cols-2 xl:grid-cols-3">
          {c.metrics.map((m) => (
            <div key={`${m.key}-${m.version}`} className="bg-surface px-5 py-3.5" title={`${m.key} v${m.version} = ${m.value ?? "null"}`}>
              <p className="truncate text-[12px] text-muted">{m.label}</p>
              <p className={cn("tabular mt-0.5 text-[17px] font-semibold tracking-tight", m.text == null && "text-[13px] font-normal text-faint")}>
                {m.text ?? "not available"}
              </p>
              <p className="mt-0.5 truncate font-mono text-[10.5px] text-faint">
                {m.key} · v{m.version}
              </p>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- plan

function latestPlan(c: Workspace): Plan | null {
  const plans = c.plans ?? [];
  return plans.length ? plans[plans.length - 1] : null;
}

function PlanCard({ c }: { c: Workspace }) {
  const plan = latestPlan(c);
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const approve = useAction<{ edited: boolean }>(c.id, undefined, (r) => {
    toast.success(r.edited ? "Plan edited and approved" : "Plan approved");
    setEditing(false);
  });
  const canApprove = plan && !plan.approved && (c.status === "open" || c.status === "planning");

  function submit() {
    if (!plan) return;
    let edited: unknown = null;
    if (editing && body.trim() !== JSON.stringify(plan.body, null, 2)) {
      try {
        edited = JSON.parse(body);
      } catch {
        setErr("The plan must be valid JSON");
        return;
      }
    }
    setErr(null);
    approve.mutate({ path: "/plan/approve", json: { plan_id: plan.id, edited_body: edited } });
  }

  return (
    <Card id="plan">
      <CardHeader
        title="Investigation plan"
        description="Drafted by the agent, approved by you before anything runs."
        action={plan && <Badge tone={plan.approved ? "ok" : "review"}>v{plan.version} · {plan.approved ? "approved" : "awaiting approval"}</Badge>}
      />
      {!plan ? (
        <Empty icon={<FileText />} title="No plan yet" className="pt-0">
          Ask the agent to propose one from the borrower&apos;s data.
        </Empty>
      ) : (
        <CardBody className="space-y-4">
          {(plan.body.business_model || plan.body.notes) && (
            <div className="grid gap-4 sm:grid-cols-2">
              {plan.body.business_model && (
                <div>
                  <p className="text-[12px] font-medium text-faint">Business model</p>
                  <p className="mt-1 text-[13.5px] leading-relaxed">{plan.body.business_model}</p>
                </div>
              )}
              {plan.body.notes && (
                <div>
                  <p className="text-[12px] font-medium text-faint">Notes</p>
                  <p className="mt-1 text-[13.5px] leading-relaxed">{plan.body.notes}</p>
                </div>
              )}
            </div>
          )}
          {plan.body.risks && plan.body.risks.length > 0 && (
            <ol className="space-y-2.5">
              {plan.body.risks.map((r, i) => (
                <li key={i} className="rounded-lg border border-line p-3.5">
                  <p className="text-[13.5px] font-medium">
                    <span className="mr-2 font-mono text-[11px] text-faint">{String(i + 1).padStart(2, "0")}</span>
                    {r.risk}
                  </p>
                  {r.why && <p className="mt-1 text-[13px] text-muted">{r.why}</p>}
                  {(r.checks?.length || r.metrics?.length) && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {r.checks?.map((k) => (
                        <Badge key={k} tone="outline" className="font-mono">
                          {k}
                        </Badge>
                      ))}
                      {r.metrics?.map((k) => (
                        <Badge key={k} tone="neutral" className="font-mono">
                          {k}
                        </Badge>
                      ))}
                    </div>
                  )}
                  {r.follow_up && <p className="mt-2 text-[12.5px] text-muted"><b>Follow-up:</b> {r.follow_up}</p>}
                </li>
              ))}
            </ol>
          )}
          {editing ? (
            <Textarea value={body} onChange={(e) => setBody(e.target.value)} rows={14} className="font-mono text-[12px]" spellCheck={false} aria-label="Plan JSON" />
          ) : (
            <details className="text-[12.5px] text-muted">
              <summary className="cursor-pointer">Plan JSON</summary>
              <pre className="mt-2 max-h-72 overflow-auto rounded-lg bg-surface-2 p-3 font-mono text-[11.5px]">{JSON.stringify(plan.body, null, 2)}</pre>
            </details>
          )}
          {err && <Notice tone="hold">{err}</Notice>}
          {canApprove && (
            <div className="flex flex-wrap justify-end gap-2">
              {!editing && (
                <Button
                  onClick={() => {
                    setBody(JSON.stringify(plan.body, null, 2));
                    setEditing(true);
                  }}
                >
                  <PenLine /> Edit
                </Button>
              )}
              <Button variant="primary" onClick={submit} disabled={approve.isPending}>
                <Check /> {editing ? "Approve edited plan" : "Approve plan"}
              </Button>
            </div>
          )}
        </CardBody>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- documents

function Documents({ c }: { c: Workspace }) {
  const router = useRouter();
  const ref = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const upload = useAction(c.id, "Uploaded");
  const rebase = useAction<{ id: string }>(c.id, "Case re-based", (r) => router.push(`/app/diligence/${r.id}`));
  const early = c.status === "open" || c.status === "planning";

  async function send(andRebase: boolean) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    await upload.mutateAsync({ path: "/documents", form });
    setFile(null);
    if (ref.current) ref.current.value = "";
    if (andRebase) rebase.mutate({ path: "/rebase" });
  }

  return (
    <Card id="documents">
      <CardHeader title="Documents" description="Uploaded files are write-once evidence, observed when they arrive." />
      {c.late_uploads > 0 && c.status !== "closed" && (
        <div className="px-5 pb-4">
          <Notice tone="review" icon={<AlertTriangle />}>
            <p>
              <strong>
                {c.late_uploads === 1 ? "A document arrived" : `${c.late_uploads} documents arrived`} after the case date.
              </strong>{" "}
              Re-base to include {c.late_uploads === 1 ? "it" : "them"}: a new case dated now opens and this one is kept as a record.
            </p>
            <Button size="sm" className="mt-2" onClick={() => rebase.mutate({ path: "/rebase" })} disabled={rebase.isPending}>
              <RefreshCw /> Re-base case
            </Button>
          </Notice>
        </div>
      )}
      {c.documents.length > 0 && (
        <div className="overflow-x-auto border-t border-line">
          <table className="w-full min-w-[560px] text-[13px]">
            <thead>
              <tr className="text-left text-[12px] text-faint">
                <th className="px-5 py-2 font-medium">File</th>
                <th className="px-3 py-2 font-medium">Observed</th>
                <th className="px-3 py-2 font-medium">This case</th>
                <th className="px-3 py-2 font-medium">Read</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {c.documents.map((d) => (
                <tr key={d.pull_id}>
                  <td className="px-5 py-2.5">
                    <p className="font-medium">{d.filename}</p>
                    <p className="font-mono text-[11px] text-faint" title={d.sha256}>
                      {d.sha256.slice(0, 16)}… · {Math.ceil(d.byte_size / 1024)} KB
                    </p>
                  </td>
                  <td className="px-3 py-2.5 text-muted">{date(d.observed_at, true)}</td>
                  <td className="px-3 py-2.5">{d.included ? <Badge tone="ok">included</Badge> : <Badge tone="review">after case date</Badge>}</td>
                  <td className="px-3 py-2.5">
                    {d.read ? <Badge tone="ok">read</Badge> : d.read_error ? <Badge tone="hold" title={d.read_error}>unreadable</Badge> : <Badge>not yet</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {c.status !== "closed" && (
        <div className="flex flex-col gap-3 border-t border-line px-5 py-4 sm:flex-row sm:items-center">
          <input ref={ref} type="file" aria-label="Choose a document" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="min-w-0 flex-1 text-[13px] file:mr-3 file:rounded-md file:border file:border-line file:bg-surface-2 file:px-3 file:py-1.5 file:text-[13px] file:text-ink" />
          <div className="flex gap-2">
            {early && (
              <Button variant="primary" size="sm" disabled={!file || upload.isPending || rebase.isPending} onClick={() => send(true)}>
                <Upload /> Upload and re-base
              </Button>
            )}
            <Button size="sm" disabled={!file || upload.isPending} onClick={() => send(false)}>
              Upload only
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- sidebar

function NextStep({ c }: { c: Workspace }) {
  const router = useRouter();
  const plan = latestPlan(c);
  const propose = useAction<{ version: number }>(c.id, undefined, (r) => toast.success(`Plan v${r.version} proposed`));
  const investigate = useAction(c.id, "Investigation started");
  const approve = useAction(c.id, "Plan approved");
  const [signing, setSigning] = useState(false);
  const sign = useAction<{ assessment_id: string }>(c.id, "Signed and frozen", (r) => router.push(`/app/assessments/${r.assessment_id}`));
  const [confirm, setConfirm] = useState(false);
  const counts = c.findings.reduce<Record<string, number>>((acc, f) => {
    const k = `${f.severity} / ${f.review_state}`;
    acc[k] = (acc[k] ?? 0) + 1;
    return acc;
  }, {});
  const early = c.status === "open" || c.status === "planning";

  return (
    <Card className="p-5">
      <h2 className="text-[14px] font-semibold">Next step</h2>
      <div className="mt-3 space-y-2.5 text-[13px] text-muted">
        {early && !plan && (
          <>
            <p>The agent drafts an investigation plan from the borrower&apos;s data. You approve it before anything runs.</p>
            <Button variant="primary" className="w-full" disabled={propose.isPending} onClick={() => propose.mutate({ path: "/plan" })}>
              <Sparkles /> {propose.isPending ? "Drafting…" : "Propose plan"}
            </Button>
          </>
        )}
        {early && plan && !plan.approved && (
          <>
            <p>Review the agent&apos;s plan below, edit it if needed, then approve.</p>
            <Button variant="primary" className="w-full" disabled={approve.isPending} onClick={() => approve.mutate({ path: "/plan/approve", json: { plan_id: plan.id } })}>
              <Check /> Approve plan
            </Button>
            <Button className="w-full" disabled={propose.isPending} onClick={() => propose.mutate({ path: "/plan" })}>
              <Sparkles /> Propose a new plan
            </Button>
          </>
        )}
        {early && plan?.approved && (
          <>
            <p>The plan is approved. The agent will run the checks and metrics it lists.</p>
            <Button variant="primary" className="w-full" disabled={investigate.isPending} onClick={() => investigate.mutate({ path: "/investigate" })}>
              <Play /> Start investigation
            </Button>
          </>
        )}
        {c.status === "investigating" && <p>The agent is investigating. This page updates on its own.</p>}
        {c.status === "review" && (
          <>
            <p>Review each finding, then sign. The policy sets the band and limit, not you.</p>
            <Button variant="primary" className="w-full" onClick={() => setSigning(true)}>
              <PenLine /> Sign report…
            </Button>
          </>
        )}
        {c.assessments[0] && (
          <Button className="w-full" asChild>
            <Link href={`/app/assessments/${c.assessments[0].id}`}>
              View assessment <ArrowRight />
            </Link>
          </Button>
        )}
        {c.status === "closed" && <p>This case is closed.</p>}
        {early || c.status === "investigating" ? (
          <p className="flex items-center gap-1.5 border-t border-line pt-3 text-[12px] text-faint">
            <Lock className="size-3.5" /> Signing unlocks once the case is in review.
          </p>
        ) : null}
      </div>
      <Dialog open={signing} onOpenChange={setSigning}>
        <DialogContent title="Sign and freeze this report?" description="The policy sets the band and limit from the reviewed findings. This can't be undone; overrides are recorded beside it.">
          <ul className="divide-y divide-line rounded-lg border border-line text-[13px]">
            {Object.keys(counts).length === 0 && <li className="px-3 py-2 text-muted">No findings</li>}
            {Object.entries(counts).map(([k, n]) => {
              const [sev, state] = k.split(" / ");
              return (
                <li key={k} className="flex items-center justify-between px-3 py-2">
                  <span className="flex items-center gap-2">
                    <span className={cn("rounded px-1.5 text-[10.5px] font-semibold uppercase", SEV[sev as keyof typeof SEV]?.chip)}>{sev}</span>
                    {state}
                  </span>
                  <span className="tabular font-medium">{n}</span>
                </li>
              );
            })}
          </ul>
          <label className="mt-4 flex items-start gap-2 text-[13px]">
            <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} className="mt-0.5 accent-[var(--brand)]" />
            I have reviewed the findings and want to sign this report.
          </label>
          <div className="mt-5 flex justify-end gap-2">
            <Button onClick={() => setSigning(false)}>Cancel</Button>
            <Button variant="primary" disabled={!confirm || sign.isPending} onClick={() => sign.mutate({ path: "/sign" })}>
              <Lock /> Sign and freeze
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function SendBack({ caseId }: { caseId: string }) {
  const [q, setQ] = useState("");
  const m = useAction(caseId, "Sent back to the agent", () => setQ(""));
  return (
    <Card className="p-5">
      <h2 className="text-[14px] font-semibold">Send back to the agent</h2>
      <Field label="Question or request" htmlFor="sendback" className="mt-3">
        <Textarea id="sendback" rows={3} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Check whether the September deposits match the payouts" />
      </Field>
      <Button className="mt-2 w-full" disabled={!q.trim() || m.isPending} onClick={() => m.mutate({ path: "/send-back", json: { question: q } })}>
        <Send /> Send back
      </Button>
    </Card>
  );
}

function Questions({ c }: { c: Workspace }) {
  const m = useAction(c.id, "Question approved");
  const qs = c.questions ?? [];
  return (
    <Card className="p-5">
      <h2 className="text-[14px] font-semibold">Borrower questions</h2>
      {qs.length === 0 ? (
        <p className="mt-2 text-[13px] text-muted">No drafted questions.</p>
      ) : (
        <ul className="mt-3 space-y-3">
          {qs.map((q) => (
            <li key={q.id} className="rounded-lg border border-line p-3 text-[13px]">
              <p>{q.body}</p>
              <div className="mt-2">
                {q.approved ? (
                  <Badge tone="ok">Approved · not sent</Badge>
                ) : (
                  <Button size="sm" onClick={() => m.mutate({ path: `/questions/${q.id}/approve` })} disabled={m.isPending}>
                    <Check /> Approve
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-3 text-[11.5px] text-faint">Nothing is sent outside Tangible: approving records your approval only.</p>
    </Card>
  );
}

function RunCard({ c }: { c: Workspace }) {
  return (
    <Card className="p-5">
      <h2 className="text-[14px] font-semibold">Agent run</h2>
      <dl className="mt-3 grid grid-cols-2 gap-y-2 text-[13px]">
        <dt className="text-muted">Step budget</dt>
        <dd className="tabular text-right">{c.budget_steps}</dd>
        <dt className="text-muted">Cost budget</dt>
        <dd className="tabular text-right">${c.budget_usd}</dd>
        {c.usage && (
          <>
            <dt className="text-muted">Model calls</dt>
            <dd className="tabular text-right">{c.usage.model_calls}</dd>
            <dt className="text-muted">Cost</dt>
            <dd className="tabular text-right">{c.usage.cost_text}</dd>
          </>
        )}
        {c.last_investigation?.stop_reason && (
          <>
            <dt className="text-muted">Stopped</dt>
            <dd className="truncate text-right font-mono text-[12px]">{c.last_investigation.stop_reason}</dd>
          </>
        )}
      </dl>
    </Card>
  );
}
