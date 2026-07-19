import { useEffect, useState } from 'react'
import { api, DeliveryPackage, DeliveryPackageRecord, DeliveryReadiness, MixStatus, MixResult } from '../api'
import { useEpisode, useNav } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'

export default function CinemaPage() {
  const { episodeId, toast } = useNav()
  const { data: ep } = useEpisode(episodeId!)
  const [mix, setMix] = useState<MixStatus | null>(null)
  const [mixBusy, setMixBusy] = useState(false)
  const [deliveryBusy, setDeliveryBusy] = useState(false)
  const [readiness, setReadiness] = useState<DeliveryReadiness | null>(null)
  const [packages, setPackages] = useState<DeliveryPackageRecord[]>([])
  const [reviewer, setReviewer] = useState('reviewer')
  const [reason, setReason] = useState('已复验交付清单与证据链')
  const [acceptedRisk, setAcceptedRisk] = useState('')
  const [feedback, setFeedback] = useState('')
  const mixTimer = useTaskTimer(`episode.${episodeId}.mix`, mixBusy)

  const refreshMix = () => {
    if (!episodeId) return
    api.get(`/episodes/${episodeId}/mix-status`)
      .then((d: unknown) => setMix(d as MixStatus))
      .catch(e => toast(String(e.message || e), true))
  }

  const refreshDelivery = () => {
    if (!episodeId) return
    Promise.all([
      api.get(`/episodes/${episodeId}/delivery/readiness`),
      api.get(`/episodes/${episodeId}/delivery/packages`),
    ]).then(([nextReadiness, nextPackages]: [DeliveryReadiness, DeliveryPackageRecord[]]) => {
      setReadiness(nextReadiness)
      setPackages(nextPackages)
    }).catch(e => toast(String(e.message || e), true))
  }

  useEffect(() => {
    refreshMix()
    refreshDelivery()
  }, [episodeId])

  if (!ep) return <div className="empty">展卷中……</div>

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="成片台" view="cinema" episodeNo={ep.episode_no} />
        <h1>成片台 <span className="sub">按镜号顺序拼接 · 预览 · 导出</span></h1>
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
                  mixTimer.start()
                  setMixBusy(true)
                  try {
                    const r = (await api.post(`/episodes/${ep.id}/concatenate`)) as MixResult
                    if (r.ffmpeg_missing) {
                      toast('服务端缺少 ffmpeg，已回退为首个片段的直链')
                    } else {
                      toast(`已合成 ${r.shots} 个片段，共约 ${r.total_duration_s}s`)
                    }
                    refreshMix()
                    refreshDelivery()
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
              <TaskTimer label="合成" timer={mixTimer} />
            </div>
          </section>
          {mix.final_video_url && (
            <section className="card">
              <h3>成品预览 <span className="hint">《{ep.title}》</span></h3>
              <video src={mix.final_video_url} controls playsInline style={{ width: '100%', maxHeight: 520, background: '#1d1a16', borderRadius: 8 }} />
            </section>
          )}
          <section className="card delivery-panel">
            <div className="delivery-head">
              <div>
                <span className="eyebrow">DELIVERY LOOP</span>
                <h3>交付就绪度</h3>
              </div>
              <span className={`stamp ${readiness?.ready ? 'green' : 'red'}`}>
                {readiness?.ready ? '硬门禁通过' : '尚不可交付'}
              </span>
              <span className="delivery-coverage">证据覆盖率 {((readiness?.evidence_coverage ?? 0) * 100).toFixed(0)}%</span>
              <button className="btn small" onClick={refreshDelivery}>复验</button>
            </div>
            <div className="delivery-checks">
              {readiness?.checks.map(item => (
                <div key={item.key} className={item.passed ? 'passed' : 'failed'}>
                  <span>{item.passed ? '✓' : '!'}</span><b>{item.message}</b><code>{item.key}</code>
                </div>
              ))}
            </div>
            {!!readiness?.warnings.length && (
              <details className="delivery-warnings"><summary>已知风险（{readiness.warnings.length}）</summary>
                {readiness.warnings.map((item, index) => <p key={index}>镜 {item.shot_no ?? '—'} · {item.message || item.code}</p>)}
              </details>
            )}
            <div className="delivery-review-form">
              <label>复验人<input value={reviewer} onChange={event => setReviewer(event.target.value)} /></label>
              <label>决定理由<input value={reason} onChange={event => setReason(event.target.value)} /></label>
              <label className="full">接受风险（仅带风险批准时必填）<textarea rows={2} value={acceptedRisk} onChange={event => setAcceptedRisk(event.target.value)} /></label>
            </div>
            <div className="dialog-actions">
              <button className="btn primary" disabled={!readiness?.ready || deliveryBusy} onClick={async () => {
                setDeliveryBusy(true)
                try {
                  const result = await api.post(`/episodes/${ep.id}/delivery/package`, {}) as DeliveryPackage
                  toast(`T3 交付候选已生成：${result.package_id}`)
                  refreshDelivery()
                } catch (e) { toast((e as Error).message, true) }
                finally { setDeliveryBusy(false) }
              }}>生成交付候选</button>
              <button className="btn" disabled={!packages.some(item => item.status === 'waiting_human') || deliveryBusy} onClick={async () => {
                setDeliveryBusy(true)
                try {
                  await api.post(`/episodes/${ep.id}/delivery/approve`, { decided_by: reviewer, decision: 'approve', reason })
                  toast('交付包已批准为 T5')
                  refreshDelivery()
                } catch (e) { toast((e as Error).message, true) }
                finally { setDeliveryBusy(false) }
              }}>批准交付</button>
              <button className="btn" disabled={!acceptedRisk.trim() || !packages.some(item => item.status === 'waiting_human') || deliveryBusy} onClick={async () => {
                setDeliveryBusy(true)
                try {
                  await api.post(`/episodes/${ep.id}/delivery/approve`, {
                    decided_by: reviewer, decision: 'approve_with_risk', reason, accepted_risk: acceptedRisk,
                  })
                  toast('风险已记录，交付包已批准为 T5')
                  refreshDelivery()
                } catch (e) { toast((e as Error).message, true) }
                finally { setDeliveryBusy(false) }
              }}>带风险批准</button>
            </div>
            {!!packages.length && <div className="delivery-packages">
              {packages.map(item => <div key={item.id}>
                <code>{item.id}</code><span className={`stamp ${item.status === 'approved' ? 'green' : 'gold'}`}>{item.status}</span>
                <a className="btn small" href={`/api/delivery/packages/${item.id}/report`} target="_blank" rel="noreferrer">导出质量报告</a>
                <a className="btn small" href={`/api/delivery/packages/${item.id}/archive`}>导出交付 ZIP</a>
              </div>)}
            </div>}
            <div className="customer-feedback">
              <input value={feedback} onChange={event => setFeedback(event.target.value)} placeholder="客户反馈将追加到证据链，不修改已交付快照" />
              <button className="btn small" disabled={!feedback.trim()} onClick={async () => {
                try {
                  await api.post(`/episodes/${ep.id}/customer-feedback`, {
                    message: feedback, created_by: 'customer', request_revision: true,
                  })
                  setFeedback(''); toast('反馈已回流，并创建修订 Run')
                } catch (e) { toast((e as Error).message, true) }
              }}>提交反馈并发起修订</button>
            </div>
          </section>
        </>
      ) : <div className="empty">加载成片台…</div>}
    </>
  )
}
