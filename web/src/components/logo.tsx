import { cn } from "@/lib/utils";

export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={cn("size-7", className)} aria-hidden>
      <rect width="32" height="32" rx="8" fill="var(--brand)" />
      <path d="M9 10.5h14" stroke="var(--brand-fg)" strokeWidth="2.6" strokeLinecap="round" />
      <path d="M16 10.5v12" stroke="var(--brand-fg)" strokeWidth="2.6" strokeLinecap="round" />
      <circle cx="22.5" cy="21.5" r="2.2" fill="var(--brand-fg)" opacity=".75" />
    </svg>
  );
}

export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <LogoMark />
      <span className="text-[17px] font-semibold tracking-tight text-ink">Tangible</span>
    </span>
  );
}
