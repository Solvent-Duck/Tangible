import {
  ArrowRight,
  BadgeCheck,
  Banknote,
  BookOpenCheck,
  Boxes,
  Building2,
  Clock3,
  FileSearch,
  Globe2,
  HeartHandshake,
  Link2,
  Lock,
  Mail,
  Megaphone,
  PenLine,
  Plug,
  Scale,
  ScanSearch,
  ShieldCheck,
  Star,
  Store,
} from "lucide-react";
import Link from "next/link";

import { Logo } from "@/components/logo";

export default function Landing() {
  return (
    <div className="min-h-dvh bg-bg">
      <Nav />
      <main>
        <Hero />
        <ProofStrip />
        <HowItWorks />
        <WhatWeLookAt />
        <Principles />
        <ForBusinesses />
        <ClosingCta />
      </main>
      <Footer />
    </div>
  );
}

function Nav() {
  return (
    <header className="sticky top-0 z-30 border-b border-line/70 bg-bg/80 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
        <Link href="/" aria-label="Tangible home">
          <Logo />
        </Link>
        <nav className="hidden items-center gap-7 text-[14px] text-muted md:flex" aria-label="Sections">
          <a href="#how" className="hover:text-ink">How it works</a>
          <a href="#signals" className="hover:text-ink">What we look at</a>
          <a href="#principles" className="hover:text-ink">Principles</a>
          <a href="#businesses" className="hover:text-ink">For businesses</a>
        </nav>
        <div className="flex items-center gap-2">
          <Link href="/sign-in" className="rounded-lg px-3 py-2 text-[14px] font-medium text-muted hover:text-ink">
            Sign in
          </Link>
          <a
            href="mailto:hello@tangible.test?subject=Tangible%20demo"
            className="hidden rounded-lg bg-brand px-4 py-2 text-[14px] font-medium text-brand-fg shadow-card hover:bg-brand-hover sm:inline-flex"
          >
            Request a demo
          </a>
        </div>
      </div>
    </header>
  );
}

function Hero() {
  return (
    <section className="relative overflow-hidden">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.55] [background-image:radial-gradient(var(--border)_1px,transparent_1px)] [background-size:22px_22px] [mask-image:radial-gradient(ellipse_at_top,black_35%,transparent_75%)]"
      />
      <div className="relative mx-auto grid max-w-6xl gap-12 px-4 pt-16 pb-20 sm:px-6 lg:grid-cols-[1.05fr_1fr] lg:items-center lg:pt-24 lg:pb-28">
        <div>
          <p className="inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-1 text-[12.5px] text-muted shadow-card">
            <span className="size-1.5 rounded-full bg-brand" />
            Operational due diligence for small-business lenders
          </p>
          <h1 className="mt-6 font-display text-[44px] leading-[1.02] tracking-tight text-ink sm:text-[60px]">
            Underwrite the business,
            <br />
            <em className="text-brand">not just the bank statement.</em>
          </h1>
          <p className="mt-6 max-w-xl text-[17px] leading-relaxed text-muted">
            Invite a borrower, and Tangible connects their bank, books, sales and inventory, reads what
            the public web says about them, and hands your underwriter a signed report where every
            number traces back to the raw line it came from.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Link
              href="/sign-in"
              className="inline-flex h-11 items-center gap-2 rounded-lg bg-brand px-5 text-[15px] font-medium text-brand-fg shadow-card hover:bg-brand-hover"
            >
              Open the workbench <ArrowRight className="size-4" />
            </Link>
            <a
              href="#how"
              className="inline-flex h-11 items-center gap-2 rounded-lg border border-line bg-surface px-5 text-[15px] font-medium text-ink shadow-card hover:bg-surface-2"
            >
              See how it works
            </a>
          </div>
          <p className="mt-6 flex items-center gap-2 text-[13px] text-faint">
            <Lock className="size-3.5" /> Read-only connections. Append-only records. Replayable decisions.
          </p>
        </div>
        <HeroVisual />
      </div>
    </section>
  );
}

