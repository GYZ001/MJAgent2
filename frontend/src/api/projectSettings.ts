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
  /** 统一配乐（2026-09-28）：开启后分镜台每段视频不再各自带配乐，改写声音描述
   *  为「无配乐，只有对白与环境音」，为成片合成阶段统一铺配乐让路。 */
  enhance_music_bed?: boolean;
  /** 片头预告（2026-09-28 起预留，暂无消费方）。 */
  enhance_teaser?: boolean;
  /** 主角内心独白（2026-09-28 起预留，暂无消费方）。 */
  enhance_monologue?: boolean;
  /** 旁白固定音色角色（2026-09-28）：值必须是本项目人物谱已有的角色正名，传空串
   *  清空（旁白不挂固定参考音频，保持现状）。只影响设置后新生成的分镜/视频，需要
   *  重新生成分镜才对已有分集生效。 */
  narrator_voice_character?: string;
}

export interface ProjectSettingsResponse {
  project_id: string;
  adaptation_mode: string;
  aspect_ratio: string;
  ai_label_enabled: boolean;
  enhance_music_bed: boolean;
  enhance_teaser: boolean;
  enhance_monologue: boolean;
  narrator_voice_character: string;
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

/** 开篇/结尾钩子（2026-09-27）单条提名的核验结果——problems 非空即未通过
 *  确定性核验；见 app/production/storyboard_short_drama_hooks.py 模块 docstring。 */
export interface StoryboardAdaptationHookNomination {
  beat_id: string;
  evidence_quote: string;
  problems: string[];
}

export interface StoryboardAdaptationHooks {
  status: "ok" | "warning";
  opening: StoryboardAdaptationHookNomination;
  ending: StoryboardAdaptationHookNomination;
}

/** 情绪因果核验（2026-09-27，P0-A）：见 app/production/storyboard_beat_
 *  causality.py 的 causality_summary 契约。忠实档/短剧档一视同仁地计算
 *  （不像 hooks 那样忠实档恒 null）。no_turns_nominated 表示模型没有提名
 *  任何情绪转折/决定性动作节拍——可能是原文确实没有，也可能是模型漏标，
 *  两者代码都区分不了，需要人工核查。 */
export interface StoryboardAdaptationCausality {
  status: "ok" | "warning" | "no_turns_nominated";
  problem_count: number;
  /** 原文没写诱因的情绪转折数（剧本层问题；2026-09-27 前的留档没有此字段）。 */
  missing_stimulus_count?: number;
}

/** 伏笔/类型信号核验（2026-09-27，P0-C）：见 app/production/storyboard_
 *  beat_foreshadowing.py 的 foreshadowing_summary 契约，结构与 causality
 *  同构。 */
export interface StoryboardAdaptationForeshadowing {
  status: "ok" | "warning" | "no_signals_nominated";
  problem_count: number;
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
  // 2026-09-27 开篇/结尾钩子：老留档没有这个字段，降级为 undefined/null，
  // 前端据此不渲染钩子相关文案（同 drop_review 既有模式）。
  hooks?: StoryboardAdaptationHooks | null;
  // 2026-09-27 情绪因果/伏笔（P0-A/C）：两者对忠实档/短剧档一视同仁地计算，
  // 老留档没有这两个字段时降级为 undefined/null，同 drop_review 既有模式。
  causality?: StoryboardAdaptationCausality | null;
  foreshadowing?: StoryboardAdaptationForeshadowing | null;
}

/** 分镜台「本集删减」面板的只读数据源：GET /episodes/{id}/storyboard-adaptation。
 *  老分集/忠实档没有改编留档时 recorded=false，但 dropped_lines 仍可能非空——
 *  对白台账独立于改编留档存在（见 app/domain/video_ops/storyboard_adaptation.py）。 */
export function getStoryboardAdaptation(episodeId: string): Promise<StoryboardAdaptationSummary> {
  return get(`/episodes/${episodeId}/storyboard-adaptation`);
}
