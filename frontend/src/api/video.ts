import { request } from "./client";
import type { NumberConstraint } from "./common";

// VideoGenerationMode 的规范定义放在这里（video 域）而不是 storyboard 域：
// storyboard/versions.ts 的 ShotVersion.image_inputs 也要用它，若定义在
// storyboard/ 会与本文件的 ShotVideoGenerationPlan 互相 import 造成循环。
export type VideoGenerationMode =
  | "REFERENCE_IMAGE_MODE"
  | "FIRST_FRAME_MODE"
  | "FIRST_LAST_FRAME_MODE"
  | "VIDEO_INPUT_MODE";

export type VideoInputIntent =
  | "CONTINUE_PREVIOUS_TAKE"
  | "MOTION_REFERENCE"
  | "CAMERA_REFERENCE"
  | "RHYTHM_REFERENCE"
  | "AUDIO_REFERENCE";

export type VideoPlanAssetSource =
  | "ASSET_REVISION"
  | "STATIC_BOUNDARY_ASSET"
  | "PREVIOUS_STATIC_TAIL"
  | "PREVIOUS_ADOPTED_TAIL"
  | "PREVIOUS_ADOPTED_VIDEO";

export type VideoPlanAssetRole =
  | "identity_reference"
  | "scene_reference"
  | "prop_reference"
  | "style_reference"
  | "first_frame"
  | "last_frame"
  | "previous_adopted_video"
  | "motion_reference_video"
  | "camera_reference_video"
  | "audio_reference_video";

export interface VideoPlanAssetRequirement {
  role: VideoPlanAssetRole;
  source: VideoPlanAssetSource;
  asset_revision_id?: string | null;
  source_shot_id?: string | null;
  fingerprint?: string | null;
}

export interface ShotVideoRelations {
  temporal: "same_moment" | "elapsed" | "jump" | "new_domain" | "unknown";
  spatial: "same_space" | "adjacent_space" | "new_space" | "unknown";
  edit:
    | "continuous_take"
    | "match_cut"
    | "angle_cut"
    | "reaction_cut"
    | "reverse_angle"
    | "insert_cut"
    | "montage"
    | "scene_cut"
    | "unknown";
  action:
    | "continues_same_action"
    | "starts_new_action"
    | "shows_result"
    | "observes_result"
    | "no_action"
    | "unknown";
}

export interface ShotVideoGenerationPlan {
  shot_plan_id: string;
  episode_video_plan_id: string;
  plan_revision: number;
  source_storyboard_revision_id: string;
  shot_id: string;
  published_shot_id: string;
  shot_no: number;
  mode: VideoGenerationMode;
  planned_mode: VideoGenerationMode | null;
  actual_mode: VideoGenerationMode | null;
  video_input_intent: VideoInputIntent | null;
  depends_on_shot_id: string | null;
  relations: ShotVideoRelations;
  state_dependency: "none" | "start_only" | "start_and_end" | "full_trajectory";
  motion_dependency: "none" | "pose" | "trajectory" | "camera" | "rhythm" | "audio";
  required_assets: VideoPlanAssetRequirement[];
  reason_codes?: string[];
  confidence: number;
  unknown_dimensions: string[];
  fallback_order: VideoGenerationMode[];
  max_attempts: number;
  max_cost: number;
  timeout_s: number;
  estimated_latency_ms: number;
  estimated_cost: number;
  critical_path_group: string | null;
  capability_snapshot_id: string;
  input_revision_fingerprints: Record<string, string>;
  status: string;
  degraded_from_mode: VideoGenerationMode | null;
  degraded_to_mode: VideoGenerationMode | null;
  degraded_reason: string | null;
}

/** POST /episodes/{id}/generate 单条镜头入队结果——成功态在 job_id/reused/active
 *  之外还可能带 version_id（幂等复用了一条已交付版本）；失败态只有 error/issue_codes，
 *  整批请求仍返回 200，不会因为其中一段失败让其余段落一起回滚（见
 *  app/domain/video_ops.py::_generate_episode_core）。 */
