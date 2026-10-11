"use client";

import { useMemo, useState } from "react";

import OnboardingPage from "@/app/onboarding/page";
import { ApiError, ApiOverride, type ApiFn, type Options } from "@/lib/api";
import { AuthContext, type AuthState, storedTenant } from "@/lib/auth-context";
import type { CatalogGroup, CatalogItem, CoverageItem, ItemResponse, Me, OnboardingState, Profile } from "@/lib/types";

// A public facsimile of the borrower onboarding (/onboarding) for demos: the same page, with the
// workbench API replaced by an in-browser mock. Nothing is sent or stored; a reload starts over.

const LENDER = "Cascade Community Lending";
const BUSINESS = "Juniper & Rye Bakery LLC";

const GROUPS: CatalogGroup[] = [
  { id: "financials", label: "Standard financials", blurb: "Always requested. Bank, books and sales data are the base of every assessment." },
  { id: "inventory", label: "Inventory", blurb: "Stock on hand, its age and how fast it turns." },
  { id: "presence", label: "Digital presence", blurb: "Where the business shows up online." },
  { id: "reputation", label: "Reputation & loyalty", blurb: "What customers say and whether they come back." },
  { id: "longevity", label: "Longevity & entity", blurb: "Who the business is and how long it has traded." },
  { id: "supply", label: "Supply chain & tariffs", blurb: "What the business imports, so known tariff actions can be stress-tested." },
];

const item = (
  id: string,
  group: string,
  label: string,
  help: string,
  kind: CatalogItem["kind"],
  feeds: CatalogItem["feeds"],
  providers: string[] = [],
  locked = false,
): CatalogItem => ({ id, group, label, help, kind, feeds, locked, providers });

// Catalog rc-2 (src/tangible/onboarding/catalog.py), as a lender would request it.
const ITEMS: CatalogItem[] = [
  item("bank", "financials", "Business bank accounts", "Read-only transactions and balances.", "connector", "ledger", ["plaid", "mx", "finicity"], true),
  item("accounting", "financials", "Accounting system", "Invoices, bills and the general ledger.", "connector", "ledger", ["quickbooks", "xero", "freshbooks", "sage", "netsuite", "wave", "zoho_books"], true),
  item("sales", "financials", "Sales & payment processors", "Orders, refunds and payouts.", "connector", "ledger", ["shopify", "stripe", "square", "amazon", "etsy", "paypal", "woocommerce", "bigcommerce", "clover", "toast", "ebay"], true),
  item("payroll", "financials", "Payroll", "Headcount and wage bill, so payroll can be matched to the bank.", "connector", "ledger", ["gusto", "adp", "rippling", "paychex"]),
  item("bank_statements", "financials", "Bank statements (PDF)", "Fallback for any account that cannot be connected.", "upload", "ledger", [], true),
  item("inventory_system", "inventory", "Inventory system", "Live stock levels and cost.", "connector", "ledger", ["shopify", "quickbooks", "cin7", "lightspeed", "netsuite", "katana"]),
  item("stock_report", "inventory", "Stock report (CSV or spreadsheet)", "Item, quantity on hand and unit cost.", "upload", "ledger"),
  item("website", "presence", "Website", "The business's own site.", "link", "intangibles"),
  item("google_business", "presence", "Google Business profile", "The listing customers find on Maps.", "link", "intangibles"),
  item("customers", "reputation", "Customer & repeat-purchase data", "Comes from the sales connection; shows how many customers come back.", "connector", "ledger", ["shopify", "square", "stripe", "toast", "clover"]),
  item("support_desk", "reputation", "Customer support desk", "Tickets and how quickly complaints are resolved.", "connector", "ledger", ["devrev", "zendesk", "intercom", "freshdesk", "gorgias"]),
  item("entity", "longevity", "Legal name, DBA and state of registration", "Who the business is on paper.", "field", "intangibles"),
  item("year_founded", "longevity", "Year founded", "When the business started trading.", "field", "intangibles"),
  item("import_mix", "supply", "Imported goods", "Product (HS chapter), country of origin and share of cost of goods.", "field", "systemic"),
];

type Store = { profile: Profile | null; responses: Record<string, ItemResponse[]>; submittedAt: string | null };

function coverage(s: Store): CoverageItem[] {
  // Same rules as onboarding.services._coverage.
  return ITEMS.map((i) => {
    const responses = s.responses[i.id] ?? [];
    let done = responses.length > 0;
    const p = s.profile;
    if (!done && p) {
      if (i.id === "entity") done = !!(p.legal_name && p.state);
      else if (i.id === "year_founded") done = p.year_founded !== null;
      else if (i.id === "website") done = !!p.website;
      else if (i.id === "import_mix") done = p.import_mix !== null;
    }
    return { ...i, done, responses };
  });
}

