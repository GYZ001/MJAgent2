import { get, mutate, request } from "./client";
import type { ArtifactEvidence, ShotTiming, TaskTiming, TextModelChoice } from "./common";
import type { EpisodePrepPack, EpisodeScreenplay, ScreenplayState } from "./screenplay";
import type { Bible } from "./bible";
import type { EpisodePipelineSummary, Shot, StoryboardStatus } from "./storyboard";

export interface ChapterContent {
  idx: number;
  title: string;
  content: string;
  prev_idx: number | null;
  next_idx: number | null;
  first_idx: number;
  last_idx: number;
  total: number;
}

export interface Episode {
  id: string;
  episode_no: number;
  title: string;
  hook: string;
  cliffhanger: string;
  synopsis: string;
  source_chapters: number[];
  target_duration_s: number;
  status: string;
  script_error?: string;
  storyboard_warning?: string | null;
  cost_cny: number;
  cost_limit_cny?: number;
  screenplay_status: string;
  screenplay_error?: string | null;
  screenplay_updated_at?: number | null;
  screenplay?: EpisodeScreenplay | null;
  /**
   * 转型后的轻量分集映射包（screenplay 契约 6.0.0+）。后端 _episode_detail_projection
   * 把这两种产物形状投影到不同字段：旧形状仍在 `screenplay`；新形状在 `prep_pack`，
   * `screenplay` 此时为 null（不会把新形状塞进旧字段）。ScriptPage 必须两个字段都看：
   * prep_pack 非空 → 渲染映射包；否则 screenplay 非空 → 旧产物占位提示；否则 → 尚无产物。
   */
  prep_pack?: EpisodePrepPack | null;
  scene_options?: string[];
  shot_count?: number;
  video_count?: number;
  pending_adoption_count?: number;
  failed_count?: number;
  screenplay_artifact_id?: string | null;
  screenplay_evidence?: ArtifactEvidence | null;
  screenplay_state?: ScreenplayState | null;
  screenplay_production?: {
    revision_id?: string;
    operation: "none" | "baseline" | "baseline_rebuild" | "finalize" | "complete";
    mode?: "none" | "baseline" | "baseline_rebuild" | "finalize" | "complete";
    mode_label?: string;
    eligibility?: {
      mode: "none" | "baseline" | "baseline_rebuild" | "finalize" | "complete";
      label: string;
      revision_id: string | null;
      revision_action: "none" | "reuse" | "rebase";
      working_artifact_id: string | null;
      working_compatible: boolean;
      reusable_checkpoint: {
        blueprint_artifact_id?: string;
        identity_artifact_id?: string;
        envelope_artifact_id?: string;
        shards?: Array<Record<string, unknown>>;
        merged_ir_artifact_id?: string;
        shard_progress?: {
          total: number;
          validated: number;
          running: number;
          failed: number;
        };
      };
      reason_code: string;
      reason: string;
      resumable: boolean;
    };
    phase: string;
    phase_label?: string;
    stage_index?: number;
    stage_count?: number;
    /**
     * 旧十步重型流水线遗留的阶段列表（{key, label, status}）。后端正在把集详情投影
     * 统一到 prep_pack_stages 单源；前端已不再读这个字段做任何回退渲染——用户报告过
     * 首屏闪现旧十步阶段带，根因就是曾经的"新字段缺失时回退旧 stages"逻辑，回退
     * 本身已被移除（见 ScriptPage.tsx 的 resolveStages）。字段仍保留在类型里只是
     * 因为后端可能仍在下发，不代表前端会用它。
     */
    stages?: Array<{
      key: string;
      display_name?: string;
      label?: string;
      state?: string;
      status?: string;
    }>;
    /**
     * 转型后的真实轻量流程阶段列表（已定稿，2026-08-24 后端上线）：
     * [{key, display_name, state}]，state: pending/active/done/blocked。
     * 这是阶段带渲染的唯一数据源（见 ScriptPage.tsx 的 resolveStages）：缺失或为
     * 空数组时前端不再回退到上面的旧 `stages`，只渲染轻量占位骨架或完全不渲染。
     * 具体每一项仍过 normalizeStage 的三级文本回退（display_name ?? label ?? key，
     * state ?? status ?? 'pending'）防御，防止这个字段自身漏子字段时渲出空文本。
     */
    prep_pack_stages?: Array<{
      key: string;
      display_name?: string;
      label?: string;
      state?: string;
      status?: string;
    }>;
    baseline_done: boolean;
    first_evaluation_done: boolean;
    task_active: boolean;
    /** 服务端 run 的起止时间（秒）。计时以此为准，不用前端本地起点。 */
    task_started_at?: number | null;
    task_finished_at?: number | null;
    can_resume_baseline?: boolean;
    can_resume_repair: boolean;
    shard_progress?: {
      total: number;
      validated: number;
      running: number;
      failed: number;
    };
    activation_count?: number;
    patch_count?: number;
    open_issue_count?: number;
    quality_score?: number | null;
    quality_issue_count?: number;
    gate_retry_exhausted?: boolean;
    yield_reason?: string;
    stage_stop_reason?: "paused" | "blocked" | "failed" | "";
  } | null;
  shots?: Shot[];
  storyboard_planned_shots?: number | null;
  storyboard_artifact_id?: string | null;
  storyboard_evidence?: ArtifactEvidence | null;
  storyboard_status?: StoryboardStatus | null;
  /** 逐镜生成耗时，键为 shot_no；累计全部重试迭代。 */
  shot_timings?: Record<string, ShotTiming>;
  /** 整集视频生成的总计时。 */
  video_task_timing?: TaskTiming;
  active_storyboard_run_id?: string | null;
  active_video_run_id?: string | null;
  video_completion_mode?: string | null;
  /**
   * 本集绑定的视频生成供应商 key（'hiagent' = Seedance 2.0 / 'minimax_h3' =
   * MiniMax H3）。与生成台强绑定：提交视频生成时后端会拿这个值和模型中心当前
   * 生效供应商比对，不一致即拒绝。切换走 POST .../video-model，见 BoardPage。
   */
  target_video_model?: string | null;
  video_supervisor?: {
    phase?: string | null;
    run_id?: string | null;
    run_status?: string | null;
    outcome?: string | null;
    task_running?: boolean;
    running?: boolean;
    active_media_jobs?: number;
    finished_at?: number | null;
    last_plan?: Record<string, unknown> | null;
  } | null;
  video_budget?: {
    used_cny: number;
    claimed_current_shots: number;
    shots_total: number;
    unclaimed_first_pass_cny: number;
    required_completion_cap_cny: number;
  } | null;
  supervisor?: {
    phase: string;
    repair_epoch: number;
    lifetime_repair_count?: number;
    activation_no?: number;
    activation_attempt_count?: number;
    activation_attempt_limit?: number;
    validated_prefix_end: number;
    next_shot_no: number;
    expected_total: number;
    outcome?: string | null;
    strategy?: string;
    frontier?: number;
    issue_codes?: string[];
    last_repair?: Record<string, unknown> | null;
    pending_control?: { action: string; pending: boolean } | null;
  } | null;
  delivery_artifact_id?: string | null;
  delivery_status?: string;
  pipeline_summary?: EpisodePipelineSummary | null;
}

