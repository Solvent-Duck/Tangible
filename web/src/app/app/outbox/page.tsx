"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, FlaskConical, Inbox, Mail } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Page, PageHeader } from "@/components/app/page-header";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Empty, Notice, Skeleton } from "@/components/ui/misc";
import { useApi } from "@/lib/api";
import type { Email } from "@/lib/types";
import { ago, cn, date } from "@/lib/utils";

export default function OutboxPage() {
  const api = useApi();
  const [sel, setSel] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["outbox"], queryFn: () => api<Email[]>("/api/outbox") });
  const current = sel ?? list.data?.[0]?.id ?? null;
  const email = useQuery({
    queryKey: ["email", current],
    queryFn: () => api<Email>(`/api/outbox/${current}`),
    enabled: !!current,
  });

  return (
    <Page wide>
      <PageHeader title="Outbox" description="Every email Tangible has sent on your behalf." />
      <Notice tone="review" icon={<FlaskConical />} className="mb-6">
        <strong>Non-production.</strong> Emails are recorded here instead of being delivered (or sent to a local
        Mailpit when <code>TANGIBLE_SMTP_URL</code> is set). Copy an invite link from here to test the business side.
      </Notice>
      {list.error && <Notice tone="hold">{(list.error as Error).message}</Notice>}
      {list.isLoading && <Skeleton className="h-96 rounded-xl" />}
      {list.data && list.data.length === 0 && (
        <Card>
          <Empty icon={<Inbox />} title="No emails yet">
            Invites you send appear here.
          </Empty>
        </Card>
      )}
      {list.data && list.data.length > 0 && (
        <Card className="grid overflow-hidden lg:grid-cols-[360px_1fr]">
          <ul className="max-h-[70vh] divide-y divide-line overflow-y-auto border-b border-line lg:border-r lg:border-b-0">
            {list.data.map((e) => (
              <li key={e.id}>
                <button
                  onClick={() => setSel(e.id)}
                  className={cn("flex w-full gap-3 px-4 py-3 text-left hover:bg-surface-2", current === e.id && "bg-brand-soft/60")}
                  aria-current={current === e.id}
                >
                  <Mail className="mt-0.5 size-4 shrink-0 text-faint" />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center justify-between gap-2">
                      <span className="truncate text-[13.5px] font-medium">{e.to_addr}</span>
                      <span className="shrink-0 text-[11.5px] text-faint">{ago(e.created_at)}</span>
                    </span>
                    <span className="block truncate text-[12.5px] text-muted">{e.subject}</span>
                    {e.error && (
                      <span className="mt-1 flex items-center gap-1 text-[11.5px] text-hold">
                        <AlertTriangle className="size-3" /> delivery failed
                      </span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <div className="min-w-0">
            {email.data ? (
              <div className="flex h-full flex-col">
                <div className="border-b border-line px-5 py-4">
                  <p className="text-[15px] font-semibold">{email.data.subject}</p>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[12.5px] text-muted">
                    <span>To {email.data.to_addr}</span>
                    <span>·</span>
                    <span>{date(email.data.created_at, true)}</span>
                    <Badge tone={email.data.transport === "smtp" ? "brand" : "neutral"}>{email.data.transport}</Badge>
                    {email.data.invite_id && (
                      <Link href={`/app/invites/${email.data.invite_id}`} className="text-brand hover:underline">
                        View invite
                      </Link>
                    )}
                  </div>
                </div>
                <iframe
                  title="Email body"
                  sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
                  srcDoc={email.data.body_html?.replace("<head>", '<head><base target="_blank">')}
                  className="min-h-[560px] w-full flex-1 bg-[#f4f4f2]"
                />
              </div>
            ) : (
              <div className="p-6">
                <Skeleton className="h-96" />
              </div>
            )}
          </div>
        </Card>
      )}
    </Page>
  );
}
