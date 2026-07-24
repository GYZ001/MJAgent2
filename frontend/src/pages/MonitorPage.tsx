import { Fragment, useMemo, useState } from 'react'
import { api } from '../api'
import { useNav, usePoll } from '../App'
import RunCenter from '../components/harness/RunCenter'
import SearchField from '../components/SearchField'

interface JobsView {
  counts: Record<string, number>
  startup_recovery?: Record<string, number>
  recent: {
    id: string
    source?: 'run' | 'job' | 'screenplay'
    kind?: string
    workflow_type?: string
    scope_type?: string
    scope_id?: string
    status: string
    raw_status?: string
    error?: string
    shot_no?: number
    episode_no?: number
    episode_title?: string
    project_name?: string
    updated_at: number
  }[]
}
interface Call {
  id: number
  ts: number
  kind: string
  model: string
  status: string
  effective_status?: string
  recovery_disposition?: string
  supersedes_call_id?: number
  superseded_by_call_id?: number
  http_status?: number
  latency_ms: number
  error?: string
  meta?: string
  request_json?: string
  response_json?: string
}

type ProviderKey = string
type ModelKind = 'text' | 'vlm' | 'video' | 'image'
interface ModelOption { provider: ProviderKey; model: string; available: boolean }
interface ModelSelection {
  key: ModelKind
  label: string
  provider: ProviderKey
  model: string
  options: ModelOption[]
}
interface Health {
  openrouter_key_configured: boolean
  bailian_key_configured: boolean
  deepseek_key_configured?: boolean
  zhipu_key_configured?: boolean
  models?: Record<ModelKind, ModelSelection>
  keys?: Record<ProviderKey, { configured: boolean; preview: string }>
}

interface ModelChoice { label: string; value: string }
interface CatalogModel {
  id: string
  provider: ProviderKey
  model: string
  label: string
  kinds: ModelKind[]
  builtin: boolean
  provider_label?: string
  base_url?: string
  key_configured?: boolean
}
interface ModelCatalog { items: CatalogModel[] }

const PROVIDERS: { key: ProviderKey; label: string }[] = [
  { key: 'hiagent', label: '火山' },
  { key: 'openrouter', label: 'OpenRouter' },
  { key: 'bailian', label: '百炼' },
  { key: 'deepseek', label: 'DeepSeek' },
  { key: 'zhipu', label: '智谱' },
]

const MODEL_ROWS: { key: ModelKind; label: string; note: string }[] = [
  { key: 'text', label: 'Text 模型', note: '分集 / 剧本 / 分镜 / 文本修复' },
  { key: 'vlm', label: 'VLM 模型', note: '参考图评审 / 视频质检' },
  { key: 'video', label: '视频模型', note: 'Seedance 视频生成' },
  { key: 'image', label: '图像模型', note: 'Seedream 参考图 / 定妆照' },
]

const OPENROUTER_MODEL_CHOICES: Record<'text' | 'vlm', ModelChoice[]> = {
  text: [
    { label: 'GLM 5.2', value: 'z-ai/glm-5.2' },
    { label: 'Claude Opus 4.8', value: 'anthropic/claude-opus-4.8' },
  ],
  vlm: [
    { label: 'Gemini 3.5 Flash', value: 'google/gemini-3.5-flash' },
  ],
}

const BAILIAN_MODEL_CHOICES: Record<'text' | 'vlm', ModelChoice[]> = {
  text: [
    { label: 'Qwen3.7-Max 2026-06-08（免费额度）', value: 'qwen3.7-max-2026-06-08' },
    { label: 'Qwen3.7-Max 2026-05-20（免费额度）', value: 'qwen3.7-max-2026-05-20' },
    { label: 'Qwen3.7-Max 2026-05-17（免费额度）', value: 'qwen3.7-max-2026-05-17' },
    { label: 'Qwen3.7-Max Preview（免费额度）', value: 'qwen3.7-max-preview' },
    { label: 'Qwen3.7-Plus 2026-05-26（免费额度）', value: 'qwen3.7-plus-2026-05-26' },
    { label: 'Qwen3.7-Max', value: 'qwen3.7-max' },
    { label: 'Qwen3.7-Plus', value: 'qwen3.7-plus' },
  ],
  vlm: [
    { label: 'Qwen3.7-Plus 2026-05-26（免费额度）', value: 'qwen3.7-plus-2026-05-26' },
    { label: 'Qwen3.7-Plus', value: 'qwen3.7-plus' },
  ],
}

const DEEPSEEK_MODEL_CHOICES: Record<'text', ModelChoice[]> = {
  text: [
    { label: 'DeepSeek V4 Pro', value: 'deepseek-v4-pro' },
  ],
}

const ZHIPU_MODEL_CHOICES: Record<'text', ModelChoice[]> = {
  text: [
    { label: 'GLM 5.2', value: 'glm-5.2' },
  ],
}

const HIAGENT_MODEL_CHOICES: Record<ModelKind, ModelChoice[]> = {
  text: [
    { label: '文本推理模型（默认）', value: 'd2a5n9rnvvm49eucvnvg' },
    { label: 'Text 模型', value: 'd71l5c8nfdb167kligqg' },
  ],
  vlm: [
    { label: '视觉质检模型（默认）', value: 'd7ev7il5boeaebtf4sgg' },
  ],
  video: [
    { label: 'Seedance 视频生成（默认）', value: 'd7jf6nd5boeaebtfbdqg' },
  ],
  image: [
    { label: 'Seedream 图像生成（默认）', value: 'd7ute7ppcc7n89uuqqp0' },
  ],
}

function providerLabel(provider: ProviderKey) {
  return PROVIDERS.find(p => p.key === provider)?.label ?? provider
}

function modelProviderSettingKey(kind: ModelKind) {
  return `model_${kind}_provider`
}

function modelSettingKey(kind: ModelKind, provider: ProviderKey) {
  if (provider.startsWith('custom:')) return ''
  if (provider === 'bailian') {
    if (kind === 'text') return 'bailian_model_text'
    if (kind === 'vlm') return 'bailian_model_vlm'
    return ''
  }
  if (provider === 'deepseek') {
    if (kind === 'text') return 'deepseek_model_text'
    return ''
  }
  if (provider === 'zhipu') {
    if (kind === 'text') return 'zhipu_model_text'
    return ''
  }
  if (provider === 'openrouter') {
    if (kind === 'text') return 'openrouter_model_text'
    if (kind === 'vlm') return 'openrouter_model_vlm'
    return ''
  }
  return `hiagent_model_${kind}`
}

function fallbackSelection(kind: ModelKind): ModelSelection {
  const provider = 'hiagent'
  return {
    key: kind,
    label: MODEL_ROWS.find(r => r.key === kind)?.label ?? kind,
    provider,
    model: '',
    options: [
      { provider: 'hiagent', model: '', available: true },
      { provider: 'openrouter', model: '', available: kind === 'text' || kind === 'vlm' },
      { provider: 'bailian', model: '', available: kind === 'text' || kind === 'vlm' },
      { provider: 'deepseek', model: '', available: kind === 'text' },
      { provider: 'zhipu', model: '', available: kind === 'text' },
    ],
  }
}

function modelChoices(kind: ModelKind, provider: ProviderKey, currentModel: string, catalog: CatalogModel[] = []): ModelChoice[] {
  let choices: ModelChoice[] = []
  if (provider === 'openrouter' && (kind === 'text' || kind === 'vlm')) {
    choices = [...OPENROUTER_MODEL_CHOICES[kind]]
  } else if (provider === 'bailian' && (kind === 'text' || kind === 'vlm')) {
    choices = [...BAILIAN_MODEL_CHOICES[kind]]
  } else if (provider === 'deepseek' && kind === 'text') {
    choices = [...DEEPSEEK_MODEL_CHOICES.text]
  } else if (provider === 'zhipu' && kind === 'text') {
    choices = [...ZHIPU_MODEL_CHOICES.text]
  } else if (provider === 'hiagent') {
    choices = [...HIAGENT_MODEL_CHOICES[kind]]
  }
  for (const item of catalog) {
    if (item.provider === provider && item.kinds.includes(kind) && !choices.some(choice => choice.value === item.model)) {
      choices.push({ label: item.label, value: item.model })
    }
  }
  // 当前配置的模型如果不在列表中，补充进去（兼容历史值）
  if (currentModel && !isDisallowedModel(provider, currentModel) && !choices.some(choice => choice.value === currentModel)) {
    choices.unshift({ label: currentModel, value: currentModel })
  }
  if (!choices.length) {
    choices.push({ label: currentModel || '未配置', value: currentModel })
  }
  return choices
}

