// 定妆照肤色局部色块核验：只读检查入口（可能触发一次多模态模型调用 + 写
// 缓存，但不计费、不消耗配额，见后端 app/domain/bible_ops/portrait_skin_blush_audit.py）。
import { request } from "../client";

export interface PortraitSkinBlushResult {
  character_name: string;
  portrait_id: string;
  image_path: string;
  checked: boolean;
  has_local_color: boolean | null;
  reason: string;
  rule_version: string;
  cached: boolean;
}

export interface PortraitSkinBlushAuditResponse {
  project_id: string;
  results: PortraitSkinBlushResult[];
}

export function auditPortraitSkinBlush(
  projectId: string,
  characterName?: string,
): Promise<PortraitSkinBlushAuditResponse> {
  const qs = characterName ? `?character_name=${encodeURIComponent(characterName)}` : "";
  return request("GET", `/projects/${projectId}/portraits/skin-blush-audit${qs}`);
}
