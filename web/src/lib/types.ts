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

export type ReviewAction = { action: string; reason: string; by: string; at: string };

export type Finding = {
  id: string;
  check_key: string;
  check_version: number;
  origin: string;
  severity: "hold" | "review" | "info";
  direction: string;
  pillar: string;
  summary: string;
  evidence_refs: string[];
  facts: Record<string, unknown>;
  tool_call_ids: string[];
  verified: boolean | null;
  unmatched_numbers: string[] | null;
  case_id: string | null;
  replaces_id: string | null;
  replaced: boolean;
  corrections: Finding[];
  review_state: string;
  actions: ReviewAction[];
};

export type Plan = {
  id: string;
  version: number;
  body: {
    business_model?: string;
    notes?: string;
    risks?: { risk: string; why?: string; checks?: string[]; metrics?: string[]; follow_up?: string }[];
    [k: string]: unknown;
  };
  created_at: string;
  approved: boolean;
};

export type RunNotices = {
  last_failure: { at: string; error: string; message: string; restored_status: string } | null;
  last_reset: { at: string; by: string; reason: string; restored_status: string } | null;
  run: {
    started_at: string | null;
    age_minutes: number | null;
    stuck_after_min: number;
    stuck: boolean;
    restores_to: string;
  } | null;
};

export type CaseStatus = { id: string; status: string } & Partial<RunNotices>;

export type Metric = { key: string; version: number; value: string | null; label: string; text: string | null };

export type AssessmentSummary = {
  id: string;
  borrower_id: string;
  as_of: string;
  decision_band: string | null;
  suggested_limit: string | null;
  policy_version: string;
  created_at: string;
  created_by: string;
  case_id: string | null;
  overrides: { id: string; by: string; reason: string; at: string }[];
  [k: string]: unknown;
};

export type Assessment = AssessmentSummary & {
  borrower: string;
  evidence_set_hash: string;
  metric_set_version: string;
  policy_hash: string;
  model_id: string | null;
  rules_fired: { rule: string; step: string; saw?: unknown; op?: string; threshold?: unknown; outcome: string; [k: string]: unknown }[];
  metrics: Metric[];
  findings: Finding[];
  can_override: boolean;
  can_replay: boolean;
};

export type Trace = {
  finding: Finding;
  tool_calls: {
    id: string;
    tool: string;
    version: number;
    args: unknown;
    result: unknown;
    error: string | null;
    started_at: string;
    duration_ms: number | null;
  }[];
  evidence: { ref: string; line: unknown; error: string | null }[];
};

export type Workspace = Partial<RunNotices> & {
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
  assessments: AssessmentSummary[];
  plans?: Plan[];
  questions?: { id: string; body: string; created_at: string; approved: boolean; sent: boolean }[];
  usage?: { model_calls: number; cost_usd: string; priced: boolean; cost_text: string };
  last_investigation?: { stop_reason?: string; wrap_up?: string; at?: string; [k: string]: unknown } | null;
  metrics: Metric[];
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
};
