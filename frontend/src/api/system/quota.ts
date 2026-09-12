import { get, mutate } from "../client";

/** EP-04 资源治理与配额：策略/分配管理 + 用量查询 + 预警 + 存储治理，与
 *  `app/quota_policy/api.py` 的响应形状逐字对应，字段名保持后端原样（下划线
 *  命名），与本仓库其余 api/*.ts 的既有约定一致。 */

export interface QuotaLimits {
  projects?: number | null;
  concurrency?: number | null;
  token?: number | null;
  video_seconds?: number | null;
  image?: number | null;
  storage_bytes?: number | null;
  project_concurrency?: number | null;
}

export interface QuotaPlan {
  id: string;
  org_id: string | null;
  key: string;
  name: string;
  builtin: boolean;
  period_days: number;
  limits: QuotaLimits;
  created_at: number;
  updated_at: number | null;
}

export interface QuotaAllocation {
  id: string;
  scope_type: "org" | "team" | "user";
  scope_id: string;
  plan_id: string;
  overrides: QuotaLimits;
  period_started_at: number | null;
  expires_at: number | null;
  created_at: number;
  usage?: Record<string, number>;
}

export interface QuotaAlert {
  id: string;
  scope_type: string;
  scope_id: string;
  resource: string;
  threshold: number;
  triggered_at: number;
  period_index: number;
  notified: number;
}

export type UsageResource = "token" | "video_seconds" | "image" | "storage_bytes";

export interface UsageSummary {
  scope_type: string;
  scope_id: string;
  usage: Record<string, number>;
  storage_sampled_at?: number | null;
}

export interface UsageTopItem {
  total: number;
  team_id?: string;
  user_id?: string;
  project_id?: string;
  model?: string;
  bytes_total?: number;
  sampled_at?: number | null;
}

export interface StorageSample {
  id: string;
  project_id: string;
  bytes_total: number;
  duration_s: number | null;
  status: string;
  error: string | null;
  sampled_at: number;
}

export interface StorageCleanupCandidate {
  version_id: string;
  shot_id: string;
  shot_no: number;
  video_path: string;
  bytes: number;
  created_at: number;
}

export function listQuotaPlans(orgId?: string): Promise<{ items: QuotaPlan[] }> {
  return get(`/system/quota/plans${orgId ? `?org_id=${encodeURIComponent(orgId)}` : ""}`);
}

export function createQuotaPlan(body: {
  org_id?: string; key: string; name: string; limits: QuotaLimits; period_days?: number;
}): Promise<QuotaPlan> {
  return mutate("POST", "/system/quota/plans", body);
}

export function listQuotaAllocations(orgId?: string): Promise<{ org_id: string; items: QuotaAllocation[] }> {
  return get(`/system/quota/allocations${orgId ? `?org_id=${encodeURIComponent(orgId)}` : ""}`);
}

export function putQuotaAllocation(
  scopeType: "org" | "team" | "user", scopeId: string,
  body: { plan_id: string; overrides?: QuotaLimits; expires_at?: number | null },
): Promise<{ id: string }> {
  return mutate("PUT", `/system/quota/allocations/${scopeType}/${encodeURIComponent(scopeId)}`, body);
}

export function listQuotaAlerts(orgId?: string): Promise<{ org_id: string; items: QuotaAlert[] }> {
  return get(`/system/quota/alerts${orgId ? `?org_id=${encodeURIComponent(orgId)}` : ""}`);
}

export function getUsageSummary(
  scope: "org" | "team" | "user" | "project", id: string,
): Promise<UsageSummary> {
  return get(`/system/usage/summary?scope=${scope}&id=${encodeURIComponent(id)}`);
}

export function getUsageTimeseries(
  scope: "org" | "team" | "user", id: string, resource: UsageResource,
): Promise<{ items: { day_started_at: number; value: number }[] }> {
  return get(`/system/usage/timeseries?scope=${scope}&id=${encodeURIComponent(id)}&resource=${resource}`);
}

export function getUsageTop(
  dimension: "team" | "user" | "project" | "model", resource: UsageResource, limit = 20,
): Promise<{ items: UsageTopItem[] }> {
  return get(`/system/usage/top?dimension=${dimension}&resource=${resource}&limit=${limit}`);
}

export function getStorageSample(projectId: string): Promise<{ project_id: string; sample: StorageSample | null }> {
  return get(`/system/storage/sample?project_id=${encodeURIComponent(projectId)}`);
}

export function getStorageCleanupCandidates(
  projectId: string, limit = 20,
): Promise<{ project_id: string; items: StorageCleanupCandidate[] }> {
  return get(`/system/storage/cleanup_candidates?project_id=${encodeURIComponent(projectId)}&limit=${limit}`);
}

export function postStorageCleanup(
  projectId: string, versionIds: string[],
): Promise<{ deleted: string[]; skipped: { version_id: string; reason: string }[]; bytes_freed: number }> {
  return mutate("POST", "/system/storage/cleanup", { project_id: projectId, version_ids: versionIds });
}