export interface Project {
  id: string;
  name: string;
  status: string;
  novel_chars: number;
  /** 世界书环节专属文本模型（provider key）；空串＝未设置，回落到系统默认文本模型。 */
  bible_text_provider?: string;
  /** 映射台环节专属文本模型；含义同上。 */
  script_text_provider?: string;
  /** 分镜台环节专属文本模型；含义同上。与「视频模型」是两回事：视频模型控制
   *  实际提交视频生成用哪个供应商，这里控制分镜台自身生成分镜内容用哪个文本模型。 */
  board_text_provider?: string;
  /** 当前可选的文本模型清单，三个环节共用同一份（凭据配置是全局的）。 */
  text_model_choices?: TextModelChoice[];
  /** 各项目级任务的服务端计时。前端本地起点会在刷新后搁浅，一律以此为准。 */
  task_timings?: {
    bible?: TaskTiming;
    refs?: TaskTiming;
    scene_refs?: TaskTiming;
    screenplay_batch?: TaskTiming;
    storyboard_batch?: TaskTiming;
  };
  bible_status: string;
  bible_error?: string;
  bible_style_name?: string | null;
  plan_status: string;
  plan_error?: string;
  bible_version?: number;
  refs_status?: string;
  refs_error?: string;
  refs_target?: string | null;
  scene_refs_status?: string;
  scene_refs_error?: string;
  scene_refs_target?: string | null;
  bible?: Bible | null;
  key_timeline?: string[];
  chapters?: {
    idx: number;
    title: string;
    char_count: number;
    preview?: string;
  }[];
  episodes?: Episode[];
  episodes_total?: number;
  episodes_page?: number;
  episodes_page_count?: number;
  episodes_query?: string;
  episodes_status_filter?: string;
  episodes_busy?: boolean;
  first_chapter_idx?: number | null;
  episode_counts?: {
    total: number;
    done: number;
    screenplay_queued: number;
    screenplay_running: number;
    scripting: number;
    screenplay_todo: number;
    storyboard_ready: number;
  };
  chapter_count?: number;
  episode_count?: number;
  bible_artifact_id?: string | null;
  bible_evidence?: ArtifactEvidence | null;
  harness_engine_enabled?: number | boolean;
  /** 以下仅在 ?view=picker&episode_limit>0 的窗口模式下返回。
   *  整份分集在千集项目里未压缩 250KB，而切换器最多只展示 60 条，
   *  故服务端只回一个窗口，另外把窗口外仍需要的信息单独带上。 */
  episode_total?: number;
  episode_match_total?: number;
  episode_offset?: number;
  episode_index?: number | null;
  episode_current?: Episode | null;
  episode_prev?: Pick<Episode, "id" | "episode_no" | "title"> | null;
  episode_next?: Pick<Episode, "id" | "episode_no" | "title"> | null;
}

