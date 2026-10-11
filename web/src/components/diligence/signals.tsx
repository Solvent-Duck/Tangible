import { Globe2, Ship } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import type { FeedLine, PublicSignalRow, Systemic } from "@/lib/types";
import { humanize } from "@/lib/utils";

const pct = (v: string | number) => `${Math.round(Number(v) * 100)}%`;

/** Public source results the search agents scored; the match score is entity_match's, not the agent's. */
export function PublicSignals({ rows }: { rows: PublicSignalRow[] }) {
  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <Globe2 className="size-4 text-faint" /> Public signals
            <span className="text-[13px] font-normal text-faint">{rows.length}</span>
          </span>
        }
        description="Found by the search agents. Match scores come from entity_match v1, not the agent; only recorded results reach the metrics."
      />
      <div className="overflow-x-auto border-t border-line">
        <table className="w-full min-w-[560px] text-[13px]">
          <thead>
            <tr className="text-left text-[12px] text-faint">
              <th className="px-5 py-2 font-medium">Source</th>
              <th className="px-3 py-2 font-medium">Result</th>
              <th className="px-3 py-2 text-right font-medium">Match</th>
              <th className="px-3 py-2 font-medium">On file</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((r) => (
              <tr key={`${r.source}-${r.source_ref}`}>
                <td className="px-5 py-2.5 text-muted">{humanize(r.source)}</td>
                <td className="px-3 py-2.5">
                  <p className="font-medium">{r.name}</p>
                  {r.address && <p className="text-[12px] text-faint">{r.address}</p>}
                  {r.rating != null && (
                    <p className="text-[12px] text-faint">
                      {r.rating} stars, {r.review_count} reviews
                      {r.status && r.status !== "OPERATIONAL" && `, ${humanize(r.status.toLowerCase())}`}
                    </p>
                  )}
                  {r.registered_on && <p className="text-[12px] text-faint">domain registered {r.registered_on}</p>}
                </td>
                <td className="tabular px-3 py-2.5 text-right">{Number(r.score).toFixed(2)}</td>
                <td className="px-3 py-2.5">
                  {r.on_file ? (
                    <Badge tone="info">recorded</Badge>
                  ) : Number(r.score) < 0.9 ? (
                    <Badge tone="hold">refused: below 0.9</Badge>
                  ) : (
                    <Badge>not recorded</Badge>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** Stated imports against enacted or announced tariff actions known at the case date. */
export function SystemicExposure({ s }: { s: Systemic }) {
  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <Ship className="size-4 text-faint" /> Systemic exposure
          </span>
        }
        description="Enacted or announced tariff actions known at the case date, against the stated import mix."
      />
      <div className="space-y-3 px-5 pb-5 text-[13px]">
        {!s.stated ? (
          <p>
            The application states no import mix, so tariff exposure is <strong>not evaluated</strong>; the stressed
            limit leaves the limit unchanged.
          </p>
        ) : s.lines.length === 0 ? (
          <p>The applicant states no imported goods.</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full min-w-[480px]">
              <thead>
                <tr className="text-left text-[12px] text-faint">
                  <th className="px-3 py-2 font-medium">Goods (HS chapter)</th>
                  <th className="px-3 py-2 font-medium">From</th>
                  <th className="px-3 py-2 text-right font-medium">Share of COGS</th>
                  <th className="px-3 py-2 text-right font-medium">New duty</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {s.lines.map((l, i) => (
                  <tr key={i}>
                    <td className="px-3 py-2">{l.hs_chapter}</td>
                    <td className="px-3 py-2">{l.country}</td>
                    <td className="tabular px-3 py-2 text-right">{pct(l.share)}</td>
                    <td className="px-3 py-2 text-right">
                      {Number(l.added_duty) > 0 ? (
                        <>
                          <Badge tone="review">+{pct(l.added_duty)}</Badge>
                          <p className="mt-0.5 text-[11.5px] text-faint">{l.actions.join(", ")}</p>
                        </>
                      ) : (
                        <span className="text-faint">none</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {s.actions.map((a) => (
          <p key={a.document_number} className="text-[12.5px] text-muted">
            <strong className="text-ink">{a.document_number}</strong>: {a.title}. Published {a.publication_date},
            effective {a.effective_on}; +{pct(a.additional_duty)} on HS {a.hs_chapters.join(", ")} from{" "}
            {a.countries.join(", ")}.
          </p>
        ))}
        <p className="text-[12px] text-faint">
          The stress assumes none of the duty is passed on. It can only reduce the limit through policy; the band is
          never changed by it.
        </p>
      </div>
    </Card>
  );
}

/** What the agent team is doing right now (display only, from agent/progress.py). */
export function TeamFeed({ lines }: { lines: FeedLine[] }) {
  const verb: Record<string, string> = { blocked: "guardrail ", finding: "finding ", stop: "stopped: " };
  return (
    <ol className="mt-4 space-y-1 rounded-lg bg-surface-2 p-3 font-mono text-[12px]" aria-label="What the agents are doing">
      {lines.map((l, i) => (
        <li key={i} className="flex gap-2">
          <span className="shrink-0 text-brand-ink">{humanize(l.agent)}</span>
          <span className={l.kind === "error" || l.kind === "blocked" ? "text-hold" : "text-muted"}>
            {verb[l.kind] ?? ""}
            {l.text}
            {l.kind === "error" && " (error)"}
          </span>
        </li>
      ))}
    </ol>
  );
}