function isDisallowedModel(provider: ProviderKey, model: string) {
  return provider === 'openrouter' && model === 'qwen/qwen3.7-max'
}

function selectedModelValue(choices: ModelChoice[], currentModel: string) {
  return choices.some(choice => choice.value === currentModel) ? currentModel : choices[0]?.value ?? ''
}

function prettyJson(raw?: string | null) {
  if (!raw) return '暂无记录'
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

type JsonRecord = Record<string, unknown>

function parseJsonRecord(raw?: string | null): JsonRecord {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed as JsonRecord
  } catch {
    // ignore malformed json in legacy logs
  }
  return {}
}

function readString(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function readNumber(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (typeof value === 'string' && value.trim()) {
      const n = Number(value)
      if (Number.isFinite(n)) return n
    }
  }
  return undefined
}

function inferReferenceType(prompt: string) {
  const match = prompt.match(/Reference type:\s*([a-z_]+)/i)
  return match?.[1]?.trim() ?? ''
}

function inferShotNo(prompt: string) {
  const match = prompt.match(/\bShot\s+(\d+)\b/i)
  if (!match) return undefined
  const n = Number(match[1])
  return Number.isFinite(n) ? n : undefined
}

function formatEpisodeShot(episodeNo?: number, shotNo?: number) {
  if (episodeNo !== undefined && shotNo !== undefined) return `第${episodeNo}集第${shotNo}镜`
  if (episodeNo !== undefined) return `第${episodeNo}集`
  if (shotNo !== undefined) return `第${shotNo}镜`
  return ''
}

const CALL_KIND_LABELS: Record<string, string> = {
  chat: '文本模型调用',
  vlm: '视觉模型调用',
  vlm_qa: '视频质检',
  video_create: '创建视频任务',
  video_poll: '轮询视频结果',
  image_generate: '生成图片',
  image_edit: '图生图',
  scene_image: '关键帧生成',
  storyboard_prompt: '整集分镜提示词',
  storyboard_shot_prompt: '逐镜分镜提示词',
  storyboard_outline_prompt: '分镜大纲提示词',
  storyboard_plan_revised: '分镜计划修订',
  screenplay_prompt: '剧本提示词',
  plan_prompt: '分集提示词',
  bible_prompt: '人物谱提示词',
  references_prompt: '参考图提示词',
  reference_image_mode_attempt_1_failed: '参考图模式首轮失败',
  reference_image_mode_retry_success: '参考图模式重试成功',
  reference_image_mode_retry_failed: '参考图模式重试失败',
  reference_image_mode_original_failure: '参考图模式最终失败',
}

const REFERENCE_TYPE_LABELS: Record<string, string> = {
  character: '角色参考图',
  scene: '场景参考图',
  plot_key_frame: '剧情参考图',
  previous_shot_frame: '承接参考图',
}

const FRAME_KIND_LABELS: Record<string, string> = {
  head: '首关键帧',
  tail: '尾关键帧',
}

const CALLER_LABELS: Record<string, string> = {
  'stages.summarize_chapter': '章节摘要',
  'stages.review_scene_image': '关键帧评审',
  'stages.qa_shot': '视频自动质检',
  'video_modes.review_reference_image': '参考图单图质检',
  'video_modes.review_reference_consistency': '参考图一致性质检',
  'video_modes.write_reference_prompt': '参考图提示词生成',
  'portraits.discover_character_candidates': '剧本新角色预检',
  'portraits.assess_new_character': '新角色建卡评估',
  'scenes.assess_new_scene': '新场景评估',
}

const CALL_STATUS_LABELS: Record<string, string> = {
  RUNNING: '调用中',
  INTERRUPTED: '已中断',
  RETRYING: '已自动重试',
  RECOVERED: '续跑已成功',
  OK: '成功',
  FAILED: '失败',
  TIMEOUT: '超时',
  NETWORK_ERROR: '网络错误',
  TASK_FAILED: '任务失败',
  QA_ERROR: '质检异常',
  REPAIR_STALLED: '修复停滞',
  FALLBACK_LAST_OUTPUT: '采用最后输出',
  COVERS_SPLIT: '大纲自动拆分',
  PLAN_REVISED: '分镜计划修订',
  COVERS_DOWNGRADED: '圣经外台词转旁白',
  PROMPT_READY: '提示词已生成',
  REFERENCE_ATTEMPT_FAILED: '参考图首轮失败',
  REFERENCE_RETRY_SUCCESS: '参考图重试成功',
  REFERENCE_RETRY_FAILED: '参考图重试失败',
  REFERENCE_MODE_ORIGINAL_FAILURE: '参考图最终失败',
}

function humanizeToken(raw: string) {
  const tokenMap: Record<string, string> = {
    chat: '文本',
    vlm: '视觉',
    qa: '质检',
    prompt: '提示词',
    storyboard: '分镜',
    shot: '镜头',
    outline: '大纲',
    screenplay: '剧本',
    bible: '人物谱',
    plan: '分集',
    video: '视频',
    poll: '轮询',
    image: '图片',
    reference: '参考图',
    mode: '模式',
    retry: '重试',
    success: '成功',
    failed: '失败',
    original: '原始',
    failure: '失败',
    attempt: '尝试',
    scene: '关键帧',
  }
  return raw.split(/[_\-.]+/).map(part => tokenMap[part] ?? part).join(' / ')
}

function callKindLabel(kind: string) {
  return CALL_KIND_LABELS[kind] ?? humanizeToken(kind)
}

function callerKey(meta: JsonRecord) {
  const moduleName = readString(meta.caller_module).replace(/^app\./, '')
  const functionName = readString(meta.caller_function)
  if (!moduleName || !functionName) return ''
  return `${moduleName}.${functionName}`
}

function callerLabel(meta: JsonRecord) {
  const key = callerKey(meta)
  if (!key) return ''
  return CALLER_LABELS[key] ?? humanizeToken(key.replace(/\./g, '_').replace(/^_+/, ''))
}

function withScope(scope: string, label: string) {
  return scope ? `${scope}${label}` : label
}

function callInitiatorLabel(call: Call, meta: JsonRecord, scope: string) {
  const stage = readString(meta.stage)
  const roleLabel = readString(meta.call_role_label)
  if (stage) {
    const stageLabel = roleLabel ? `${stage}${roleLabel}` : stage
    return withScope(scope, stageLabel)
  }

  const explicit = readString(meta.initiator_label)
  if (explicit) return withScope(scope, explicit)

  const caller = callerLabel(meta)
  if (!caller) return ''
  if (call.kind === 'chat' || call.kind === 'vlm_qa') return withScope(scope, caller)
  return caller
}

// 修复重试为何被触发：把上一轮输出的校验错误（meta.latest_errors）透出，
// 否则"主生成 成功 HTTP 200"后紧跟两条"修复重试"会让人误以为主生成内容已通过——
// 实际是 HTTP 通了、内容没过校验。有了它，运营一眼能看清重试根因（如 episode_no 漏写），不必再翻库。
function callRepairTrigger(meta: JsonRecord): string {
  if (readString(meta.call_role) !== 'stage_repair') return ''
  const errs = meta.latest_errors
  if (!Array.isArray(errs)) return ''
  const texts = errs.filter((e): e is string => typeof e === 'string' && e.trim().length > 0)
  if (!texts.length) return ''
  const shown = texts.slice(0, 3).join('；')
  return texts.length > 3 ? `${shown}（另有 ${texts.length - 3} 条）` : shown
}

