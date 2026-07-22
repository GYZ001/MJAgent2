class ApiError extends Error {
  // code/category/errorId 来自后端报错码系统：技术类报错前端只拿到这三样，原文留后端日志。
  constructor(
    public status: number,
    message: string,
    public code?: string,
    public category?: string,
    public errorId?: string,
  ) { super(message) }
}

async function handle(resp: Response) {
  if (resp.ok) return resp.json()
  let detail = `HTTP ${resp.status}`
  let code: string | undefined
  let category: string | undefined
  let errorId: string | undefined
  try {
    const body = await resp.json()
    detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    code = typeof body.code === 'string' ? body.code : undefined
    category = typeof body.category === 'string' ? body.category : undefined
    errorId = typeof body.error_id === 'string' ? body.error_id : undefined
  } catch { /* keep default */ }
  // 后端已把展示串拼进 detail（业务类=友好提示+码；技术类=安全提示+码+ID）；
  // 兜底：万一旧响应只有 code/category 而 detail 不含码，补一段可读后缀。
  let message = detail
  if (code && errorId && !detail.includes(errorId)) {
    message = `${detail}（${category ?? ''}${category ? ' · ' : ''}${code} · ${errorId}）`
  }
  throw new ApiError(resp.status, message, code, category, errorId)
}

export const api = {
  get: (path: string) => fetch(`/api${path}`).then(handle),
  post: (path: string, body?: unknown) =>
    fetch(`/api${path}`, {
      method: 'POST',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }).then(handle),
  put: (path: string, body: unknown) =>
    fetch(`/api${path}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(handle),
  del: (path: string) => fetch(`/api${path}`, { method: 'DELETE' }).then(handle),
  upload: (path: string, form: FormData) =>
    fetch(`/api${path}`, { method: 'POST', body: form }).then(handle),

  /* ── 便捷方法 ── */
  episodeGenerate: (episodeId: string) =>
    fetch(`/api/episodes/${episodeId}/generate`, { method: 'POST' }).then(handle),
  sceneGenerate: (shotId: string, kinds?: ('head' | 'tail')[]) =>
    fetch(`/api/shots/${shotId}/scene`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kinds }),
    }).then(handle),
  shotGenerate: (shotId: string, promptOverride?: string, reroll?: boolean, withCritique?: boolean) =>
    fetch(`/api/shots/${shotId}/generate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt_override: promptOverride, reroll, with_critique: withCritique,
      }),
    }).then(handle),
  sceneApprove: (shotId: string, sceneId: string, kind: string, reason?: string) =>
    fetch(`/api/shots/${shotId}/scene/approve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scene_id: sceneId, kind, reason }),
    }).then(handle),
  adoptVersion: (shotId: string, versionId: string, reason?: string) =>
    fetch(`/api/shots/${shotId}/adopt`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version_id: versionId, reason }),
    }).then(handle),
  deleteVersion: (versionId: string) =>
    fetch(`/api/versions/${versionId}`, { method: 'DELETE' }).then(handle),
  discardReferenceImage: (versionId: string, refId: string) =>
    fetch(`/api/versions/${versionId}/reference-images/${refId}`, { method: 'DELETE' }).then(handle),
  restoreReferenceImage: (versionId: string, refId: string) =>
    fetch(`/api/versions/${versionId}/reference-images/${refId}/restore`, { method: 'POST' }).then(handle),
  clearEpisodeArtifacts: (episodeId: string) =>
    fetch(`/api/episodes/${episodeId}/clear-artifacts`, { method: 'POST' }).then(handle),
  clearShotArtifacts: (shotId: string) =>
    fetch(`/api/shots/${shotId}/clear-artifacts`, { method: 'POST' }).then(handle),
  /* 场景图素材库 */
  genSceneBible: (projectId: string) =>
    fetch(`/api/projects/${projectId}/scene-bible`, { method: 'POST' }).then(handle),
  genSceneRefs: (projectId: string, scene?: string) =>
    fetch(`/api/projects/${projectId}/scene-refs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scene }),
    }).then(handle),
  cancelSceneRefs: (projectId: string) =>
    fetch(`/api/projects/${projectId}/scene-refs/cancel`, { method: 'POST' }).then(handle),
  editScenePrompt: (projectId: string, sceneName: string, scenePrompt: string) =>
    fetch(`/api/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/prompt`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scene_prompt: scenePrompt }),
    }).then(handle),
}

export interface RunSummary {
  id: string
  workflow_type: string
  scope_type: string
  scope_id: string
  status: string
  current_step_key?: string | null
  cost_cny: number
  budget_limit_cny?: number | null
  started_at?: number | null
  updated_at: number
  finished_at?: number | null
  failure_code?: string | null
  failure_message?: string | null
  resume_from_step?: string | null
}

