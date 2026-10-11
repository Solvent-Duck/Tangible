"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Check, ChevronDown, Ruler, Sparkles } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, useApi } from "@/lib/api";
import type { Finding } from "@/lib/types";
import { cn, date, humanize } from "@/lib/utils";

export const SEV = {
  hold: { label: "Hold", chip: "bg-hold-soft text-hold", bar: "bg-hold" },
  review: { label: "Review", chip: "bg-review-soft text-review", bar: "bg-review" },
  info: { label: "Info", chip: "bg-info-soft text-info", bar: "bg-info" },
} as const;

const STATE_TONE: Record<string, "neutral" | "ok" | "hold" | "review" | "info"> = {
  open: "neutral",
  accepted: "ok",
  rejected: "hold",
  disputed: "review",
  more_requested: "info",
};

export function FindingCard({
  f,
  caseId,
  reviewable,
}: {
  f: Finding;
  caseId: string | null;
  reviewable?: boolean;
}) {
  const sev = SEV[f.severity] ?? SEV.info;
  const [open, setOpen] = useState(false);
  return (
    <article className="relative overflow-hidden rounded-xl border border-line bg-surface shadow-card">
      <span className={cn("absolute inset-y-0 left-0 w-1", sev.bar)} aria-hidden />
      <div className="py-4 pr-5 pl-6">
        <div className="flex items-start gap-3">
          <span className={cn("mt-0.5 rounded px-1.5 py-0.5 text-[10.5px] font-semibold tracking-wide uppercase", sev.chip)}>
            {sev.label}
          </span>
          <div className="min-w-0 flex-1">
            <h4 className="text-[14.5px] leading-snug font-medium">{f.summary}</h4>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <Badge tone="outline" className="font-mono">
                {f.check_key} v{f.check_version}
              </Badge>
              <Badge tone="neutral">{humanize(f.pillar)}</Badge>
              <Badge tone="neutral">{humanize(f.direction)}</Badge>
              {f.origin === "model" ? (
                f.verified ? (
                  <Badge tone="ok">
                    <Check /> verified
                  </Badge>
                ) : (
                  <Badge tone="review">
                    <AlertTriangle /> unverified model finding
                  </Badge>
                )
              ) : (
                <Badge tone="neutral">
                  <Ruler /> {f.origin}
                </Badge>
              )}
              {f.origin === "model" && (
                <Badge tone="brand">
                  <Sparkles /> agent
                </Badge>
              )}
              {f.replaces_id && <Badge tone="info">corrected</Badge>}
              <Badge tone={STATE_TONE[f.review_state] ?? "neutral"}>{humanize(f.review_state)}</Badge>
            </div>
            {f.unmatched_numbers && f.unmatched_numbers.length > 0 && (
              <p className="mt-2 text-[12.5px] text-review">
                Numbers not found in the cited results: {f.unmatched_numbers.join(", ")}
              </p>
            )}
          </div>
          {caseId && (
            <Link
              href={`/app/diligence/${caseId}/trace/${f.id}`}
              className="hidden shrink-0 items-center gap-1 text-[12.5px] font-medium text-brand hover:underline sm:flex"
            >
              Trace <ArrowRight className="size-3.5" />
            </Link>
          )}
        </div>

        {(f.actions.length > 0 || f.corrections.length > 0) && (
          <button
            onClick={() => setOpen(!open)}
            className="mt-3 flex items-center gap-1 text-[12.5px] text-muted hover:text-ink"
            aria-expanded={open}
          >
            <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
            {f.actions.length > 0 && `${f.actions.length} review ${f.actions.length === 1 ? "action" : "actions"}`}
            {f.actions.length > 0 && f.corrections.length > 0 && " · "}
            {f.corrections.length > 0 && `${f.corrections.length} superseded ${f.corrections.length === 1 ? "version" : "versions"}`}
          </button>
        )}
        {open && (
          <div className="mt-2 space-y-2 rounded-lg bg-surface-2 p-3 text-[12.5px]">
            {f.actions.map((a, i) => (
              <p key={i}>
                <span className="font-medium">{a.by}</span> {humanize(a.action).toLowerCase()} — {a.reason}{" "}
                <span className="text-faint">· {date(a.at, true)}</span>
              </p>
            ))}
            {f.corrections.map((x) => (
              <p key={x.id} className="text-muted">
                <span className="line-through">{x.summary}</span>{" "}
                <span className="text-faint">({x.verified ? "verified" : "unverified"})</span>
              </p>
            ))}
          </div>
        )}
        {reviewable && caseId && <ReviewForm caseId={caseId} f={f} />}
      </div>
    </article>
  );
}

function ReviewForm({ caseId, f }: { caseId: string; f: Finding }) {
  const api = useApi();
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const m = useMutation({
    mutationFn: (action: string) =>
      api(`/api/cases/${caseId}/findings/${f.id}/review`, { json: { action, reason } }),
    onSuccess: () => {
      setReason("");
      toast.success("Review recorded");
      qc.invalidateQueries({ queryKey: ["workspace", caseId] });
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Could not record the review"),
  });
  const actions = [
    { id: "accept", label: "Accept", variant: "soft" as const },
    { id: "reject", label: "Reject", variant: "danger" as const },
    { id: "dispute", label: "Dispute", variant: "secondary" as const },
    { id: "request_more", label: "Request more", variant: "ghost" as const },
  ];
  return (
    <form className="mt-4 border-t border-line pt-3" onSubmit={(e) => e.preventDefault()}>
      <label htmlFor={`reason-${f.id}`} className="sr-only">
        Reason
      </label>
      <div className="flex flex-col gap-2 lg:flex-row">
        <Input
          id={`reason-${f.id}`}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Reason (required): why you accept, reject or dispute this"
          className="h-8 text-[13px]"
        />
        <div className="flex shrink-0 flex-wrap gap-1.5">
          {actions.map((a) => (
            <Button
              key={a.id}
              type="button"
              size="sm"
              variant={a.variant}
              disabled={!reason.trim() || m.isPending}
              onClick={() => m.mutate(a.id)}
            >
              {a.label}
            </Button>
          ))}
        </div>
      </div>
    </form>
  );
}