function callFunctionLabel(call: Call) {
  const meta = parseJsonRecord(call.meta)
  const request = parseJsonRecord(call.request_json)
  const prompt = readString(request.prompt)
  const episodeNo = readNumber(meta.episode_no, request.episode_no)
  const shotNo = readNumber(meta.shot_no, inferShotNo(prompt))
  const scope = formatEpisodeShot(episodeNo, shotNo)
  const assetKind = readString(meta.asset_kind)
  const frameKind = readString(meta.frame_kind)
  const referenceType = readString(meta.reference_type, inferReferenceType(prompt))
  const characterName = readString(meta.character_name)
  const sceneName = readString(meta.scene_name)
  const initiatorLabel = callInitiatorLabel(call, meta, scope)

  switch (call.kind) {
    case 'chat':
      return initiatorLabel || (scope ? `${scope}文本模型调用` : CALL_KIND_LABELS.chat)
    case 'screenplay_prompt':
      return episodeNo !== undefined ? `第${episodeNo}集剧本` : '剧本'
    case 'storyboard_outline_prompt':
      return episodeNo !== undefined ? `第${episodeNo}集分镜大纲` : '分镜大纲'
    case 'storyboard_shot_prompt':
      return scope ? `${scope}分镜` : '分镜'
    case 'storyboard_outline_split':
      return scope ? `${scope}分镜拆分` : '分镜拆分'
    case 'storyboard_plan_revised':
      return episodeNo !== undefined ? `第${episodeNo}集分镜计划修订` : '分镜计划修订'
    case 'storyboard_outline_downgrade':
      return scope ? `${scope}大纲降级` : '大纲降级'
    case 'plan_prompt':
      return '分集规划'
    case 'bible_prompt':
      return '人物谱'
    case 'references_prompt':
      return scope ? `${scope}参考图规划` : (episodeNo !== undefined ? `第${episodeNo}集参考图规划` : '参考图规划')
    case 'video_create':
      return scope ? `${scope}的视频` : '视频'
    case 'video_poll':
      return scope ? `${scope}的视频轮询` : '视频轮询'
    case 'vlm_qa':
      return initiatorLabel || (scope ? `${scope}视频质检` : CALL_KIND_LABELS.vlm_qa)
    case 'reference_image_mode_attempt_1_failed':
    case 'reference_image_mode_retry_success':
    case 'reference_image_mode_retry_failed':
    case 'reference_image_mode_original_failure':
      return scope ? `${scope}参考图模式` : '参考图模式'
    case 'image_generate':
    case 'image_edit':
    case 'image':
      if (assetKind === 'keyframe') {
        const frameLabel = FRAME_KIND_LABELS[frameKind] ?? '关键帧'
        return scope ? `${scope}${frameLabel}` : frameLabel
      }
      if (assetKind === 'reference_image') {
        const refLabel = REFERENCE_TYPE_LABELS[referenceType] ?? '参考图'
        return scope ? `${scope}的${refLabel}` : refLabel
      }
      if (assetKind === 'portrait') {
        const prefix = episodeNo !== undefined ? `第${episodeNo}集起` : ''
        return `${prefix}${characterName || '角色'}定妆照`
      }
      if (assetKind === 'scene_reference') {
        const prefix = episodeNo !== undefined ? `第${episodeNo}集起` : ''
        return `${prefix}${sceneName || '场景'}素材图`
      }
      return initiatorLabel || callKindLabel(call.kind)
    default:
      return initiatorLabel || callKindLabel(call.kind)
  }
}

function callStatusLabel(status: string) {
  return CALL_STATUS_LABELS[status] ?? humanizeToken(status.toLowerCase())
}

function displayCallStatus(call: Call) {
  return call.effective_status || call.status
}

function callStatusColor(status: string) {
  if (status === 'OK' || status === 'RECOVERED' || status.endsWith('SUCCESS') || status === 'PROMPT_READY') return 'green'
  if (status === 'TIMEOUT' || status === 'NETWORK_ERROR' || status.includes('FAILED') || status.includes('ERROR')) return 'red'
  return 'gold'
}

const JOB_STATUS_LABELS: Record<string, string> = {
  queued: '排队中', running: '运行中', waiting_retry: '等待重试', waiting_human: '等待人工确认',
  succeeded: '已完成', partial: '部分完成', failed: '失败', paused_budget: '预算暂停',
  paused_external: '外部中断', cancelled: '已取消',
  recovering: '恢复排队中', recovered: '已自动续跑',
}

const RECOVERY_WORKFLOW_LABELS: Record<string, string> = {
  media: '参考图/视频', auto_project: '全自动项目', character_bible: '人物谱',
  character_references: '人物参考图', scene_references: '场景参考图', episode_mapping: '分集规划',
  screenplay: '剧本', storyboard: '分镜', delivery: '交付包',
}

function jobStatusLabel(status: string) {
  return JOB_STATUS_LABELS[status] ?? status
}

const WORKFLOW_LABELS: Record<string, string> = {
  auto_project: '全自动制作', character_bible: '人物谱', character_references: '人物定妆照',
  scene_bible: '场景圣经', scene_references: '场景参考图', episode_mapping: '分集映射',
  screenplay: '剧本', storyboard: '分镜', scene_generation: '关键帧生成',
  video_generation: '视频生成', delivery: '交付',
}

function jobWorkLabel(job: JobsView['recent'][number]) {
  const kind = job.workflow_type || job.kind || '任务'
  const label = WORKFLOW_LABELS[kind] || kind
  if (job.episode_no != null && job.shot_no != null) return `第${job.episode_no}集 · 镜${job.shot_no} · ${label}`
  if (job.episode_no != null) return `第${job.episode_no}集 · ${label}`
  return label
}

function jobStampClass(status: string) {
  if (status === 'succeeded' || status === 'recovered') return 'green'
  if (['failed', 'partial', 'paused_budget', 'paused_external'].includes(status)) return 'red'
  if (['running', 'queued', 'recovering', 'waiting_retry', 'waiting_human'].includes(status)) return 'gold'
  return 'grey'
}

type MonitorSection = 'overview' | 'runs' | 'jobs' | 'models' | 'calls' | 'settings'

const MONITOR_SECTIONS: { key: MonitorSection; label: string; description: string }[] = [
  { key: 'overview', label: '总览', description: '关键状态与异常' },
  { key: 'runs', label: '运行中心', description: '步骤与人工门禁' },
  { key: 'jobs', label: '任务队列', description: '生成任务与失败' },
  { key: 'models', label: '模型中心', description: '模型分配与连接' },
  { key: 'calls', label: '调用日志', description: '请求、响应与耗时' },
  { key: 'settings', label: '系统设置', description: '并发、预算与保留期' },
]

function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
}: {
  page: number
  pageSize: number
  total: number
  onPageChange: (page: number) => void
  onPageSizeChange: (pageSize: number) => void
}) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const currentPage = Math.min(page, pageCount)
  const start = total ? (currentPage - 1) * pageSize + 1 : 0
  const end = Math.min(currentPage * pageSize, total)
  return (
    <div className="monitor-pagination" aria-label="分页">
      <span>显示 {start}–{end} / 共 {total} 条</span>
      <label>
        每页
        <select value={pageSize} onChange={e => onPageSizeChange(Number(e.target.value))}>
          {[10, 20, 40, 80].map(size => <option value={size} key={size}>{size}</option>)}
        </select>
      </label>
      <button type="button" disabled={currentPage <= 1} onClick={() => onPageChange(currentPage - 1)}>上一页</button>
      <b>{currentPage} / {pageCount}</b>
      <button type="button" disabled={currentPage >= pageCount} onClick={() => onPageChange(currentPage + 1)}>下一页</button>
    </div>
  )
}

