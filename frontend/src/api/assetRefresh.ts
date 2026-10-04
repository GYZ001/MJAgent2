// 「参考资产已更新」面板（生成台/成片台）：参考资产（人物定妆照/场景图/道具
// 卡）在本集已采用视频之后才补齐或更新时，按实体成组展示影响范围、成组重
// 生成、整组原子采纳。独立文件而不是塞进 video.ts：该文件已经贴着
// FILE_CONVENTIONS.toml 前端 300 行默认阈值，没有余量（CLAUDE.md「装不下时
// 先想怎么拆，不要先想加基线」）。
import { request } from "./client";

export type AssetRefreshEntityType = "character" | "scene" | "prop";
export type AssetRefreshCategory = "added" | "updated" | "removed";
export type AssetRefreshMemberStatus =
  | "not_adopted"
  | "latest"
  | "has_candidate"
  | "needs_regen";

export interface AssetRefreshCandidate {
  version_id: string;
  created_at: number;
}

export interface AssetRefreshMember {
  shot_id: string;
  shot_no: number;
  duration_s: number;
  status: AssetRefreshMemberStatus;
  status_label: string;
  reason: string;
  candidates: AssetRefreshCandidate[];
  entity_category: AssetRefreshCategory | null;
}

export interface AssetRefreshGroup {
  entity_key: string;
  entity_type: AssetRefreshEntityType;
  entity_name: string;
  category: AssetRefreshCategory;
  category_label: string;
  members: AssetRefreshMember[];
  needs_regen_shot_ids: string[];
  needs_regen_seconds: number;
}

export interface AssetRefreshQuotaAccount {
  unlimited: boolean;
  sub_remaining: number | null;
  addon_balance: number;
  total_remaining: number | null;
  reset_at?: number;
}

export interface AssetRefreshReport {
  episode_id: string;
  groups: AssetRefreshGroup[];
  quota: {
    needs_regen_shot_count: number;
    needs_regen_seconds: number;
    account: AssetRefreshQuotaAccount | null;
    enough: boolean;
  };
}

export interface AssetRefreshRegenerateError {
  shot_id: string;
  status_code: number;
  detail: unknown;
}

export interface AssetRefreshRegenerateResult {
  episode_id: string;
  queued: string[];
  errors: AssetRefreshRegenerateError[];
  message: string;
}

export interface AssetRefreshAdoptResult {
  episode_id: string;
  entity_key: string;
  adopted: { shot_id: string; version_id: string }[];
}

export const api_asset_refresh = {
  /** 只读：按实体成组的影响分析 + 额度估算。不触发任何生成或采纳。 */
  getAssetRefreshReport: (episodeId: string) =>
    request("GET", `/episodes/${episodeId}/asset-refresh`) as Promise<AssetRefreshReport>,

  /** 对所选组里"需要重生成"的段逐一走现有单镜生成命令；`entityKeys` 为空表示
   *  本集全部分组。串行发起，闸门 409 原样透传在 `errors` 里，不中断其余段落。 */
  regenerateAssetRefresh: (
    episodeId: string,
    entityKeys: string[],
    idempotencyKey: string,
    qualificationVersion?: string,
  ) =>
    request("POST", `/episodes/${episodeId}/asset-refresh/regenerate`, {
      entity_keys: entityKeys,
      idempotency_key: idempotencyKey,
      qualification_version: qualificationVersion,
    }) as Promise<AssetRefreshRegenerateResult>,

  /** 整组原子采纳：`versions` 是 {shot_id: version_id}，由调用方显式指定每段
   *  采用哪个版本；任何一段不合格整组 409，全部合格才一次性替换。 */
  adoptAssetRefreshGroup: (
    episodeId: string,
    entityKey: string,
    versions: Record<string, string>,
    reason: string,
    idempotencyKey?: string,
    qualificationVersion?: string,
  ) =>
    request("POST", `/episodes/${episodeId}/asset-refresh/adopt`, {
      entity_key: entityKey,
      versions,
      reason,
      idempotency_key: idempotencyKey,
      qualification_version: qualificationVersion,
    }) as Promise<AssetRefreshAdoptResult>,
};
