import type { ArtifactEvidence } from "../common";
import type { Dialogue } from "../screenplay";
import type { ShotVideoGenerationPlan } from "../video";
import type { ShotPipelineStatus, ShotVersion } from "./versions";
import type { StoryboardPackSegment } from "./pack";

interface AudioTimelineItem {
  start_s: number;
  end_s: number;
  type: string;
  speaker_id?: string | null;
  text: string;
  lip_sync?: boolean;
  emotion?: string;
}

interface RequiredText {
  surface: string;
  exact_text: string;
  strategy?: "audio_only" | "deterministic_insert" | "embedded_prop" | "none" | string;
  delivery_owner_shot_no?: number | null;
  appear_start_s?: number;
  stable_until_s?: number | null;
  style?: string;
  allow_other_text?: boolean;
  max_other_text?: number;
  font_role?: string;
  reading_priority?: string;
}

interface ContinuityState {
  scene?: {
    scene_revision_id?: string;
    time_of_day?: string;
    lighting_state?: string;
    axis_id?: string;
    landmarks?: Record<string, string>;
  };
  characters?: Record<string, {
    look_revision_id?: string;
    outfit_revision_id?: string;
    visibility?: string;
    screen_side?: string;
    pose?: string;
    facing?: string;
    gaze_target?: string;
    left_hand?: string;
    right_hand?: string;
  }>;
  props?: Record<string, {
    canonical_name?: string;
    revision_id?: string;
    owner?: string;
    location?: string;
    form?: string;
    visibility?: string;
    text_state?: string;
    required?: boolean;
  }>;
}

export interface ShotContribution {
  shot_contribution_id: string;
  experience_intent_ids: string[];
  target_delta_ids: string[];
  assimilation_task_ids: string[];
  evidence_ids: string[];
  story_delta_fact_ids: string[];
  character_state_delta_ids: string[];
  audience_state_delta_ids: string[];
  affective_delta: Record<string, unknown>;
  spatial_temporal_delta: Record<string, unknown>;
  dramatic_pressure_delta: number;
}

export interface ShotCapacityBudget {
  action_phase_s: number;
  spoken_and_text_s: number;
  attention_switch_s: number;
  inference_processing_s: number;
  reaction_registration_s: number;
  spatial_reorientation_s: number;
  entry_exit_settle_s: number;
  other_s: number;
  other_reason?: string | null;
}

export interface AudienceStatePathRef {
  audience_prior_id: string;
  audience_state_in_id: string;
  audience_state_out_target_id: string;
}

export interface NarrativeBoundaryContract {
  boundary_id: string;
  previous_shot_id: string;
  next_shot_id: string;
  narrative_relation: string;
  required_state_invariants: string[];
  allowed_state_deltas: string[];
  state_delta_transitions: Array<{
    transition_id: string;
    basis_type: string;
    source_fact_id?: string | null;
    target_fact_id?: string | null;
    basis_action_phase_id?: string | null;
    custom_basis?: string | null;
    reason: string;
  }>;
  forbidden_replay_action_ids: string[];
  handoff_action_phase_id?: string | null;
  spatial_orientation_contract: Record<string, unknown>;
  temporal_orientation_contract: Record<string, unknown>;
  audience_state_handoffs: Record<string, unknown>[];
  affective_handoff: Record<string, unknown>;
  cut_motivation: string;
}

export interface Shot {
  id: string;
  episode_id: string;
  script_id?: string | null;
  shot_no: number;
  duration_s: number;
  shot_size: string;
  camera_angle?: string;
  camera_move: string;
  scene_time: string;
  scene_name: string;
  scene_setting: string;
  characters: string[];
  action_desc: string;
  first_frame_desc: string;
  last_frame_desc: string;
  source_excerpt: string;
  narration: string | null;
  dialogues: Dialogue[];
  transition: string;
  spoken_content_chars?: number;
  spoken_limit?: number;
  has_legacy_narration?: boolean;
  spoken_contract_status?: "coherent" | "conflict" | "legacy" | string;
  spine_beat_ids?: string[];
  key_line_ids?: string[];
  information_ids?: string[];
  continuity_from_prev: number;
  adopted_version_id: string | null;
  story_event_id?: string;
  purpose?: string;
  context_requirement_ids?: string[];
  resulting_change?: string;
  readability_focus?: "context" | "action" | "emotion" | "dialogue" | "evidence" | "transition" | string;
  camera_motivation?: string;
  repeat_of_shot_id?: string | null;
  repeat_gain?: string;
  new_information_ids?: string[];
  new_information_items?: {
    info_id: string;
    content: string;
    source?: "ledger" | "derived";
  }[];
  reinforcement_info_ids?: string[];
  state_in?: string;
  primary_action?: string;
  state_out?: string;
  observed_state_out?: string;
  continuity_mode?: string;
  characters_visible?: string[];
  audio_cast?: string[];
  audio_timeline?: AudioTimelineItem[];
  required_text?: RequiredText | null;
  continuity_state_in?: ContinuityState;
  continuity_state_out?: ContinuityState;
  reference_roles?: string[];
  do_not_repeat?: string[];
  risk_tags?: string[];
  prompt_contract_version?: string;
  legacy_unvalidated?: boolean;
  shot_id?: string;
  scene_id?: string;
  event_ids?: string[];
  primary_action_id?: string | null;
  supporting_action_ids?: string[];
  action_phase_ids?: string[];
  visible_entity_ids?: string[];
  offscreen_action_actor_ids?: string[];
  offscreen_action_target_ids?: string[];
  capacity_budget?: ShotCapacityBudget | null;
  shot_contribution?: ShotContribution | null;
  audience_state_paths?: AudienceStatePathRef[];
  planned_state_in_fact_ids?: string[];
  planned_delta_add_fact_ids?: string[];
  planned_delta_remove_fact_ids?: string[];
  planned_state_out_fact_ids?: string[];
  completed_before_action_ids?: string[];
  completed_before_action_phase_ids?: string[];
  reserved_future_event_ids?: string[];
  readability_window_ids?: string[];
  narrative_boundary_from_previous?: NarrativeBoundaryContract | null;
  mode_plan?: ShotVideoGenerationPlan | null;
  is_final?: boolean;
  preflight_errors?: string[];
  qa_warnings?: string[];
  prompt_preview?: string;
  versions: ShotVersion[];
  version_count?: number;
  video_stale: boolean;
  video_status?:
    | "pending_generation"
    | "generating"
    | "pending_adoption"
    | "adopted"
    | "generation_failed"
    | null;
  video_grade?: "A" | "B" | "C" | null;
  fallback_reason?: string | null;
  continuity_degraded?: boolean;
  storyboard_artifact_id?: string | null;
  storyboard_evidence?: ArtifactEvidence | null;
  source_binding?: {
    shot_id?: string;
    chapter_id: number;
    chapter_idx: number;
    source_version_hash: string;
    start_offset: number;
    end_offset: number;
    excerpt_hash: string;
  } | null;
  pipeline?: ShotPipelineStatus | null;
  /** 非 null 时这一行是分镜台 2.0.0 的 15 秒段落，见 StoryboardPackSegment 注释。 */
  storyboard_pack_segment?: StoryboardPackSegment | null;
}