export default function MonitorPage() {
  const { toast } = useNav()
  const [activeSection, setActiveSection] = useState<MonitorSection>('overview')
  const { data: jobs } = usePoll<JobsView>(
    () => api.get('/system/jobs'),
    activeSection === 'jobs' || activeSection === 'overview' ? 4000 : 0,
    [activeSection],
  )
  const { data: calls } = usePoll<Call[]>(
    () => api.get('/system/calls?limit=200'),
    activeSection === 'calls' || activeSection === 'overview' ? 6000 : 0,
    [activeSection],
  )
  const { data: settings, refresh: refreshSettings } = usePoll<Record<string, string>>(() => api.get('/settings'), 0)
  const { data: health, refresh: refreshHealth } = usePoll<Health>(() => api.get('/system/health'), 0)
  const { data: modelCatalog, refresh: refreshModelCatalog } = usePoll<ModelCatalog>(() => api.get('/models'), 0)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [modelDraft, setModelDraft] = useState<Record<string, string>>({})
  const [expandedCallId, setExpandedCallId] = useState<number | null>(null)
  const [showAddModel, setShowAddModel] = useState(false)
  const [showModelLibrary, setShowModelLibrary] = useState(false)
  const [libraryMessage, setLibraryMessage] = useState('')
  const [addingModel, setAddingModel] = useState(false)
  const [editingModel, setEditingModel] = useState<CatalogModel | null>(null)
  const [connectionTest, setConnectionTest] = useState<{ status: 'idle' | 'testing' | 'ok' | 'error'; message: string; signature?: string }>({ status: 'idle', message: '' })
  const [newModel, setNewModel] = useState<{ label: string; model: string; provider: ProviderKey; provider_label: string; base_url: string; api_key: string; kinds: ModelKind[] }>({
    label: '', model: '', provider: 'custom', provider_label: '', base_url: '', api_key: '', kinds: ['text'],
  })
  const [credentialModel, setCredentialModel] = useState<CatalogModel | null>(null)
  const [credentialDraft, setCredentialDraft] = useState({ base_url: '', api_key: '' })
  const [credentialTest, setCredentialTest] = useState<{ status: 'idle' | 'testing' | 'ok' | 'error'; message: string; signature?: string }>({ status: 'idle', message: '' })
  const [jobSearch, setJobSearch] = useState('')
  const [jobStatus, setJobStatus] = useState('all')
  const [jobPage, setJobPage] = useState(1)
  const [jobPageSize, setJobPageSize] = useState(20)
  const [callSearch, setCallSearch] = useState('')
  const [callStatus, setCallStatus] = useState('all')
  const [callPage, setCallPage] = useState(1)
  const [callPageSize, setCallPageSize] = useState(20)

  const refreshModelState = () => {
    refreshSettings()
    refreshHealth()
    refreshModelCatalog()
  }

  const toggleNewModelKind = (kind: ModelKind) => {
    setConnectionTest({ status: 'idle', message: '' })
    setNewModel(prev => ({
      ...prev,
      kinds: prev.kinds.includes(kind) ? prev.kinds.filter(k => k !== kind) : [...prev.kinds, kind],
    }))
  }

  const modelDraftSignature = () => JSON.stringify({
    provider: newModel.provider,
    provider_label: newModel.provider_label.trim(),
    model: newModel.model.trim(),
    base_url: newModel.base_url.trim().replace(/\/$/, ''),
    api_key: newModel.api_key,
    kinds: [...newModel.kinds].sort(),
  })

  const testNewModel = async () => {
    setConnectionTest({ status: 'testing', message: '正在连接模型…' })
    try {
      const result = editingModel
        ? await api.post(`/models/${encodeURIComponent(editingModel.id)}/test`, newModel)
        : await api.post('/models/test', newModel)
      setConnectionTest({ status: 'ok', message: `${result.preview || '连接成功'} · ${result.latency_ms} ms`, signature: modelDraftSignature() })
    } catch (e: unknown) {
      setConnectionTest({ status: 'error', message: (e as Error).message })
    }
  }

  const editModel = (item: CatalogModel) => {
    setShowModelLibrary(false)
    setEditingModel(item)
    setNewModel({
      label: item.label, model: item.model, provider: item.provider.startsWith('custom:') ? 'custom' : item.provider,
      provider_label: item.provider_label || providerLabel(item.provider), base_url: item.base_url || '', api_key: '', kinds: [...item.kinds],
    })
    setConnectionTest({ status: 'idle', message: '' })
    setShowAddModel(true)
  }

  const addModel = async () => {
    if (!newModel.label.trim() || !newModel.model.trim() || !newModel.kinds.length) {
      toast('请填写模型名称、模型 ID，并至少选择一种能力', true)
      return
    }
    if (connectionTest.status !== 'ok' || connectionTest.signature !== modelDraftSignature()) {
      toast('请先测试连接，确认当前配置可用', true)
      return
    }
    setAddingModel(true)
    try {
      if (editingModel) {
        await api.put(`/models/${encodeURIComponent(editingModel.id)}`, { ...newModel, label: newModel.label.trim(), model: newModel.model.trim() })
        toast('模型配置已更新')
      } else {
        await api.post('/models', { ...newModel, label: newModel.label.trim(), model: newModel.model.trim() })
        toast('模型已加入模型库')
      }
      setShowAddModel(false)
      setEditingModel(null)
      setConnectionTest({ status: 'idle', message: '' })
      setNewModel({ label: '', model: '', provider: 'custom', provider_label: '', base_url: '', api_key: '', kinds: ['text'] })
      refreshModelCatalog()
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setAddingModel(false)
    }
  }

  const removeModel = async (item: CatalogModel) => {
    if (!confirm(`确认从模型库移除「${item.label}」？`)) return
    try {
      await api.del(`/models/${encodeURIComponent(item.id)}`)
      toast('模型已移除')
      refreshModelCatalog()
    } catch (e: unknown) { toast((e as Error).message, true) }
  }

  const saveModelCredentials = async () => {
    if (!credentialModel) return
    const signature = JSON.stringify(credentialDraft)
    if (credentialTest.status !== 'ok' || credentialTest.signature !== signature) {
      toast('请先测试当前连接配置', true)
      return
    }
    try {
      await api.put(`/models/${encodeURIComponent(credentialModel.id)}/credentials`, credentialDraft)
      toast('模型连接配置已保存')
      setCredentialModel(null)
      setCredentialDraft({ base_url: '', api_key: '' })
      refreshModelCatalog()
    } catch (e: unknown) { toast((e as Error).message, true) }
  }

  const testModelCredentials = async () => {
    if (!credentialModel) return
    setCredentialTest({ status: 'testing', message: '正在测试…' })
    try {
      const result = await api.post(`/models/${encodeURIComponent(credentialModel.id)}/test`, credentialDraft)
      setCredentialTest({ status: 'ok', message: `${result.preview || '连接成功'} · ${result.latency_ms} ms`, signature: JSON.stringify(credentialDraft) })
    } catch (e: unknown) { setCredentialTest({ status: 'error', message: (e as Error).message }) }
  }

  const selectionFor = (kind: ModelKind) => health?.models?.[kind] ?? fallbackSelection(kind)

  const providerFor = (kind: ModelKind, sel: ModelSelection) => {
    if (kind === 'video' || kind === 'image') return 'hiagent'
    return (modelDraft[modelProviderSettingKey(kind)] as ProviderKey | undefined) ?? sel.provider
  }

  const buildModelPayload = () => {
    const payload: Record<string, string> = {}

    for (const row of MODEL_ROWS) {
      const sel = selectionFor(row.key)
      const providerKey = modelProviderSettingKey(row.key)
      const provider = providerFor(row.key, sel)
      if (provider !== sel.provider) {
        payload[providerKey] = provider
      }
      const settingKey = modelSettingKey(row.key, provider)
      if (!settingKey) continue
      const option = sel.options.find(opt => opt.provider === provider)
      let modelValue = (modelDraft[settingKey] ?? option?.model ?? '').trim()
      if (isDisallowedModel(provider, modelValue)) {
        modelValue = modelChoices(row.key, provider, '', modelCatalog?.items)[0]?.value ?? ''
      }
      if (modelDraft[settingKey] !== undefined && !modelValue) {
        return { error: `${row.label} 模型不能为空`, payload }
      }
      if (modelValue && modelValue !== (option?.model ?? '')) {
        payload[settingKey] = modelValue
      }
    }
    return { payload }
  }

  const saveModelSettings = async () => {
    const built = buildModelPayload()
    if (built.error) {
      toast(built.error, true)
      return
    }
    if (!Object.keys(built.payload).length) {
      toast('没有需要保存的模型修改')
      setModelDraft({})
      return
    }
    try {
      await api.put('/settings', built.payload)
      toast('模型设置已保存')
      setModelDraft({})
      refreshModelState()
    } catch (e: unknown) { toast((e as Error).message, true) }
  }

  const modelSavePreview = buildModelPayload()
  const hasModelChanges = Object.keys(modelSavePreview.payload).length > 0

  const fmtTime = (t: number) => new Date(t * 1000).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })

  const SETTING_LABELS: Record<string, string> = {
    video_submit_concurrency: '视频提交并发',
    video_inflight_limit: '上游视频在途上限',
    video_poll_concurrency: '视频轮询并发',
    reference_pipeline_concurrency: '参考图流水线并发',
    image_request_concurrency: '图片请求并发',
    vlm_request_concurrency: 'VLM 质检并发',
    download_concurrency: '下载并发',
    finalize_concurrency: '落盘/校验并发',
    episode_video_inflight_limit: '单集上游在途上限',
    project_video_inflight_limit: '单项目上游在途上限',
    reference_prepared_backlog: '参考图领先视频槽位数',
    video_concurrency: '（兼容）视频并发数',
    auto_concurrency: '（兼容）全自动并发',
    episode_cost_limit_cny: '单集成本上限（¥）',
    use_character_refs: '定妆照参考图（true/false，人物一致性）',
    max_ref_images: '单镜头最多参考图数',
    auto_qa: '自动质检（true/false，需本机 ffmpeg）',
    auto_retake_threshold: '自动重抽阈值（QA 总分低于此值重抽一次）',
    max_repair_attempts: '修复重试上限（校验失败时让模型反复修正的次数）',
    provider_call_retention_days: '模型调用日志保留天数',
    error_log_retention_days: '错误日志保留天数',
  }

  const jobStatuses = useMemo(
    () => Array.from(new Set((jobs?.recent ?? []).map(job => job.status))).sort(),
    [jobs?.recent],
  )
  const filteredJobs = useMemo(() => {
    const keyword = jobSearch.trim().toLowerCase()
    return (jobs?.recent ?? []).filter(job => {
      if (jobStatus !== 'all' && job.status !== jobStatus) return false
      if (!keyword) return true
      return [job.id, job.kind, job.workflow_type, job.scope_type, job.scope_id, job.status,
        job.project_name, job.episode_title, job.error, job.episode_no, job.shot_no, jobWorkLabel(job)]
        .some(value => String(value ?? '').toLowerCase().includes(keyword))
    })
  }, [jobs?.recent, jobSearch, jobStatus])
  const jobPageCount = Math.max(1, Math.ceil(filteredJobs.length / jobPageSize))
  const safeJobPage = Math.min(jobPage, jobPageCount)
  const pagedJobs = filteredJobs.slice((safeJobPage - 1) * jobPageSize, safeJobPage * jobPageSize)
  const startupRecoveryEntries = Object.entries(jobs?.startup_recovery ?? {})
    .filter(([key, value]) => key !== 'abandoned_partial_files_removed' && Number(value) > 0)
  const startupRecoveryCount = startupRecoveryEntries.reduce((total, [, value]) => total + Number(value), 0)
  const removedPartialFiles = jobs?.startup_recovery?.abandoned_partial_files_removed ?? 0

  const callStatuses = useMemo(
    () => Array.from(new Set((calls ?? []).map(displayCallStatus))).sort(),
    [calls],
  )
  const filteredCalls = useMemo(() => {
    const keyword = callSearch.trim().toLowerCase()
    return (calls ?? []).filter(call => {
      if (callStatus !== 'all' && displayCallStatus(call) !== callStatus) return false
      if (!keyword) return true
      return [call.id, call.kind, call.model, call.status, displayCallStatus(call), call.http_status, call.error, callFunctionLabel(call)]
        .some(value => String(value ?? '').toLowerCase().includes(keyword))
    })
  }, [calls, callSearch, callStatus])
  const callPageCount = Math.max(1, Math.ceil(filteredCalls.length / callPageSize))
  const safeCallPage = Math.min(callPage, callPageCount)
  const pagedCalls = filteredCalls.slice((safeCallPage - 1) * callPageSize, safeCallPage * callPageSize)
  const failedCalls = (calls ?? []).filter(call => callStatusColor(displayCallStatus(call)) === 'red')
  const configuredModels = modelCatalog?.items.filter(item => item.key_configured).length ?? 0

  const openSection = (section: MonitorSection) => {
    setActiveSection(section)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  return (
    <div className="monitor-page">
      <header className="desk-head">
        <div className="crumb">漫剧案头 / 监制房</div>
        <h1>监制房 <span className="sub">运行、任务、模型与日志各归其位</span></h1>
        <hr className="rule" />
      </header>

      <nav className="monitor-subnav" aria-label="监制房子菜单">
        {MONITOR_SECTIONS.map(section => {
          const badge = section.key === 'jobs'
            ? (jobs?.counts.running ?? 0) + (jobs?.counts.queued ?? 0)
              + (jobs?.counts.recovering ?? 0)
              + (jobs?.counts.waiting_retry ?? 0) + (jobs?.counts.waiting_human ?? 0)
              + (jobs?.counts.paused_budget ?? 0) + (jobs?.counts.paused_external ?? 0)
            : section.key === 'calls' ? failedCalls.length : undefined
          return (
            <button
              type="button"
              key={section.key}
              className={activeSection === section.key ? 'active' : ''}
              aria-current={activeSection === section.key ? 'page' : undefined}
              onClick={() => openSection(section.key)}
            >
              <span>{section.label}{badge !== undefined && badge > 0 && <em>{badge}</em>}</span>
              <small>{section.description}</small>
            </button>
          )
        })}
      </nav>

      {activeSection === 'overview' && (
        <div className="monitor-section">
          <div className="monitor-section-head">
            <div><span className="eyebrow">CONTROL OVERVIEW</span><h2>制作运行总览</h2></div>
            <p>先看异常，再进入对应子菜单处理。</p>
          </div>
          <div className="stat-row monitor-stats">
            {[
              { key: 'queued', label: '排队', count: (jobs?.counts.queued ?? 0) + (jobs?.counts.recovering ?? 0) + (jobs?.counts.waiting_retry ?? 0) },
              { key: 'running', label: '运行中', count: jobs?.counts.running ?? 0 },
              { key: 'succeeded', label: '已完成', count: jobs?.counts.succeeded ?? 0 },
              { key: 'failed', label: '失败 / 部分', count: (jobs?.counts.failed ?? 0) + (jobs?.counts.partial ?? 0) },
              { key: 'waiting_human', label: '待人工', count: jobs?.counts.waiting_human ?? 0 },
              { key: 'paused', label: '已暂停', count: (jobs?.counts.paused_budget ?? 0) + (jobs?.counts.paused_external ?? 0) },
            ].map(item => (
              <button type="button" className="stat-cell" key={item.key} onClick={() => openSection('jobs')}>
                <div className="s-label">{item.label}</div>
                <div className="cost-ink" style={item.key === 'failed' && item.count > 0 ? { color: 'var(--cinnabar)' } : undefined}>
                  {item.count}
                </div>
                <span>查看任务队列 →</span>
              </button>
            ))}
          </div>
          <div className="monitor-overview-grid">
            <section className="card monitor-overview-card">
              <div className="monitor-card-head"><div><span className="eyebrow">EXCEPTIONS</span><h3>需要关注</h3></div><button type="button" onClick={() => openSection('calls')}>查看全部</button></div>
              {!failedCalls.length ? <div className="monitor-ok">当前没有失败的模型调用</div> : (
                <div className="monitor-brief-list">
                  {failedCalls.slice(0, 5).map(call => (
                    <button type="button" key={call.id} onClick={() => { setCallStatus(displayCallStatus(call)); setCallPage(1); openSection('calls') }}>
                      <span><b>{callFunctionLabel(call)}</b><small>{call.model || '未记录模型'}</small></span>
                      <span className="stamp red">{callStatusLabel(displayCallStatus(call))}</span>
                    </button>
                  ))}
                </div>
              )}
            </section>
            <section className="card monitor-overview-card">
              <div className="monitor-card-head"><div><span className="eyebrow">RECENT JOBS</span><h3>最近任务</h3></div><button type="button" onClick={() => openSection('jobs')}>查看全部</button></div>
              {!jobs?.recent.length ? <div className="monitor-ok">当前没有生成任务</div> : (
                <div className="monitor-brief-list">
                  {jobs.recent.slice(0, 5).map(job => (
                    <button type="button" key={job.id} onClick={() => { setJobStatus(job.status); setJobPage(1); openSection('jobs') }}>
                      <span><b>{job.project_name || '未命名项目'}</b><small>{jobWorkLabel(job)}</small></span>
                      <span className={`stamp ${jobStampClass(job.status)}`}>{jobStatusLabel(job.status)}</span>
                    </button>
                  ))}
                </div>
              )}
            </section>
            <section className="card monitor-overview-card monitor-system-card">
              <div className="monitor-card-head"><div><span className="eyebrow">SYSTEM</span><h3>配置概况</h3></div><button type="button" onClick={() => openSection('models')}>管理模型</button></div>
              <dl>
                <div><dt>已配置连接</dt><dd>{configuredModels} / {modelCatalog?.items.length ?? 0}</dd></div>
                <div><dt>上游在途上限</dt><dd>{settings?.video_inflight_limit ?? settings?.auto_concurrency ?? '—'}</dd></div>
                <div><dt>视频提交并发</dt><dd>{settings?.video_submit_concurrency ?? settings?.video_concurrency ?? '—'}</dd></div>
                <div><dt>单集预算</dt><dd>¥ {settings?.episode_cost_limit_cny ?? '—'}</dd></div>
                <div><dt>日志保留</dt><dd>{settings?.provider_call_retention_days ?? '—'} 天</dd></div>
              </dl>
            </section>
          </div>
        </div>
      )}

      {activeSection === 'runs' && <div className="monitor-section"><RunCenter /></div>}

      {activeSection === 'models' && <section className="card model-hub monitor-section">
        <div className="model-hub-head">
          <div>
            <h3>模型中心</h3>
            <p>按工作类型分配模型。新增模型后会立即出现在对应的选择器中。</p>
          </div>
          <button className="btn primary small model-add-btn" type="button" onClick={() => {
            setEditingModel(null)
            setNewModel({ label: '', model: '', provider: 'custom', provider_label: '', base_url: '', api_key: '', kinds: ['text'] })
            setConnectionTest({ status: 'idle', message: '' })
            setShowAddModel(true)
          }}>
            <span aria-hidden="true">＋</span> 添加模型
          </button>
          <button className="btn small model-library-btn" type="button" onClick={() => { setLibraryMessage(''); setShowModelLibrary(true) }}>
            管理模型
          </button>
        </div>

        <div className="model-grid">
          {MODEL_ROWS.map(row => {
            const sel = selectionFor(row.key)
            const provider = providerFor(row.key, sel)
            const option = sel.options.find(opt => opt.provider === provider)
            const settingKey = modelSettingKey(row.key, provider)
            const currentModel = modelDraft[settingKey] ?? option?.model ?? ''
            const choices = modelChoices(row.key, provider, currentModel, modelCatalog?.items)
            const selectedModel = selectedModelValue(choices, currentModel)
            const providerDisabled = row.key === 'video' || row.key === 'image'
            const modelDisabled = !settingKey || !option?.available
            const providerChoices = sel.options.map(opt => ({
              key: opt.provider,
              label: modelCatalog?.items.find(item => item.provider === opt.provider)?.provider_label ?? providerLabel(opt.provider),
            }))
            const activeCatalogModel = modelCatalog?.items.find(item => item.provider === provider && item.model === selectedModel)
            return (
              <div className="model-row" key={row.key}>
                <div className="model-name">
                  <span className={`model-kind-icon ${row.key}`} aria-hidden="true">
                    {({ text: 'T', vlm: 'V', video: '▶', image: '◇' } as Record<ModelKind, string>)[row.key]}
                  </span>
                  <b>{row.label}</b>
                  <span>{row.note}</span>
                </div>
                <div className="model-selects">
                  <label className="model-select-field">
                    <span>服务</span>
                    <select
                      value={provider}
                      disabled={providerDisabled}
                      onChange={e => {
                        const nextProvider = e.target.value as ProviderKey
                        setModelDraft(prev => ({ ...prev, [modelProviderSettingKey(row.key)]: nextProvider }))
                      }}
                    >
                      {providerChoices.map(p => {
                        const opt = sel.options.find(o => o.provider === p.key)
                        return (
                          <option value={p.key} disabled={!opt?.available} key={p.key}>
                            {p.label}{opt?.available ? '' : '（暂未接入）'}
                          </option>
                        )
                      })}
                    </select>
                  </label>
                  <label className="model-select-field model-select-field-wide">
                    <span>模型</span>
                    <select
                      value={selectedModel}
                      disabled={modelDisabled}
                      onChange={e => setModelDraft(prev => ({ ...prev, [settingKey]: e.target.value }))}
                    >
                      {choices.map(choice => (
                        <option value={choice.value} key={choice.value}>
                          {choice.label} · {choice.value}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <div className="model-current">
                  <span className="model-live-dot" /> 当前运行
                  <strong>{modelCatalog?.items.find(item => item.provider === sel.provider)?.provider_label ?? providerLabel(sel.provider)}</strong>
                  <code>{sel.model || '未配置'}</code>
                  {activeCatalogModel && (
                    <button className="model-connect-btn" type="button" onClick={() => {
                      const defaults: Record<string, string> = { hiagent: '', openrouter: 'https://openrouter.ai/api/v1', bailian: 'https://dashscope.aliyuncs.com/compatible-mode/v1', deepseek: 'https://api.deepseek.com/v1', zhipu: 'https://open.bigmodel.cn/api/paas/v4' }
                      setCredentialModel(activeCatalogModel)
                      setCredentialDraft({ base_url: activeCatalogModel.base_url || defaults[activeCatalogModel.provider] || '', api_key: '' })
                      setCredentialTest({ status: 'idle', message: '' })
                    }}>{activeCatalogModel.key_configured ? '更新连接' : '配置连接'}</button>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        <div className="model-actions">
          {hasModelChanges && <span className="model-current">有未保存修改</span>}
          <button className="btn primary small" onClick={saveModelSettings} disabled={!hasModelChanges}>
            保存模型设置
          </button>
        </div>
      </section>}

      {showAddModel && (
        <div className="model-modal-backdrop" role="presentation" onMouseDown={e => {
          if (e.currentTarget === e.target) setShowAddModel(false)
        }}>
          <section className="model-modal" role="dialog" aria-modal="true" aria-labelledby="add-model-title">
            <div className="model-modal-head">
              <div>
                <span className="eyebrow">MODEL CATALOG</span>
                <h2 id="add-model-title">{editingModel ? '编辑模型' : '添加模型'}</h2>
                <p>模型参数保存在本机；密钥仍由上方的服务密钥统一管理。</p>
              </div>
              <button className="model-modal-close" type="button" aria-label="关闭" onClick={() => { setShowAddModel(false); setEditingModel(null) }}>×</button>
            </div>
            <div className="model-form-grid">
              <label className="model-form-field">
                <span>显示名称</span>
                <input autoFocus value={newModel.label} placeholder="例如 Claude Sonnet" onChange={e => setNewModel(prev => ({ ...prev, label: e.target.value }))} />
              </label>
              <label className="model-form-field">
                <span>服务商</span>
                <select value={newModel.provider} onChange={e => {
                  const provider = e.target.value as ProviderKey
                  const allowed = provider === 'hiagent' ? MODEL_ROWS.map(r => r.key) : provider === 'openrouter' || provider === 'bailian' || provider === 'custom' ? ['text', 'vlm'] as ModelKind[] : ['text'] as ModelKind[]
                  setNewModel(prev => ({ ...prev, provider, kinds: prev.kinds.filter(k => allowed.includes(k)).length ? prev.kinds.filter(k => allowed.includes(k)) : [allowed[0]] }))
                }}>
                  <option value="custom">自定义 OpenAI 兼容服务</option>
                  {PROVIDERS.map(p => <option key={p.key} value={p.key}>{p.label}</option>)}
                </select>
              </label>
              {newModel.provider === 'custom' && <>
                <label className="model-form-field">
                  <span>服务名称</span>
                  <input value={newModel.provider_label} placeholder="例如 公司内部网关" onChange={e => setNewModel(prev => ({ ...prev, provider_label: e.target.value }))} />
                </label>
                <label className="model-form-field">
                  <span>Base URL</span>
                  <input className="mono" value={newModel.base_url} placeholder="https://api.example.com/v1" onChange={e => setNewModel(prev => ({ ...prev, base_url: e.target.value }))} />
                </label>
                <label className="model-form-field model-form-wide">
                  <span>该模型的 API Key</span>
                  <input type="password" autoComplete="new-password" value={newModel.api_key} placeholder="仅保存，不会在页面回显" onChange={e => setNewModel(prev => ({ ...prev, api_key: e.target.value }))} />
                </label>
              </>}
              <label className="model-form-field model-form-wide">
                <span>模型 ID</span>
                <input className="mono" value={newModel.model} placeholder="例如 anthropic/claude-sonnet-4" onChange={e => setNewModel(prev => ({ ...prev, model: e.target.value }))} />
                <small>请填写服务商 API 实际接收的 model 字段，不要填网页展示名。</small>
              </label>
              <div className="model-form-field model-form-wide">
                <span>模型能力</span>
                <div className="capability-picker">
                  {MODEL_ROWS.map(row => {
                    const allowed = newModel.provider === 'hiagent' || ((newModel.provider === 'openrouter' || newModel.provider === 'bailian' || newModel.provider === 'custom') && (row.key === 'text' || row.key === 'vlm')) || ((newModel.provider === 'deepseek' || newModel.provider === 'zhipu') && row.key === 'text')
                    return (
                      <button type="button" key={row.key} disabled={!allowed} className={newModel.kinds.includes(row.key) ? 'active' : ''} onClick={() => toggleNewModelKind(row.key)}>
                        {row.label}
                      </button>
                    )
                  })}
                </div>
              </div>
            </div>
            {!!modelCatalog?.items.some(item => !item.builtin) && (
              <div className="custom-model-list">
                <span>已添加模型</span>
                {modelCatalog.items.filter(item => !item.builtin).map(item => (
                  <div className="custom-model-item" key={item.id}>
                    <div><b>{item.label}</b><code>{providerLabel(item.provider)} · {item.model}</code></div>
                    <div className="custom-model-actions">
                      <button type="button" onClick={() => editModel(item)}>编辑</button>
                      <button type="button" onClick={async () => {
                        setConnectionTest({ status: 'testing', message: `正在测试 ${item.label}…` })
                        try {
                          const result = await api.post(`/models/${encodeURIComponent(item.id)}/test`)
                          setConnectionTest({ status: 'ok', message: `${item.label} 可用 · ${result.latency_ms} ms` })
                        } catch (e: unknown) { setConnectionTest({ status: 'error', message: (e as Error).message }) }
                      }}>测试</button>
                      <button type="button" onClick={() => removeModel(item)}>删除</button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div className="model-modal-actions">
              <span className={`connection-test-result ${connectionTest.status}`}>{connectionTest.message || '保存前需要先通过连接测试'}</span>
              <button className="btn small" type="button" disabled={connectionTest.status === 'testing'} onClick={testNewModel}>{connectionTest.status === 'testing' ? '测试中…' : '测试连接'}</button>
              <button className="btn primary small" type="button" disabled={addingModel || connectionTest.status !== 'ok' || connectionTest.signature !== modelDraftSignature()} onClick={addModel}>{addingModel ? '保存中…' : editingModel ? '保存修改' : '添加到模型库'}</button>
            </div>
          </section>
        </div>
      )}

      {showModelLibrary && (
        <div className="model-modal-backdrop" role="presentation" onMouseDown={e => {
          if (e.currentTarget === e.target) setShowModelLibrary(false)
        }}>
          <section className="model-modal model-library-modal" role="dialog" aria-modal="true" aria-labelledby="model-library-title">
            <div className="model-modal-head">
              <div>
                <span className="eyebrow">MODEL LIBRARY</span>
                <h2 id="model-library-title">管理模型</h2>
                <p>共 {modelCatalog?.items.length ?? 0} 个模型；每个模型独立维护连接和密钥。</p>
              </div>
              <button className="model-modal-close" type="button" aria-label="关闭" onClick={() => setShowModelLibrary(false)}>×</button>
            </div>
            <div className="model-library-list">
              {modelCatalog?.items.map(item => (
                <div className="model-library-item" key={item.id}>
                  <div className="model-library-main">
                    <div><b>{item.label}</b>{!item.builtin && <span className="stamp gold">自定义</span>}</div>
                    <code>{item.provider_label ?? providerLabel(item.provider)} · {item.model}</code>
                    <span>{item.kinds.map(kind => MODEL_ROWS.find(row => row.key === kind)?.label).join(' / ')}</span>
                  </div>
                  <span className={`stamp ${item.key_configured ? 'green' : 'red'}`}>{item.key_configured ? '连接已配置' : '待配置'}</span>
                  <div className="model-library-actions">
                    <button type="button" onClick={async () => {
                      setLibraryMessage(`正在测试「${item.label}」…`)
                      try {
                        const result = await api.post(`/models/${encodeURIComponent(item.id)}/test`)
                        setLibraryMessage(`「${item.label}」${result.preview || '连接成功'} · ${result.latency_ms} ms`)
                      } catch (e: unknown) { setLibraryMessage((e as Error).message) }
                    }}>测试</button>
                    <button type="button" onClick={() => {
                      const defaults: Record<string, string> = { hiagent: '', openrouter: 'https://openrouter.ai/api/v1', bailian: 'https://dashscope.aliyuncs.com/compatible-mode/v1', deepseek: 'https://api.deepseek.com/v1', zhipu: 'https://open.bigmodel.cn/api/paas/v4' }
                      setShowModelLibrary(false)
                      setCredentialModel(item)
                      setCredentialDraft({ base_url: item.base_url || defaults[item.provider] || '', api_key: '' })
                      setCredentialTest({ status: 'idle', message: '' })
                    }}>连接</button>
                    {!item.builtin && <button type="button" onClick={() => editModel(item)}>编辑</button>}
                    {!item.builtin && <button className="danger" type="button" onClick={() => removeModel(item)}>删除</button>}
                  </div>
                </div>
              ))}
            </div>
            {libraryMessage && <div className="model-library-feedback">{libraryMessage}</div>}
          </section>
        </div>
      )}

      {credentialModel && (
        <div className="model-modal-backdrop" role="presentation" onMouseDown={e => {
          if (e.currentTarget === e.target) setCredentialModel(null)
        }}>
          <section className="model-modal model-credential-modal" role="dialog" aria-modal="true" aria-labelledby="credential-title">
            <div className="model-modal-head">
              <div>
                <span className="eyebrow">PER-MODEL CONNECTION</span>
                <h2 id="credential-title">配置模型连接</h2>
                <p>{credentialModel.label} · {credentialModel.model}</p>
              </div>
              <button className="model-modal-close" type="button" aria-label="关闭" onClick={() => setCredentialModel(null)}>×</button>
            </div>
            <div className="model-form-grid">
              <label className="model-form-field model-form-wide">
                <span>Base URL</span>
                <input className="mono" value={credentialDraft.base_url} placeholder="https://api.example.com/v1" onChange={e => { setCredentialDraft(prev => ({ ...prev, base_url: e.target.value })); setCredentialTest({ status: 'idle', message: '' }) }} />
              </label>
              <label className="model-form-field model-form-wide">
                <span>该模型专用 API Key</span>
                <input autoFocus type="password" autoComplete="new-password" value={credentialDraft.api_key} placeholder={credentialModel.key_configured ? '输入新 Key 以替换当前配置' : '输入该模型的 API Key'} onChange={e => { setCredentialDraft(prev => ({ ...prev, api_key: e.target.value })); setCredentialTest({ status: 'idle', message: '' }) }} />
                <small>密钥只会发送给这个模型配置的 Base URL，接口不会返回原始值。</small>
              </label>
            </div>
            <div className="model-modal-actions">
              <span className={`connection-test-result ${credentialTest.status}`}>{credentialTest.message || '保存前需要先通过连接测试'}</span>
              <button className="btn small" type="button" onClick={() => setCredentialModel(null)}>取消</button>
              <button className="btn small" type="button" onClick={testModelCredentials}>{credentialTest.status === 'testing' ? '测试中…' : '测试连接'}</button>
              <button className="btn primary small" type="button" disabled={credentialTest.status !== 'ok' || credentialTest.signature !== JSON.stringify(credentialDraft)} onClick={saveModelCredentials}>保存连接</button>
            </div>
          </section>
        </div>
      )}

      {activeSection === 'jobs' && (
        <section className="card monitor-section">
          <div className="monitor-section-head compact">
            <div><span className="eyebrow">JOB QUEUE</span><h2>任务队列</h2></div>
            <p>按状态或项目定位生成任务，失败原因直接保留在列表中。</p>
          </div>
          {jobs?.startup_recovery && (
            <div className={`monitor-recovery-summary ${startupRecoveryCount > 0 ? 'active' : ''}`}>
              <div><b>启动恢复对账已完成</b><span>{startupRecoveryCount > 0
                ? `本次服务启动已自动接管 ${startupRecoveryCount} 项未完成任务`
                : '本次启动未发现需要续跑的任务'}</span></div>
              <small>{[
                ...startupRecoveryEntries.map(([key, value]) => `${RECOVERY_WORKFLOW_LABELS[key] ?? humanizeToken(key)} ${value}`),
                ...(removedPartialFiles > 0 ? [`清理未完整临时文件 ${removedPartialFiles}`] : []),
              ].join(' · ') || '持久化队列、Run/Step 和上游调用已对账'}</small>
            </div>
          )}
          <div className="monitor-toolbar">
            <div className="monitor-search"><span>搜索</span><SearchField value={jobSearch} placeholder="搜索项目、集数、镜号或错误" ariaLabel="搜索任务" onChange={value => { setJobSearch(value); setJobPage(1) }} /></div>
            <label><span>状态</span><select value={jobStatus} onChange={e => { setJobStatus(e.target.value); setJobPage(1) }}><option value="all">全部状态</option>{jobStatuses.map(status => <option value={status} key={status}>{jobStatusLabel(status)}</option>)}</select></label>
            {(jobSearch || jobStatus !== 'all') && <button type="button" className="monitor-clear" onClick={() => { setJobSearch(''); setJobStatus('all'); setJobPage(1) }}>清除筛选</button>}
          </div>
          {!filteredJobs.length ? <div className="empty monitor-table-empty">没有符合条件的任务</div> : (
            <div className="monitor-table-wrap">
              <table className="ledger monitor-ledger jobs-ledger">
                <thead><tr><th>更新时间</th><th>项目</th><th>工作项</th><th>状态</th><th>错误（原始报文）</th></tr></thead>
                <tbody>
                  {pagedJobs.map(job => (
                    <tr key={job.id}>
                      <td className="mono">{fmtTime(job.updated_at)}</td>
                      <td>{job.project_name || '—'}</td>
                      <td>{jobWorkLabel(job)}</td>
                      <td><span className={`stamp ${jobStampClass(job.status)}`}>{jobStatusLabel(job.status)}</span></td>
                      <td className="monitor-error-cell">{job.error ?? ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Pagination page={safeJobPage} pageSize={jobPageSize} total={filteredJobs.length} onPageChange={setJobPage} onPageSizeChange={size => { setJobPageSize(size); setJobPage(1) }} />
        </section>
      )}

      {activeSection === 'calls' && (
      <section className="card monitor-section">
        <div className="monitor-section-head compact">
          <div><span className="eyebrow">PROVIDER CALLS</span><h2>调用日志</h2></div>
          <p>每一次模型请求都可展开查看发送内容、返回结果和元信息。</p>
        </div>
        <div className="monitor-toolbar">
          <div className="monitor-search"><span>搜索</span><SearchField value={callSearch} placeholder="搜索功能、模型、HTTP 或错误" ariaLabel="搜索调用日志" onChange={value => { setCallSearch(value); setCallPage(1) }} /></div>
          <label><span>状态</span><select value={callStatus} onChange={e => { setCallStatus(e.target.value); setCallPage(1) }}><option value="all">全部状态</option>{callStatuses.map(status => <option value={status} key={status}>{callStatusLabel(status)}</option>)}</select></label>
          {(callSearch || callStatus !== 'all') && <button type="button" className="monitor-clear" onClick={() => { setCallSearch(''); setCallStatus('all'); setCallPage(1) }}>清除筛选</button>}
        </div>
        {!filteredCalls.length ? <div className="empty monitor-table-empty">没有符合条件的调用记录</div> : <div className="monitor-table-wrap">
        <table className="ledger monitor-ledger calls-ledger">
          <thead><tr><th>时间</th><th>功能定位</th><th>模型</th><th>状态</th><th>HTTP 状态</th><th>延迟</th><th>错误</th></tr></thead>
          <tbody>
            {pagedCalls.map(c => {
              const expanded = expandedCallId === c.id
              const functionLabel = callFunctionLabel(c)
              const repairTrigger = callRepairTrigger(parseJsonRecord(c.meta))
              return (
                <Fragment key={c.id}>
                  <tr
                    className={`ledger-clickable ${expanded ? 'expanded' : ''}`}
                    tabIndex={0}
                    onClick={() => setExpandedCallId(expanded ? null : c.id)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        setExpandedCallId(expanded ? null : c.id)
                      }
                    }}
                  >
                    <td className="mono">{fmtTime(c.ts)}</td>
                    <td title={`${functionLabel} ｜ 原始类型：${c.kind}${repairTrigger ? ` ｜ 触发原因：${repairTrigger}` : ''}`}>
                      {functionLabel}
                      {repairTrigger && (
                        <div className="hint" style={{ marginTop: 2, color: 'var(--cinnabar-deep)' }}>触发：{repairTrigger}</div>
                      )}
                    </td>
                    <td className="mono">{c.model}</td>
                    <td><span className={`stamp ${callStatusColor(displayCallStatus(c))}`} title={c.status}>{callStatusLabel(displayCallStatus(c))}</span></td>
                    <td className="mono">{c.http_status ? `HTTP ${c.http_status}` : '未返回'}</td>
                    <td className="mono">{(c.latency_ms / 1000).toFixed(1)}s</td>
                    <td className="monitor-error-cell">{c.error ?? ''}</td>
                  </tr>
                  {expanded && (
                    <tr className="ledger-detail-row">
                      <td colSpan={7}>
                        <div className="call-detail">
                          <div className="call-json-pane">
                            <b>发送内容</b>
                            <pre>{prettyJson(c.request_json)}</pre>
                          </div>
                          <div className="call-json-pane">
                            <b>接收内容</b>
                            <pre>{prettyJson(c.response_json)}</pre>
                          </div>
                          <div className="call-json-pane">
                            <b>元信息</b>
                            <pre>{prettyJson(c.meta)}{c.superseded_by_call_id ? `\n\n续跑结果：调用 #${c.superseded_by_call_id}` : ''}{c.supersedes_call_id ? `\n\n续接自：调用 #${c.supersedes_call_id}` : ''}</pre>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
        </div>}
        <Pagination page={safeCallPage} pageSize={callPageSize} total={filteredCalls.length} onPageChange={setCallPage} onPageSizeChange={size => { setCallPageSize(size); setCallPage(1) }} />
      </section>
      )}

      {activeSection === 'settings' && (
        <section className="card monitor-section monitor-settings">
          <div className="monitor-section-head compact">
            <div><span className="eyebrow">SYSTEM POLICY</span><h2>系统设置</h2></div>
            <p>修改会写入数据库；保存前可一次核对全部改动。</p>
          </div>
          <div className="monitor-settings-grid">
            {settings && Object.keys(SETTING_LABELS).map(key => (
              <label key={key}>
                <span>{SETTING_LABELS[key]}</span>
                <input type="text" value={draft[key] ?? settings[key] ?? ''} onChange={e => setDraft({ ...draft, [key]: e.target.value })} />
                <code>{key}</code>
              </label>
            ))}
          </div>
          <div className="monitor-settings-actions">
            <span>{Object.keys(draft).length ? `${Object.keys(draft).length} 项待保存` : '当前没有未保存修改'}</span>
            <button className="btn primary small" onClick={async () => {
              try { await api.put('/settings', draft); toast('系统设置已更新'); setDraft({}); refreshSettings() }
              catch (e: unknown) { toast((e as Error).message, true) }
            }} disabled={!Object.keys(draft).length}>保存系统设置</button>
          </div>
        </section>
      )}
    </div>
  )
}
