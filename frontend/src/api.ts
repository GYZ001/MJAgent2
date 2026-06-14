export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

async function handle(resp: Response) {
  if (resp.ok) return resp.json()
  let detail = `HTTP ${resp.status}`
  try {
    const body = await resp.json()
    detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
  } catch { /* keep default */ }
  throw new ApiError(resp.status, detail)
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
  shotGenerate: (shotId: string, promptOverride?: string, reroll?: boolean, withCritique?: boolean, modeOverride?: string) =>
    fetch(`/api/shots/${shotId}/generate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt_override: promptOverride, reroll, with_critique: withCritique, mode_override: modeOverride,
      }),
    }).then(handle),
  sceneApprove: (shotId: string, sceneId: string, kind: string) =>
    fetch(`/api/shots/${shotId}/scene/approve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scene_id: sceneId, kind }),
    }).then(handle),
  sceneDelete: (sceneId: string) =>
    fetch(`/api/scenes/${sceneId}`, { method: 'DELETE' }).then(handle),
  adoptVersion: (shotId: string, versionId: string) =>
    fetch(`/api/shots/${shotId}/adopt`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version_id: versionId }),
    }).then(handle),
  deleteVersion: (versionId: string) =>
    fetch(`/api/versions/${versionId}`, { method: 'DELETE' }).then(handle),
}

export interface Dialogue { speaker: string; line: string; emotion: string }

export interface ScreenplayBeat {
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

export interface ShotVersion {
  id: string; version_no: number; prompt_text: string; status: string
  error?: string; video_url?: string; qa?: { overall: number; issues: string[] } | null
  cost_cny: number; latency_s: number
  image_inputs?: {
    first_frame_used: boolean; first_frame_src?: string | null; first_frame_scene_id?: string | null
    last_frame_used?: boolean; last_frame_src?: string | null; last_frame_scene_id?: string | null
    mode?: 'FIRST_LAST_FRAME_MODE' | 'REFERENCE_IMAGE_MODE' | string | null
    mode_decision?: {
      mode?: string; reason?: string; confidence?: number
      ruleMode?: string | null; llmUsed?: boolean; defaulted?: boolean
      needReusePreviousScene?: boolean; needGenerateNewReferences?: boolean
      referenceImagePlan?: {
        totalCount?: number; reusePreviousSceneCount?: number; generateNewCount?: number; types?: string[]
      }
    } | null
    reference_image_used?: boolean
    reference_images?: {
      id: string; image_url?: string | null; type: string; source: string
      qualityScore?: number | null; selectedForSeedance?: boolean
      rejectReason?: string | null; qa?: { overall?: number; issues?: string[] } | null
    }[]
    reference_failure_logs?: { type?: string; reason?: string; error?: string; fallback?: string; qa?: { overall?: number; issues?: string[] } }[]
    fallback_reason?: string | null
    retry_reason?: string | null
  }
}

export interface SceneQa {
  overall: number; expectation_match?: number; continuity?: number; clean_frame?: number; issues?: string[]
}
export interface SceneCandidate {
  id: string; version_no: number; kind: 'head' | 'tail'; status: string; error?: string
  qa?: SceneQa | null; image_url?: string
}

export interface ModePlan {
  mode: 'FIRST_LAST_FRAME_MODE' | 'REFERENCE_IMAGE_MODE' | string
  reason: string
  confidence: number
  ruleMode?: string | null
  llmUsed?: boolean
  defaulted?: boolean
  needReusePreviousScene?: boolean
  needGenerateNewReferences?: boolean
  referenceImagePlan?: {
    totalCount: number
    reusePreviousSceneCount: number
    generateNewCount: number
    types: string[]
    prompts?: { type: string; prompt: string }[]
  } | null
}

export interface Shot {
  id: string; episode_id: string; script_id?: string | null; shot_no: number; duration_s: number; shot_size: string; camera_move: string
  scene_setting: string; characters: string[]; action_desc: string
  first_frame_desc: string; last_frame_desc: string
  source_excerpt: string
  narration: string | null; dialogues: Dialogue[]; transition: string
  continuity_from_prev: number; adopted_version_id: string | null
  est_cost_cny: number; versions: ShotVersion[]
  scene_status: string; approved_scene_id: string | null
  approved_head_scene_id?: string | null; approved_tail_scene_id?: string | null
  required_keyframes?: ('head' | 'tail')[]; scenes: SceneCandidate[]
  video_stale: boolean
  mode_plan?: ModePlan | null
}

export interface Episode {
  id: string; episode_no: number; title: string; hook: string; cliffhanger: string
  synopsis: string; source_chapters: number[]; target_duration_s: number
  status: string; script_error?: string; cost_cny: number; cost_limit_cny?: number
  screenplay_status: string; screenplay_error?: string | null; screenplay_beats?: number; screenplay_mode?: string
  screenplay?: EpisodeScreenplay | null
  shots?: Shot[]
}

export interface MixShot {
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

export interface Character {
  name: string; role: string; appearance_canonical: string
  personality: string; speech_style: string
  relationships: { to: string; relation: string }[]
  ref_image_path?: string | null
  ref_image_url?: string | null
  portrait_prompt_override?: string | null
  portrait_prompt_effective?: string
}

export interface Bible { characters: Character[]; world: { era: string; genre: string; visual_style_canonical: string } }

export interface Project {
  id: string; name: string; status: string; novel_chars: number
  bible_status: string; bible_error?: string; plan_status: string; plan_error?: string
  bible_version?: number; refs_status?: string; refs_error?: string
  refs_target?: string | null
  bible?: Bible | null; key_timeline?: string[]
  chapters?: { idx: number; title: string; char_count: number }[]
  episodes?: Episode[]
  chapter_count?: number; episode_count?: number
}

export interface AutoProgress {
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
