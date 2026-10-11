import type { Stage } from "@/lib/types";

export const STAGES: { id: Stage; label: string; short: string }[] = [
  { id: "invited", label: "Invited", short: "Invited" },
  { id: "opened", label: "Opened the link", short: "Opened" },
  { id: "account_created", label: "Account created", short: "Account" },
  { id: "connecting", label: "Connecting data", short: "Connecting" },
  { id: "submitted", label: "Ready for diligence", short: "Ready" },
  { id: "in_diligence", label: "In diligence", short: "Diligence" },
  { id: "signed", label: "Signed", short: "Signed" },
];

export const STAGE_TONE: Record<Stage, "neutral" | "info" | "review" | "brand" | "ok"> = {
  invited: "neutral",
  opened: "neutral",
  account_created: "info",
  connecting: "info",
  submitted: "review",
  in_diligence: "brand",
  signed: "ok",
};

export function stageLabel(s: string): string {
  return STAGES.find((x) => x.id === s)?.label ?? s;
}

export const EVENT_LABELS: Record<string, string> = {
  sent: "Invite sent",
  opened: "Opened the invite",
  accepted: "Created an account",
  profile_saved: "Saved business profile",
  source_connected: "Connected",
  document_added: "Uploaded",
  link_added: "Added a link",
  answer_added: "Answered",
  submitted: "Submitted for review",
  resent: "Invite re-sent",
  revoked: "Invite revoked",
  case_opened: "Diligence started",
};

export const PROVIDER_LABELS: Record<string, string> = {
  plaid: "Bank (Plaid)",
  mx: "Bank (MX)",
  finicity: "Bank (Finicity)",
  quickbooks: "QuickBooks",
  xero: "Xero",
  freshbooks: "FreshBooks",
  sage: "Sage",
  netsuite: "NetSuite",
  wave: "Wave",
  zoho_books: "Zoho Books",
  shopify: "Shopify",
  stripe: "Stripe",
  square: "Square",
  amazon: "Amazon",
  etsy: "Etsy",
  paypal: "PayPal",
  woocommerce: "WooCommerce",
  bigcommerce: "BigCommerce",
  clover: "Clover",
  toast: "Toast",
  ebay: "eBay",
  gusto: "Gusto",
  adp: "ADP",
  rippling: "Rippling",
  paychex: "Paychex",
  cin7: "Cin7",
  lightspeed: "Lightspeed",
  katana: "Katana",
  devrev: "DevRev",
  zendesk: "Zendesk",
  intercom: "Intercom",
  freshdesk: "Freshdesk",
  gorgias: "Gorgias",
};

export function providerLabel(p: string): string {
  return PROVIDER_LABELS[p] ?? p;
}

export const CASE_STATUS: Record<string, { label: string; tone: "neutral" | "info" | "review" | "brand" | "ok" | "hold" }> = {
  open: { label: "Open", tone: "neutral" },
  planning: { label: "Planning", tone: "info" },
  investigating: { label: "Investigating", tone: "brand" },
  review: { label: "In review", tone: "review" },
  signed: { label: "Signed", tone: "ok" },
  closed: { label: "Closed", tone: "neutral" },
};
