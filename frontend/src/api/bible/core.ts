import { mutate, request } from "../client";
import type { Character } from "./characters";

export interface BibleImpactPreview {
  project_id: string;
  bible_version: number;
  computed_at: number;
  fingerprint: string;
  change_types: string[];
  style_changed: boolean;
  stale_descendant_ids: string[];
  stale_count: number;
  stale_assets?: Array<{
    id: string;
    type: string;
    status: string;
    scope_type?: string | null;
    scope_id?: string | null;
  }>;
  stale_assets_truncated?: boolean;
  by_artifact_type?: Record<string, number>;
  paid_assets?: { character_portraits?: number; scene_references?: number };
  rebuild?: {
    image_count: number;
    unit_price_cny: number;
    estimated_cost_cny: number;
    max_retry_budget_cny: number;
    note?: string;
  };
  requires_reconfirm?: boolean;
  paid_media_invalidated?: boolean;
  old_asset_policy?: string;
}

export interface RefsCostPrecheck {
  quote_id: string;
  computed_at: number;
  quote_expires_at: number;
  project_id: string;
  action: string;
  character?: string | null;
  view_role?: string | null;
  character_count: number;
  views_per_character: number;
  image_count: number;
  unit_price_cny: number;
  estimated_cost_cny: number;
  max_retry_budget_cny: number;
  budget_cap_cny: number;
  scope: Array<Record<string, unknown>>;
  old_asset_policy?: string;
  idempotency_hint?: string;
  stop_policy?: string;
}

export interface Bible {
  characters: Character[];
  world: { era: string; genre: string; visual_style_canonical: string };
  scenes?: import("./scenes").Scene[];
}

/** 人物谱/场景库两页共用的「首次/全量重生成」路径：POST /projects/{id}/bible
 *  （重路径，含 LLM）。见 BiblePage.tsx::startBibleAfterStyle 与
 *  ScenesPage.tsx::startBibleAndSceneLibrary 的调用点注释。 */
export function generateBible(
  projectId: string,
  body: {
    confirm: true;
    quote_id: string;
    idempotency_key: string;
    style_name: string;
  },
) {
  return mutate("POST", `/projects/${projectId}/bible`, body);
}

export function cancelBibleGeneration(projectId: string) {
  return mutate("POST", `/projects/${projectId}/bible/cancel`);
}

export interface BibleUpdateResult {
  style_changed?: boolean;
  purged?: { versions: number } | null;
  impact?: Record<string, unknown>;
}

export function updateBible(
  projectId: string,
  body: {
    bible: unknown;
    expected_version: number;
    confirm: true;
    impact_preview_fingerprint: string;
  },
): Promise<BibleUpdateResult> {
  return mutate("PUT", `/projects/${projectId}/bible`, body);
}

export function bibleImpactPreview(
  projectId: string,
  body: {
    bible: unknown;
    expected_version?: number | null;
  },
): Promise<BibleImpactPreview> {
  return request("POST", `/projects/${projectId}/bible/impact-preview`, body);
}

export interface VisualStyleCatalog {
  default: string;
  items: Array<{ name: string; description: string; sample_image: string }>;
}

export function bibleVisualStyles(projectId: string): Promise<VisualStyleCatalog> {
  return request("GET", `/projects/${projectId}/bible/visual-styles`);
}

/** 导入面板选画风用：项目尚未创建，没有 project_id 可传，取值与
 *  bibleVisualStyles 完全一致（同一份后端 VISUAL_STYLE_PRESETS）。 */
export function bibleVisualStylesUnscoped(): Promise<VisualStyleCatalog> {
  return request("GET", "/bible/visual-styles");
}

/**
 * 只切换项目统一画风，不重新生成人物谱角色内容。两段式：不带 confirm 时，
 * 画风未变化直接返回 changed=false；画风有变化则后端抛 409
 * （ApiError.code === 'PAYMENT_CONFIRM_REQUIRED'），detail.precheck 是人物+
 * 场景合并报价。带 confirm+quote_id 确认后，后端在同一次请求内发起人物定妆
 * 照与场景图两条生成线。2026-08-31 起画风只在导入项目时选定一次
 * （见 projects.ts::importProject），人物谱/场景库不再提供前端换风格入口，
 * 本函数与后端路由继续保留供 Agent/MCP 调用。
 */
export function setBibleStyle(
  projectId: string,
  body: {
    style_name: string;
    expected_version: number;
    confirm?: boolean;
    quote_id?: string;
  },
): Promise<{
  project_id: string;
  style_name: string;
  changed: boolean;
  bible_version?: number;
  scene_bible_ready?: boolean;
  scenes_total?: number;
  idempotent_replay?: boolean;
  quote_id?: string;
  task_id?: string;
  refs_started?: boolean;
  refs_error?: string | null;
  scene_refs_started?: boolean;
  scene_refs_error?: string | null;
}> {
  return request("POST", `/projects/${projectId}/bible/style`, body);
}

export function bibleGeneratePrecheck(
  projectId: string,
  body?: { style_name?: string },
): Promise<
  RefsCostPrecheck & {
    estimated_duration_min?: number[];
    estimate_note?: string;
    character_names?: string[];
    style_name?: string;
  }
> {
  return request("POST", `/projects/${projectId}/bible/generate-precheck`, body || {});
}

export function saveBibleDraft(
  projectId: string,
  body: { bible: unknown; expected_version?: number | null },
) {
  return request("POST", `/projects/${projectId}/bible/draft`, body);
}

export function getBibleDraft(projectId: string): Promise<{
  draft: unknown;
  updated_at?: number | null;
  bible_version: number;
}> {
  return request("GET", `/projects/${projectId}/bible/draft`);
}
