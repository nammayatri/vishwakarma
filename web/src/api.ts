// Typed client for /api/console. The auth token (when RBAC is enabled) is
// kept in localStorage and sent as X-VK-Token; Google SSO replaces this at
// deployment without changing callers.

const BASE = "/api/console";

export function getToken(): string {
  return localStorage.getItem("vk_token") || "";
}
export function setToken(t: string) {
  localStorage.setItem("vk_token", t);
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string>),
  };
  const token = getToken();
  if (token) headers["X-VK-Token"] = token;
  const r = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!r.ok) {
    const body = await r.text().catch(() => "");
    throw new Error(`${r.status}: ${body.slice(0, 200)}`);
  }
  return r.json() as Promise<T>;
}

// ── Types (mirror console_api responses) ─────────────────────────────────────

export interface Investigation {
  id: string;
  alert_key?: string;
  cloud?: string;
  status: string;
  phase?: string;
  step?: number;
  attempt?: number;
  worker_id?: string;
  heartbeat_at?: number;
  created_at?: number;
  updated_at?: number;
  messages?: { role: string; content: string }[];
  findings?: unknown;
  code_session?: unknown;
}

export interface Incident {
  id: string;
  title: string;
  source?: string;
  severity?: string;
  status?: string;
  question?: string;
  analysis?: string;
  created_at?: number;
  labels?: Record<string, unknown>;
  slack_ts?: string;
  pdf_path?: string;
  tool_outputs?: unknown[];
}

export interface Runbook {
  id: string;
  title: string;
  content_md: string;
  cloud_type: string;
  keywords: string[];
  services: string[];
  author?: string;
  version: number;
  status: string;
  hit_count: number;
  miss_count: number;
}

export interface Fleet {
  queues: Record<string, { depth: number; pending: number }>;
  executors: { worker_id: string; cloud: string; running_jobs: number; heartbeat_age_s: number }[];
  orphaned: { id: string; worker_id?: string; step?: number }[];
}

export interface Overview {
  active_investigations: Investigation[];
  incident_stats: { total: number; by_status: Record<string, number>; by_source: Record<string, number> };
  fleet: Fleet;
}

// ── Calls ────────────────────────────────────────────────────────────────────

export const api = {
  overview: () => req<Overview>("/overview"),
  investigations: (status?: string) =>
    req<Investigation[]>(`/investigations${status ? `?status=${status}` : ""}`),
  investigation: (id: string) => req<Investigation>(`/investigations/${id}`),
  incidents: (q?: string) =>
    req<Incident[]>(`/incidents${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  incident: (id: string) => req<Incident>(`/incidents/${id}`),
  feedback: (id: string, correct: boolean, runbook_ids: string[] = [], alert_name = "") =>
    req(`/incidents/${id}/feedback`, {
      method: "POST",
      body: JSON.stringify({ correct, runbook_ids, alert_name }),
    }),
  runbooks: (status = "active") => req<Runbook[]>(`/runbooks?status=${status}`),
  runbook: (id: string) => req<Runbook>(`/runbooks/${id}`),
  saveRunbook: (id: string, body: Partial<Runbook>) =>
    req<Runbook>(`/runbooks/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteRunbook: (id: string) => req(`/runbooks/${id}`, { method: "DELETE" }),
  mapAlert: (id: string, alert_name: string) =>
    req(`/runbooks/${id}/mappings`, { method: "POST", body: JSON.stringify({ alert_name }) }),
  dryRun: (alert_text: string, cloud = "") =>
    req<{ id: string; title: string; cloud_type: string }[]>(`/runbooks/dry-run`, {
      method: "POST",
      body: JSON.stringify({ alert_text, cloud }),
    }),
  fixes: () => req<Investigation[]>("/fixes"),
  fleet: () => req<Fleet>("/fleet"),
};

// SSE stream of live events. Returns a cleanup function.
export function subscribeEvents(
  onEvent: (evt: Record<string, unknown>) => void,
  incidentId?: string,
): () => void {
  const url = `${BASE}/events${incidentId ? `?incident_id=${incidentId}` : ""}`;
  const es = new EventSource(url);
  es.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data));
    } catch {
      /* keepalives/hello */
    }
  };
  return () => es.close();
}