export interface StepRun {
  id: string
  run_id: string
  step_key: string
  iteration_no: number
  status: string
  decision?: string | null
  exit_reason?: string | null
  started_at?: number | null
  finished_at?: number | null
  latency_ms: number
  output_artifact_id?: string | null
  error_message?: string | null
}

export interface RunEvent {
  id: string
  run_id: string
  step_run_id?: string | null
  ts: number
  event_type: string
  severity: string
  message: string
  payload?: Record<string, unknown>
}

export interface EvidenceIssue {
  code: string
  severity: 'info' | 'warning' | 'blocker'
  subject: string
  message: string
  repair_hint?: string | null
  repairable: boolean
}

interface EvaluationSummary {
  id: string
  evaluator_type: string
  evaluator_name: string
  evaluator_version: string
  status: string
  hard_gate_passed: number | boolean
  score?: number | null
  issues?: EvidenceIssue[]
  evidence?: Record<string, unknown>
  recovered: number | boolean
}

export interface ArtifactEvidence {
  id: string
  type: string
  version: number
  status: string
  trust_level: string
  content_hash: string
  contract_version?: string | null
  prompt_version?: string | null
  parent_artifact_ids?: string[]
  stale_reason?: string | null
  evaluations: EvaluationSummary[]
}

interface Dialogue { speaker: string; line: string; emotion: string }

interface ScreenplayBeat {
  beat_no: number
  day_offset: number
  time_of_day: string
  location: string
  characters: string[]
  dramatic_event: string
  visible_action: string
  key_dialogues: string[]
  turn: string
  carry: string
  beat_type: string
  source_excerpt: string
}

export interface ScriptScene {
  scene_no: number
  scene_heading: string
  story_function: string
  characters: string[]
  summary: string
  conflict?: string
  turn?: string
  source_basis?: string
}

export interface EpisodeScreenplay {
  id?: string | null
  episode_no: number
  mode?: 'full_script' | string
  title?: string
  source_text_range?: string
  logline?: string
  script_format_note?: string
  dramatic_question?: string
  protagonist_goal?: string
  obstacle?: string
  stakes?: string
  key_lines?: string[]
  key_plot_points?: string[]
  scene_outline?: ScriptScene[]
  full_script_text?: string
  character_state_changes?: string[]
  emotional_curve?: string
  ending_hook?: string
  source_basis?: string
  adaptation_direction?: string
  opening?: string
  development?: string
  conflict?: string
  climax?: string
  created_at?: number | null
  updated_at?: number | null
  beats: ScreenplayBeat[]
}

export interface ReferenceImage {
  id: string; image_url?: string | null; type: string; source: string
  qualityScore?: number | null; selectedForSeedance?: boolean; deleted?: boolean
  rejectReason?: string | null; qa?: { overall?: number; issues?: string[] } | null
}

export interface ShotVersion {
  id: string; version_no: number; prompt_text: string; status: string
  error?: string; video_url?: string; qa?: { overall: number; issues: string[] } | null
  cost_cny: number; latency_s: number
  artifact_id?: string | null; adoption_reason?: string | null
  technical_validation_json?: string | null
  image_inputs?: {
    reference_image_used?: boolean
    reference_images?: ReferenceImage[]
    reference_failure_logs?: { type?: string; reason?: string; error?: string; fallback?: string; qa?: { overall?: number; issues?: string[] } }[]
    fallback_reason?: string | null
    retry_reason?: string | null
  }
}

interface SceneQa {
  overall: number; expectation_match?: number; continuity?: number; clean_frame?: number; issues?: string[]
}
interface SceneCandidate {
  id: string; version_no: number; kind: 'head' | 'tail'; status: string; error?: string
  qa?: SceneQa | null; image_url?: string
  artifact_id?: string | null; adoption_reason?: string | null
}

export interface Shot {
  id: string; episode_id: string; script_id?: string | null; shot_no: number; duration_s: number; shot_size: string; camera_move: string
  scene_setting: string; characters: string[]; action_desc: string
  first_frame_desc: string; last_frame_desc: string
  source_excerpt: string
  narration: string | null; dialogues: Dialogue[]; transition: string
  continuity_from_prev: number; adopted_version_id: string | null
  est_cost_cny: number; versions: ShotVersion[]
  scene_est_cost_cny?: number
  scene_status: string; approved_scene_id: string | null
  approved_head_scene_id?: string | null; approved_tail_scene_id?: string | null
  required_keyframes?: ('head' | 'tail')[]; scenes: SceneCandidate[]
  video_stale: boolean
  storyboard_artifact_id?: string | null
  storyboard_evidence?: ArtifactEvidence | null
}