function HeroVisual() {
  return (
    <div className="relative mx-auto w-full max-w-[520px]">
      <div className="absolute -inset-6 -z-10 rounded-[28px] bg-gradient-to-br from-brand-soft via-transparent to-transparent blur-2xl" />
      <div className="rounded-2xl border border-line bg-surface p-5 shadow-pop">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-[12px] text-faint">Case · as of Oct 3</p>
            <p className="text-[16px] font-semibold tracking-tight">Harbor &amp; Pine Provisions</p>
          </div>
          <span className="rounded-full bg-review-soft px-2.5 py-0.5 text-[12px] font-medium text-review">In review</span>
        </div>
        <ol className="mt-4 grid grid-cols-4 gap-1.5" aria-hidden>
          {["Plan", "Investigate", "Review", "Sign"].map((s, i) => (
            <li key={s} className="space-y-1.5">
              <div className={`h-1 rounded-full ${i < 3 ? "bg-brand" : "bg-surface-3"}`} />
              <p className={`text-[11px] ${i === 2 ? "font-medium text-ink" : "text-faint"}`}>{s}</p>
            </li>
          ))}
        </ol>
        <div className="mt-5 space-y-2.5">
          <MockFinding
            tone="hold"
            title="Payouts of $18,420 never reached the business account"
            meta="payouts_vs_deposits v2 · verified"
          />
          <MockFinding
            tone="review"
            title="Recurring $1,250 debit to an undisclosed lender since June"
            meta="unexplained_recurring_debit v1 · verified"
          />
          <MockFinding tone="info" title="Repeat customers: 41% of orders over 90 days" meta="repeat_customer_share v1" />
        </div>
        <div className="mt-4 truncate rounded-lg border border-dashed border-line-strong bg-surface-2 px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-muted">
          <span className="text-faint">trace →</span> bank_txn #4f1a… · line 1,284 · plaid pull 2026-10-02
        </div>
      </div>
      <div className="absolute -bottom-10 -left-6 hidden rounded-xl border border-line bg-surface px-4 py-3 shadow-pop sm:block">
        <p className="text-[11.5px] text-faint">Policy 0.3.0</p>
        <p className="flex items-center gap-2 text-[14px] font-semibold">
          <span className="grid size-6 place-items-center rounded-md bg-brand text-[12px] text-brand-fg">B</span>
          Suggested limit $60,000
        </p>
      </div>
    </div>
  );
}

function MockFinding({ tone, title, meta }: { tone: "hold" | "review" | "info"; title: string; meta: string }) {
  const tones = {
    hold: "bg-hold-soft text-hold",
    review: "bg-review-soft text-review",
    info: "bg-info-soft text-info",
  };
  return (
    <div className="flex items-start gap-3 rounded-lg border border-line px-3 py-2.5">
      <span className={`mt-0.5 rounded px-1.5 py-0.5 text-[10.5px] font-semibold tracking-wide uppercase ${tones[tone]}`}>
        {tone}
      </span>
      <div className="min-w-0">
        <p className="text-[13px] leading-snug font-medium">{title}</p>
        <p className="mt-0.5 font-mono text-[11px] text-faint">{meta}</p>
      </div>
    </div>
  );
}

