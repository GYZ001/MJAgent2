// 角色固定音色域（U4，对接 U1 的 app/voice/ 新包，另一个代理同步开发中）：按
// docs/角色固定音色_声音生成接口调研与实施方案_2026-09-23.md 5.4 节冻结的字段名
// 逐字照用，不在前端自行改名/加字段。GET /voices 覆盖人物谱里每个具名角色（没有
// 声音的也在，current 为 null），前端据此渲染四态而不需要另外判断"这个角色有没有
// 声音记录"。

import { get, mutate } from "./client";

/** 生成/校验中的一个声音版本：current 与 candidates[] 里的每一项同一形状。
 *  audio_url/clip_url 为空串表示没有文件（生成中或失败态）。 */
export interface VoiceVersion {
  id: string;
  status: "generating" | "candidate" | "current" | "failed";
  source: "design";
  voice_prompt: string;
  preview_text: string;
  audio_url: string;
  clip_url: string;
  clip_duration_s: number | null;
  check_status: "passed" | "failed" | "unchecked";
  check_reason?: string | null;
  asr_text?: string | null;
  error?: string | null;
  created_at: number;
  adopted_at: number | null;
}

/** 一个角色（按 anchor_key 年龄段）的声音状态；items 覆盖人物谱里每个具名角色。 */
export interface VoiceItem {
  character_name: string;
  anchor_key: string;
  current: VoiceVersion | null;
  candidates: VoiceVersion[];
  generating: boolean;
}

export interface ProjectVoices {
  voice_model_configured: boolean;
  auto_generate: boolean;
  items: VoiceItem[];
}

export function getProjectVoices(projectId: string): Promise<ProjectVoices> {
  return get(`/projects/${encodeURIComponent(projectId)}/voices`);
}

/** 建议音色描述（免费，不落库）：人物卡外观/性格/语风/年代按 5.2 节写出音色描述
 *  与试听文本；调用方只是把结果填进草稿框，用户仍可编辑，不自动提交生成。 */
export function suggestVoiceDescription(
  projectId: string,
  characterName: string,
): Promise<{ voice_prompt: string; preview_text: string }> {
  return mutate(
    "POST",
    `/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(characterName)}/voice-description`,
  );
}

/** 生成一版声音（付费，约 10 秒同步返回）：描述留空时后端自动写。没有当前声音时
 *  第一个通过校验的候选自动成为当前；已有当前声音时只进候选，需人工采用。
 *  409/502/404 的 detail 是可以直接展示给用户的中文原因。run_id 对应观测台的一次
 *  「角色声音生成」运行（一次运行一个步骤），可据此跳转查看进度。 */
export function generateCharacterVoice(
  projectId: string,
  characterName: string,
  body: { voice_prompt: string; preview_text: string; idempotency_key: string },
): Promise<{ voice: VoiceVersion; run_id: string }> {
  return mutate(
    "POST",
    `/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(characterName)}/voices`,
    body,
  );
}

export function adoptCharacterVoice(
  projectId: string,
  characterName: string,
  voiceId: string,
): Promise<{ voice: VoiceVersion }> {
  return mutate(
    "POST",
    `/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(characterName)}/voices/${encodeURIComponent(voiceId)}/adopt`,
  );
}

/** 批量补齐：为人物谱里所有"没有当前声音也没在生成中"的具名角色各触发一次生成，
 *  后台执行、立即返回受理结果，不在这次请求里等待完成。run_id 对应观测台的一次
 *  「角色声音生成」运行（一次运行、每个角色一个步骤）；accepted 为 0 时没有可生成
 *  的角色，run_id 为 null。 */
export function generateMissingVoices(
  projectId: string,
): Promise<{ accepted: number; characters: string[]; run_id: string | null }> {
  return mutate("POST", `/projects/${encodeURIComponent(projectId)}/voices/generate-missing`);
}
