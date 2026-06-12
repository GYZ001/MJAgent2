import { useEffect, useState } from 'react'
import { api, Shot, ShotVersion, MixStatus, MixResult, numToCn } from '../api'
import { useEpisode, useNav } from '../App'

export default function WallPage() {
  const { episodeId, projectId, go, toast } = useNav()
  const { data: ep, refresh } = useEpisode(episodeId!, 5000)
  const [mix, setMix] = useState<MixStatus | null>(null)
  const [mixBusy, setMixBusy] = useState(false)

  useEffect(() => {
    if (!episodeId) return
    api.get(`/episodes/${episodeId}/mix-status`)
      .then((d: unknown) => setMix(d as MixStatus))
      .catch(e => toast(String(e.message || e), true))
  }, [episodeId])

  const refreshMix = () => {
    if (!episodeId) return
    api.get(`/episodes/${episodeId}/mix-status`).then((d: unknown) => setMix(d as MixStatus))
  }

  if (!ep) return <div className="empty">展卷中……</div>

  const overLimit = ep.cost_limit_cny !== undefined && ep.cost_cny >= ep.cost_limit_cny

  return (
    <>
      <header className="desk-head">
        <div className="crumb">
          <a style={{ cursor: 'pointer' }} onClick={() => go('board', projectId, episodeId)}>分镜台</a> / 第{numToCn(ep.episode_no)}集
        </div>
        <h1>评审墙 <span className="sub">逐镜过目 · 采用 / 改词重生 / 原词重抽</span></h1>
        <hr className="rule" />
      </header>

      <section className="card" style={{ display: 'flex', gap: 22, alignItems: 'center', flexWrap: 'wrap' }}>
        <div className="cost-ink">¥{ep.cost_cny.toFixed(1)} <small>/ 上限 ¥{ep.cost_limit_cny}</small></div>
        {overLimit && (
          <>
            <span className="stamp red">预算熔断</span>
            <button className="btn small" onClick={async () => {
              try { const r = await api.post(`/episodes/${ep.id}/resume`); toast(`已恢复 ${r.resumed_jobs} 个任务（请先在监制房调高上限）`); refresh(); refreshMix() }
              catch (e: unknown) { toast((e as Error).message, true) }
            }}>调高上限后恢复队列</button>
          </>
        )}
        <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>
          {ep.shots?.filter(s => s.versions.some(v => v.status === 'succeeded')).length ?? 0} / {ep.shots?.length ?? 0} 镜已有成片
        </span>
      </section>

      <div style={{ height: 20 }} />
      <div className="wall">
        {ep.shots?.map(s => <FrameCard key={s.id} shot={s} onChanged={() => { refresh(); refreshMix() }} />)}
      </div>

      <div style={{ height: 28 }} />
      <header className="desk-head">
        <h2>成片台 <span className="sub">按镜号顺序拼接 · 预览 · 导出</span></h2>
        <hr className="rule" />
      </header>

      {mix ? (
        <>
          <section className="card">
            <div style={{ display: 'flex', gap: 18, alignItems: 'center', flexWrap: 'wrap' }}>
              <span className={`stamp ${mix.ready ? 'green' : 'gold'}`}>
                {mix.ready ? '可合成' : '制作中'}
              </span>
              <span style={{ fontSize: 14, color: 'var(--ink-soft)' }}>
                {mix.shots_ready} / {mix.shots_total} 镜已有成片（{Math.floor((mix.shots_ready / (mix.shots_total || 1)) * 100)}%）
              </span>
              <span style={{ flex: 1 }} />
              <button className="btn" onClick={refreshMix}>刷新状态</button>
              <button
                className="btn primary"
                disabled={!mix.ready || mixBusy}
                onClick={async () => {
                  setMixBusy(true)
                  try {
                    const r = (await api.post(`/episodes/${ep.id}/concatenate`)) as MixResult
                    if (r.ffmpeg_missing) {
                      toast('服务端缺少 ffmpeg，已回退为首个片段的直链')
                    } else {
                      toast(`已合成 ${r.shots} 个片段，共约 ${r.total_duration_s}s`)
                    }
                    refreshMix()
                  } catch (e) {
                    toast((e as Error).message, true)
                  } finally {
                    setMixBusy(false)
                  }
                }}
              >合成成品</button>
              {mix.final_video_url && (
                <a className="btn" href={mix.final_video_url} target="_blank" rel="noreferrer" style={{ textDecoration: 'none' }}>
                  下载成品
                </a>
              )}
            </div>
          </section>
          {mix.final_video_url && (
            <section className="card">
              <h3>成品预览 <span className="hint">《{ep.title}》</span></h3>
              <video src={mix.final_video_url} controls playsInline style={{ width: '100%', maxHeight: 520, background: '#1d1a16', borderRadius: 8 }} />
            </section>
          )}
          <section className="card">
            <h3>逐镜预览 <span className="hint">按镜号顺序点击任一镜成片</span></h3>
            <div className="wall">
              {mix.shots.map(s => (
                <div key={s.shot_id} className="frame-card">
                  {s.video_url
                    ? <video src={s.video_url} controls muted playsInline style={{ width: '100%', aspectRatio: '9/16', background: '#1d1a16', display: 'block' }} />
                    : <div style={{ aspectRatio: '9/16', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--paper-deep)', color: 'var(--ink-faint)', letterSpacing: '0.18em', fontSize: 13 }}>
                        镜 {String(s.shot_no).padStart(2, '0')} — 未采用
                      </div>}
                  <div className="fc-body">
                    <div className="fc-title"><span>镜 {String(s.shot_no).padStart(2, '0')} · {s.duration_s}s</span>
                      <span className={`stamp ${s.has_adopted ? 'green' : 'grey'}`}>{s.has_adopted ? '已采用' : '待成片'}</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </>
      ) : <div className="empty">加载成片台…</div>}
    </>
  )
}

function FrameCard({ shot, onChanged }: { shot: Shot; onChanged: () => void }) {
  const { toast } = useNav()
  const [showVer, setShowVer] = useState<string | null>(null)
  const [editPrompt, setEditPrompt] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const adopted = shot.versions.find(v => v.id === shot.adopted_version_id)
  const current = showVer ? shot.versions.find(v => v.id === showVer)! : (adopted ?? shot.versions[0])

  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    setBusy(true)
    try { await fn(); if (msg) toast(msg); onChanged() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  return (
    <div className="frame-card">
      {current?.video_url
        ? <video src={current.video_url} controls loop muted playsInline />
        : <div style={{ aspectRatio: '9/16', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--paper-deep)', color: 'var(--ink-faint)', letterSpacing: '0.2em', flexDirection: 'column', gap: 8 }}>
            {current?.status === 'running' || current?.status === 'queued'
              ? <><span className="stamp gold">生成中</span><span style={{ fontSize: 12 }}>{shot.duration_s}s · 约需数分钟</span></>
              : current?.status === 'failed' ? <span className="stamp red">失败</span>
              : current?.status === 'paused_budget' ? <span className="stamp red">预算暂停</span>
              : <span style={{ fontSize: 13 }}>未生成</span>}
          </div>}
      <div className="fc-body">
        <div className="fc-title">
          <span>镜{String(shot.shot_no).padStart(2, '0')} · {shot.duration_s}s</span>
          {current?.id === shot.adopted_version_id && current && <span className="stamp green">已采用</span>}
        </div>
        <div className="fc-qa" style={{ minHeight: 30 }}>
          {shot.action_desc}
          {current?.qa && current.qa.overall >= 0 && (
            <><br />质检 {current.qa.overall.toFixed(2)}{current.qa.issues?.length ? ` · ${current.qa.issues[0]}` : ''}</>
          )}
        </div>
        {current?.status === 'failed' && current.error && (
          <div className="error-banner" style={{ margin: '6px 0', fontSize: 12 }}>{current.error}</div>
        )}

        {shot.versions.length > 1 && (
          <select style={{ width: '100%', marginBottom: 8 }} value={current?.id ?? ''}
            onChange={e => setShowVer(e.target.value)}>
            {shot.versions.map(v => (
              <option key={v.id} value={v.id}>
                第{v.version_no}版 · {v.status}{v.id === shot.adopted_version_id ? ' · 已采用' : ''}{v.qa && v.qa.overall >= 0 ? ` · QA ${v.qa.overall.toFixed(2)}` : ''}
              </option>
            ))}
          </select>
        )}

        {editPrompt !== null ? (
          <>
            <textarea rows={6} style={{ fontSize: 12.5 }} value={editPrompt} onChange={e => setEditPrompt(e.target.value)} />
            <div className="fc-actions">
              <button className="btn small primary" disabled={busy}
                onClick={() => act(async () => { await api.post(`/shots/${shot.id}/generate`, { prompt_override: editPrompt }); setEditPrompt(null) }, '已按修改后的 prompt 入队')}>提交生成</button>
              <button className="btn small ghost" onClick={() => setEditPrompt(null)}>放弃</button>
            </div>
          </>
        ) : (
          <div className="fc-actions">
            {current && current.status === 'succeeded' && current.id !== shot.adopted_version_id && (
              <button className="btn small primary" disabled={busy}
                onClick={() => act(() => api.post(`/shots/${shot.id}/adopt`, { version_id: current.id }), '已采用该版本')}>采用此版</button>
            )}
            <button className="btn small" disabled={busy}
              onClick={() => setEditPrompt(current?.prompt_text ?? '')}>改词重生</button>
            <button className="btn small" disabled={busy}
              onClick={() => act(() => api.post(`/shots/${shot.id}/generate`, { reroll: true }), '已原词重抽（新任务入队）')}>原词重抽</button>
          </div>
        )}
      </div>
    </div>
  )
}