/** GET /projects 项目列表——App.tsx::refreshProjects、Studio.tsx。 */
export function listProjects(): Promise<Project[]> {
  return get("/projects");
}

/** 项目详情，`query` 是不带前导 `?` 的完整 query string（view=xxx&...），
 *  由调用方按各自需要的视图拼装——App.tsx::useProject、EpisodesPage 分页查询、
 *  EpisodeCrumb 分集切换器都走这一个方法，只是各自传不同的 query。 */
export function getProject(projectId: string | null, query?: string): Promise<Project> {
  return get(`/projects/${projectId}${query ? `?${query}` : ""}`);
}

/** 分集详情，`query` 同上（view=script/board/wall/cinema）——App.tsx::useEpisode。 */
export function getEpisode(episodeId: string, query?: string): Promise<Episode> {
  return get(`/episodes/${episodeId}${query ? `?${query}` : ""}`);
}

export function getChapter(projectId: string, idx: number): Promise<ChapterContent> {
  return get(`/projects/${projectId}/chapters/${idx}`);
}

export function importProject(body: {
  attachment_token: string;
  name: string;
  style_name?: string; // 统一画风预设名，导入时一次性选定并落进项目 world
}): Promise<{
  project_id: string;
  ingestion: { chapter_count: number; total_chars: number; auto_split?: boolean };
  episode_planning?: { status?: string };
  asset_generation?: { status?: string };
}> {
  return mutate("POST", "/projects/import", body);
}

