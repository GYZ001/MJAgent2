import { Fragment, useState } from 'react'
import { api } from '../api'
import { useNav, usePoll } from '../App'

interface JobsView {
  counts: Record<string, number>
  recent: { id: string; status: string; error?: string; shot_no?: number; episode_no?: number; episode_title?: string; project_name?: string; updated_at: number }[]
}
interface Call {
  id: number
  ts: number
  kind: string
  model: string
  status: string
  http_status?: number
  latency_ms: number
  error?: string
  meta?: string
  request_json?: string
  response_json?: string
}

type ProviderKey = 'hiagent' | 'openrouter' | 'bailian' | 'deepseek'
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
  models?: Record<ModelKind, ModelSelection>
  keys?: Record<ProviderKey, { configured: boolean; preview: string }>
}

interface ModelChoice { label: string; value: string }

const PROVIDERS: { key: ProviderKey; label: string }[] = [
  { key: 'hiagent', label: '火山' },
  { key: 'openrouter', label: 'OpenRouter' },
  { key: 'bailian', label: '百炼' },
  { key: 'deepseek', label: 'DeepSeek' },
]

const MODEL_ROWS: { key: ModelKind; label: string; note: string }[] = [
  { key: 'text', label: 'Text 模型', note: '分集 / 剧本 / 分镜 / 文本修复' },
  { key: 'vlm', label: 'VLM 模型', note: '关键帧评审 / 视频质检' },
  { key: 'video', label: '视频模型', note: 'Seedance 视频生成' },
  { key: 'image', label: '图像模型', note: 'Seedream 关键帧 / 定妆照' },
]

const OPENROUTER_MODEL_CHOICES: Record<'text' | 'vlm', ModelChoice[]> = {
  text: [
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
  if (provider === 'bailian') {
    if (kind === 'text') return 'bailian_model_text'
    if (kind === 'vlm') return 'bailian_model_vlm'
    return ''
  }
  if (provider === 'deepseek') {
    if (kind === 'text') return 'deepseek_model_text'
    return ''
  }
  if (provider === 'openrouter') {
    if (kind === 'text') return 'openrouter_model_text'
    if (kind === 'vlm') return 'openrouter_model_vlm'
    return ''
  }
  return `hiagent_model_${kind}`
}

function fallbackSelection(kind: ModelKind, health?: Health | null): ModelSelection {
  const provider = kind === 'video' || kind === 'image' ? 'hiagent' : 'hiagent'
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
    ],
  }
}

function modelChoices(kind: ModelKind, provider: ProviderKey, currentModel: string): ModelChoice[] {
  let choices: ModelChoice[] = []
  if (provider === 'openrouter' && (kind === 'text' || kind === 'vlm')) {
    choices = [...OPENROUTER_MODEL_CHOICES[kind]]
  } else if (provider === 'bailian' && (kind === 'text' || kind === 'vlm')) {
    choices = [...BAILIAN_MODEL_CHOICES[kind]]
  } else if (provider === 'deepseek' && kind === 'text') {
    choices = [...DEEPSEEK_MODEL_CHOICES.text]
  } else if (provider === 'hiagent') {
    choices = [...HIAGENT_MODEL_CHOICES[kind]]
  }
  // 当前配置的模型如果不在列表中，补充进去（兼容历史值）
  if (currentModel && !isDisallowedModel(kind, provider, currentModel) && !choices.some(choice => choice.value === currentModel)) {
    choices.unshift({ label: currentModel, value: currentModel })
  }
  if (!choices.length) {
    choices.push({ label: currentModel || '未配置', value: currentModel })
  }
  return choices
}

function isDisallowedModel(kind: ModelKind, provider: ProviderKey, model: string) {
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
  screenplay_prompt: '剧本提示词',
  plan_prompt: '分集提示词',
  bible_prompt: '人物谱提示词',
  references_prompt: '参考图提示词',
  reference_image_mode_attempt_1_failed: '参考图模式首轮失败',
  reference_image_mode_retry_success: '参考图模式重试成功',
  reference_image_mode_retry_failed: '参考图模式重试失败',
  reference_image_mode_original_failure: '参考图模式最终失败',
}

