import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const ROLE_LABELS: Record<string, string> = {
  analyst: "Analyst",
  credit_admin: "Credit admin",
  lender_underwriter: "Lender underwriter",
  borrower: "Business owner",
};

export function roleLabel(role: string | null | undefined): string {
  return (role && ROLE_LABELS[role]) || humanize(role);
}

/** `raises_risk` -> `Raises risk`. */
export function humanize(v: string | null | undefined): string {
  const s = String(v ?? "").replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Numbers on screen: dollars to the cent, everything else to two decimals (a whole number stays
// whole). Same rules as the API's tangible.numfmt, so a figure reads the same everywhere.

function toNumber(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

const TWO = { minimumFractionDigits: 2, maximumFractionDigits: 2 };

/** "$75,000.00" from "75000"; "—" when absent. */
export function money(v: string | number | null | undefined): string {
  const n = toNumber(v);
  if (n === null) return v === null || v === undefined || v === "" ? "—" : String(v);
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", ...TWO });
}

/** "0.41" from "0.41237", "1,234.50" from 1234.5, "183" from "183"; "—" when absent. */
export function num(v: string | number | null | undefined): string {
  const n = toNumber(v);
  if (n === null) return v === null || v === undefined || v === "" ? "—" : String(v);
  if (Number.isInteger(n)) return n.toLocaleString("en-US");
  const s = n.toLocaleString("en-US", TWO);
  return s === "-0.00" ? "0.00" : s;
}

/** A share as a percent to two decimals: "87.31%" from 0.873149, "25%" from 0.25. */
export function pct(v: string | number | null | undefined): string {
  const n = toNumber(v);
  if (n === null) return v === null || v === undefined || v === "" ? "—" : String(v);
  const p = n * 100;
  if (p > 0 && p < 0.005) return "<0.01%";
  return `${num(Math.round(p * 100) / 100)}%`;
}

/** `num` for a value that may or may not be a number (policy "saw" and "threshold" fields). */
export function numeric(v: unknown): string {
  if (typeof v === "number") return num(v);
  if (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v.trim())) return num(v);
  return v === undefined || v === null ? "–" : String(v);
}

export function date(v: string | null | undefined, withTime = false): string {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    ...(withTime ? { hour: "numeric", minute: "2-digit" } : {}),
  });
}

export function ago(v: string | null | undefined): string {
  if (!v) return "";
  const then = new Date(v).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.round((Date.now() - then) / 1000);
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  if (d < 30) return `${d}d ago`;
  return date(v);
}

export function shortId(v: string | null | undefined): string {
  return String(v ?? "").slice(0, 8);
}

export function initials(name: string | null | undefined): string {
  const parts = String(name ?? "?").replace(/[^A-Za-z0-9 ]/g, " ").trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "?") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}
