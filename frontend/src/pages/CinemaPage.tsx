import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { api, DeliveryPackageRecord, DeliveryReadiness, MixStatus, MixResult } from '../api'
import { useEpisode, useNav } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import QueryState from '../components/QueryState'
import DecisionDialog from '../components/DecisionDialog'
import OperationError from '../components/OperationError'

const DELIVERY_STATUS_LABELS: Record<string, string> = {
  waiting_human: '待人工复验',
  approved: '已批准',
  approved_with_risk: '带风险批准',
  rejected: '已拒绝',
}

export type CinemaTab = 'preview' | 'readiness' | 'records'
const CINEMA_TABS: CinemaTab[] = ['preview', 'readiness', 'records']

export const deliveryStatusLabel = (status: string) => DELIVERY_STATUS_LABELS[status] || '处理中'

export function nextCinemaTab(current: CinemaTab, key: string): CinemaTab | null {
  const index = CINEMA_TABS.indexOf(current)
  if (key === 'Home') return CINEMA_TABS[0]
  if (key === 'End') return CINEMA_TABS[CINEMA_TABS.length - 1]
  if (key === 'ArrowRight') return CINEMA_TABS[(index + 1) % CINEMA_TABS.length]
  if (key === 'ArrowLeft') return CINEMA_TABS[(index - 1 + CINEMA_TABS.length) % CINEMA_TABS.length]
  return null
}

export function canConcatenateMix(mix: Pick<MixStatus, 'shots_ready'> | null): boolean {
  return Boolean(mix && mix.shots_ready > 0)
}