export interface Episode {
  id: string; episode_no: number; title: string; hook: string; cliffhanger: string
  synopsis: string; source_chapters: number[]; target_duration_s: number
  status: string; script_error?: string; cost_cny: number; cost_limit_cny?: number
  screenplay_status: string; screenplay_error?: string | null; screenplay_beats?: number; screenplay_mode?: string
  screenplay?: EpisodeScreenplay | null
  screenplay_artifact_id?: string | null
  screenplay_evidence?: ArtifactEvidence | null
  shots?: Shot[]
  storyboard_planned_shots?: number | null
  storyboard_artifact_id?: string | null
  storyboard_evidence?: ArtifactEvidence | null
  delivery_artifact_id?: string | null
  delivery_status?: string
}

interface DeliveryCheck {
  key: string; passed: boolean; message: string; evidence?: unknown
}

export interface DeliveryReadiness {
  episode_id: string; ready: boolean; evidence_coverage: number
  checks: DeliveryCheck[]; blockers: DeliveryCheck[]
  warnings: { code?: string; message?: string; shot_no?: number }[]
}

export interface DeliveryPackage {
  package_id: string; artifact_id: string; trust_level: string
  status: string; package_path: string
  archive_path?: string
  manifest: Record<string, unknown>; quality_report: Record<string, unknown>
}

export interface DeliveryPackageRecord {
  id: string; episode_id: string; artifact_id: string; status: string
  package_path: string; created_at: number; approved_at?: number | null
}

interface MixShot {
  shot_id: string; shot_no: number; duration_s: number
  video_url: string | null; has_adopted: boolean
}

export interface MixStatus {
  episode_id: string; title: string; episode_no: number
  shots_total: number; shots_ready: number; ready: boolean
  final_video_url: string | null; shots: MixShot[]
}

export interface MixResult {
  video_url: string; shots: number; total_duration_s: number
  ffmpeg_missing?: boolean; note?: string
}

export interface Portrait {
  id: string
  ep_start: number
  ep_end: number | null
  appearance?: string | null
  base_portrait_id?: string | null
  image_url?: string | null
}

export interface Character {
  name: string; role: string; appearance_canonical: string
  personality: string; speech_style: string
  relationships: { to: string; relation: string }[]
  ref_image_path?: string | null
  ref_image_url?: string | null
  portrait_prompt_override?: string | null
  portrait_prompt_effective?: string
  portraits?: Portrait[]
}

export interface ChapterContent {
  idx: number; title: string; content: string
  prev_idx: number | null; next_idx: number | null
  first_idx: number; last_idx: number; total: number
}

export interface SceneRefSegment {
  ep_start: number
  ep_end: number | null
  scene_canonical?: string | null
  image_url?: string | null
  qa?: { overall?: number; issues?: string[] } | null
  qa_overall?: number | null
  artifact_id?: string | null
  evidence?: ArtifactEvidence | null
}

export interface SceneReferenceCandidate {
  artifact_id: string
  status: string
  trust_level: string
  attempt?: number | null
  image_url?: string | null
  evidence?: ArtifactEvidence | null
}

export interface Scene {
  name: string
  scene_canonical: string
  location_kind?: string
  ref_image_path?: string | null
  ref_image_url?: string | null
  scene_prompt_override?: string | null
  scene_prompt_effective?: string
  scene_refs?: SceneRefSegment[]
  scene_candidates?: SceneReferenceCandidate[]
}

export interface Bible {
  characters: Character[]
  world: { era: string; genre: string; visual_style_canonical: string }
  scenes?: Scene[]
}

export interface Project {
  id: string; name: string; status: string; novel_chars: number
  bible_status: string; bible_error?: string; plan_status: string; plan_error?: string
  bible_version?: number; refs_status?: string; refs_error?: string
  refs_target?: string | null
  scene_refs_status?: string; scene_refs_error?: string; scene_refs_target?: string | null
  bible?: Bible | null; key_timeline?: string[]
  chapters?: { idx: number; title: string; char_count: number; preview?: string }[]
  episodes?: Episode[]
  chapter_count?: number; episode_count?: number
  bible_artifact_id?: string | null
  bible_evidence?: ArtifactEvidence | null
  harness_engine_enabled?: number | boolean
}

interface AutoProgress {
  bible?: string; refs?: string; plan?: string
  episodes_total?: number; episodes_done?: number
  screenplays_ready?: number
  shots_total?: number; shots_keyframed?: number; shots_video?: number
}
export interface AutoStatus {
  running: boolean
  phase?: string | null
  error?: string | null
  log?: { t: number; msg: string }[]
  started_at?: number | null
  updated_at?: number | null
  export_dir?: string | null
  progress?: AutoProgress
}

export interface BrowseResult {
  path: string
  parent: string | null
  drives: string[]
  dirs: { name: string; path: string }[]
}

export const numToCn = (n: number): string => {
  const cn = '零一二三四五六七八九'
  if (n <= 10) return n === 10 ? '十' : cn[n]
  if (n < 20) return '十' + cn[n % 10]
  if (n < 100) return cn[Math.floor(n / 10)] + '十' + (n % 10 ? cn[n % 10] : '')
  return String(n)
}
