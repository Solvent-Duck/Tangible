import { Check } from "lucide-react";

import { STAGES } from "@/lib/labels";
import type { Stage } from "@/lib/types";
import { cn } from "@/lib/utils";

export function StageStepper({ stage, muted }: { stage: Stage; muted?: boolean }) {
  const at = STAGES.findIndex((s) => s.id === stage);
  return (
    <ol className="grid grid-cols-7 gap-1.5" aria-label="Progress">
      {STAGES.map((s, i) => {
        const done = i < at || (stage === "signed" && i === at);
        const current = i === at && !done;
        return (
          <li key={s.id} className="min-w-0" aria-current={current ? "step" : undefined}>
            <div
              className={cn(
                "h-1.5 rounded-full",
                muted ? "bg-surface-3" : done ? "bg-brand" : current ? "bg-brand/50" : "bg-surface-3",
              )}
            />
            <p
              className={cn(
                "mt-2 hidden truncate text-[11.5px] sm:flex sm:items-center sm:gap-1",
                current ? "font-medium text-ink" : "text-faint",
              )}
            >
              {done && <Check className="size-3 text-brand" />}
              {s.short}
            </p>
          </li>
        );
      })}
    </ol>
  );
}
