import { get, mutate } from "./client";

// 只在 Shot.dialogues 里使用；导出以便 storyboard.ts 引用同一个形状。
export interface Dialogue {
  speaker: string;
  line: string;
  emotion: string;
}

export interface ScriptScene {
  scene_no: number;
  scene_heading: string;
  story_function: string;
  characters: string[];
  summary: string;
  conflict?: string;
  turn?: string;
  source_basis?: string;
  entry_state?: string;
  exit_state?: string;
  context_requirements?: string[];
}

export interface PlotSpineBeat {
  beat_id: string;
  who?: string;
  does?: string;
  turn?: string;
  must_keep?: boolean;
  source_segment_ids?: string[];
  purpose?: string;
}

export interface PlotSpine {
  episode_premise?: string;
  spine_beats?: PlotSpineBeat[];
  must_keep_ending?: string;
  drop_list?: string[];
}

export interface KeyDialogueTurn {
  speaker: string;
  line: string;
  function:
    | "trigger"
    | "announcement"
    | "question"
    | "response"
    | "decision"
    | "statement"
    | string;
  source_text: string;
}

export interface KeyDialogueChain {
  chain_id: string;
  topic: string;
  turns: KeyDialogueTurn[];
}

export interface EpisodeScreenplay {
  id?: string | null;
  episode_no: number;
  mode?: "full_script" | string;
  title?: string;
  source_text_range?: string;
  logline?: string;
  script_format_note?: string;
  dramatic_question?: string;
  protagonist_goal?: string;
  obstacle?: string;
  stakes?: string;
  key_lines?: string[];
  dialogue_chains?: KeyDialogueChain[];
  key_plot_points?: string[];
  plot_spine?: PlotSpine | null;
  source_coverage?: {
    source_segment_id: string;
    disposition: "deliver" | "merge" | "context" | "duplicate";
    beat_ids: string[];
    duplicate_of?: string | null;
    reason?: string;
  }[];
  scene_outline?: ScriptScene[];
  full_script_text?: string;
  character_state_changes?: string[];
  emotional_curve?: string;
  ending_hook?: string;
  source_basis?: string;
  adaptation_direction?: string;
  opening?: string;
  development?: string;
  conflict?: string;
  climax?: string;
  episode_premise?: string;
  events?: Record<string, unknown>[];
  information_ledger?: Record<string, unknown>[];
  voice_bible?: Record<string, unknown>[];
  approved_adaptations?: string[];
  forbidden_additions?: string[];
  narrative_plan?: Record<string, unknown> | null;
  created_at?: number | null;
  updated_at?: number | null;
}


// 映射包（EpisodePrepPack / PrepPack*）契约类型见同目录 prepPack.ts。
export interface ScreenplayState {
  version: number;
  code: string;
  message: string;
  recommended_action:
    | "generate_screenplay"
    | "stop_screenplay"
    | "resume_screenplay"
    | "generate_storyboard"
    | "resume_storyboard"
    | "view_storyboard"
    | "view_save_progress"
    | "view_cancel_progress"
    | "refresh";
  screenplay_status: string;
  storyboard_status: string;
  screenplay_run_id?: string | null;
  storyboard_run_id?: string | null;
  checkpoint_shot?: number | null;
  storyboard_running: boolean;
  publish_blocked: boolean;
  reason?: string;
}

/** 映射台生成前的预检——ScriptPage.tsx::openScreenplayPreview。响应形状随蓝图预算
 *  演进，前端只读取 blueprint_budget 字段，其余原样透传给二次确认弹窗展示。 */
export function screenplayPreflight(episodeId: string): Promise<{
  blueprint_budget?: Record<string, unknown>;
  [key: string]: unknown;
}> {
  return mutate("POST", `/episodes/${episodeId}/screenplay/preflight`, {});
}

export function generateScreenplay(episodeId: string, body: Record<string, unknown>) {
  return mutate("POST", `/episodes/${episodeId}/screenplay`, body);
}

export function resumeScreenplay(
  episodeId: string,
  body: { idempotency_key: string },
) {
  return mutate("POST", `/episodes/${episodeId}/screenplay/resume`, body);
}

export function cancelScreenplay(episodeId: string) {
  return mutate("POST", `/episodes/${episodeId}/screenplay/cancel`, {});
}

export function deleteScreenplay(episodeId: string) {
  return mutate("DELETE", `/episodes/${episodeId}/screenplay`);
}

type ScreenplayLightStatus = Record<string, unknown> & { id: string; active: boolean };

/** 映射台轻量状态轮询——只读「详情之后发生的变化」，见 App.tsx::useScriptEpisode
 *  上的调用点注释（为什么不与详情接口重复拉全量正文）。 */
export function getScreenplayStatus(episodeId: string): Promise<ScreenplayLightStatus> {
  return get(`/episodes/${episodeId}/screenplay/status`);
}
