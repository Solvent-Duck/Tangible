import { GROUP_ICONS, KIND_ICONS } from "@/components/app/group-icons";
import type { CatalogItem } from "@/lib/types";

const LABELS: Record<string, string> = {
  financials: "Standard financials",
  inventory: "Inventory",
  presence: "Digital presence",
  reputation: "Reputation & loyalty",
  longevity: "Longevity & entity",
  community: "Community",
  custom: "Also requested",
};

export function RequestedList({ items }: { items: CatalogItem[] }) {
  const groups: { id: string; items: CatalogItem[] }[] = [];
  for (const i of items) {
    const g = groups.find((x) => x.id === i.group);
    if (g) g.items.push(i);
    else groups.push({ id: i.group, items: [i] });
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {groups.map((g) => {
        const Icon = GROUP_ICONS[g.id];
        return (
          <div key={g.id} className="rounded-xl border border-line bg-surface p-4">
            <p className="flex items-center gap-2 text-[13px] font-semibold">
              {Icon && <Icon className="size-4 text-brand" />}
              {LABELS[g.id] ?? g.id}
            </p>
            <ul className="mt-2.5 space-y-1.5">
              {g.items.map((i) => {
                const K = KIND_ICONS[i.kind];
                return (
                  <li key={i.id} className="flex items-start gap-2 text-[13px] text-muted">
                    {K && <K className="mt-0.5 size-3.5 shrink-0 text-faint" />}
                    <span>{i.label}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </div>
  );
}
