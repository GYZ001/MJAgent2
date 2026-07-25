import { useEffect, useState } from 'react'
import { api, DeliveryPackage, DeliveryPackageRecord, DeliveryReadiness, MixStatus, MixResult } from '../api'
import { useEpisode, useNav } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'

export default function CinemaPage() {
  const { episodeId, toast } = useNav()
  const { data: ep, error, loading } = useEpisode(episodeId!, 'cinema')
  const [mix, setMix] = useState<MixStatus | null>(null)
  const [mixBusy, setMixBusy] = useState(false)
  const [deliveryBusy, setDeliveryBusy] = useState(false)
  const [readiness, setReadiness] = useState<DeliveryReadiness | null>(null)
  const [packages, setPackages] = useState<DeliveryPackageRecord[]>([])
  const [selectedPackageId, setSelectedPackageId] = useState<string | null>(null)
  const [reviewer, setReviewer] = useState('')
  const [reason, setReason] = useState('')
  const [acceptedRisk, setAcceptedRisk] = useState('')
  const [feedback, setFeedback] = useState('')
  const [activeTab, setActiveTab] = useState<'preview' | 'readiness' | 'records'>('preview')
  const mixTimer = useTaskTimer(`episode.${episodeId}.mix`, mixBusy)

  const refreshMix = () => {
    if (!episodeId) return Promise.resolve()
    return api.get(`/episodes/${episodeId}/mix-status`)
      .then((d: unknown) => setMix(d as MixStatus))
      .catch(e => toast(String(e.message || e), true))
  }

  const refreshDelivery = () => {
    if (!episodeId) return Promise.resolve()
    return Promise.all([
      api.get(`/episodes/${episodeId}/delivery/readiness`),
      api.get(`/episodes/${episodeId}/delivery/packages`),
    ]).then(([nextReadiness, nextPackages]: [DeliveryReadiness, DeliveryPackageRecord[]]) => {
      setReadiness(nextReadiness)
      setPackages(nextPackages)
      setSelectedPackageId(current => {
        if (current && nextPackages.some(item => item.id === current)) return current
        return nextPackages.find(item => item.status === 'waiting_human')?.id
          ?? nextPackages[0]?.id
          ?? null
      })
    }).catch(e => toast(String(e.message || e), true))
  }

  useEffect(() => {
    refreshMix()
    refreshDelivery()
  }, [episodeId])

  // 合成进行中或尚未就绪时自动刷新，避免成片台状态冻结
  useEffect(() => {
    if (!episodeId) return
    const needPoll = mixBusy || (mix != null && !mix.ready) || packages.some(p => p.status === 'waiting_human')
    if (!needPoll) return
    const timer = window.setInterval(() => {
      void refreshMix()
      void refreshDelivery()
    }, 4000)
    return () => window.clearInterval(timer)
  }, [episodeId, mixBusy, mix?.ready, packages])

  if (error && !ep) return <div className="empty">{error}</div>
  if (loading && !ep) return <div className="empty">展卷中……</div>
  if (!ep) return <div className="empty">展卷中……</div>

  const selectedPackage = packages.find(item => item.id === selectedPackageId) ?? null
  const canReview = selectedPackage?.status === 'waiting_human'

  const decide = async (decision: 'approve' | 'approve_with_risk' | 'reject') => {
    if (!selectedPackage) {
      toast('请先选择一个交付包', true)
      return
    }
    if (!reviewer.trim()) {
      toast('请填写真实审核人', true)
      return
    }
    if (!reason.trim()) {
      toast('请填写审核意见', true)
      return
    }
    if (decision === 'approve_with_risk' && !acceptedRisk.trim()) {
      toast('带风险批准必须填写接受风险说明', true)
      return
    }
    setDeliveryBusy(true)
    try {
      await api.post(`/episodes/${ep.id}/delivery/approve`, {
        package_id: selectedPackage.id,
        decided_by: reviewer.trim(),
        decision,
        reason: reason.trim(),
        accepted_risk: decision === 'approve_with_risk' ? acceptedRisk.trim() : undefined,
      })
      toast(decision === 'reject' ? '交付包已拒绝' : decision === 'approve_with_risk' ? '风险已记录，交付包已批准为 T5' : '交付包已批准为 T5')
      setReason('')
      setAcceptedRisk('')
      refreshDelivery()
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setDeliveryBusy(false)
    }
  }

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="成片台" view="cinema" episodeNo={ep.episode_no} />
        <h1>成片台 <span className="sub">预览成片、完成交付检查并沉淀可追溯记录</span></h1>
        <hr className="rule" />
      </header>

      {mix ? (
        <>
          <section className="card cinema-status">
            <div className="cinema-status-copy">
              <span className={`stamp ${mix.ready ? 'green' : 'gold'}`}>{mix.ready ? '可合成' : '制作中'}</span>
              <div>
                <b>{mix.shots_ready} / {mix.shots_total} 镜已就绪</b>
                <span>所有镜头完成后即可合成最终成片</span>
              </div>
            </div>
            <div className="cinema-progress" aria-label="成片准备进度">
              <i style={{ width: `${Math.floor((mix.shots_ready / (mix.shots_total || 1)) * 100)}%` }} />
            </div>
            <div className="cinema-status-actions">
              <button className="btn" onClick={() => { refreshMix(); refreshDelivery() }}>刷新状态</button>
              <button
                className="btn primary"
                disabled={!mix.ready || mixBusy}
                onClick={async () => {
                  mixTimer.start()
                  setMixBusy(true)
                  try {
                    const r = (await api.post(`/episodes/${ep.id}/concatenate`)) as MixResult
                    // 立即用返回 URL 更新预览，避免仍显示空成片
                    if (r.video_url) {
                      setMix(prev => prev ? { ...prev, final_video_url: r.video_url } : prev)
                    }
                    if (r.ffmpeg_missing) {
                      toast(r.note || '服务端缺少 ffmpeg，已回退为首个片段的直链（非最终成片）', true)
                    } else {
                      toast(`已合成 ${r.shots} 个片段，共约 ${r.total_duration_s}s`)
                    }
                    refreshMix()
                    refreshDelivery()
                  } catch (e) {
                    toast((e as Error).message, true)
                    mixTimer.clear()
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

          <nav className="cinema-tabs" aria-label="成片台视图">
            <button className={activeTab === 'preview' ? 'active' : ''} onClick={() => setActiveTab('preview')}>
              <span>01</span>成片预览
            </button>
            <button className={activeTab === 'readiness' ? 'active' : ''} onClick={() => setActiveTab('readiness')}>
              <span>02</span>交付检查
              <i className={readiness?.ready ? 'ok' : 'warn'}>{readiness?.ready ? '通过' : '待处理'}</i>
            </button>
            <button className={activeTab === 'records' ? 'active' : ''} onClick={() => setActiveTab('records')}>
              <span>03</span>交付记录
              {!!packages.length && <i>{packages.length}</i>}
            </button>
          </nav>

          {activeTab === 'preview' && (
            <section className="card cinema-preview">
              <div className="section-heading">
                <div><span className="eyebrow">FINAL CUT</span><h3>《{ep.title}》</h3></div>
                {mix.final_video_url && <span className="stamp green">最新合成版</span>}
              </div>
              {mix.final_video_url ? (
                <video src={mix.final_video_url} controls playsInline preload="metadata" />
              ) : (
                <div className="cinema-preview-empty">
                  <span>▶</span>
                  <b>{mix.ready ? '镜头已齐，可以合成成品' : '成品尚未生成'}</b>
                  <p>{mix.ready ? '点击上方“合成成品”，完成后将在这里直接预览。' : `还需完成 ${Math.max(mix.shots_total - mix.shots_ready, 0)} 个镜头。`}</p>
                </div>
              )}
            </section>
          )}

          {activeTab === 'readiness' && (
            <section className="card delivery-panel">
              <div className="delivery-head">
                <div><span className="eyebrow">DELIVERY LOOP</span><h3>交付就绪度</h3></div>
                <span className={`stamp ${readiness?.ready ? 'green' : 'red'}`}>
                  {readiness?.ready ? '硬门禁通过' : '尚不可交付'}
                </span>
                <span className="delivery-coverage">证据覆盖率 {((readiness?.evidence_coverage ?? 0) * 100).toFixed(0)}%</span>
                <button className="btn small" onClick={refreshDelivery}>重新检查</button>
              </div>
              <div className="delivery-checks">
                {readiness?.checks.map(item => (
                  <div key={item.key} className={item.passed ? 'passed' : 'failed'}>
                    <span>{item.passed ? '✓' : '!'}</span><b>{item.message}</b><code>{item.key}</code>
                  </div>
                ))}
              </div>
              {!!readiness?.warnings.length && (
                <details className="delivery-warnings"><summary>查看已知风险（{readiness.warnings.length}）</summary>
                  {readiness.warnings.map((item, index) => <p key={index}>镜 {item.shot_no ?? '—'} · {item.message || item.code}</p>)}
                </details>
              )}
              {!!packages.length && (
                <div className="delivery-package-picker">
                  <label>审核目标交付包
                    <select
                      value={selectedPackageId ?? ''}
                      onChange={event => setSelectedPackageId(event.target.value || null)}
                    >
                      {packages.map(item => (
                        <option key={item.id} value={item.id}>
                          {item.id} · {item.status}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              )}
              <div className="delivery-review-form">
                <label>复验人（必填）<input value={reviewer} onChange={event => setReviewer(event.target.value)} placeholder="填写真实审核人姓名" /></label>
                <label>审核意见（必填）<input value={reason} onChange={event => setReason(event.target.value)} placeholder="说明通过或拒绝的依据" /></label>
                <label className="full">接受风险（仅带风险批准时必填）<textarea rows={2} value={acceptedRisk} onChange={event => setAcceptedRisk(event.target.value)} /></label>
              </div>
              <div className="dialog-actions">
                <button className="btn primary" disabled={!readiness?.ready || deliveryBusy} onClick={async () => {
                  setDeliveryBusy(true)
                  try {
                    const result = await api.post(`/episodes/${ep.id}/delivery/package`, {}) as DeliveryPackage
                    toast(`T3 交付候选已生成：${result.package_id}`)
                    refreshDelivery()
                    setActiveTab('records')
                  } catch (e) { toast((e as Error).message, true) }
                  finally { setDeliveryBusy(false) }
                }}>生成交付候选</button>
                <button className="btn" disabled={!canReview || deliveryBusy} onClick={() => decide('approve')}>批准交付</button>
                <button className="btn" disabled={!canReview || deliveryBusy} onClick={() => decide('approve_with_risk')}>带风险批准</button>
                <button className="btn ghost danger" disabled={!canReview || deliveryBusy} onClick={() => decide('reject')}>拒绝</button>
              </div>
            </section>
          )}

          {activeTab === 'records' && (
            <section className="card delivery-records">
              <div className="section-heading">
                <div><span className="eyebrow">AUDIT TRAIL</span><h3>交付包与反馈记录</h3></div>
                <span className="hint">已交付快照不会被后续反馈覆盖</span>
              </div>
              {packages.length ? (
                <div className="delivery-packages">
                  {packages.map(item => <div key={item.id}>
                    <button
                      type="button"
                      className={`btn small${item.id === selectedPackageId ? ' primary' : ''}`}
                      onClick={() => { setSelectedPackageId(item.id); setActiveTab('readiness') }}
                    >选择审核</button>
                    <code>{item.id}</code>
                    <span className={`stamp ${item.status === 'approved' ? 'green' : item.status === 'rejected' ? 'red' : 'gold'}`}>{item.status}</span>
                    <a className="btn small" href={`/api/delivery/packages/${item.id}/report`} target="_blank" rel="noreferrer">质量报告</a>
                    <a className="btn small" href={`/api/delivery/packages/${item.id}/archive`}>交付 ZIP</a>
                  </div>)}
                </div>
              ) : (
                <div className="delivery-records-empty">暂无交付记录，请先通过交付检查并生成交付候选。</div>
              )}
              <div className="customer-feedback">
                <input value={feedback} onChange={event => setFeedback(event.target.value)} placeholder="输入客户反馈，将创建新的修订任务" />
                <button className="btn primary small" disabled={!feedback.trim()} onClick={async () => {
                  try {
                    await api.post(`/episodes/${ep.id}/customer-feedback`, {
                      message: feedback, created_by: reviewer.trim() || 'customer', request_revision: true,
                    })
                    setFeedback(''); toast('反馈已回流，并创建修订 Run')
                  } catch (e) { toast((e as Error).message, true) }
                }}>提交并发起修订</button>
              </div>
            </section>
          )}
        </>
      ) : <div className="empty">加载成片台…</div>}
    </>
  )
}
