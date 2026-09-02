export interface StoryboardStatus {
  contract_version: string;
  snapshot_version: number;
  state_fingerprint: string;
  /** 服务端 run 的起止时间（秒）。任务计时以此为准，不用前端本地起点。 */
  task_started_at?: number | null;
  task_finished_at?: number | null;
  state:
    | "no_screenplay"
    | "empty"
    | "running"
    | "paused"
    | "failed"
    | "ready_to_confirm"
    | "confirmed"
    | "syncing";
  headline: string;
  screenplay_available: boolean;
  task_phase?: string | null;
  planned_shots: number;
  produced_shots: number;
  validated_shots: number;
  /** 当前仍保留、可在页面审阅的草稿镜头数。 */
  draft_shots?: number;
  /** 连续通过结构检查、可作为安全恢复点保留的镜头数。 */
  safe_checkpoint_shots?: number;
  /** 安全恢复点之后、继续任务时可能更新的现有草稿镜头数。 */
  pending_revalidation_shots?: number;
  resume_from_shot?: number;
  /** 继续任务是生成后续镜头、修复现有完整分镜，还是只签发发布证据。 */
  resume_mode?: "continue_generation" | "repair_existing" | "finalize_evidence" | null;
  final_shot_valid: boolean;
  hard_gates_passed: boolean;
  hard_gate_issue_count?: number;
  hard_gate_issues?: string[];
  system_error?: string | null;
  feature_flags?: {
    safe_readonly: boolean;
    structure_edit: boolean;
    source_rebind: boolean;
  };
  confirmed: boolean;
  editable: boolean;
  confirmable: boolean;
  recommended_action:
    | "go_screenplay"
    | "generate_storyboard"
    | "view_progress"
    | "resume_storyboard"
    | "confirm_storyboard"
    | "go_review_wall"
    | "refresh_status";
  write_block_reason?: string | null;
}

/** 分镜台「开始生成」预检响应——BoardPage.tsx::loadStartPreview。 */
export type StartPreview = {
  preview_token: string;
  action: "create" | "resume";
  resume_mode?: "create" | "continue_generation" | "repair_existing" | "finalize_evidence" | null;
  kept_validated_shots: number;
  planned_shots?: number | null;
  remaining_shots?: number | null;
  checkpoint: { available: boolean; phase?: string | null; resume_from_shot: number };
  can_start?: boolean;
  blocking_reason?: string | null;
  current_gate_issue_count?: number;
  current_gate_issues?: string[];
  warning?: string | null;
  repair?: {
    lifetime_repair_count: number;
    activation_no: number;
    activation_attempt_count: number;
    max_attempts_per_activation: number;
    external_calls: number;
    cache_reuses: number;
    candidate_preserves_official_shots: boolean;
    last_issue_messages: string[];
  };
};

/** 分镜确认前预检响应——BoardPage.tsx::runPrimary('confirm_storyboard') 分支。 */
export type ConfirmPreview = {
  preview_token?: string;
  storyboard_artifact_id?: string | null;
  shot_count: number;
  planned_shots: number;
  total_duration_s: number;
  final_shot_valid: boolean;
  hard_gates: { passed: boolean; errors: string[] };
  warnings: string[];
  unlocks: string[];
  recovery_action?: string | null;
};

/** 清空视频提示词前预检响应——BoardPage.tsx::previewClearStoryboard。 */
export type StoryboardClearPreview = {
  preview_token: string;
  shot_count: number;
  video_version_count: number;
  reference_asset_count: number;
  workflow_run_count: number;
  delivery_package_count: number;
  active_task_will_stop: boolean;
  screenplay_preserved: true;
  irreversible: true;
};

/** 切换视频模型撞上已有产物时，409 详情里的确认信息——BoardPage.tsx::submitVideoModel。 */
export type VideoModelSwitchConfirm = {
  requested_target_video_model: string;
  current_target_video_model: string;
  prompt_artifact_count: number;
};

export type VideoModelSwitchResult = {
  changed: boolean;
  cleared_videos: number;
  target_video_model: string;
};

// 供应商付费任务尚未终态时的清空阻塞（app/completion_grant.py
// ProviderTasksNotTerminalError.detail）；recovery_action 是后端给出的下一步
// 建议，前端只做文案翻译，不臆造新含义。
export type ProviderTaskBlocker = {
  job_id: string;
  shot_id: string | null;
  version_id: string | null;
  job_status: string;
  provider_operation_id: string | null;
  provider_task_id: string | null;
  provider_create_state: string;
  claim_status: string | null;
  amount_cny: number;
  recovery_status: "waiting_provider" | "waiting_human" | string;
  recovery_action: "review_provider_failure" | "continue_provider_poll" | "restore_provider_poll" | "reconcile_provider_create" | string;
};

export type ProviderTaskClearance = {
  safe_to_clear: boolean;
  resume_supported: boolean;
  blockers: ProviderTaskBlocker[];
};

export type ProviderTaskReconcileResult = {
  episode_id: string;
  blockers_before: number;
  provider_confirmed_terminal_job_ids: string[];
  superseded_jobs_closed_job_ids: string[];
  clearance: ProviderTaskClearance;
};
