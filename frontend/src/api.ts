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
}

export interface Dialogue { speaker: string; line: string; emotion: string }

export interface ShotVersion {
  id: string; version_no: number; prompt_text: string; status: string
  error?: string; video_url?: string; qa?: { overall: number; issues: string[] } | null
  cost_cny: number; latency_s: number
}

export interface Shot {
  id: string; shot_no: number; duration_s: number; shot_size: string; camera_move: string
  scene_setting: string; characters: string[]; action_desc: string
  narration: string | null; dialogues: Dialogue[]; transition: string
  continuity_from_prev: number; adopted_version_id: string | null
  est_cost_cny: number; versions: ShotVersion[]
}

export interface Episode {
  id: string; episode_no: number; title: string; hook: string; cliffhanger: string
  synopsis: string; source_chapters: number[]; target_duration_s: number
  status: string; script_error?: string; cost_cny: number; cost_limit_cny?: number
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

export const numToCn = (n: number): string => {
  const cn = '零一二三四五六七八九'
  if (n <= 10) return n === 10 ? '十' : cn[n]
  if (n < 20) return '十' + cn[n % 10]
  if (n < 100) return cn[Math.floor(n / 10)] + '十' + (n % 10 ? cn[n % 10] : '')
  return String(n)
}
