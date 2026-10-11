"use client";

import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";

export function CopyLink({ link }: { link: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2 rounded-lg border border-line bg-surface-2 p-1.5 pl-3">
      <code className="min-w-0 flex-1 truncate font-mono text-[12px] text-muted">{link}</code>
      <Button
        type="button"
        size="sm"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(link);
            setCopied(true);
            toast.success("Link copied");
            setTimeout(() => setCopied(false), 1600);
          } catch {
            toast.error("Copy failed; select the link instead");
          }
        }}
      >
        {copied ? <Check /> : <Copy />} {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}
