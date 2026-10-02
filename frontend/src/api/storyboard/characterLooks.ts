/**
 * 人物造型照（2026-10-02）：非默认造型段以人物正面定妆照为种子图、按本段
 * wardrobe 文本单独生成的「同一个人、换了本段这身衣服」正面全身照，替代被
 * 视频供应商判成真人隐私拒收的头像九宫格。后端 app.video_modes.
 * character_looks_api。
 */
import { get, mutate } from "../client";

export interface CharacterLookItem {
  shot_id: string;
  shot_no: number;
  identity_id: string;
  character_name: string;
  wardrobe_text: string;
  look_key: string;
  status: "ready" | "running" | "failed" | "missing";
  image_path?: string | null;
  error?: string | null;
}

export interface CharacterLooksSummary {
  ready: number;
  generating: number;
  failed: number;
  missing: number;
}

export interface CharacterLooksStatus {
  items: CharacterLookItem[];
  summary: CharacterLooksSummary;
}

export function getCharacterLooks(episodeId: string): Promise<CharacterLooksStatus> {
  return get(`/episodes/${episodeId}/character-looks`);
}

export function startCharacterLooks(episodeId: string, shotIds?: string[]) {
  return mutate(
    "POST",
    `/episodes/${episodeId}/character-looks`,
    shotIds && shotIds.length ? { shot_ids: shotIds } : {},
  );
}