const CALL_STATUS_LABELS: Record<string, string> = {
  OK: '成功',
  FAILED: '失败',
  TIMEOUT: '超时',
  NETWORK_ERROR: '网络错误',
  TASK_FAILED: '任务失败',
  QA_ERROR: '质检异常',
  REPAIR_STALLED: '修复停滞',
  FALLBACK_LAST_OUTPUT: '采用最后输出',
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

function callStatusLabel(status: string) {
  return CALL_STATUS_LABELS[status] ?? humanizeToken(status.toLowerCase())
}

function callStatusColor(status: string) {
  if (status === 'OK' || status.endsWith('SUCCESS') || status === 'PROMPT_READY') return 'green'
  if (status === 'TIMEOUT' || status === 'NETWORK_ERROR' || status.includes('FAILED') || status.includes('ERROR')) return 'red'
  return 'gold'
}

interface KeyInfo { configured: boolean; preview: string; label: string; key_name: string }
type KeyStatus = Record<ProviderKey, KeyInfo>

const KEY_PROVIDERS: { key: ProviderKey; label: string; placeholder: string }[] = [
  { key: 'hiagent', label: '火山引擎（HiAgent）', placeholder: '填写火山引擎 API Key' },
  { key: 'openrouter', label: 'OpenRouter', placeholder: 'sk-or-v1-...' },
  { key: 'bailian', label: '百炼（阿里云 DashScope）', placeholder: 'sk-...' },
  { key: 'deepseek', label: 'DeepSeek', placeholder: 'sk-...' },
]

export default function MonitorPage() {
  const { toast } = useNav()
  const { data: jobs } = usePoll<JobsView>(() => api.get('/system/jobs'), 4000)
  const { data: calls } = usePoll<Call[]>(() => api.get('/system/calls?limit=40'), 6000)
  const { data: settings, refresh: refreshSettings } = usePoll<Record<string, string>>(() => api.get('/settings'), 0)
  const { data: health, refresh: refreshHealth } = usePoll<Health>(() => api.get('/system/health'), 0)
  const { data: keyStatus, refresh: refreshKeys } = usePoll<KeyStatus>(() => api.get('/keys'), 0)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [modelDraft, setModelDraft] = useState<Record<string, string>>({})
  const [keyDraft, setKeyDraft] = useState<Record<string, string>>({})
  const [savingKeys, setSavingKeys] = useState(false)
  const [expandedCallId, setExpandedCallId] = useState<number | null>(null)

  const refreshModelState = () => {
    refreshSettings()
    refreshHealth()
    refreshKeys()
  }

  const saveKeys = async () => {
    const payload: Record<string, string> = {}
    for (const p of KEY_PROVIDERS) {
      const v = (keyDraft[p.key] || '').trim()
      if (v) payload[p.key] = v
    }
    if (!Object.keys(payload).length) {
      toast('请至少填写一个 Key')
      return
    }
    setSavingKeys(true)
    try {
      await api.put('/keys', payload)
      toast('密钥已保存，立即生效')
      setKeyDraft({})
      refreshModelState()
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setSavingKeys(false)
    }
  }

  const setOne = async (key: string, value: string) => {
    try { await api.put('/settings', { [key]: value }); toast('已更新'); refreshSettings(); refreshHealth() }
    catch (e: unknown) { toast((e as Error).message, true) }
  }
  const selectionFor = (kind: ModelKind) => health?.models?.[kind] ?? fallbackSelection(kind, health)

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
      if (isDisallowedModel(row.key, provider, modelValue)) {
        modelValue = modelChoices(row.key, provider, '')[0]?.value ?? ''
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

  const fmtTime = (t: number) => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false })

  const SETTING_LABELS: Record<string, string> = {
    video_concurrency: '视频并发数',
    episode_cost_limit_cny: '单集成本上限（¥）',
    use_character_refs: '定妆照参考图（true/false，人物一致性）',
    use_first_frame_chaining: '参考图视频模式（固定启用；旧项保留兼容）',
    max_ref_images: '单镜头最多参考图数',
    auto_qa: '自动质检（true/false，需本机 ffmpeg）',
    auto_retake_threshold: '自动重抽阈值（QA 总分低于此值重抽一次）',
    plan_episode_count: '分集每批集数（自动续写铺满全书）',
    max_repair_attempts: '修复重试上限（校验失败时让模型反复修正的次数）',
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">漫剧案头 / 监制房</div>
        <h1>监制房 <span className="sub">队列 · 成本 · 对外调用，失败必须在这里看得见</span></h1>
        <hr className="rule" />
      </header>

      <div className="stat-row">
        {(['queued', 'running', 'succeeded', 'failed', 'paused_budget'] as const).map(k => (
          <div className="stat-cell" key={k}>
            <div className="s-label">{({ queued: '排队', running: '生成中', succeeded: '成片', failed: '失败', paused_budget: '预算暂停' })[k]}</div>
            <div className="cost-ink" style={k === 'failed' && (jobs?.counts[k] ?? 0) > 0 ? { color: 'var(--cinnabar)' } : undefined}>
              {jobs?.counts[k] ?? 0}
            </div>
          </div>
        ))}
      </div>

      <div style={{ height: 20 }} />

      <section className="card">
        <h3>密钥管理 <span className="hint">填写后保存到 .env，下次启动自动加载；留空表示不修改</span></h3>
        <div className="model-grid">
          {KEY_PROVIDERS.map(p => {
            const info = keyStatus?.[p.key]
            const isConfigured = info?.configured ?? false
            return (
              <div className="model-row" key={p.key}>
                <div className="model-name">
                  <b>{p.label}</b>
                  <span className={`stamp ${isConfigured ? 'green' : 'red'}`}>
                    {isConfigured ? `已配置 ${info?.preview || ''}` : '未配置'}
                  </span>
                </div>
                <div className="model-selects" style={{ flex: 1 }}>
                  <input
                    type="password"
                    autoComplete="off"
                    placeholder={p.placeholder}
                    value={keyDraft[p.key] || ''}
                    onChange={e => setKeyDraft(prev => ({ ...prev, [p.key]: e.target.value }))}
                    style={{ width: '100%', padding: '6px 10px', fontSize: 13 }}
                  />
                </div>
              </div>
            )
          })}
        </div>
        <div className="model-actions">
          <button className="btn primary small" onClick={saveKeys} disabled={savingKeys || !Object.keys(keyDraft).some(k => keyDraft[k]?.trim())}>
            {savingKeys ? '保存中…' : '保存密钥'}
          </button>
        </div>
      </section>

      <section className="card">
        <h3>模型选择 <span className="hint">每类任务单独选择服务和模型；视频和图像当前由火山生成</span></h3>

        <div className="model-grid">
          {MODEL_ROWS.map(row => {
            const sel = selectionFor(row.key)
            const provider = providerFor(row.key, sel)
            const option = sel.options.find(opt => opt.provider === provider)
            const settingKey = modelSettingKey(row.key, provider)
            const currentModel = modelDraft[settingKey] ?? option?.model ?? ''
            const choices = modelChoices(row.key, provider, currentModel)
            const selectedModel = selectedModelValue(choices, currentModel)
            const providerDisabled = row.key === 'video' || row.key === 'image'
            const modelDisabled = !settingKey || !option?.available
            const providerChoices = PROVIDERS.filter(p => sel.options.some(opt => opt.provider === p.key))
            return (
              <div className="model-row" key={row.key}>
                <div className="model-name">
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
                  当前：{providerLabel(sel.provider)} · {sel.model || '未配置'}
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
      </section>

      <section className="card">
        <h3>近期任务</h3>
        {!jobs?.recent.length ? <div className="empty" style={{ padding: 30 }}>暂无任务</div> : (
          <table className="ledger">
            <thead><tr><th>时间</th><th>项目</th><th>集/镜</th><th>状态</th><th>错误（原始报文）</th></tr></thead>
            <tbody>
              {jobs.recent.map(j => (
                <tr key={j.id}>
                  <td className="mono">{fmtTime(j.updated_at)}</td>
                  <td>{j.project_name}</td>
                  <td>第{j.episode_no}集 · 镜{j.shot_no}</td>
                  <td><span className={`stamp ${j.status === 'succeeded' ? 'green' : j.status === 'failed' || j.status === 'paused_budget' ? 'red' : j.status === 'running' ? 'gold' : 'grey'}`}>{j.status}</span></td>
                  <td style={{ color: 'var(--cinnabar-deep)', fontSize: 12, maxWidth: 380, wordBreak: 'break-all' }}>{j.error ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h3>对外调用账本 <span className="hint">每一次模型调用都在此留痕</span></h3>
        <table className="ledger">
          <thead><tr><th>时间</th><th>类型</th><th>模型</th><th>状态</th><th>HTTP 状态</th><th>延迟</th><th>错误</th></tr></thead>
          <tbody>
            {calls?.map(c => {
              const expanded = expandedCallId === c.id
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
                    <td title={c.kind}>{callKindLabel(c.kind)}</td>
                    <td className="mono">{c.model}</td>
                    <td><span className={`stamp ${callStatusColor(c.status)}`} title={c.status}>{callStatusLabel(c.status)}</span></td>
                    <td className="mono">{c.http_status ? `HTTP ${c.http_status}` : '未返回'}</td>
                    <td className="mono">{(c.latency_ms / 1000).toFixed(1)}s</td>
                    <td style={{ color: 'var(--cinnabar-deep)', fontSize: 12, maxWidth: 360, wordBreak: 'break-all' }}>{c.error ?? ''}</td>
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
                            <pre>{prettyJson(c.meta)}</pre>
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
      </section>

      <section className="card">
        <h3>定例 <span className="hint">修改即生效，写入数据库</span></h3>
        {settings && Object.keys(SETTING_LABELS).map(key => (
          <div key={key} style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 10 }}>
            <span style={{ width: 330, fontSize: 13.5 }}>{SETTING_LABELS[key]}</span>
            <input type="text" style={{ width: 140 }} value={draft[key] ?? settings[key] ?? ''}
              onChange={e => setDraft({ ...draft, [key]: e.target.value })} />
          </div>
        ))}
        <button className="btn primary small" onClick={async () => {
          try { await api.put('/settings', draft); toast('定例已更新'); setDraft({}); refreshSettings() }
          catch (e: unknown) { toast((e as Error).message, true) }
        }} disabled={!Object.keys(draft).length}>存定例</button>
      </section>
    </>
  )
}
