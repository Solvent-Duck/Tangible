// Shapes returned by the workbench JSON API. Decimals and datetimes arrive as strings.

export type Role = "analyst" | "lender_underwriter" | "credit_admin" | "borrower";

export type Membership = {
  tenant_id: string;
  tenant_name: string;
  role: Role;
  display_name: string;
  email: string | null;
  borrower_id: string | null;
};

export type Me = {
  subject: string;
  email: string | null;
  auth_mode: "dev" | "oidc" | "clerk";
  memberships: Membership[];
};

export type CatalogGroup = { id: string; label: string; blurb: string };
export type ItemKind = "connector" | "upload" | "field" | "link";
export type CatalogItem = {
  id: string;
  group: string;
  label: string;
  help: string;
  kind: ItemKind;
  feeds: "ledger" | "intangibles" | "lender";
  locked: boolean;
  default_on?: boolean;
  providers: string[];
};
export type Catalog = {
  version: string;
  groups: CatalogGroup[];
  items: CatalogItem[];
  max_custom: number;
};

export type Stage =
  | "invited"
  | "opened"
  | "account_created"
  | "connecting"
  | "submitted"
  | "in_diligence"
  | "signed";

export type InviteSummary = {
  id: string;
  borrower_id: string;
  business_name: string;
  email: string;
  contact_name: string | null;
  loan_amount: string | null;
  created_by: string;
  created_at: string;
  expires_at: string;
  state: "live" | "revoked" | "replaced" | "expired";
  stage: Stage;
  requested: number;
  answered: number;
  groups: string[];
  case_id: string | null;
  case_status: string | null;
  last_activity: string;
  last_event: string | null;
};

export type ItemResponse = { kind: string; detail: Record<string, unknown>; at: string };
export type CoverageItem = CatalogItem & { done: boolean; responses: ItemResponse[] };

export type InviteEvent = {
  kind: string;
  item_id: string | null;
  detail: Record<string, unknown>;
  actor: string | null;
  at: string;
};

export type Email = {
  id: string;
  invite_id: string | null;
  to_addr: string;
  subject: string;
  body_text?: string;
  body_html?: string;
  transport: string;
  created_at: string;
  delivered_at: string | null;
  error: string | null;
};

export type Profile = {
  legal_name: string | null;
  dba: string | null;
  website: string | null;
  phone: string | null;
  address: string | null;
  state: string | null;
  naics: string | null;
  year_founded: number | null;
  stated_revenue_90d: string | null;
  observed_at: string;
};

export type InviteDetail = InviteSummary & {
  message: string | null;
  catalog_version: string;
  replaces_id: string | null;
  items: CoverageItem[];
  profile: Profile | null;
  events: InviteEvent[];
  sent_email: Email | null;
  connections: { source: string; scopes: string[]; granted_at: string; revoked_at: string | null }[];
};

export type Dashboard = {
  lender_name: string;
  kpis: {
    invited: number;
    awaiting_borrower: number;
    ready_for_diligence: number;
    in_diligence: number;
    signed_30d: number;
  };
  by_stage: Record<Stage, number>;
  pipeline: InviteSummary[];
  activity: {
    kind: string;
    item_id: string | null;
    actor: string | null;
    at: string;
    invite_id: string;
    business_name: string;
    detail: Record<string, unknown>;
  }[];
};

export type InvitePreview = {
  state: InviteSummary["state"];
  lender_name: string;
  business_name: string;
  contact_name: string | null;
  email: string;
  loan_amount: string | null;
  message: string | null;
  expires_at: string;
  items: CatalogItem[];
  groups: { id: string; label: string }[];
};

export type OnboardingState = {
  invite_id: string;
  lender_name: string;
  business_name: string;
  loan_amount: string | null;
  message: string | null;
  profile: Profile | null;
  items: CoverageItem[];
  groups: CatalogGroup[];
  submitted_at: string | null;
  can_submit: boolean;
  missing_for_submit: string[];
};

// ---------------------------------------------------------------- diligence

export type CaseRow = {
  id: string;
  borrower_id: string;
  borrower: string;
  as_of: string;
  status: string;
  assigned_analyst: string | null;
  previous_case_id: string | null;
  assessment: { id: string; band: string; limit: string | null } | null;
};

export type Finding = {
  id: string;
  check_key?: string;
  key?: string;
  severity: "hold" | "review" | "info";
  pillar?: string;
  title?: string;
  summary?: string;
  detail?: Record<string, unknown>;
  [k: string]: unknown;
};

export type Workspace = {
  id: string;
  borrower_id: string;
  borrower: string;
  as_of: string;
  loan_amount: string | null;
  budget_steps: number;
  budget_usd: string;
  assigned_analyst: string | null;
  status: string;
  previous_case_id: string | null;
  findings: Finding[];
  assessments: { id: string; decision_band: string; suggested_limit: string | null; [k: string]: unknown }[];
  plans?: { id: string; version: number; body: Record<string, unknown>; approved_at?: string | null; [k: string]: unknown }[];
  questions?: { id: string; [k: string]: unknown }[];
  usage?: { model_calls: number; cost_usd: string; priced: boolean; cost_text: string };
  last_investigation?: Record<string, unknown> | null;
  metrics: { key: string; version: number; value: string | null; label: string; text: string | null }[];
  documents: {
    pull_id: string;
    filename: string;
    sha256: string;
    byte_size: number;
    observed_at: string;
    included: boolean;
    read: boolean;
    read_error: string | null;
  }[];
  late_uploads: number;
  viewer_role: Role;
  [k: string]: unknown;
};
