import { get, request } from "../client";

export interface SettingSchema {
  label: string;
  type: "integer" | "number" | "boolean" | "enum" | "string";
  default: string;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  options?: string[];
  immediate: boolean;
  experimental: boolean;
  max_length?: number;
  allow_empty?: boolean;
  format?: "public_http_url";
}

export interface SettingsView {
  values: Record<string, string>;
  effective: Record<string, string>;
  schema: Record<string, SettingSchema>;
  version: number;
  health: "ok" | "invalid";
  issues: Array<{ field: string; message: unknown }>;
  server_time: number;
  features: {
    overview_state_v2: boolean;
    jobs_query_v2: boolean;
    run_center_v2: boolean;
    call_detail_v2: boolean;
    settings_edit_v2: boolean;
  };
}

export interface SettingsUpdateResult {
  version: number;
  items: Array<{ key: string; requested: string; effective: string; apply_mode: string }>;
  effect_scope?: {
    new_tasks?: boolean;
    queued_not_started?: boolean;
    running_tasks?: boolean;
  };
}

export function getSettings(includeSchema = true): Promise<SettingsView> {
  return get(`/settings${includeSchema ? "?include_schema=true" : ""}`);
}

export function updateSettings(body: {
  version: number;
  patch: Record<string, string>;
}): Promise<SettingsUpdateResult> {
  return request("PUT", "/settings", body);
}
