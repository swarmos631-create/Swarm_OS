export type Role = "viewer" | "operator" | "admin";
export type Provenance = "live" | "cached" | "demo" | "operator" | "derived" | "synthetic" | "records" | "mixed" | string;
export type Severity = "low" | "moderate" | "high" | "critical";

export interface Me {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  all_cities: boolean;
  city_ids: string[];
  csrf_token: string | null;
  environment: string;
  demo_mode: boolean;
}

export interface City {
  id: string;
  name: string;
  state: string;
  lat: number;
  lon: number;
  timezone: string;
  zoom: number;
  bbox: [number, number, number, number];
  bbox_note: string;
  population: number | null;
  population_source: string | null;
  aliases: string[];
  is_pilot: boolean;
  has_live_data?: boolean;
  has_demo_data?: boolean;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Indicator {
  id: string;
  label: string;
  domain: string;
  status: "available" | "unavailable";
  value: number | string | null;
  unit?: string | null;
  provenance?: Provenance;
  kind?: string;
  observed_at?: string | null;
  fetched_at?: string | null;
  source_id?: string | null;
  note?: string | null;
}

export interface Freshness {
  state: "fresh" | "stale" | "never";
  age_s: number | null;
  threshold_s: number;
}

export interface SourceStatus {
  id: string;
  name: string;
  kind: string;
  categories: string[];
  status: string;
  status_detail: string | null;
  applies_to_city: boolean;
  scope: string;
  last_run: { status: string; started_at: string; records: number; error: string | null; duration_ms: number | null } | null;
  freshness: Freshness | null;
  attribution: string;
}

export interface Trend {
  unit: string;
  provenance: Provenance;
  kind: string;
  source_id: string;
  points: [string, number][];
}

export interface Dashboard {
  city: City;
  generated_at: string;
  indicators: Indicator[];
  incidents: { open: number; by_severity: Record<Severity, number>; recent: IncidentSummary[] };
  infrastructure: { osm_feature_counts: Record<string, number>; earthquakes_7d_500km: number; strongest_quake: { magnitude: number } | null };
  agents: { recent_tasks: { id: string; prompt: string; status: string; created_at: string }[]; executions_24h: Record<string, number> };
  trends: Record<string, Trend | null>;
  alerts: { id: number; level: string; title: string; body: string; created_at: string; link: string | null }[];
  source_health: SourceStatus[];
  sync_failures: SourceStatus[];
  audit: { at: string; actor: string; action: string; target: string; outcome: string }[];
}

export interface IncidentSummary {
  id: string;
  title: string;
  severity: Severity;
  status: string;
  category: string;
  reported_at: string;
  provenance: Provenance;
}

export interface Incident extends IncidentSummary {
  city_id: string;
  description: string;
  lat: number | null;
  lon: number | null;
  department: string | null;
  source: string;
  updated_at: string;
  resolved_at: string | null;
  version: number;
  events?: { kind: string; from_status: string | null; to_status: string | null; note: string | null; actor_id: string | null; created_at: string }[];
}

export interface AgentInfo {
  id: string;
  name: string;
  domain: string;
  domain_label: string;
  description: string;
  capabilities: string[];
  keywords: string[];
  data_requirements: string[];
  depends_on: string[];
  aggregate: boolean;
  model: string | null;
  min_role: Role;
  timeout_s: number | null;
  max_retries: number;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  stats_7d: { runs: number; health: string; by_status?: Record<string, number>; avg_ms?: number | null; useful_rate?: number | null; last_run_at?: string | null };
  dependents?: string[];
  recent_executions?: { task_id: string; city_id: string; status: string; duration_ms: number | null; confidence: number | null; finished_at: string | null; error: string | null }[];
}

export interface Finding {
  title: string;
  detail: string;
  severity: "info" | "low" | "moderate" | "high" | "critical";
  location: { lat: number; lon: number } | null;
  agent_id?: string;
}

export interface AgentResult {
  status: "ok" | "degraded" | "unavailable" | "failed";
  summary: string;
  findings: Finding[];
  metrics: Record<string, any>;
  actions: { kind: string; title: string; rationale: string; risk: string; required_role: string; payload: Record<string, unknown> }[];
  confidence: number | null;
  method: string;
  data_refs: Record<string, any>[];
  provenance: string[];
  limitations: string[];
  missing: string[];
}

export interface Execution {
  agent_id: string;
  name: string;
  status: string;
  attempts: number;
  depends_on: string[];
  reason: string | null;
  input: Record<string, unknown>;
  output: AgentResult | null;
  error: string | null;
  confidence: number | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
}

export interface PlanAgent {
  id: string;
  name: string;
  domain: string;
  reason: string;
  depends_on: string[];
}

export interface Plan {
  agents: PlanAgent[];
  edges: { from: string; to: string }[];
  stages: string[][];
  excluded: Record<string, string>;
}

export interface ProposedAction {
  id: string;
  task_id: string | null;
  agent_id: string;
  agent_name: string;
  kind: string;
  title: string;
  rationale: string;
  payload: Record<string, any>;
  risk: string;
  required_role: Role;
  status: string;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  execution_result: Record<string, unknown> | null;
}

export interface TaskSummary {
  id: string;
  city_id: string;
  prompt: string;
  status: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested: boolean;
  agent_count: number;
  error: string | null;
  summary: string | null;
}

export interface TaskDetail extends TaskSummary {
  plan: Plan;
  params: Record<string, unknown>;
  result: {
    status: string;
    summary: string;
    counts: Record<string, number>;
    confidence: number | null;
    provenance: string[];
    limitations: string[];
    missing_data: string[];
    findings: Finding[];
    proposed_action_ids: string[];
    data_freshness: { id: string; name: string; status: string; freshness: Freshness | null }[];
    execution_policy: string;
  } | null;
  executions: Execution[];
  handoffs?: { from: string; to: string; digest: string; status: string; at: string }[];
  actions?: ProposedAction[];
}

export interface DataSource {
  id: string;
  name: string;
  kind: string;
  categories: string[];
  coverage: string;
  auth_required: string;
  license: string;
  attribution: string;
  homepage: string | null;
  freshness_threshold_s: number;
  status: string;
  status_detail: string | null;
  enabled: boolean;
  last_success_at: string | null;
  last_attempt_at: string | null;
  last_error_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
  limitations: string[];
  setup: string | null;
  scope: string;
  min_sync_interval_s: number | null;
}

export interface ModelVersionInfo {
  id: string;
  model_name: string;
  version: string;
  task_type: string;
  algorithm: string;
  metrics: Record<string, number>;
  baseline_metrics: Record<string, number | string>;
  dataset: Record<string, any>;
  status: string;
  passed_gate: boolean;
  created_at: string;
  promoted_at: string | null;
  artifact_sha256: string;
  report_path: string | null;
}

export interface ModelInfo {
  model_name: string;
  description: string;
  task_type: string;
  algorithm: string;
  used_by: string[];
  gate: Record<string, any>;
  dataset: Record<string, any>;
  promoted: ModelVersionInfo | null;
  versions: ModelVersionInfo[];
  latest_job: { id: string; status: string; error: string | null; created_at: string; finished_at: string | null } | null;
}

export interface RuntimeProvider {
  id: string;
  kind: string;
  available: boolean;
  endpoint: string | null;
  detail: string | null;
  models?: string[];
}
