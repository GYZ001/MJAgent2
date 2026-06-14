import { useState } from 'react'
import { api } from '../api'
import { useNav, usePoll } from '../App'

interface JobsView {
  counts: Record<string, number>
  recent: { id: string; status: string; error?: string; shot_no?: number; episode_no?: number; episode_title?: string; project_name?: string; updated_at: number }[]
}
interface Call { id: number; ts: number; kind: string; model: string; status: string; http_status?: number; latency_ms: number; error?: string }

type ProviderKey = 'hiagent' | 'openrouter' | 'bailian'
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
  models?: Record<ModelKind, ModelSelection>
  audio_enabled?: boolean
  audio_key_configured?: boolean
  audio_tts_model?: string
  audio_asr_model?: string
  audio_voice?: string
  keys?: Record<ProviderKey, { configured: boolean; preview: string }>
}

const AUDIO_VOICES = ['Cherry', 'Chelsie', 'Ethan', 'Serena', 'Dylan', 'Jada']
interface ModelChoice { label: string; value: string }

const PROVIDERS: { key: ProviderKey; label: string }[] = [
  { key: 'hiagent', label: '火山' },
  { key: 'openrouter', label: 'OpenRouter' },
  { key: 'bailian', label: '百炼' },
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
    ],
  }
}

function modelChoices(kind: ModelKind, provider: ProviderKey, currentModel: string): ModelChoice[] {
  let choices: ModelChoice[] = []
  if (provider === 'openrouter' && (kind === 'text' || kind === 'vlm')) {
    choices = [...OPENROUTER_MODEL_CHOICES[kind]]
  } else if (provider === 'bailian' && (kind === 'text' || kind === 'vlm')) {
    choices = [...BAILIAN_MODEL_CHOICES[kind]]
  }
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

interface KeyInfo { configured: boolean; preview: string; label: string; key_name: string }
type KeyStatus = Record<ProviderKey, KeyInfo>

const KEY_PROVIDERS: { key: ProviderKey; label: string; placeholder: string }[] = [
  { key: 'hiagent', label: '火山引擎（HiAgent）', placeholder: '填写火山引擎 API Key' },
  { key: 'openrouter', label: 'OpenRouter', placeholder: 'sk-or-v1-...' },
  { key: 'bailian', label: '百炼（阿里云 DashScope）', placeholder: 'sk-...' },
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
  const audioEnabled = (settings?.audio_enabled ?? (health?.audio_enabled ? 'true' : 'false')) === 'true'
  const audioVoice = settings?.audio_voice ?? health?.audio_voice ?? 'Cherry'

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
    use_first_frame_chaining: '首尾帧衔接（兼容旧项；现已强制使用预生成首/尾关键图）',
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
        <h3>配音 / 音频 <span className="hint">TTS 配音 + ASR 校验（百炼）；关闭则成片无声、全流程自动跳过音频</span></h3>
        <div style={{ display: 'flex', gap: 16, alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <label className="model-select-field">
            <span>总开关</span>
            <select value={audioEnabled ? 'true' : 'false'} onChange={e => setOne('audio_enabled', e.target.value)}>
              <option value="false">关闭（无声）</option>
              <option value="true">开启（生成配音并混入成片）</option>
            </select>
          </label>
          <label className="model-select-field">
            <span>音色</span>
            <select value={audioVoice} onChange={e => setOne('audio_voice', e.target.value)}>
              {AUDIO_VOICES.map(v => <option key={v} value={v}>{v}</option>)}
              {audioVoice && !AUDIO_VOICES.includes(audioVoice) && <option value={audioVoice}>{audioVoice}</option>}
            </select>
          </label>
          {audioEnabled && (
            <span className={`stamp ${health?.audio_key_configured ? 'green' : 'red'}`}>
              {health?.audio_key_configured ? '百炼 Key 已配置' : '未配置 BAILIAN_API_KEY'}
            </span>
          )}
        </div>
        <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', marginTop: 8 }}>
          TTS：{health?.audio_tts_model ?? '—'} · ASR：{health?.audio_asr_model ?? '—'}。
          配音按镜生成、ASR 预检（关键词必须读对）后，在「整集合成」时混入成片。正音词库（人名/术语读音）在每本书的「人物谱」页维护。
        </p>
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
          <thead><tr><th>时间</th><th>类型</th><th>模型</th><th>状态</th><th>HTTP</th><th>延迟</th><th>错误</th></tr></thead>
          <tbody>
            {calls?.map(c => (
              <tr key={c.id}>
                <td className="mono">{fmtTime(c.ts)}</td>
                <td>{c.kind}</td>
                <td className="mono">{c.model}</td>
                <td><span className={`stamp ${c.status === 'OK' ? 'green' : 'red'}`}>{c.status}</span></td>
                <td className="mono">{c.http_status ?? '—'}</td>
                <td className="mono">{(c.latency_ms / 1000).toFixed(1)}s</td>
                <td style={{ color: 'var(--cinnabar-deep)', fontSize: 12, maxWidth: 360, wordBreak: 'break-all' }}>{c.error ?? ''}</td>
              </tr>
            ))}
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
