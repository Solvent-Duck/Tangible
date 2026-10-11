"use client";

import { useQuery } from "@tanstack/react-query";
import { ChartColumn, CheckCircle2, FlaskConical, XCircle } from "lucide-react";

import { Page, PageHeader } from "@/components/app/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { Empty, Notice, Skeleton } from "@/components/ui/misc";
import { useApi } from "@/lib/api";
import { humanize } from "@/lib/utils";

type Summary = {
  synthetic: boolean;
  policy_version: string;
  cohort: Record<string, string>;
  loss_by_band: Record<string, string>[];
  plants: { catch_rate: string; caught: string; planted: string; rows: Record<string, string>[]; misses: string[] };
  false_alarms: { per_clean_loan: string; alarms: string; clean_loans: string; loans_with_alarm: string; by_check: [string, string][] };
  replay: { matched: string; loans: string; all_match: boolean; mismatches: string[] };
  time_leak: { violations: string; clean: boolean; hidden_rows: string; visible_rows: string; loans: string };
  top_metrics: Record<string, string>[];
  metric_count: number;
};
type Report = { summary: Summary | null; problem: string | null; report_path: string; hint: string };

function Table({ cols, rows }: { cols: [string, string][]; rows: Record<string, string>[] }) {
  return (
    <div className="overflow-x-auto border-t border-line">
      <table className="w-full min-w-[480px] text-[13px]">
        <thead>
          <tr className="text-left text-[12px] text-faint">
            {cols.map(([, l], i) => (
              <th key={l} className={`py-2 font-medium ${i === 0 ? "px-5" : "px-3 text-right"}`}>
                {l}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map((r, n) => (
            <tr key={n}>
              {cols.map(([k], i) => (
                <td key={k} className={`py-2.5 ${i === 0 ? "px-5 font-medium" : "tabular px-3 text-right text-muted"}`}>
                  {r[k]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function BacktestPage() {
  const api = useApi();
  const q = useQuery({ queryKey: ["backtest"], queryFn: () => api<Report>("/api/backtest") });
  const s = q.data?.summary;
  return (
    <Page wide>
      <PageHeader title="Backtest" description="How the checks, metrics and policy perform on a cohort with known outcomes." />
      {q.error && <Notice tone="hold">{(q.error as Error).message}</Notice>}
      {q.isLoading && <Skeleton className="h-96" />}
      {q.data && !s && (
        <Card>
          <Empty icon={<ChartColumn />} title="No backtest report yet">
            <p>
              {q.data.problem === "missing"
                ? `Nothing at ${q.data.report_path}. Generate one on the synthetic cohort:`
                : `${humanize(q.data.problem)}. Regenerate it:`}
            </p>
            <code className="mt-2 block font-mono text-[12px]">{q.data.hint}</code>
          </Empty>
        </Card>
      )}
      {s && (
        <div className="space-y-6">
          {s.synthetic && (
            <Notice tone="review" icon={<FlaskConical />}>
              <strong>Synthetic cohort.</strong> Planted problems on generated merchants, scored with policy {s.policy_version}.
            </Notice>
          )}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {[
              ["Loans", s.cohort.loans, `${s.cohort.default_rate} defaulted`],
              ["Plant catch rate", s.plants.catch_rate, `${s.plants.caught} of ${s.plants.planted}`],
              ["False alarms / clean loan", s.false_alarms.per_clean_loan, `${s.false_alarms.loans_with_alarm} of ${s.false_alarms.clean_loans} loans`],
              ["Replay", `${s.replay.matched}/${s.replay.loans}`, s.replay.all_match ? "all match" : "mismatches"],
            ].map(([l, v, h]) => (
              <Card key={l} className="p-4">
                <p className="text-[12.5px] text-muted">{l}</p>
                <p className="tabular mt-1.5 text-[28px] leading-none font-semibold tracking-tight">{v}</p>
                <p className="mt-1.5 text-[12px] text-faint">{h}</p>
              </Card>
            ))}
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge tone={s.time_leak.clean ? "ok" : "hold"}>
              {s.time_leak.clean ? <CheckCircle2 /> : <XCircle />} Time leak: {s.time_leak.violations} violations across {s.time_leak.loans} loans
            </Badge>
            <Badge tone={s.replay.all_match ? "ok" : "hold"}>
              {s.replay.all_match ? <CheckCircle2 /> : <XCircle />} Replay {s.replay.all_match ? "bit-identical" : "differs"}
            </Badge>
          </div>
          <div className="grid gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader title="Loss by band" description="Does a better band mean fewer defaults?" />
              <Table
                cols={[["band", "Band"], ["count", "Loans"], ["defaults", "Defaults"], ["default_rate", "Rate"], ["loss_amount", "Loss"]]}
                rows={s.loss_by_band}
              />
            </Card>
            <Card>
              <CardHeader title="Planted problems" description="Which plants the checks caught." />
              <Table cols={[["plant", "Plant"], ["planted", "Planted"], ["caught", "Caught"], ["catch_rate", "Rate"]]} rows={s.plants.rows} />
            </Card>
            <Card className="xl:col-span-2">
              <CardHeader title="Most predictive metrics" description={`Top of ${s.metric_count} by AUC.`} />
              <Table
                cols={[["metric", "Metric"], ["auc", "AUC"], ["ks", "KS"], ["direction", "Direction"], ["n", "n"], ["none_share", "Missing"]]}
                rows={s.top_metrics}
              />
            </Card>
          </div>
        </div>
      )}
    </Page>
  );
}
