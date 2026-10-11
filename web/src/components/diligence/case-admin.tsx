"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, UserPlus } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/input";
import { ApiError, useApi } from "@/lib/api";
import { cn } from "@/lib/utils";

type Lookups = { borrowers: { id: string; name: string }[]; analysts: { subject: string; name: string }[] };
type Tab = "new" | "assign" | "reset";

function localNow(): string {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

/** Credit-admin tools: open a case for an existing borrower, assign one, reset a stuck run. */
export function CaseAdmin() {
  const api = useApi();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("new");
  const lookups = useQuery({ queryKey: ["lookups"], queryFn: () => api<Lookups>("/api/cases/lookups"), enabled: open });
  const [f, setF] = useState({ borrower_id: "", as_of: localNow(), loan_amount: "", analyst: "", case_id: "", reason: "" });
  const m = useMutation({
    mutationFn: async () => {
      if (tab === "new") {
        return api("/api/cases", {
          json: {
            borrower_id: f.borrower_id,
            as_of: new Date(f.as_of).toISOString(),
            loan_amount: f.loan_amount.replace(/[$,\s]/g, "") || null,
            assigned_analyst: f.analyst || null,
          },
        });
      }
      if (tab === "assign") return api(`/api/cases/${f.case_id.trim()}/assign`, { json: { analyst: f.analyst } });
      return api(`/api/cases/${f.case_id.trim()}/reset-investigation`, { json: { reason: f.reason } });
    },
    onSuccess: () => {
      toast.success(tab === "new" ? "Case opened" : tab === "assign" ? "Analyst assigned" : "Investigation reset");
      qc.invalidateQueries({ queryKey: ["cases"] });
      setOpen(false);
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Something went wrong"),
  });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  const ready =
    tab === "new" ? !!f.borrower_id && !!f.as_of : tab === "assign" ? !!f.case_id && !!f.analyst : !!f.case_id && !!f.reason.trim();

  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>
        <Plus /> Case tools
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent title="Case tools" description="Cases for invited businesses open from the invite. Use these for anything else.">
          <div className="mb-4 flex gap-1 rounded-lg bg-surface-2 p-1" role="tablist">
            {(
              [
                ["new", "New case", Plus],
                ["assign", "Assign", UserPlus],
                ["reset", "Reset run", RefreshCw],
              ] as const
            ).map(([id, label, Icon]) => (
              <button
                key={id}
                role="tab"
                aria-selected={tab === id}
                onClick={() => setTab(id)}
                className={cn(
                  "flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[13px]",
                  tab === id ? "bg-surface font-medium text-ink shadow-card" : "text-muted",
                )}
              >
                <Icon className="size-3.5" /> {label}
              </button>
            ))}
          </div>
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              m.mutate();
            }}
          >
            {tab === "new" && (
              <>
                <Field label="Borrower" htmlFor="borrower">
                  <Select id="borrower" value={f.borrower_id} onChange={set("borrower_id")} required>
                    <option value="">Choose…</option>
                    {lookups.data?.borrowers.map((b) => (
                      <option key={b.id} value={b.id}>
                        {b.name}
                      </option>
                    ))}
                  </Select>
                </Field>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="As of" htmlFor="asof">
                    <Input id="asof" type="datetime-local" value={f.as_of} onChange={set("as_of")} required />
                  </Field>
                  <Field label="Loan" hint="optional" htmlFor="loan">
                    <Input id="loan" inputMode="decimal" value={f.loan_amount} onChange={set("loan_amount")} placeholder="$50,000" />
                  </Field>
                </div>
              </>
            )}
            {tab !== "new" && (
              <Field label="Case id" htmlFor="caseid">
                <Input id="caseid" value={f.case_id} onChange={set("case_id")} className="font-mono text-[12px]" spellCheck={false} required />
              </Field>
            )}
            {tab !== "reset" && (
              <Field label="Analyst" hint={tab === "new" ? "optional" : undefined} htmlFor="analyst">
                <Select id="analyst" value={f.analyst} onChange={set("analyst")}>
                  <option value="">{tab === "new" ? "Assign later" : "Choose…"}</option>
                  {lookups.data?.analysts.map((a) => (
                    <option key={a.subject} value={a.subject}>
                      {a.name}
                    </option>
                  ))}
                </Select>
              </Field>
            )}
            {tab === "reset" && (
              <Field label="Reason" htmlFor="reason">
                <Input id="reason" value={f.reason} onChange={set("reason")} placeholder="Server restarted during the run" required />
              </Field>
            )}
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button variant="primary" disabled={!ready || m.isPending}>
                {tab === "new" ? "Open case" : tab === "assign" ? "Assign" : "Reset"}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
