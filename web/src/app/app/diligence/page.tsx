"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Briefcase, Search, Ship } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Page, PageHeader } from "@/components/app/page-header";
import { CaseAdmin } from "@/components/diligence/case-admin";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Empty, Notice, Skeleton } from "@/components/ui/misc";
import { useApi } from "@/lib/api";
import { CASE_STATUS } from "@/lib/labels";
import { useMembership } from "@/lib/session";
import type { BookExposure, CaseRow } from "@/lib/types";
import { cn, date, money, pct } from "@/lib/utils";

const FILTERS = [
  { id: "active", label: "Active", match: (s: string) => ["open", "planning", "investigating", "review"].includes(s) },
  { id: "review", label: "In review", match: (s: string) => s === "review" },
  { id: "signed", label: "Signed", match: (s: string) => s === "signed" },
  { id: "all", label: "All", match: () => true },
];

const BAND_TONE: Record<string, string> = {
  A: "bg-ok-soft text-ok",
  B: "bg-brand-soft text-brand-ink",
  C: "bg-review-soft text-review",
  D: "bg-hold-soft text-hold",
};

export default function CasesPage() {
  const api = useApi();
  const { membership } = useMembership();
  const analyst = membership?.role === "analyst";
  const q = useQuery({ queryKey: ["cases", membership?.tenant_id], queryFn: () => api<CaseRow[]>("/api/cases") });
  const book = useQuery({
    queryKey: ["exposure", membership?.tenant_id],
    queryFn: () => api<BookExposure[]>("/api/cases/exposure"),
  });
  const [filter, setFilter] = useState(analyst ? "active" : "all");
  const [text, setText] = useState("");
  const f = FILTERS.find((x) => x.id === filter)!;
  const rows = useMemo(
    () =>
      (q.data ?? []).filter(
        (c) => f.match(c.assessment ? "signed" : c.status) && c.borrower.toLowerCase().includes(text.trim().toLowerCase()),
      ),
    [q.data, f, text],
  );

  return (
    <Page wide>
      <PageHeader
        title="Due diligence"
        description={
          analyst
            ? "Cases assigned to you. Plan, investigate, review, sign."
            : "Signed reports for your portfolio. Cases appear here once the analyst signs."
        }
        actions={membership?.role === "credit_admin" && <CaseAdmin />}
      />
      <Card className="overflow-hidden">
        <div className="flex flex-col gap-3 border-b border-line px-5 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap gap-1">
            {FILTERS.map((x) => (
              <button
                key={x.id}
                onClick={() => setFilter(x.id)}
                aria-pressed={filter === x.id}
                className={cn(
                  "rounded-full px-3 py-1 text-[13px]",
                  filter === x.id ? "bg-brand-soft font-medium text-brand-ink" : "text-muted hover:bg-surface-2",
                )}
              >
                {x.label}
                <span className="tabular ml-1.5 text-[11.5px] text-faint">
                  {(q.data ?? []).filter((c) => x.match(c.assessment ? "signed" : c.status)).length}
                </span>
              </button>
            ))}
          </div>
          <div className="relative sm:w-64">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-faint" />
            <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Search borrowers" className="h-8 pl-8 text-[13px]" aria-label="Search borrowers" />
          </div>
        </div>
        {q.error && <Notice tone="hold" className="m-5">{(q.error as Error).message}</Notice>}
        {q.isLoading && (
          <div className="space-y-2 p-5">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        )}
        {q.data && rows.length === 0 && (
          <Empty icon={<Briefcase />} title={q.data.length ? "Nothing matches" : "No cases yet"}>
            {!q.data.length && (analyst ? "Cases assigned to you will appear here." : "Signed reports will appear here.")}
          </Empty>
        )}
        {rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[680px] text-[13.5px]">
              <thead>
                <tr className="text-left text-[12px] text-faint">
                  <th className="px-5 py-2.5 font-medium">Borrower</th>
                  <th className="px-3 py-2.5 font-medium">Status</th>
                  <th className="px-3 py-2.5 font-medium">As of</th>
                  <th className="px-3 py-2.5 font-medium">Analyst</th>
                  <th className="px-3 py-2.5 font-medium">Decision</th>
                  <th className="w-10" />
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.map((c) => {
                  const st = CASE_STATUS[c.status] ?? { label: c.status, tone: "neutral" as const };
                  return (
                    <tr key={c.id} className="group hover:bg-surface-2/60">
                      <td className="px-5 py-3">
                        <Link href={`/app/diligence/${c.id}`} className="font-medium group-hover:text-brand">
                          {c.borrower}
                        </Link>
                        {c.previous_case_id && <span className="ml-2 text-[11.5px] text-faint">re-based</span>}
                      </td>
                      <td className="px-3 py-3">
                        <Badge tone={st.tone}>{st.label}</Badge>
                      </td>
                      <td className="px-3 py-3 text-muted">{date(c.as_of)}</td>
                      <td className="px-3 py-3 text-muted">{c.assigned_analyst_name ?? c.assigned_analyst ?? "—"}</td>
                      <td className="px-3 py-3">
                        {c.assessment ? (
                          <span className="flex items-center gap-2">
                            <span className={cn("grid size-6 place-items-center rounded-md text-[12px] font-bold", BAND_TONE[c.assessment.band] ?? "bg-surface-2 text-muted")}>
                              {c.assessment.band ?? "–"}
                            </span>
                            <span className="tabular text-muted">{c.assessment.limit ? money(c.assessment.limit) : "no limit"}</span>
                          </span>
                        ) : (
                          <span className="text-faint">—</span>
                        )}
                      </td>
                      <td className="px-3 py-3">
                        <Link href={`/app/diligence/${c.id}`} className="text-faint group-hover:text-brand" aria-label={`Open ${c.borrower}`}>
                          <ArrowRight className="size-4" />
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {book.data && book.data.length > 0 && (
        <Card className="mt-6 overflow-hidden">
          <div className="px-5 pt-4 pb-3">
            <h2 className="flex items-center gap-2 text-[15px] font-semibold tracking-tight">
              <Ship className="size-4 text-faint" /> Book exposure to tariff actions
            </h2>
            <p className="mt-0.5 text-[13px] text-muted">
              Borrowers whose stated imports a tariff action covers, at each case&apos;s date. Only enacted or announced
              actions count, by publication date; Tangible does not forecast tariffs.
            </p>
          </div>
          <div className="overflow-x-auto border-t border-line">
            <table className="w-full min-w-[520px] text-[13.5px]">
              <thead>
                <tr className="text-left text-[12px] text-faint">
                  <th className="px-5 py-2 font-medium">Action</th>
                  <th className="px-3 py-2 font-medium">Effective</th>
                  <th className="px-3 py-2 text-right font-medium">Borrowers</th>
                  <th className="px-3 py-2 text-right font-medium">Share of requested</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {book.data.map((a) => (
                  <tr key={a.document_number}>
                    <td className="px-5 py-2.5">
                      <p className="font-medium">{a.document_number}</p>
                      {a.title && <p className="text-[12px] text-faint">{a.title}</p>}
                    </td>
                    <td className="px-3 py-2.5 text-muted">{a.effective_on ?? "—"}</td>
                    <td className="tabular px-3 py-2.5 text-right">{a.n}</td>
                    <td className="tabular px-3 py-2.5 text-right">
                      {a.share != null ? pct(a.share) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </Page>
  );
}
