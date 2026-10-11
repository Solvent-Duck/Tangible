"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleAlert, Database, Wrench } from "lucide-react";
import { useParams } from "next/navigation";

import { Page, PageHeader } from "@/components/app/page-header";
import { FindingCard } from "@/components/diligence/finding-card";
import { Badge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { Notice, Skeleton } from "@/components/ui/misc";
import { useApi } from "@/lib/api";
import type { Trace } from "@/lib/types";
import { date } from "@/lib/utils";

function pretty(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v, null, 2);
}

export default function TracePage() {
  const { caseId, findingId } = useParams<{ caseId: string; findingId: string }>();
  const api = useApi();
  const q = useQuery({
    queryKey: ["trace", caseId, findingId],
    queryFn: () => api<Trace>(`/api/cases/${caseId}/findings/${findingId}/trace`),
  });
  return (
    <Page>
      <PageHeader
        crumbs={[
          { href: "/app/diligence", label: "Due diligence" },
          { href: `/app/diligence/${caseId}`, label: "Case" },
        ]}
        title="Trace to source"
        description="The finding, the tool calls it cites, and the raw evidence lines behind them."
      />
      {q.error && <Notice tone="hold">{(q.error as Error).message}</Notice>}
      {q.isLoading && <Skeleton className="h-96" />}
      {q.data && (
        <ol className="relative space-y-6 border-l border-line pl-6">
          <li>
            <span className="absolute -left-[5px] mt-5 size-2.5 rounded-full bg-brand" aria-hidden />
            <FindingCard f={q.data.finding} caseId={null} />
          </li>
          <li>
            <span className="absolute -left-[5px] mt-5 size-2.5 rounded-full bg-brand" aria-hidden />
            <Card>
              <CardHeader
                title={
                  <span className="flex items-center gap-2">
                    <Wrench className="size-4 text-faint" /> Tool calls
                  </span>
                }
                description={`${q.data.tool_calls.length} cited`}
              />
              <div className="divide-y divide-line border-t border-line">
                {q.data.tool_calls.length === 0 && <p className="px-5 py-4 text-[13px] text-muted">This finding comes from a deterministic check, not a tool call.</p>}
                {q.data.tool_calls.map((t) => (
                  <details key={t.id} className="group px-5 py-3">
                    <summary className="flex cursor-pointer items-center gap-3 text-[13.5px]">
                      <Badge tone="outline" className="font-mono">
                        {t.tool} v{t.version}
                      </Badge>
                      <span className="flex-1 text-[12px] text-faint">
                        {date(t.started_at, true)}
                        {t.duration_ms != null && ` · ${t.duration_ms} ms`}
                      </span>
                      {t.error && <Badge tone="hold">error</Badge>}
                    </summary>
                    <div className="mt-3 grid gap-3 lg:grid-cols-2">
                      <div>
                        <p className="mb-1 text-[11.5px] font-medium text-faint">Arguments</p>
                        <pre className="max-h-80 overflow-auto rounded-lg bg-surface-2 p-3 font-mono text-[11.5px] leading-relaxed">{pretty(t.args)}</pre>
                      </div>
                      <div>
                        <p className="mb-1 text-[11.5px] font-medium text-faint">Result</p>
                        <pre className="max-h-80 overflow-auto rounded-lg bg-surface-2 p-3 font-mono text-[11.5px] leading-relaxed">{t.error ?? pretty(t.result)}</pre>
                      </div>
                    </div>
                  </details>
                ))}
              </div>
            </Card>
          </li>
          <li>
            <span className="absolute -left-[5px] mt-5 size-2.5 rounded-full bg-brand" aria-hidden />
            <Card>
              <CardHeader
                title={
                  <span className="flex items-center gap-2">
                    <Database className="size-4 text-faint" /> Raw evidence
                  </span>
                }
                description="Each reference resolved to the exact line in the stored, write-once source file."
              />
              <div className="divide-y divide-line border-t border-line">
                {q.data.evidence.length === 0 && <p className="px-5 py-4 text-[13px] text-muted">No evidence references.</p>}
                {q.data.evidence.map((e) => (
                  <div key={e.ref} className="px-5 py-3">
                    <p className="truncate font-mono text-[11.5px] text-faint" title={e.ref}>
                      {e.ref}
                    </p>
                    {e.error ? (
                      <p className="mt-1 flex items-center gap-1.5 text-[12.5px] text-hold">
                        <CircleAlert className="size-3.5" /> {e.error}
                      </p>
                    ) : (
                      <pre className="mt-1.5 overflow-x-auto rounded-lg bg-surface-2 p-2.5 font-mono text-[12px]">{pretty(e.line)}</pre>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          </li>
        </ol>
      )}
    </Page>
  );
}
