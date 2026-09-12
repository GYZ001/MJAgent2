import { get, mutate } from "../client";

/** 模型中心管理界面新增的只读聚合 + 用途绑定读写（EP-05 §8）。
 *  读接口挂在 `/models/registry/*`（`app/models_registry/api.py`），与既有
 *  `/models` 系列（`./models.ts`）是两个独立后端路由，共用鉴权闸门
 *  （仅系统管理员）。写操作只有"用途绑定"是这里新开的——模型启停/限速走既有
 *  `updateModel`（见 `./models.ts` 的 `CatalogModel.enabled`/`rate_limit`），
 *  凭据轮换走既有 `saveModelCredentials`，本文件不重复。 */

export type ModelHealthState = "healthy" | "degraded" | "circuit_open" | "half_open";

export interface ModelHealthItem {
  model_id: string;
  label: string;
  provider: string;
  kinds: string[];
  enabled: boolean;
  state: ModelHealthState;
  calls_window: number;
  failures_window: number;
  failure_rate_window: number;
  p50_latency_ms_window: number | null;
  p95_latency_ms_window: number | null;
  window_hours: number;
  opened_at: number | null;
  last_error_code: string | null;
  last_error_at: number | null;
}

export interface ModelCredentialSummary {
  model_id: string;
  base_url: string;
  key_fingerprint: string;
  masked_key: string;
  rotated_at: number;
  rotated_by: string | null;
}

export interface PurposeBindingRow {
  id: string;
  purpose: string;
  model_id: string;
  label: string;
  priority: number;
  enabled: boolean;
  params: Record<string, unknown>;
  updated_at: number;
}

export interface PurposeStatus {
  purpose: string;
  missing_priority_zero: boolean;
  bindings: PurposeBindingRow[];
  fallback_active: boolean;
  active_priority: number | null;
  active_model_id: string | null;
  active_label: string | null;
  reason_code: string;
  reason_label: string;
  since: number | null;
}

export function getModelHealth(): Promise<{ items: ModelHealthItem[] }> {
  return get("/models/registry/health");
}

export function getModelCredentialsSummary(): Promise<{ items: ModelCredentialSummary[] }> {
  return get("/models/registry/credentials");
}

export function getPurposeStatus(): Promise<{ items: PurposeStatus[] }> {
  return get("/models/registry/purposes");
}

export function upsertModelBinding(body: {
  purpose: string;
  model_id: string;
  priority: number;
  enabled?: boolean;
  params?: Record<string, unknown>;
}) {
  return mutate("PUT", "/models/registry/bindings", body);
}
