import { get } from "../client";

/** 操作审计域：谁、在什么时候、经哪个入口（页面/AI 代理/MCP/系统）触发了什么
 *  动作、作用在哪个对象上、结果如何。后端契约见
 *  GET /system/audit/events、/system/audit/events/{id}、/system/audit/facets
 *  （2026-09-02 冻结，系统管理员会话专属）。 */

export type AuditOutcome = "ok" | "failed" | "rejected" | "waiting_approval" | "error";
export type AuditSource = "ui" | "agent" | "mcp" | "system";

export interface AuditEvent {
  id: string;
  ts: number;
  user_id: string | null;
  username: string | null;
  is_system_admin: boolean | null;
  source: AuditSource;
  event: string;
  event_label: string | null;
  method: string | null;
  path: string | null;
  project_id: string | null;
  project_name: string | null;
  episode_id: string | null;
  target: string | null;
  outcome: AuditOutcome;
  http_status: number | null;
  error_id: string | null;
  error_code: string | null;
  summary: string | null;
  duration_ms: number | null;
  ip: string | null;
}

/** 详情端点比列表端点多两个重字段（user_agent、args），只在展开单行时才拉取。 */
export interface AuditEventDetail extends AuditEvent {
  user_agent: string | null;
  args: unknown;
}

export interface AuditEventsPage {
  items: AuditEvent[];
  next_cursor: string | null;
  server_time: number;
}

export interface AuditFacets {
  events: { event: string; event_label: string | null; count: number }[];
  users: { user_id: string; username: string; count: number }[];
  outcomes: { outcome: AuditOutcome; count: number }[];
  sources: { source: AuditSource; count: number }[];
  projects: { project_id: string; project_name: string | null; count: number }[];
}

export interface AuditEventsQuery {
  since?: number;
  until?: number;
  user_id?: string;
  event?: string;
  outcome?: string;
  source?: string;
  project_id?: string;
  q?: string;
  limit?: number;
  cursor?: string;
}

function toQueryString(params: Record<string, unknown>): string {
  const usp = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") usp.set(key, String(value));
  });
  const query = usp.toString();
  return query ? `?${query}` : "";
}

export function listAuditEvents(query: AuditEventsQuery = {}): Promise<AuditEventsPage> {
  return get(`/system/audit/events${toQueryString({ ...query })}`);
}

export function getAuditEvent(id: string): Promise<AuditEventDetail> {
  return get(`/system/audit/events/${encodeURIComponent(id)}`);
}

export function getAuditFacets(range: { since?: number; until?: number } = {}): Promise<AuditFacets> {
  return get(`/system/audit/facets${toQueryString({ ...range })}`);
}