export function deliveryWarningLabel(value: string): string {
  const translated = value
    .replace(/Duplicate frames(?:\s*\(frame \d+ and frame \d+\))?/gi, '存在重复画面帧')
    .replace(/Missing start state of /gi, '未呈现预期起始状态：')
    .replace(/End state mismatch:\s*/gi, '结束状态不符合预期：')
    .replace(/Mismatched starting state/gi, '起始状态不符合预期')
    .replace(/Start state partially mismatched:\s*/gi, '起始状态部分不符合预期：')
    .replace(/Character outfit does not match the expected design(?:\s*\([^)]*\))?/gi, '角色服装与预期设计不一致')
    .replace(/The expected core action is not fully completed/gi, '预期核心动作未完整完成')
    .replace(/Some character faces do not match the provided character anchors/gi, '部分角色面部与人物设定不一致')
    .replace(/target character/gi, '目标角色')
    .replace(/is not present in the scene/gi, '未出现在画面中')
    .replace(/the girl is bowing instead of standing calmly as expected/gi, '角色正在鞠躬，而预期为平静站立')
    .replace(/Xiao Xun'er/gi, '萧薰儿')
    .replace(/Xiao Yan/gi, '萧炎')
    .replace(/'s outfit has incorrect accessory\s*/gi, '的服装配饰与人物设定不一致：')
    .replace(/'s outfit does not match the character anchor/gi, '的服装与人物设定不一致')
    .replace(/'s outfit does not match the expected light green top and tight pants, instead wearing a purple dress/gi, '的服装不符合预期：应为淡绿色上衣搭配紧腿长裤，实际为紫色连衣裙')
    .replace(/has raised his head instead of not responding yet/gi, '已抬头回应，而预期仍未作出反应')
    .replace(/preparing to approach/gi, '准备走向')
    .replaceAll('角色锚点', '人物设定')
    .replaceAll('锚点', '设定参考')
    .replaceAll('AI生成', '生成工具')
    .replace(/(\d+)s\b/gi, '$1 秒')
  return /[A-Za-z]{3}/.test(translated)
    ? '画面状态或人物一致性与预期不符，请结合对应镜头人工复验'
    : translated
}

export function deliveryCheckLabel(value: string): string {
  return value
    .replace('每镜时长为模型选择的 5~10 秒整数', '每镜时长为 5 到 10 秒整数')
    .replace('每镜都有已采用且通过技术校验的视频', '每镜都有已采用且可正常播放的视频')
}

export function formatDeliveryTime(value: number): string {
  const timestamp = value < 1_000_000_000_000 ? value * 1000 : value
  return new Date(timestamp).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export type DeliveryDecision = 'approve' | 'approve_with_risk' | 'reject'

export function deliveryReviewDisabledReason(
  decision: DeliveryDecision,
  busy: boolean,
  packageStatus: string | null,
  reviewer: string,
  reason: string,
  acceptedRisk: string,
): string {
  if (busy) return '正在处理上一项交付操作'
  if (!packageStatus) return '请先生成并选择一个待复验交付候选'
  if (packageStatus !== 'waiting_human') {
    return `当前候选${deliveryStatusLabel(packageStatus)}，不能重复审核`
  }
  if (!reviewer.trim()) return '请先填写复验人'
  if (!reason.trim()) return '请先填写审核意见'
  if (decision === 'approve_with_risk' && !acceptedRisk.trim()) return '请先填写接受风险说明'
  return ''
}

export default function CinemaPage() {
  const { episodeId, toast } = useNav()
  const { data: ep, error, loading, refresh: refreshEpisode } = useEpisode(episodeId!, 'cinema')
  const [mix, setMix] = useState<MixStatus | null>(null)
  const [mixError, setMixError] = useState<string | null>(null)
  const [mixBusy, setMixBusy] = useState(false)
  const [mixConfirmOpen, setMixConfirmOpen] = useState(false)
  const [packageConfirmOpen, setPackageConfirmOpen] = useState(false)
  const [deliveryBusy, setDeliveryBusy] = useState(false)
  const [readiness, setReadiness] = useState<DeliveryReadiness | null>(null)
  const [deliveryError, setDeliveryError] = useState<string | null>(null)
  const [packages, setPackages] = useState<DeliveryPackageRecord[]>([])
  const [selectedPackageId, setSelectedPackageId] = useState<string | null>(null)
  const [reviewer, setReviewer] = useState('')
  const [reason, setReason] = useState('')
  const [acceptedRisk, setAcceptedRisk] = useState('')
  const [feedback, setFeedback] = useState('')
  const [feedbackBusy, setFeedbackBusy] = useState(false)
  const [feedbackConfirmOpen, setFeedbackConfirmOpen] = useState(false)
  const [downloadBusy, setDownloadBusy] = useState<string | null>(null)
  const [reviewDecision, setReviewDecision] = useState<DeliveryDecision | null>(null)
  const [activeTab, setActiveTab] = useState<CinemaTab>('preview')
  const tabGroupId = useId()
  const tabRefs = useRef<Record<CinemaTab, HTMLButtonElement | null>>({
    preview: null,
    readiness: null,
    records: null,
  })
  const dialogTriggerRef = useRef<HTMLElement | null>(null)
  const mixTimer = useTaskTimer(`episode.${episodeId}.mix`, mixBusy)

  const refreshMix = () => {
    if (!episodeId) return Promise.resolve()
    setMixError(null)
    return api.get(`/episodes/${episodeId}/mix-status`)
      .then((d: unknown) => setMix(d as MixStatus))
      .catch(e => {
        const message = String(e.message || e)
        setMixError(message)
        toast('成片状态刷新失败，当前保留上次成功结果', true)
      })
  }

  const refreshDelivery = () => {
    if (!episodeId) return Promise.resolve()
    setDeliveryError(null)
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
    }).catch(e => {
      const message = String(e.message || e)
      setDeliveryError(message)
      toast('交付检查刷新失败，当前保留上次成功结果', true)
    })
  }

  useEffect(() => {
    refreshMix()
    refreshDelivery()
  }, [episodeId])

  // 仅在确有运行任务时自动刷新，避免未完成分集在成片台永久轮询。
  useEffect(() => {
    if (!episodeId) return
    const needPoll = mixBusy || ep?.status === 'generating'
    if (!needPoll) return
    const timer = window.setInterval(() => {
      void refreshMix()
      void refreshDelivery()
    }, 4000)
    return () => window.clearInterval(timer)
  }, [episodeId, ep?.status, mixBusy])

  if (!ep) {
    return (
      <QueryState
        loading={loading}
        error={error}
        hasData={false}
        objectName="成片台"
        loadingText="正在加载成片、交付检查与交付记录…"
        emptyText="未找到可展示的成片数据，请刷新后重试。"
        onRetry={() => void refreshEpisode()}
      >
        {null}
      </QueryState>
    )
  }

  const selectedPackage = packages.find(item => item.id === selectedPackageId) ?? null
  const canReview = selectedPackage?.status === 'waiting_human'
  const selectedPackageIndex = selectedPackage
    ? packages.findIndex(item => item.id === selectedPackage.id)
    : -1
  const selectedPackageLabel = selectedPackageIndex >= 0
    ? `第 ${selectedPackageIndex + 1} 个交付候选`
    : '当前交付候选'
  const createPackageDisabledReason = deliveryBusy
    ? '正在处理上一项交付操作'
    : readiness == null
      ? '正在检查交付条件'
      : !readiness.ready
        ? '需先通过全部交付检查'
        : ''
  const approveDisabledReason = deliveryReviewDisabledReason(
    'approve',
    deliveryBusy,
    selectedPackage?.status ?? null,
    reviewer,
    reason,
    acceptedRisk,
  )
  const riskApprovalDisabledReason = deliveryReviewDisabledReason(
    'approve_with_risk',
    deliveryBusy,
    selectedPackage?.status ?? null,
    reviewer,
    reason,
    acceptedRisk,
  )
  const rejectDisabledReason = deliveryReviewDisabledReason(
    'reject',
    deliveryBusy,
    selectedPackage?.status ?? null,
    reviewer,
    reason,
    acceptedRisk,
  )

  const activateTab = (tab: CinemaTab, moveFocus = false) => {
    setActiveTab(tab)
    if (moveFocus) {
      window.requestAnimationFrame(() => tabRefs.current[tab]?.focus())
    }
  }

  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, current: CinemaTab) => {
    const next = nextCinemaTab(current, event.key)
    if (!next) return
    event.preventDefault()
    activateTab(next, true)
  }

  const decide = async (decision: DeliveryDecision) => {
    if (!selectedPackage) {
      toast('请先选择一个交付候选', true)
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
      toast(decision === 'reject'
        ? '交付候选已拒绝'
        : decision === 'approve_with_risk'
          ? '风险已记录，交付候选已标记为交付已验证'
          : '交付候选已标记为交付已验证')
      setReason('')
      setAcceptedRisk('')
      refreshDelivery()
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setDeliveryBusy(false)
    }
  }

  const concatenate = async () => {
    const currentMix = mix
    if (!currentMix || !canConcatenateMix(currentMix)) return
    mixTimer.start()
    setMixBusy(true)
    try {
      const result = (await api.post(`/episodes/${ep.id}/concatenate`)) as MixResult
      if (result.video_url) {
        setMix(previous => previous ? { ...previous, final_video_url: result.video_url } : previous)
      }
      if (result.ffmpeg_missing) {
        toast(result.note || '服务端缺少视频合成组件，当前仅返回首个片段，不能视为最终成片', true)
      } else {
        const skipped = result.shots_skipped ?? Math.max((result.shots_total ?? currentMix.shots_total) - result.shots, 0)
        toast(`已按分镜顺序合成 ${result.shots} 个片段，共约 ${result.total_duration_s} 秒${skipped ? `；跳过 ${skipped} 镜` : ''}`)
      }
      refreshMix()
      refreshDelivery()
    } catch (e) {
      toast((e as Error).message, true)
      mixTimer.clear()
    } finally {
      setMixBusy(false)
    }
  }

  const createDeliveryPackage = async () => {
    setDeliveryBusy(true)
    try {
      await api.post(`/episodes/${ep.id}/delivery/package`, {})
      toast('交付候选已生成，等待人工复验')
      await refreshDelivery()
      setActiveTab('records')
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setDeliveryBusy(false)
    }
  }

  const submitCustomerFeedback = async () => {
    const message = feedback.trim()
    if (!message) return
    setFeedbackBusy(true)
    try {
      await api.post(`/episodes/${ep.id}/customer-feedback`, {
        message,
        created_by: reviewer.trim() || 'customer',
        request_revision: true,
      })
      setFeedback('')
      toast('反馈已回流，并创建修订任务')
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setFeedbackBusy(false)
    }
  }

  const downloadDeliveryFile = async (
    item: DeliveryPackageRecord,
    index: number,
    kind: 'report' | 'archive',
  ) => {
    const busyKey = `${item.id}:${kind}`
    setDownloadBusy(busyKey)
    try {
      const blob = await api.download(`/delivery/packages/${item.id}/${kind}`)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = kind === 'report'
        ? `episode-${ep.episode_no}-delivery-${index + 1}-quality-report.html`
        : `episode-${ep.episode_no}-delivery-${index + 1}.zip`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast(kind === 'report' ? '质量报告已开始下载' : '交付归档已开始下载')
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setDownloadBusy(null)
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
          {mixError && (
            <OperationError
              title="成片状态刷新失败"
              message={mixError}
              guidance="当前仍展示上次成功结果，不会将旧状态误报为最新状态。"
              variant="warning"
            >
              <button type="button" className="btn small" onClick={() => void refreshMix()}>
                重试刷新
              </button>
            </OperationError>
          )}
          <section className="card cinema-status">
            <div className="cinema-status-copy">
              <span className={`stamp ${canConcatenateMix(mix) ? 'green' : 'gold'}`}>{canConcatenateMix(mix) ? '可合成' : '暂无片段'}</span>
              <div>
                <b>{mix.shots_ready} 个已采纳片段可合成</b>
                <span>将按分镜号顺序合成；{Math.max(mix.shots_total - mix.shots_ready, 0)} 镜未采纳或不可用，本次跳过</span>
              </div>
            </div>
            <div className="cinema-progress" aria-label="成片准备进度">
              <i style={{ width: `${Math.floor((mix.shots_ready / (mix.shots_total || 1)) * 100)}%` }} />
            </div>
            <div className="cinema-status-actions">
              <button className="btn" onClick={() => { refreshMix(); refreshDelivery() }}>刷新状态</button>
              <button
                className="btn primary"
                disabled={!canConcatenateMix(mix) || mixBusy}
                aria-label={
                  mixBusy
                    ? '合成成品，正在处理中'
                    : !canConcatenateMix(mix)
                      ? '合成成品，暂不可用：至少需要 1 个已采纳且可用的视频'
                      : '合成成品'
                }
                title={
                  mixBusy
                    ? '成品正在合成，请稍候'
                    : !canConcatenateMix(mix)
                      ? '至少需要 1 个已采纳且可用的视频'
                      : '按分镜顺序合成已采纳视频，未采纳分镜自动跳过'
                }
                onClick={event => {
                  dialogTriggerRef.current = event.currentTarget
                  setMixConfirmOpen(true)
                }}
              >合成成品</button>
              {mix.final_video_url && (
                <a className="btn" href={mix.final_video_url} download={`episode-${ep.episode_no}-final.mp4`} style={{ textDecoration: 'none' }}>
                  下载成品
                </a>
              )}
              <TaskTimer label="合成" timer={mixTimer} />
            </div>
          </section>

          <div className="cinema-tabs" role="tablist" aria-label="成片台视图">
            <button
              ref={node => { tabRefs.current.preview = node }}
              id={`${tabGroupId}-tab-preview`}
              type="button"
              role="tab"
              aria-selected={activeTab === 'preview'}
              aria-controls={`${tabGroupId}-panel-preview`}
              tabIndex={activeTab === 'preview' ? 0 : -1}
              className={activeTab === 'preview' ? 'active' : ''}
              onClick={() => activateTab('preview')}
              onKeyDown={event => handleTabKeyDown(event, 'preview')}
            >
              <span>01</span>成片预览
            </button>
            <button
              ref={node => { tabRefs.current.readiness = node }}
              id={`${tabGroupId}-tab-readiness`}
              type="button"
              role="tab"
              aria-selected={activeTab === 'readiness'}
              aria-controls={`${tabGroupId}-panel-readiness`}
              tabIndex={activeTab === 'readiness' ? 0 : -1}
              className={activeTab === 'readiness' ? 'active' : ''}
              onClick={() => activateTab('readiness')}
              onKeyDown={event => handleTabKeyDown(event, 'readiness')}
            >
              <span>02</span>交付检查
              <i className={readiness?.ready ? 'ok' : 'warn'}>
                {readiness == null ? '检查中' : readiness.ready ? '通过' : '待处理'}
              </i>
            </button>
            <button
              ref={node => { tabRefs.current.records = node }}
              id={`${tabGroupId}-tab-records`}
              type="button"
              role="tab"
              aria-selected={activeTab === 'records'}
              aria-controls={`${tabGroupId}-panel-records`}
              tabIndex={activeTab === 'records' ? 0 : -1}
              className={activeTab === 'records' ? 'active' : ''}
              onClick={() => activateTab('records')}
              onKeyDown={event => handleTabKeyDown(event, 'records')}
            >
              <span>03</span>交付记录
              {!!packages.length && <i>{packages.length}</i>}
            </button>
          </div>

          {activeTab === 'preview' && (
            <section
              id={`${tabGroupId}-panel-preview`}
              className="card cinema-preview"
              role="tabpanel"
              aria-labelledby={`${tabGroupId}-tab-preview`}
              tabIndex={0}
            >
              <div className="section-heading">
                <div><span className="eyebrow">最终成片</span><h3>《{ep.title}》</h3></div>
                {mix.final_video_url && <span className="stamp green">最新合成版</span>}
              </div>
              {mix.final_video_url ? (
                <video src={mix.final_video_url} controls playsInline preload="metadata" />
              ) : (
                <div className="cinema-preview-empty">
                  <span>▶</span>
                  <b>{canConcatenateMix(mix) ? '已有可用采纳片段，可以合成' : '成品尚未生成'}</b>
                  <p>{canConcatenateMix(mix) ? '点击上方“合成成品”；未采纳分镜会自动跳过。' : '至少生成并采纳 1 个可用视频后即可合成。'}</p>
                </div>
              )}
            </section>
          )}

          {activeTab === 'readiness' && (
            <section
              id={`${tabGroupId}-panel-readiness`}
              className="card delivery-panel"
              role="tabpanel"
              aria-labelledby={`${tabGroupId}-tab-readiness`}
              tabIndex={0}
            >
              <div className="delivery-head">
                <div><span className="eyebrow">交付检查</span><h3>交付就绪度</h3></div>
                <span className={`stamp ${readiness == null ? 'gold' : readiness.ready ? 'green' : 'red'}`}>
                  {readiness == null ? '检查中' : readiness.ready ? '必检项全部通过' : '尚不可交付'}
                </span>
                <span className="delivery-coverage">
                  {readiness == null
                    ? '正在计算证据覆盖率…'
                    : `证据覆盖率 ${(readiness.evidence_coverage * 100).toFixed(0)}%`}
                </span>
                <button className="btn small" disabled={deliveryBusy}
                  aria-label={deliveryBusy ? '重新检查，暂不可用：正在处理交付操作' : '重新检查交付条件'}
                  onClick={() => void refreshDelivery()}>重新检查</button>
              </div>
              {deliveryError && (
                <OperationError
                  title="交付检查刷新失败"
                  message={deliveryError}
                  guidance="当前检查结果可能不是最新状态；在重新加载成功前不会自动提交交付。"
                >
                  <button type="button" className="btn small" onClick={() => void refreshDelivery()}>重试加载</button>
                </OperationError>
              )}
              <div className="delivery-checks">
                {readiness?.checks.map(item => (
                  <div key={item.key} className={item.passed ? 'passed' : 'failed'}>
                    <span>{item.passed ? '✓' : '!'}</span><b>{deliveryCheckLabel(item.message)}</b>
                    <details className="delivery-check-tech"><summary aria-label={`查看“${deliveryCheckLabel(item.message)}”的检查标识`}>检查标识</summary><code>{item.key}</code></details>
                  </div>
                ))}
              </div>
              {!!readiness?.warnings.length && (
                <details className="delivery-warnings"><summary>查看已知风险（{readiness.warnings.length}）</summary>
                  {readiness.warnings.map((item, index) => <p key={index}>镜 {item.shot_no ?? '—'} · {deliveryWarningLabel(item.message || item.code || '未提供风险说明')}</p>)}
                </details>
              )}
              {!!packages.length && (
                <div className="delivery-package-picker">
                  <label>审核目标交付候选
                    <select
                      value={selectedPackageId ?? ''}
                      disabled={deliveryBusy}
                      aria-label={deliveryBusy
                        ? '审核目标交付候选，暂不可用：正在处理交付操作'
                        : '审核目标交付候选'}
                      onChange={event => setSelectedPackageId(event.target.value || null)}
                    >
                      {packages.map((item, index) => (
                        <option key={item.id} value={item.id}>
                          第 {index + 1} 个交付候选 · {deliveryStatusLabel(item.status)}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              )}
              <div className="delivery-review-form">
                <label>复验人（必填）<input disabled={deliveryBusy}
                  aria-label={deliveryBusy ? '复验人，暂不可用：正在处理交付操作' : '复验人（必填）'}
                  value={reviewer} onChange={event => setReviewer(event.target.value)} placeholder="填写真实审核人姓名" /></label>
                <label>审核意见（必填）<input disabled={deliveryBusy}
                  aria-label={deliveryBusy ? '审核意见，暂不可用：正在处理交付操作' : '审核意见（必填）'}
                  value={reason} onChange={event => setReason(event.target.value)} placeholder="说明通过或拒绝的依据" /></label>
                <label className="full">接受风险（仅带风险批准时必填）<textarea rows={2} disabled={deliveryBusy}
                  aria-label={deliveryBusy ? '接受风险，暂不可用：正在处理交付操作' : '接受风险（仅带风险批准时必填）'}
                  value={acceptedRisk} onChange={event => setAcceptedRisk(event.target.value)} /></label>
              </div>
              <div className="dialog-actions">
                <button
                  className="btn primary"
                  disabled={Boolean(createPackageDisabledReason)}
                  aria-label={createPackageDisabledReason ? `生成交付候选，暂不可用：${createPackageDisabledReason}` : '生成交付候选'}
                  title={createPackageDisabledReason || '基于当前成片与检查结果创建待人工复验的交付候选'}
                  onClick={event => {
                    dialogTriggerRef.current = event.currentTarget
                    setPackageConfirmOpen(true)
                  }}>{deliveryBusy ? '处理中…' : '生成交付候选'}</button>
                <button className="btn" disabled={Boolean(approveDisabledReason)} aria-label={approveDisabledReason ? `批准交付，暂不可用：${approveDisabledReason}` : '批准交付'} title={approveDisabledReason || '确认后将写入交付审计记录'} onClick={event => {
                  dialogTriggerRef.current = event.currentTarget
                  setReviewDecision('approve')
                }}>批准交付</button>
                <button className="btn" disabled={Boolean(riskApprovalDisabledReason)} aria-label={riskApprovalDisabledReason ? `带风险批准，暂不可用：${riskApprovalDisabledReason}` : '带风险批准'} title={riskApprovalDisabledReason || '确认后将记录接受风险并写入交付审计'} onClick={event => {
                  dialogTriggerRef.current = event.currentTarget
                  setReviewDecision('approve_with_risk')
                }}>带风险批准</button>
                <button className="btn ghost danger" disabled={Boolean(rejectDisabledReason)} aria-label={rejectDisabledReason ? `拒绝交付，暂不可用：${rejectDisabledReason}` : '拒绝交付'} title={rejectDisabledReason || '确认后该候选将不能用于交付'} onClick={event => {
                  dialogTriggerRef.current = event.currentTarget
                  setReviewDecision('reject')
                }}>拒绝</button>
              </div>
              <p className="delivery-action-hint" role="status">
                {readiness == null
                  ? '正在加载交付检查，完成前不会开放提交。'
                  : !readiness.ready
                    ? '需先通过全部交付检查，才能生成交付候选。'
                  : !packages.length
                    ? '先生成交付候选，再填写复验人和审核意见。'
                    : !canReview
                      ? `当前选择的交付候选为“${deliveryStatusLabel(selectedPackage?.status || '')}”；请选择“待人工复验”的候选。`
                      : approveDisabledReason
                        ? `${approveDisabledReason}。`
                        : riskApprovalDisabledReason
                          ? '当前已可批准或拒绝；如需带风险批准，请填写接受风险说明。'
                          : '当前可批准、带风险批准或拒绝；提交前会再次确认范围和后果。'}
              </p>
            </section>
          )}

          {activeTab === 'records' && (
            <section
              id={`${tabGroupId}-panel-records`}
              className="card delivery-records"
              role="tabpanel"
              aria-labelledby={`${tabGroupId}-tab-records`}
              tabIndex={0}
            >
              <div className="section-heading">
                <div><span className="eyebrow">审计记录</span><h3>交付候选与反馈记录</h3></div>
                <span className="hint">已交付快照不会被后续反馈覆盖</span>
              </div>
              {packages.length ? (
                <div className="delivery-packages">
                  {packages.map((item, index) => <div key={item.id}>
                    <button
                      type="button"
                      className={`btn small${item.id === selectedPackageId ? ' primary' : ''}`}
                      onClick={() => { setSelectedPackageId(item.id); activateTab('readiness') }}
                    >查看或审核</button>
                    <div className="delivery-package-copy">
                      <b>交付候选 {index + 1}</b>
                      <time dateTime={new Date(item.created_at < 1_000_000_000_000 ? item.created_at * 1000 : item.created_at).toISOString()}>
                        创建于 {formatDeliveryTime(item.created_at)}
                      </time>
                    </div>
                    <span className={`stamp ${['approved', 'approved_with_risk'].includes(item.status) ? 'green' : item.status === 'rejected' ? 'red' : 'gold'}`}>{deliveryStatusLabel(item.status)}</span>
                    <details><summary>技术标识</summary><code>{item.id}</code></details>
                    <button
                      type="button"
                      className="btn small"
                      disabled={downloadBusy !== null}
                      aria-label={downloadBusy
                        ? `下载交付候选 ${index + 1} 的质量报告，暂不可用：正在下载其他交付文件`
                        : `下载交付候选 ${index + 1} 的质量报告`}
                      onClick={() => { void downloadDeliveryFile(item, index, 'report') }}
                    >{downloadBusy === `${item.id}:report` ? '报告下载中…' : '下载质量报告'}</button>
                    <button
                      type="button"
                      className="btn small"
                      disabled={downloadBusy !== null}
                      aria-label={downloadBusy
                        ? `下载交付候选 ${index + 1} 的交付归档，暂不可用：正在下载其他交付文件`
                        : `下载交付候选 ${index + 1} 的交付归档`}
                      onClick={() => { void downloadDeliveryFile(item, index, 'archive') }}
                    >{downloadBusy === `${item.id}:archive` ? '归档下载中…' : '下载交付归档'}</button>
                  </div>)}
                </div>
              ) : (
                <div className="delivery-records-empty">暂无交付记录，请先通过交付检查并生成交付候选。</div>
              )}
              <div className="customer-feedback">
                <input disabled={feedbackBusy}
                  aria-label={feedbackBusy ? '客户反馈，暂不可用：正在提交并创建修订任务' : '客户反馈'}
                  value={feedback} onChange={event => setFeedback(event.target.value)}
                  placeholder="输入客户反馈；确认后创建新的修订任务" />
                <button className="btn primary small" disabled={feedbackBusy || !feedback.trim()}
                  aria-label={feedbackBusy
                    ? '提交客户反馈，暂不可用：正在创建修订任务'
                    : !feedback.trim()
                      ? '提交客户反馈，暂不可用：请先填写反馈内容'
                      : '预览客户反馈影响并发起修订'}
                  onClick={event => {
                    dialogTriggerRef.current = event.currentTarget
                    setFeedbackConfirmOpen(true)
                  }}>{feedbackBusy ? '提交中…' : '提交并发起修订'}</button>
              </div>
            </section>
          )}

          {reviewDecision && selectedPackage && (
            <DecisionDialog
              title={reviewDecision === 'approve'
                ? '批准当前交付候选？'
                : reviewDecision === 'approve_with_risk'
                  ? '带风险批准当前交付候选？'
                  : '拒绝当前交付候选？'}
              summary={`${selectedPackageLabel} · ${deliveryStatusLabel(selectedPackage.status)}`}
              message={reviewDecision === 'approve'
                ? '确认后将标记为交付已验证，并写入不可覆盖的交付审计记录。'
                : reviewDecision === 'approve_with_risk'
                  ? '确认后将连同接受风险说明一起标记为交付已验证，并写入交付审计记录。'
                  : '确认后该候选将被标记为已拒绝，不能再作为交付结果。'}
              details={[
                `复验人：${reviewer.trim()}`,
                `审核意见：${reason.trim()}`,
                ...(reviewDecision === 'approve_with_risk' ? [`接受风险：${acceptedRisk.trim()}`] : []),
                '本次审核不会生成新媒体或产生模型费用；既有成片和其他候选保留',
              ]}
              confirmLabel={reviewDecision === 'approve'
                ? '确认批准交付'
                : reviewDecision === 'approve_with_risk'
                  ? '确认带风险批准'
                  : '确认拒绝交付'}
              cancelLabel="返回检查"
              danger={reviewDecision === 'reject'}
              returnFocus={dialogTriggerRef.current}
              onClose={() => setReviewDecision(null)}
              onConfirm={() => {
                const decision = reviewDecision
                setReviewDecision(null)
                void decide(decision)
              }}
            />
          )}

          {packageConfirmOpen && (
            <DecisionDialog
              title="生成当前交付候选？"
              summary={`第 ${ep.episode_no} 集 · 《${ep.title}》`}
              message="系统会把当前成片、交付检查和质量依据固化为一个新的待人工复验候选。"
              details={[
                '不会自动批准或对外交付，生成后仍需填写复验人和审核意见',
                '不会重新生成图片或视频，也不产生模型生成费用',
                '现有成片、既有交付候选和审核记录都会保留',
              ]}
              confirmLabel="确认生成交付候选"
              cancelLabel="取消（不生成）"
              returnFocus={dialogTriggerRef.current}
              onClose={() => setPackageConfirmOpen(false)}
              onConfirm={() => {
                setPackageConfirmOpen(false)
                void createDeliveryPackage()
              }}
            />
          )}

          {feedbackConfirmOpen && (
            <DecisionDialog
              title="提交客户反馈并发起修订？"
              summary={`第 ${ep.episode_no} 集 · ${feedback.trim()}`}
              message="确认后会保存这条反馈并创建新的修订任务，供制作人员后续处理。"
              details={[
                '不会立即重新生成图片、视频或产生模型费用',
                '当前成片、已交付快照和既有审核记录不会被覆盖',
                '反馈提交成功后会清空本次输入',
              ]}
              confirmLabel="确认提交并创建修订任务"
              cancelLabel="返回修改反馈"
              returnFocus={dialogTriggerRef.current}
              onClose={() => setFeedbackConfirmOpen(false)}
              onConfirm={() => {
                setFeedbackConfirmOpen(false)
                void submitCustomerFeedback()
              }}
            />
          )}

          {mixConfirmOpen && (
            <DecisionDialog
              title={mix.final_video_url ? '重新合成本集成品？' : '合成本集成品？'}
              summary={`${mix.shots_ready} 个已采用镜头将按镜号顺序合成`}
              message={mix.final_video_url
                ? '现有最新成片入口会更新为本次结果；镜头候选、采用关系和既有交付记录不会随之删除。'
                : '将使用当前各镜采用版本生成本集成片，不会自动生成或批准交付候选。'}
              details={[
                '合成过程不产生模型生成费用',
                mix.skipped_shot_nos?.length
                  ? `本次跳过镜号：${mix.skipped_shot_nos.join('、')}`
                  : '本次没有需要跳过的分镜',
                '若视频合成组件不可用，系统会明确提示，首个片段不会冒充最终成片',
              ]}
              confirmLabel={mix.final_video_url ? '确认重新合成' : '确认合成成品'}
              cancelLabel="暂不合成"
              returnFocus={dialogTriggerRef.current}
              onClose={() => setMixConfirmOpen(false)}
              onConfirm={() => {
                setMixConfirmOpen(false)
                void concatenate()
              }}
            />
          )}
        </>
      ) : (
        <QueryState
          loading={!mixError}
          error={mixError}
          hasData={false}
          objectName="成片状态"
          loadingText="正在检查镜头就绪度与成片状态…"
          emptyText="尚未取得成片状态，请刷新后重试。"
          onRetry={() => void refreshMix()}
        >
          {null}
        </QueryState>
      )}
    </>
  )
}