export function uploadNovelAttachment(form: FormData): Promise<{ attachment_token: string }> {
  return request("POST", "/attachments/novel", undefined, { form });
}

/** 软删除：项目移入回收站，数据与产物原样保留，24 小时后自动彻底清理。 */
export function deleteProject(projectId: string) {
  return mutate("DELETE", `/projects/${projectId}`);
}

/** 回收站条目：list_deleted_projects 在 Project 字段之外多回 deleted_at / purge_at /
 *  retention_seconds_remaining，用于展示"剩余保留时间"。 */
export interface DeletedProject extends Project {
  deleted_at: number;
  purge_at: number;
  retention_seconds_remaining: number;
}

/** GET /projects/deleted 回收站列表——项目空间管理页的回收站入口。 */
export function listDeletedProjects(): Promise<DeletedProject[]> {
  return get("/projects/deleted");
}

/** 从回收站恢复项目：清空软删除标记，数据与产物本就未被改动。 */
export function restoreProject(projectId: string) {
  return mutate("POST", `/projects/${projectId}/restore`);
}

/** 彻底删除某个已软删除的项目：物理删除数据库行与磁盘产物，不可恢复。 */
export function purgeProject(projectId: string) {
  return mutate("DELETE", `/projects/${projectId}/purge`);
}

/** 一键清空回收站：彻底删除全部已软删除的项目，不可恢复。
 *  failed 非空表示部分项目本次未能清理（例如供应商任务尚未到终态），
 *  不阻塞其余项目——留给下一次操作或 24 小时自动巡检重试。 */
export function purgeAllDeletedProjects(): Promise<{
  purged: string[];
  purged_count: number;
  failed: { project_id: string; error_id: string; error: string }[];
}> {
  return mutate("DELETE", "/projects/deleted");
}

export function setStageTextModel(
  projectId: string,
  body: Partial<Record<
    "bible_text_provider" | "script_text_provider" | "board_text_provider",
    string
  >>,
) {
  return mutate("PUT", `/projects/${projectId}/text-models`, body);
}

export function replanEpisodes(projectId: string) {
  return mutate("POST", `/projects/${projectId}/plan`);
}

export function generateAllScreenplays(projectId: string): Promise<{ started: number }> {
  return mutate("POST", `/projects/${projectId}/screenplay-all`);
}

export function generateAllStoryboards(projectId: string): Promise<{ started: number }> {
  return mutate("POST", `/projects/${projectId}/storyboard-all`);
}

export function cancelAllScreenplays(projectId: string): Promise<{ stopped: number }> {
  return mutate("POST", `/projects/${projectId}/screenplay-all/cancel`);
}

export function deleteEpisode(episodeId: string): Promise<{ renumbered?: number }> {
  return mutate("DELETE", `/episodes/${episodeId}`);
}

export function setEpisodeTargetDuration(
  episodeId: string,
  body: { target_duration_s: number },
) {
  return mutate("PUT", `/episodes/${episodeId}/target-duration`, body);
}

export interface StoryboardMetrics {
  active_storyboard_runs: number;
  scripting_episodes: number;
  waiting_human: number;
  paused: number;
  repairing: number;
  waiting_authorization?: number;
  phase_counts: Record<string, number>;
}

export function getStoryboardMetrics(projectId: string | null): Promise<StoryboardMetrics> {
  return get(`/projects/${projectId}/storyboard-metrics`);
}

/** 分集规划页的分页查询——EpisodesPage.tsx。 */
export function listEpisodesPage(
  projectId: string | null,
  params: { page: number; pageSize: number; query: string; statusFilter: string },
): Promise<Project> {
  const query =
    `view=episodes&page=${params.page}&page_size=${params.pageSize}` +
    `&query=${encodeURIComponent(params.query)}&status_filter=${encodeURIComponent(params.statusFilter)}`;
  return get(`/projects/${projectId}?${query}`);
}
