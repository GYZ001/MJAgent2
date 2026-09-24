import { get, mutate } from "./client";

/** 项目级设置域：改编强度 / 画幅 / AI 生成标识（2026-09-23 用户拍板）。三项都
 *  只影响"之后新生成"的产物，不回溯已有内容——具体生效范围见 api/projects.ts
 *  的 Project 字段注释。setStageTextModel 从 projects.ts 搬来（同属项目设置，
 *  PUT /projects/{id}/text-models），棘轮下拧腾出的行数用于本次三项新设置，
 *  外部调用方不受影响——仍经 api/index.ts 聚合成 api.setStageTextModel。 */

export function setStageTextModel(
  projectId: string,
  body: Partial<Record<
    "bible_text_provider" | "script_text_provider" | "board_text_provider",
    string
  >>,
) {
  return mutate("PUT", `/projects/${projectId}/text-models`, body);
}

export interface ProjectSettingsUpdate {
  adaptation_mode?: string;
  aspect_ratio?: string;
  ai_label_enabled?: boolean;
}

export interface ProjectSettingsResponse {
  project_id: string;
  adaptation_mode: string;
  aspect_ratio: string;
  ai_label_enabled: boolean;
}

/** PUT /projects/{id}/settings：Body 为三字段任意子集；非法值后端返回 409 中文
 *  detail，client.ts 的 handle() 已把它规整进 ApiError.message，调用方直接展示
 *  message 即可。 */
export function updateProjectSettings(
  projectId: string,
  body: ProjectSettingsUpdate,
): Promise<ProjectSettingsResponse> {
  return mutate("PUT", `/projects/${projectId}/settings`, body);
}

export interface AspectRatioImpact {
  project_id: string;
  current_aspect_ratio: string;
  target_aspect_ratio: string;
  adopted_videos_total: number;
  adopted_videos_mismatched: number;
  scene_images_total: number;
  scene_images_mismatched: number | null;
}

/** 切换画幅前的影响预估：GET /projects/{id}/aspect-ratio-impact?aspect_ratio=xxx。
 *  scene_images_mismatched 为 null 表示后端没有记录场景图的画幅归属（旧数据），
 *  前端据此如实展示"画幅未记录"，不编造一个具体数字。 */
export function getAspectRatioImpact(
  projectId: string,
  aspectRatio: string,
): Promise<AspectRatioImpact> {
  return get(`/projects/${projectId}/aspect-ratio-impact?aspect_ratio=${encodeURIComponent(aspectRatio)}`);
}

export interface StoryboardAdaptationDroppedSpan {
  source_segment_index: number;
  from_unit: number;
  to_unit: number;
  reason: string;
  chapter_idx: number;
  start_offset: number;
  end_offset: number;
  excerpt: string;
  chars: number;
}

export interface StoryboardAdaptationDroppedLine {
  quote_id: string;
  reason: string;
  text: string;
}

/** 删减复核（2026-09-24）留档里的一条必保条目：复核判定为交代了后续剧情
 *  会用到的信息、因此被强制保留的原文内容——见
 *  app/production/storyboard_short_drama_review.py 模块 docstring。 */
export interface StoryboardAdaptationMustKeepItem {
  item_id: string;
  kind: string;
  text: string;
  evidence_quote: string;
}

export interface StoryboardAdaptationDropReview {
  status: "ok" | "failed" | "skipped";
  reviewed_count: number;
  must_keep: StoryboardAdaptationMustKeepItem[];
  second_pass: boolean;
}

export interface StoryboardAdaptationSummary {
  recorded: boolean;
  adaptation_mode: string;
  target_duration_s: number | null;
  segment_count: number | null;
  over_target: boolean;
  dropped_source_spans: StoryboardAdaptationDroppedSpan[];
  dropped_lines: StoryboardAdaptationDroppedLine[];
  // 2026-09-24 新增：老留档（改造前生成）没有这五个字段，后端用 dict.get()
  // 降级为 None/False——这里标 optional 而不是必填，前端据此按现有字段降级
  // 显示，不假装拿到了一个具体数字。
  final_duration_s?: number | null;
  max_duration_s?: number | null;
  planned_over_cap?: boolean;
  kept_dialogue_chars?: number | null;
  dialogue_budget_chars?: number | null;
  // 2026-09-24 删减复核：同样是老留档没有的字段，降级为 undefined/null。
  drop_review?: StoryboardAdaptationDropReview | null;
}

/** 分镜台「本集删减」面板的只读数据源：GET /episodes/{id}/storyboard-adaptation。
 *  老分集/忠实档没有改编留档时 recorded=false，但 dropped_lines 仍可能非空——
 *  对白台账独立于改编留档存在（见 app/domain/video_ops/storyboard_adaptation.py）。 */
export function getStoryboardAdaptation(episodeId: string): Promise<StoryboardAdaptationSummary> {
  return get(`/episodes/${episodeId}/storyboard-adaptation`);
}
