"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Download, Lock, PenLine, RefreshCw, XCircle } from "lucide-react";
import { useParams } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Page, PageHeader } from "@/components/app/page-header";
import { FindingCard } from "@/components/diligence/finding-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Empty, Notice, Skeleton } from "@/components/ui/misc";
import { ApiError, useApi } from "@/lib/api";
import type { Assessment } from "@/lib/types";
import { cn, date, money, numeric } from "@/lib/utils";

const BAND: Record<string, string> = {
  A: "bg-ok text-white",
  B: "bg-brand text-brand-fg",
  C: "bg-review text-white",
  D: "bg-hold text-white",
};
const OUTCOME: Record<string, "ok" | "hold" | "review" | "neutral" | "info"> = {
  pass: "ok",
  fail: "hold",
  knockout: "hold",
  applied: "info",
  cap: "review",
};

export default function AssessmentPage() {
  const { id } = useParams<{ id: string }>();
  const api = useApi();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["assessment", id], queryFn: () => api<Assessment>(`/api/assessments/${id}`) });
  const [reason, setReason] = useState("");
  const override = useMutation({
    mutationFn: () => api(`/api/assessments/${id}/override`, { json: { reason } }),
    onSuccess: () => {
      setReason("");
      toast.success("Override recorded beside the assessment");
      qc.invalidateQueries({ queryKey: ["assessment", id] });
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Could not record the override"),
  });
  const replay = useMutation({
    mutationFn: () => api<{ match: boolean; diffs: string[] }>(`/api/assessments/${id}/replay`, { method: "POST" }),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Replay failed"),
  });

  async function exportJson() {
    try {
      const data = await api<unknown>(`/api/assessments/${id}/export`);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `assessment-${id}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

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
        <Skeleton className="h-40" />
        <Skeleton className="mt-6 h-96" />
      </Page>
    );
  }
  const a = q.data;
  return (
    <Page>
      <PageHeader
        crumbs={[
          { href: "/app/diligence", label: "Due diligence" },
          ...(a.case_id ? [{ href: `/app/diligence/${a.case_id}`, label: a.borrower }] : []),
        ]}
        title={`${a.borrower}: signed assessment`}
        eyebrow={`Signed by ${a.created_by} · ${date(a.created_at, true)} · policy ${a.policy_version}`}
        actions={
          <>
            <Button onClick={exportJson}>
              <Download /> Export JSON
            </Button>
            {a.can_replay && (
              <Button onClick={() => replay.mutate()} disabled={replay.isPending}>
                <RefreshCw /> Replay
              </Button>
            )}
          </>
        }
      />
      {replay.data && (
        <Notice tone={replay.data.match ? "ok" : "hold"} icon={replay.data.match ? <CheckCircle2 /> : <XCircle />} className="mb-6">
          {replay.data.match ? (
            <>
              <strong>Replay matches.</strong> Re-running the frozen inputs through the policy gives the same decision.
            </>
          ) : (
            <>
              <strong>Replay differs:</strong> {replay.data.diffs.join("; ")}
            </>
          )}
        </Notice>
      )}

      <Card className="grid overflow-hidden sm:grid-cols-[auto_1fr_1fr]">
        <div className="flex items-center justify-center bg-surface-2 p-6">
          <span className={cn("grid size-20 place-items-center rounded-2xl text-[40px] font-semibold", a.decision_band ? BAND[a.decision_band] : "bg-surface-3 text-muted")}>
            {a.decision_band ?? "–"}
          </span>
        </div>
        <div className="border-line p-6 sm:border-r">
          <p className="text-[12px] text-faint">Decision band</p>
          <p className="mt-1 text-[24px] font-semibold tracking-tight">{a.decision_band ? `Band ${a.decision_band}` : "No band (knock-out)"}</p>
          <p className="mt-2 flex items-center gap-1.5 text-[12px] text-muted">
            <Lock className="size-3.5" /> Frozen at signing; overrides sit beside it
          </p>
        </div>
        <div className="p-6">
          <p className="text-[12px] text-faint">Suggested limit</p>
          <p className="tabular mt-1 text-[24px] font-semibold tracking-tight">{a.suggested_limit != null ? money(a.suggested_limit) : "None"}</p>
          <p className="mt-2 text-[12px] text-muted">Set by policy {a.policy_version}, not by the agent.</p>
        </div>
      </Card>

      <div className="mt-6 space-y-6">
        <Card>
          <CardHeader title={a.decision_band ? `Band ${a.decision_band} because` : "No band because"} description="Policy rules that fired, in order." />
          <div className="overflow-x-auto border-t border-line">
            <table className="w-full min-w-[640px] text-[13px]">
              <thead>
                <tr className="text-left text-[12px] text-faint">
                  <th className="px-5 py-2 font-medium">Step</th>
                  <th className="px-3 py-2 font-medium">Rule</th>
                  <th className="px-3 py-2 font-medium">Saw</th>
                  <th className="px-3 py-2 font-medium">Test</th>
                  <th className="px-3 py-2 font-medium">Outcome</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {a.rules_fired.map((r, i) => (
                  <tr key={i}>
                    <td className="px-5 py-2.5">
                      <Badge tone="neutral">{String(r.step)}</Badge>
                    </td>
                    <td className="px-3 py-2.5">{r.rule}</td>
                    <td className="px-3 py-2.5 font-mono text-[12px]">{numeric(r.saw)}</td>
                    <td className="px-3 py-2.5 font-mono text-[12px]">{r.op ? `${r.op} ${numeric(r.threshold)}` : "–"}</td>
                    <td className="px-3 py-2.5">
                      <Badge tone={OUTCOME[r.outcome] ?? "neutral"}>{r.outcome}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <section>
          <h2 className="mb-3 text-[17px] font-semibold tracking-tight">
            Findings <span className="ml-1 text-[13px] font-normal text-faint">{a.findings.length}</span>
          </h2>
          {a.findings.length === 0 ? (
            <Card>
              <Empty icon={<CheckCircle2 />} title="No findings" />
            </Card>
          ) : (
            <div className="space-y-2.5">
              {a.findings.map((f) => (
                <FindingCard key={f.id} f={f} caseId={a.case_id} />
              ))}
            </div>
          )}
        </section>

        <Card>
          <CardHeader title="Metrics" description="As frozen in the assessment." />
          <div className="grid gap-px overflow-hidden border-t border-line bg-line sm:grid-cols-2 lg:grid-cols-3">
            {a.metrics.map((m) => (
              <div key={`${m.key}-${m.version}`} className="bg-surface px-5 py-3.5" title={`${m.key} v${m.version}`}>
                <p className="truncate text-[12px] text-muted">{m.label}</p>
                <p className="tabular mt-0.5 text-[17px] font-semibold">{m.text ?? <span className="text-[13px] font-normal text-faint">not available</span>}</p>
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <CardHeader title="Overrides" description="Recorded beside the assessment; the assessment itself never changes." />
          <CardBody className="space-y-3">
            {a.overrides.length === 0 ? (
              <p className="text-[13px] text-muted">None.</p>
            ) : (
              <ul className="space-y-2">
                {a.overrides.map((o) => (
                  <li key={o.id} className="rounded-lg border border-line p-3 text-[13px]">
                    <p>{o.reason}</p>
                    <p className="mt-1 text-[11.5px] text-faint">
                      {o.by} · {date(o.at, true)}
                    </p>
                  </li>
                ))}
              </ul>
            )}
            {a.can_override && (
              <form
                className="flex gap-2 border-t border-line pt-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  override.mutate();
                }}
              >
                <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why the lender is departing from this assessment" aria-label="Override reason" />
                <Button variant="primary" disabled={!reason.trim() || override.isPending} className="shrink-0">
                  <PenLine /> Record override
                </Button>
              </form>
            )}
          </CardBody>
        </Card>
      </div>
    </Page>
  );
}