function state(s: Store): OnboardingState {
  const items = coverage(s);
  const missing: string[] = [];
  if (!s.profile?.legal_name) missing.push("business profile");
  if (!items.some((i) => i.done && (i.id === "bank" || i.id === "bank_statements")))
    missing.push("bank connection or bank statements");
  return {
    invite_id: "demo",
    lender_name: LENDER,
    business_name: BUSINESS,
    loan_amount: "60000.00",
    message: "Hi Maya, connecting your accounts here replaces the statements we'd otherwise ask you to email.",
    profile: s.profile,
    items,
    groups: GROUPS,
    submitted_at: s.submittedAt,
    can_submit: missing.length === 0,
    missing_for_submit: missing,
  };
}

const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

function mockApi(s: Store): ApiFn {
  const now = () => new Date().toISOString();
  const add = (id: string, kind: string, detail: Record<string, unknown>) => {
    s.responses[id] = [...(s.responses[id] ?? []), { kind, detail, at: now() }];
  };
  const handle = (path: string, opts: Options): unknown => {
    const body = (opts.json ?? {}) as Record<string, unknown>;
    if (path === "/api/me") {
      const me: Me = {
        subject: "demo-business",
        email: "maya@juniperandrye.example",
        auth_mode: "dev",
        // The tenant left as stored, so a staff session in this browser keeps its choice.
        memberships: [
          { tenant_id: storedTenant() ?? "", tenant_name: LENDER, role: "borrower", display_name: "Maya Lindqvist", email: "maya@juniperandrye.example", borrower_id: "demo" },
        ],
      };
      return me;
    }
    if (path === "/api/onboarding/state") return state(s);
    if (path === "/api/onboarding/profile") {
      if (!text(body.legal_name)) throw new ApiError(422, "legal name is required");
      const year = Number(body.year_founded);
      s.profile = {
        legal_name: text(body.legal_name),
        dba: text(body.dba),
        website: text(body.website),
        phone: text(body.phone),
        address: text(body.address),
        state: text(body.state),
        naics: text(body.naics),
        year_founded: Number.isFinite(year) && year > 0 ? year : null,
        stated_revenue_90d: text(body.stated_revenue_90d),
        import_mix: (body.import_mix as Profile["import_mix"]) ?? null,
        observed_at: now(),
      };
      return state(s);
    }
    if (path === "/api/onboarding/submit") {
      s.submittedAt = now();
      return state(s);
    }
    const m = path.match(/^\/api\/onboarding\/items\/([^/]+)\/(connect|documents|answer)$/);
    if (m) {
      const [, id, action] = m;
      const it = ITEMS.find((i) => i.id === id);
      if (!it) throw new ApiError(404, `${id} was not requested`);
      if (action === "connect") add(id, "source_connected", { provider: body.provider, simulated: true });
      if (action === "documents") {
        const f = opts.form?.get("file");
        add(id, "document_added", { filename: f instanceof File ? f.name : "upload", bytes: f instanceof File ? f.size : 0 });
      }
      if (action === "answer") {
        const v = String(body.value ?? "").trim();
        if (!v) throw new ApiError(422, "enter a value");
        add(id, it.kind === "link" ? "link_added" : "answer_added", it.kind === "link" ? { url: v } : { text: v });
      }
      return state(s);
    }
    throw new ApiError(404, "not available in the demo");
  };
  return <T,>(path: string, opts: Options = {}) =>
    new Promise<T>((resolve, reject) =>
      setTimeout(() => {
        try {
          resolve(handle(path, opts) as T);
        } catch (e) {
          reject(e);
        }
      }, 250),
    );
}

export default function DemoOnboardingPage() {
  // One store per visit: the mock mutates it in place, so it lives with the fetcher.
  const [api] = useState(() => mockApi({ profile: null, responses: {}, submittedAt: null }));
  const auth = useMemo<AuthState>(
    () => ({
      mode: "dev",
      ready: true,
      signedIn: true, // the page sends signed-out visitors to /sign-in
      authHeaders: async () => ({}),
      signOut: async () => {},
      devSubject: "demo-business",
    }),
    [],
  );
  return (
    <AuthContext.Provider value={auth}>
      <ApiOverride.Provider value={api}>
        <OnboardingPage />
      </ApiOverride.Provider>
    </AuthContext.Provider>
  );
}
