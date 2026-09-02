import type { ReferenceImage } from "../bible";
import type { VideoGenerationMode, VideoInputIntent } from "../video";

export interface ShotVersion {
  id: string;
  version_no: number;
  prompt_text: string;
  status: string;
  error?: string;
  video_url?: string;
  qa?: {
    overall: number;
    issues: string[];
    failure_types?: string[];
    observed_state_out?: string;
    start_state_match?: number | boolean;
    end_state_match?: number | boolean;
  } | null;
  latency_s: number;
  /** 在跑任务的服务端起点（秒）；有值表示这条正在生成，用于实时计时。 */
  running_since?: number | null;
  /** 供应商任务编号（app/media_exec/run_job.py 写入 shot_versions.provider_task_id）；
   *  失败/隔离候选排障用，原样透传不翻译。 */
  provider_task_id?: string | null;
  artifact_id?: string | null;
  adoption_reason?: string | null;
  playback_rate?: number | null;
  technical_validation_json?: string | null;
  created_at?: number | null;
  image_inputs?: {
    first_frame_used?: boolean;
    first_frame_src?: string | null;
    first_frame_source?: string | null;
    first_frame_scene_id?: string | null;
    first_frame_image_url?: string | null;
    last_frame_used?: boolean;
    last_frame_src?: string | null;
    last_frame_source?: string | null;
    last_frame_scene_id?: string | null;
    last_frame_image_url?: string | null;
    video_input_url?: string | null;
    video_input_source_revision_id?: string | null;
    mode?: VideoGenerationMode;
    planned_mode?: VideoGenerationMode;
    actual_mode?: VideoGenerationMode | null;
    video_input_intent?: VideoInputIntent | null;
    episode_video_plan_id?: string | null;
    shot_plan_id?: string | null;
    plan_revision?: number | null;
    capability_snapshot_id?: string | null;
    after_shot_id?: string | null;
    plan_status?: string | null;
    degraded_reason?: string | null;
    stale?: boolean;
    stale_reason?: string | null;
    ai_video_prompt_contract_version?: string | null;
    ai_video_prompt_generated_at?: number | null;
    required_reference_characters?: string[];
    required_interaction_reference_characters?: string[];
    reference_image_used?: boolean;
    reference_images?: ReferenceImage[];
    reference_failure_logs?: {
      type?: string;
      reason?: string;
      error?: string;
      fallback?: string;
      qa?: { overall?: number; issues?: string[] };
    }[];
    fallback_reason?: string | null;
    retry_reason?: string | null;
    /** image_inputs 体积超上限被后端整块裁掉（_public_shot_versions 的
     *  _MAX_PUBLIC_IMAGE_INPUT_CHARS）；此时上面各字段是缺省值而非事实，
     *  尤其 reference_images 空数组只代表「没下发」，不代表「没参考图」。 */
    omitted_for_size?: boolean;
  };
}

export interface ShotPipelineStatus {
  /** 持久化媒体任务已创建；不等于供应商已接单。 */
  task_accepted?: boolean;
  task_id?: string | null;
  task_created_at?: number | null;
  task_updated_at?: number | null;
  next_retry_at?: number | null;
  retry_count?: number;
  /** 供应商已返回 task id，表示实际生成请求已下发上游。 */
  provider_submitted?: boolean;
  video_status?:
    | "pending_generation"
    | "generating"
    | "pending_adoption"
    | "adopted"
    | "generation_failed";
  video_status_label?: "待生成" | "生成中" | "待采纳" | "已采纳" | "生成失败";
  pipeline_status: string;
  pipeline_stage?: string | null;
  current_stage?: string | null;
  stage_label?: string | null;
  stage_progress?: {
    current?: number;
    total?: number;
    unit?: string;
    attempt?: number;
    attempt_limit?: number;
  } | null;
  queue_position?: number | null;
  provider_elapsed_s?: number | null;
  stage_elapsed_s?: number | null;
  stage_started_at?: number | null;
  reference_progress?: { done: number; total: number } | null;
  candidate_count: number;
  retake_count: number;
  attempt?: number;
  attempt_limit?: number;
  blocked_reason?: string | null;
  reason_code?: string | null;
  reason_text?: string | null;
  scheduler_lane?: string | null;
  blocked_by_shot_id?: string | null;
  next_stage?: string | null;
  state_revision?: number;
  estimated_start_at?: number | null;
  estimated_finish_at?: number | null;
}

export interface EpisodePipelineSummary {
  shots_total: number;
  adopted: number;
  with_candidate: number;
  upstream_generating: number;
  preparing_references: number;
  video_ready?: number;
  waiting_continuity?: number;
  video_qa?: number;
  queued: number;
  waiting_human: number;
  failed?: number;
  paused?: number;
  video_status_counts?: {
    pending_generation: number;
    generating: number;
    pending_adoption: number;
    adopted: number;
    generation_failed: number;
  };
}
