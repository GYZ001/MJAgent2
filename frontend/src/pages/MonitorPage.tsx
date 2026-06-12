import { useState } from 'react'
import { api } from '../api'
import { useNav, usePoll } from '../App'

interface JobsView {
  counts: Record<string, number>
  recent: { id: string; status: string; error?: string; shot_no?: number; episode_no?: number; episode_title?: string; project_name?: string; updated_at: number }[]
}
interface Call { id: number; ts: number; kind: string; model: string; status: string; http_status?: number; latency_ms: number; error?: string }

export default function MonitorPage() {
  const { toast } = useNav()
  const { data: jobs } = usePoll<JobsView>(() => api.get('/system/jobs'), 4000)
  const { data: calls } = usePoll<Call[]>(() => api.get('/system/calls?limit=40'), 6000)
  const { data: settings, refresh: refreshSettings } = usePoll<Record<string, string>>(() => api.get('/settings'), 0)
  const [draft, setDraft] = useState<Record<string, string>>({})

  const fmtTime = (t: number) => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false })

  const SETTING_LABELS: Record<string, string> = {
    video_concurrency: '视频并发数',
    episode_cost_limit_cny: '单集成本上限（¥）',
    use_character_refs: '定妆照参考图（true/false，人物一致性）',
    use_first_frame_chaining: '首尾帧衔接（true/false，镜头连贯性）',
    max_ref_images: '单镜头最多参考图数',
    auto_qa: '自动质检（true/false，需本机 ffmpeg）',
    auto_retake_threshold: '自动重抽阈值（QA 总分低于此值重抽一次）',
    plan_episode_count: '每次规划集数',
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
        <h3>对外调用账本 <span className="hint">每一次 HiAgent 调用都在此留痕</span></h3>
        <table className="ledger">
          <thead><tr><th>时间</th><th>类型</th><th>状态</th><th>HTTP</th><th>延迟</th><th>错误</th></tr></thead>
          <tbody>
            {calls?.map(c => (
              <tr key={c.id}>
                <td className="mono">{fmtTime(c.ts)}</td>
                <td>{c.kind}</td>
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