function ProofStrip() {
  const items = [
    ["23", "versioned metrics"],
    ["17", "integrity checks"],
    ["100%", "of numbers traceable to a raw line"],
    ["0", "numbers set by the model"],
  ];
  return (
    <section className="border-y border-line bg-surface">
      <dl className="mx-auto grid max-w-6xl grid-cols-2 gap-y-6 px-4 py-8 sm:px-6 md:grid-cols-4">
        {items.map(([n, l]) => (
          <div key={l} className="px-2">
            <dt className="sr-only">{l}</dt>
            <dd>
              <span className="font-display text-[34px] leading-none text-ink">{n}</span>
              <span className="mt-1 block text-[13px] text-muted">{l}</span>
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function SectionHead({ eyebrow, title, children }: { eyebrow: string; title: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="max-w-2xl">
      <p className="text-[12.5px] font-semibold tracking-[0.12em] text-brand uppercase">{eyebrow}</p>
      <h2 className="mt-3 font-display text-[34px] leading-[1.1] tracking-tight sm:text-[42px]">{title}</h2>
      {children && <p className="mt-4 text-[16px] leading-relaxed text-muted">{children}</p>}
    </div>
  );
}

function HowItWorks() {
  const steps = [
    {
      icon: Mail,
      title: "Invite",
      body: "Pick what you need beyond standard financials: inventory, digital presence, reputation, longevity. The borrower gets one secure link.",
    },
    {
      icon: Plug,
      title: "Connect",
      body: "The business creates an account and connects its bank, accounting and sales systems read-only, uploads what can't be connected, and submits.",
    },
    {
      icon: ScanSearch,
      title: "Investigate",
      body: "An agent plans the review, your analyst approves it, and it runs versioned checks and metrics. Every finding cites the tool calls behind it.",
    },
    {
      icon: PenLine,
      title: "Sign",
      body: "The analyst accepts or rejects each finding. Policy turns the result into a band and a limit, frozen into a report you can replay later.",
    },
  ];
  return (
    <section id="how" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-24 sm:px-6">
      <SectionHead eyebrow="How it works" title="From invite to signed report, without the PDF chase.">
        One flow for the lender, the borrower and the analyst. Nobody emails a spreadsheet.
      </SectionHead>
      <ol className="mt-12 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {steps.map((s, i) => (
          <li key={s.title} className="relative rounded-2xl border border-line bg-surface p-6 shadow-card">
            <div className="flex items-center justify-between">
              <span className="grid size-10 place-items-center rounded-xl bg-brand-soft text-brand-ink">
                <s.icon className="size-5" />
              </span>
              <span className="font-mono text-[12px] text-faint">0{i + 1}</span>
            </div>
            <h3 className="mt-5 text-[17px] font-semibold tracking-tight">{s.title}</h3>
            <p className="mt-2 text-[14px] leading-relaxed text-muted">{s.body}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}

function WhatWeLookAt() {
  const financial = [
    { icon: Banknote, label: "Bank accounts", note: "Transactions, balances, deposits vs payouts" },
    { icon: BookOpenCheck, label: "Books", note: "Invoices, bills, payables discipline" },
    { icon: Store, label: "Sales & processors", note: "Orders, refunds, payouts, coverage" },
    { icon: Boxes, label: "Inventory", note: "Stock at cost, ageing, days of inventory" },
  ];
  const intangible = [
    { icon: Globe2, label: "Digital presence", note: "Website, listings, social profiles" },
    { icon: Star, label: "Reputation", note: "Ratings, review volume, owner responses" },
    { icon: HeartHandshake, label: "Loyalty", note: "Repeat customers, what reviews say" },
    { icon: Clock3, label: "Longevity", note: "Entity status, years trading, domain age" },
    { icon: Megaphone, label: "Community", note: "Local press, awards, associations" },
  ];
  return (
    <section id="signals" className="scroll-mt-20 border-y border-line bg-surface">
      <div className="mx-auto max-w-6xl px-4 py-24 sm:px-6">
        <SectionHead eyebrow="What we look at" title={<>The numbers, and the things numbers miss.</>}>
          Standard financials are the base of every case. On top, each lender chooses which
          intangibles matter for their book, invite by invite.
        </SectionHead>
        <div className="mt-12 grid gap-6 lg:grid-cols-2">
          <SignalPanel title="Financials" tag="Scored today" items={financial} />
          <SignalPanel
            title="Intangibles"
            tag="Collected now · scoring rolling out"
            items={intangible}
            note="Intangibles can lift a band by one notch at most, never above B, and only from public signals matched to the business with analyst confirmation."
          />
        </div>
      </div>
    </section>
  );
}

function SignalPanel({
  title,
  tag,
  items,
  note,
}: {
  title: string;
  tag: string;
  items: { icon: typeof Banknote; label: string; note: string }[];
  note?: string;
}) {
  return (
    <div className="rounded-2xl border border-line bg-bg p-6">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-[18px] font-semibold tracking-tight">{title}</h3>
        <span className="rounded-full border border-line bg-surface px-2.5 py-0.5 text-[12px] text-muted">{tag}</span>
      </div>
      <ul className="mt-5 divide-y divide-line">
        {items.map((it) => (
          <li key={it.label} className="flex items-center gap-4 py-3.5">
            <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-surface text-brand-ink shadow-card">
              <it.icon className="size-[18px]" />
            </span>
            <div>
              <p className="text-[14.5px] font-medium">{it.label}</p>
              <p className="text-[13px] text-muted">{it.note}</p>
            </div>
          </li>
        ))}
      </ul>
      {note && <p className="mt-4 text-[12.5px] leading-relaxed text-faint">{note}</p>}
    </div>
  );
}

function Principles() {
  const ps = [
    {
      icon: FileSearch,
      title: "The agent investigates",
      body: "It plans, queries and reads. It never sets a number, a band or a limit, and a finding whose numbers can't be found in its cited results is flagged unverified.",
    },
    {
      icon: Scale,
      title: "Tools compute",
      body: "Metrics and checks are pure, versioned functions. Change a formula and you get a new version; a released one is never edited.",
    },
    {
      icon: ShieldCheck,
      title: "Policy decides",
      body: "A versioned policy turns reviewed findings into knock-outs, a band and a limit. Signed assessments are frozen and can be replayed bit for bit.",
    },
  ];
  return (
    <section id="principles" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-24 sm:px-6">
      <SectionHead eyebrow="Principles" title="Built so a credit committee can check the work.">
        As-of dates are honoured: a backtest only ever sees what was known on the day. Ledger rows
        and decisions are append-only, so the record is the record.
      </SectionHead>
      <div className="mt-12 grid gap-4 md:grid-cols-3">
        {ps.map((p) => (
          <div key={p.title} className="rounded-2xl border border-line bg-surface p-6 shadow-card">
            <p.icon className="size-5 text-brand" />
            <h3 className="mt-4 text-[17px] font-semibold tracking-tight">{p.title}</h3>
            <p className="mt-2 text-[14px] leading-relaxed text-muted">{p.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function ForBusinesses() {
  const points = [
    { icon: Link2, text: "One link from your lender. No forms to print, no statements to scan." },
    { icon: Lock, text: "Read-only connections. You see exactly what is shared before you connect." },
    { icon: BadgeCheck, text: "Show more than your bank balance: stock, reviews, years of trading." },
  ];
  return (
    <section id="businesses" className="scroll-mt-20 border-y border-line bg-surface">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-20 sm:px-6 lg:grid-cols-2 lg:items-center">
        <div>
          <p className="text-[12.5px] font-semibold tracking-[0.12em] text-brand uppercase">For businesses</p>
          <h2 className="mt-3 font-display text-[34px] leading-[1.1] tracking-tight sm:text-[40px]">
            Invited by your lender?
          </h2>
          <p className="mt-4 text-[16px] leading-relaxed text-muted">
            Open the link in your email. You&apos;ll create an account, connect your systems in a few
            minutes and submit when you&apos;re ready.
          </p>
        </div>
        <ul className="space-y-3">
          {points.map((p) => (
            <li key={p.text} className="flex items-start gap-4 rounded-xl border border-line bg-bg p-4">
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand-ink">
                <p.icon className="size-[18px]" />
              </span>
              <p className="pt-1.5 text-[14.5px] leading-relaxed">{p.text}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function ClosingCta() {
  return (
    <section className="mx-auto max-w-6xl px-4 py-24 sm:px-6">
      <div className="relative overflow-hidden rounded-3xl bg-brand px-6 py-14 text-center text-brand-fg sm:px-12">
        <div
          aria-hidden
          className="absolute inset-0 opacity-20 [background-image:radial-gradient(currentColor_1px,transparent_1px)] [background-size:18px_18px]"
        />
        <div className="relative">
          <Building2 className="mx-auto size-7 opacity-80" />
          <h2 className="mt-4 font-display text-[34px] leading-tight sm:text-[44px]">See a case end to end.</h2>
          <p className="mx-auto mt-3 max-w-lg text-[16px] opacity-85">
            Send an invite, watch the borrower connect, and read the signed report, in one sitting.
          </p>
          <div className="mt-8 flex flex-wrap justify-center gap-3">
            <Link
              href="/sign-in"
              className="inline-flex h-11 items-center gap-2 rounded-lg bg-surface px-5 text-[15px] font-medium text-ink hover:bg-surface-2"
            >
              Open the workbench <ArrowRight className="size-4" />
            </Link>
            <a
              href="mailto:hello@tangible.test?subject=Tangible%20demo"
              className="inline-flex h-11 items-center rounded-lg border border-current/30 px-5 text-[15px] font-medium hover:bg-white/10"
            >
              Request a demo
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-4 px-4 py-8 text-[13px] text-faint sm:flex-row sm:items-center sm:px-6">
        <Logo className="opacity-80" />
        <p>Non-production build. Emails go to an in-app outbox; connections are simulated.</p>
      </div>
    </footer>
  );
}