/** reused=true 时说明"为什么没有新建"——不是猜的，是服务端按命中的那条
 *  记录的真实状态直接翻译过来的（见 app/media_exec/enqueue.py
 *  ``_reused_reason_for_status``）：
 *  - "succeeded"：已有交付版本，无需重新生成；
 *  - "in_flight"：仍在排队/生成/等待供应商轮询，重复提交只会双花；
 *  - "stuck_needs_human"：命中的记录卡在需要人工处理，没有新任务被提交
 *    ——这种情况不应该被算进"已提交"。 */
export type ReusedReason = 'succeeded' | 'in_flight' | 'stuck_needs_human';

export interface EpisodeGenerateEnqueueResult {
  shot_id: string;
  reused?: boolean;
  reused_reason?: ReusedReason;
  job_id?: string;
  task_accepted?: boolean;
  active?: boolean;
  version_id?: string;
  error?: string;
  issue_codes?: string[];
}

export interface EpisodeGenerateResult {
  episode_video_plan_id: string;
  plan_revision: number;
  mode_distribution: Record<string, number>;
  critical_path_latency_ms: number;
  estimated_cost: number;
  enqueued: EpisodeGenerateEnqueueResult[];
  /** only_incomplete=true 时被跳过的已完成段数——服务端口径，不是前端猜的。 */
  skipped_completed: number;
  /** 本次实际提交入队循环的段数（已完成段被 only_incomplete 过滤后剩下的）。 */
  selected_shots: number;
  recovered_partial_operation?: boolean;
  remaining_requires_new_idempotency_key?: boolean;
}

export interface ReviewUpstreamSnapshot {
  episode_id: string;
  episode_status: string;
  published_screenplay_artifact_id?: string | null;
  confirmed_storyboard_artifact_id?: string | null;
  active_upstream_runs: Array<{
    kind: string;
    run_id?: string | null;
    status: string;
    stage?: string | null;
  }>;
  qualification_version: string;
  /** 按镜作用域的资格版本：本镜生成/采纳只应比对自己这一份——兄弟镜新增
   * 素材不会改变它，真正的本镜素材漂移或上游变化仍会改变它。 */
  shot_qualification_versions?: Record<string, string>;
  eligible_for_production: boolean;
  blockers: string[];
  assets: {
    eligible: boolean;
    status: string;
    checked_inputs: number;
    blockers: Array<Record<string, unknown>>;
    soft_warnings: Array<Record<string, unknown>>;
  };
  server_time: number;
}

export interface ReviewWallContext {
  episode_id: string;
  object_version: string;
  upstream: ReviewUpstreamSnapshot;
  archived_versions: Record<
    string,
    { version_id: string; reason?: string | null; archived_at: number }
  >;
  authorization_constraints: {
    budget_cap_cny: NumberConstraint;
    wall_clock_cap_s: NumberConstraint;
    add_budget_cny: NumberConstraint;
    add_wall_clock_s: NumberConstraint;
  };
  server_time: number;
}

export const api_video = {
  episodeGenerate: (
    episodeId: string,
    idempotencyKey: string,
    options?: { onlyIncomplete?: boolean; qualificationVersion?: string },
  ): Promise<EpisodeGenerateResult> =>
    request("POST", `/episodes/${episodeId}/generate`, {
      idempotency_key: idempotencyKey,
      only_incomplete: options?.onlyIncomplete,
      qualification_version: options?.qualificationVersion,
    }),
  projectVideoCompletion: (projectId: string, body?: Record<string, unknown>) =>
    request("POST", `/projects/${projectId}/video-completion`, body || {}),
  shotGenerate: (
    shotId: string,
    promptOverride?: string,
    reroll?: boolean,
    withCritique?: boolean,
    qualificationVersion?: string,
    idempotencyKey?: string,
  ) =>
    request("POST", `/shots/${shotId}/generate`, {
      prompt_override: promptOverride,
      reroll,
      with_critique: Boolean(withCritique),
      qualification_version: qualificationVersion,
      idempotency_key: idempotencyKey,
    }),
  getReviewContext: (episodeId: string) =>
    request(
      "GET",
      `/episodes/${episodeId}/review-context`,
    ) as Promise<ReviewWallContext>,
};
